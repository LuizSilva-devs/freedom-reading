"""Tokens de uso único enviados por e-mail (confirmar e-mail / redefinir senha)."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from ..models import AuthToken, User

VERIFY = "verify"
RESET = "reset"


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def issue(db: Session, user: User, purpose: str, lifetime: timedelta) -> str:
    """Cria um token novo e invalida os anteriores do mesmo tipo (só o link mais recente vale)."""
    revoke(db, user, purpose)
    raw = secrets.token_urlsafe(32)
    db.add(AuthToken(user_id=user.id, purpose=purpose, token_hash=_hash(raw), expires_at=_now() + lifetime))
    db.flush()
    return raw


def revoke(db: Session, user: User, purpose: str) -> None:
    """Invalida os links pendentes (ex.: trocar a senha anula um "esqueci minha senha" antigo)."""
    db.execute(
        update(AuthToken)
        .where(AuthToken.user_id == user.id, AuthToken.purpose == purpose, AuthToken.used_at.is_(None))
        .values(used_at=_now())
    )


def consume(db: Session, raw: str, purpose: str) -> User | None:
    """Valida e marca o token como usado. Devolve o usuário ou None (inválido, expirado ou já usado).

    O UPDATE ... WHERE used_at IS NULL garante uso único mesmo com dois cliques simultâneos no link.
    """
    if not raw or len(raw) > 200:
        return None
    row = db.execute(
        update(AuthToken)
        .where(AuthToken.token_hash == _hash(raw), AuthToken.purpose == purpose,
               AuthToken.used_at.is_(None), AuthToken.expires_at > _now())
        .values(used_at=_now())
        .returning(AuthToken.user_id)
    ).first()
    if not row:
        return None
    return db.get(User, row.user_id)


def purge_expired(db: Session) -> None:
    """Remove tokens vencidos há mais de um dia (limpeza feita a cada emissão)."""
    db.execute(AuthToken.__table__.delete().where(AuthToken.expires_at < _now() - timedelta(days=1)))

