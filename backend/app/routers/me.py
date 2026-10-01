"""Dados pessoais do usuário: favoritos, biblioteca, progresso e configurações."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import Bookmark, Highlight, SearchLog, User, UserBook
from ..schemas import (
    BookRef, ImportIn, LibraryIn, ProgressIn, SettingsIO, StatsOut, UserBookOut,
)
from .annotations import create_highlight, upsert_bookmark

router = APIRouter(prefix="/api/me", tags=["usuário"])

COMPLETED_AT = 99.5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_or_create(db: Session, user: User, ref: BookRef) -> UserBook:
    # INSERT ... ON CONFLICT DO NOTHING: duas requisições ao mesmo tempo (ex.: favoritar
    # enquanto o progresso é salvo) não geram mais erro 500 de chave duplicada.
    db.execute(
        pg_insert(UserBook)
        .values(user_id=user.id, book_key=ref.book_key, title=ref.title, author=ref.author or "",
                is_favorite=False, page=0, total_pages=0, percent=0.0)
        .on_conflict_do_nothing(constraint="uq_user_book")
    )
    ub = db.scalar(select(UserBook).where(UserBook.user_id == user.id, UserBook.book_key == ref.book_key))
    ub.title = ref.title or ub.title
    ub.author = ref.author or ub.author or ""
    ub.cover_url = ref.cover_url or ub.cover_url
    ub.gutenberg_id = ref.gutenberg_id or ub.gutenberg_id
    db.flush()  # garante que a próxima busca pelo mesmo livro encontre esta linha
    return ub


def _prune(db: Session, ub: UserBook) -> None:
    if not ub.is_favorite and not ub.status and not ub.last_read:
        db.delete(ub)


def _apply_progress(ub: UserBook, page: int, total_pages: int) -> None:
    ub.page = min(page, total_pages - 1)
    ub.total_pages = total_pages
    ub.percent = round((ub.page + 1) / total_pages * 100, 1)
    ub.last_read = _now()
    if ub.status != "concluido":
        ub.status = "concluido" if ub.percent >= COMPLETED_AT else "lendo"
        ub.added_at = ub.added_at or _now()


@router.get("/books", response_model=list[UserBookOut])
def my_books(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.scalars(
        select(UserBook).where(UserBook.user_id == user.id).order_by(UserBook.id.desc())
    ).all()


@router.put("/favorites", response_model=UserBookOut)
def add_favorite(ref: BookRef, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ub = _get_or_create(db, user, ref)
    if not ub.is_favorite:
        ub.is_favorite = True
        ub.favorited_at = _now()
    db.commit()
    db.refresh(ub)
    return ub


@router.delete("/favorites", status_code=status.HTTP_204_NO_CONTENT)
def remove_favorite(key: str = Query(max_length=120, pattern=r"^[^\x00]*$"), user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    ub = db.scalar(select(UserBook).where(UserBook.user_id == user.id, UserBook.book_key == key))
    if ub:
        ub.is_favorite = False
        ub.favorited_at = None
        _prune(db, ub)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/library", response_model=UserBookOut)
def set_library(data: LibraryIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ub = _get_or_create(db, user, data)
    ub.status = data.status
    ub.added_at = ub.added_at or _now()
    db.commit()
    db.refresh(ub)
    return ub


@router.delete("/library", status_code=status.HTTP_204_NO_CONTENT)
def remove_from_library(key: str = Query(max_length=120, pattern=r"^[^\x00]*$"), user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    ub = db.scalar(select(UserBook).where(UserBook.user_id == user.id, UserBook.book_key == key))
    if ub:
        ub.status = None
        ub.added_at = None
        ub.last_read = None
        ub.page = 0
        ub.percent = 0.0
        _prune(db, ub)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/progress", response_model=UserBookOut)
def save_progress(data: ProgressIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ub = _get_or_create(db, user, data)
    _apply_progress(ub, data.page, data.total_pages)
    db.commit()
    db.refresh(ub)
    return ub


def _load_settings(raw: dict | None) -> SettingsIO:
    """Lê as configurações salvas ignorando valores antigos/ inválidos (em vez de dar erro 500)."""
    defaults = SettingsIO().model_dump()
    merged = dict(defaults)
    for k, v in (raw or {}).items():
        if k not in defaults:
            continue
        try:
            SettingsIO(**{**defaults, k: v})
            merged[k] = v
        except ValidationError:
            pass
    return SettingsIO(**merged)


@router.get("/settings", response_model=SettingsIO)
def get_settings_(user: User = Depends(get_current_user)):
    return _load_settings(user.settings)


@router.put("/settings", response_model=SettingsIO)
def put_settings(data: SettingsIO, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user.settings = data.model_dump()
    db.commit()
    return data


@router.get("/stats", response_model=StatsOut)
def stats(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    books = db.scalars(select(UserBook).where(UserBook.user_id == user.id)).all()
    # Conta só as identificações que encontraram um livro (antes contava qualquer tentativa).
    ids = db.scalar(select(func.count(SearchLog.id)).where(
        SearchLog.user_id == user.id, SearchLog.top_book_id.is_not(None))) or 0
    bookmarks = db.scalar(select(func.count(Bookmark.id)).where(Bookmark.user_id == user.id)) or 0
    highlights = db.scalar(select(func.count(Highlight.id)).where(Highlight.user_id == user.id)) or 0
    return StatsOut(
        favorites=sum(b.is_favorite for b in books),
        library=sum(1 for b in books if b.status),
        reading=sum(1 for b in books if b.status == "lendo"),
        completed=sum(1 for b in books if b.status == "concluido"),
        identifications=ids,
        bookmarks=bookmarks,
        highlights=highlights,
    )


@router.post("/import", response_model=list[UserBookOut])
def import_guest_data(data: ImportIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Migra o que o visitante fez antes de criar conta (sem sobrescrever dados do servidor)."""
    for ref in data.favorites:
        ub = _get_or_create(db, user, ref)
        if not ub.is_favorite:
            ub.is_favorite, ub.favorited_at = True, _now()
    db.flush()
    for item in data.library:
        ub = _get_or_create(db, user, item)
        if not ub.status:
            ub.status, ub.added_at = item.status, _now()
    db.flush()
    for p in data.progress:
        ub = _get_or_create(db, user, p)
        if p.page >= ub.page:
            _apply_progress(ub, p.page, p.total_pages)
    if data.settings and not user.settings:
        user.settings = data.settings.model_dump()
    for bm in data.bookmarks:
        upsert_bookmark(db, user, bm, keep_existing_note=True)
    existing = {
        (h.book_key, h.page, h.version, h.start, h.end)
        for h in db.scalars(select(Highlight).where(Highlight.user_id == user.id)).all()
    }
    for hl in data.highlights:
        sig = (hl.book_key, hl.page, hl.version, hl.start, hl.end)
        if sig not in existing:  # importar duas vezes não duplica
            create_highlight(db, user, hl)
            existing.add(sig)
    db.commit()
    return db.scalars(select(UserBook).where(UserBook.user_id == user.id)).all()
