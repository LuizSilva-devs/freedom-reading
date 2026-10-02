"""Conta: cadastro, login, confirmação de e-mail, senha, sessões e exclusão."""
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import ratelimit
from ..config import get_settings
from ..database import get_db
from ..deps import get_current_user
from ..i18n import get_lang, msg
from ..models import Book, PageTranslation, User
from ..schemas import (
    ChangePasswordIn, DeleteAccountIn, ForgotPasswordIn, LoginIn, MessageOut, ProfileIn, RegisterIn,
    ResetPasswordIn, TokenOut, UserOut, VerifyEmailIn,
)
from ..security import create_access_token, hash_password, verify_password
from ..services import auth_tokens, email, gutenberg

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Hash válido de uma senha qualquer: usado para gastar o mesmo tempo quando o e-mail não existe,
# assim o tempo de resposta não revela quais e-mails têm conta.
_DUMMY_HASH = hash_password("senha-que-nao-existe")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _token_out(user: User) -> TokenOut:
    return TokenOut(access_token=create_access_token(user.id, user.token_version), user=UserOut.model_validate(user))


def _user_lang(user: User, fallback: str) -> str:
    lang = (user.settings or {}).get("language")
    return lang if lang in ("pt", "en") else fallback


def _limit(key: str, lang: str, limit: int, window: int) -> None:
    if not ratelimit.allow(key, limit, window):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, msg("too_many_attempts", lang))


def _send_verification(db: Session, user: User, lang: str, tasks: BackgroundTasks) -> None:
    s = get_settings()
    raw = auth_tokens.issue(db, user, auth_tokens.VERIFY, timedelta(hours=s.verify_token_hours))
    link = f"{s.app_url.rstrip('/')}/#/confirmar-email?token={raw}"
    tasks.add_task(email.send, email.build("verify", _user_lang(user, lang), user.email, user.name, link))


# ---------------------------------------------------------------- cadastro e login
@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
def register(data: RegisterIn, request: Request, tasks: BackgroundTasks,
             db: Session = Depends(get_db), lang: str = Depends(get_lang)):
    _limit(f"register:{_client_ip(request)}", lang, get_settings().registrations_per_hour, 3600)
    email_addr = data.email.lower()
    if db.scalar(select(User).where(func.lower(User.email) == email_addr)):
        raise HTTPException(status.HTTP_409_CONFLICT, msg("email_taken", lang))
    user = User(email=email_addr, name=data.name, password_hash=hash_password(data.password), settings={},
                email_verified=False, token_version=0)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:  # dois cadastros simultâneos com o mesmo e-mail
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, msg("email_taken", lang)) from None
    _send_verification(db, user, lang, tasks)
    db.commit()
    db.refresh(user)
    return _token_out(user)


@router.post("/login", response_model=TokenOut)
def login(data: LoginIn, request: Request, db: Session = Depends(get_db), lang: str = Depends(get_lang)):
    key = f"login:{_client_ip(request)}"
    limit = get_settings().login_attempts_per_5min
    # Bloqueado: recusa ANTES de conferir a senha (senão um acerto na 50ª tentativa passaria).
    if ratelimit.is_blocked(key, limit, 300):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, msg("too_many_attempts", lang))
    user = db.scalar(select(User).where(func.lower(User.email) == data.email.lower()))
    ok = verify_password(data.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not ok:
        ratelimit.allow(key, limit, 300)  # só tentativas erradas contam
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, msg("bad_login", lang))
    return _token_out(user)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.patch("/me", response_model=UserOut)
def update_me(data: ProfileIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user.name = data.name
    db.commit()
    db.refresh(user)
    return user


# ---------------------------------------------------------------- confirmação de e-mail
@router.post("/verify-email", response_model=UserOut)
def verify_email(data: VerifyEmailIn, db: Session = Depends(get_db), lang: str = Depends(get_lang)):
    """Confirma o e-mail pelo link recebido. Funciona mesmo sem estar logado (ex.: link aberto no celular)."""
    user = auth_tokens.consume(db, data.token, auth_tokens.VERIFY)
    if not user:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg("bad_token", lang))
    user.email_verified = True
    db.commit()
    db.refresh(user)
    return user


@router.post("/resend-verification", response_model=MessageOut)
def resend_verification(tasks: BackgroundTasks, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db), lang: str = Depends(get_lang)):
    if user.email_verified:
        return MessageOut(message=msg("already_verified", lang))
    _limit(f"verify-mail:{user.id}", lang, get_settings().email_requests_per_hour, 3600)
    _send_verification(db, user, lang, tasks)
    db.commit()
    return MessageOut(message=msg("verify_sent", lang))


# ---------------------------------------------------------------- senha
@router.post("/forgot-password", response_model=MessageOut)
def forgot_password(data: ForgotPasswordIn, request: Request, tasks: BackgroundTasks,
                    db: Session = Depends(get_db), lang: str = Depends(get_lang)):
    """Sempre responde a mesma mensagem, exista ou não a conta (não revela quem está cadastrado)."""
    s = get_settings()
    email_addr = data.email.lower()
    _limit(f"forgot-ip:{_client_ip(request)}", lang, s.email_requests_per_hour, 3600)
    # Limite por e-mail sem erro visível: evita usar o site para lotar a caixa de alguém.
    if ratelimit.allow(f"forgot-mail:{email_addr}", 3, 3600):
        user = db.scalar(select(User).where(func.lower(User.email) == email_addr))
        if user:
            auth_tokens.purge_expired(db)
            raw = auth_tokens.issue(db, user, auth_tokens.RESET, timedelta(minutes=s.reset_token_minutes))
            link = f"{s.app_url.rstrip('/')}/#/redefinir-senha?token={raw}"
            tasks.add_task(email.send, email.build("reset", _user_lang(user, lang), user.email, user.name, link))
            db.commit()
    return MessageOut(message=msg("reset_sent", lang))


@router.post("/reset-password", response_model=TokenOut)
def reset_password(data: ResetPasswordIn, db: Session = Depends(get_db), lang: str = Depends(get_lang)):
    """Define a nova senha pelo link do e-mail, desloga todos os aparelhos e já entra na conta."""
    user = auth_tokens.consume(db, data.token, auth_tokens.RESET)
    if not user:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg("bad_token", lang))
    user.password_hash = hash_password(data.new_password)
    user.token_version += 1
    user.email_verified = True  # quem abriu o link provou que o e-mail é seu
    db.commit()
    db.refresh(user)
    return _token_out(user)


@router.post("/change-password", response_model=TokenOut)
def change_password(data: ChangePasswordIn, request: Request, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db), lang: str = Depends(get_lang)):
    """Troca a senha (pede a atual). Os outros aparelhos saem; este recebe um token novo."""
    key = f"password:{user.id}"
    limit = get_settings().login_attempts_per_5min
    if ratelimit.is_blocked(key, limit, 300):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, msg("too_many_attempts", lang))
    if not verify_password(data.current_password, user.password_hash):
        ratelimit.allow(key, limit, 300)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg("wrong_password", lang))
    if data.new_password == data.current_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg("same_password", lang))
    user.password_hash = hash_password(data.new_password)
    user.token_version += 1
    auth_tokens.revoke(db, user, auth_tokens.RESET)  # link de "esqueci a senha" pedido antes deixa de valer
    db.commit()
    db.refresh(user)
    return _token_out(user)


# ---------------------------------------------------------------- sessões e conta
@router.post("/logout-all", response_model=TokenOut)
def logout_all(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Sai de todos os OUTROS aparelhos: os tokens antigos deixam de valer; este recebe um novo."""
    user.token_version += 1
    db.commit()
    db.refresh(user)
    return _token_out(user)


@router.post("/delete-account", response_model=MessageOut)
def delete_account(data: DeleteAccountIn, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db), lang: str = Depends(get_lang)):
    """Exclui a conta e todos os dados (livros, marcadores, sublinhados, links pendentes).

    As tabelas filhas têm ON DELETE CASCADE; o histórico anônimo de identificações fica (user_id = NULL).
    """
    key = f"password:{user.id}"
    limit = get_settings().login_attempts_per_5min
    if ratelimit.is_blocked(key, limit, 300):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, msg("too_many_attempts", lang))
    if not verify_password(data.password, user.password_hash):
        ratelimit.allow(key, limit, 300)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg("wrong_password", lang))
    # Livros enviados: o banco apaga junto (ON DELETE CASCADE); as traduções em cache saem aqui.
    own = select(Book.gutenberg_id).where(Book.owner_id == user.id)
    db.execute(delete(PageTranslation).where(PageTranslation.gutenberg_id.in_(own)))
    db.delete(user)
    db.commit()
    gutenberg.clear_page_cache()
    return MessageOut(message=msg("account_deleted", lang))
