"""Marcadores de página e trechos sublinhados do usuário."""
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..i18n import get_lang, msg
from ..models import Bookmark, Highlight, User
from ..schemas import (
    AnnotationsOut, BookmarkIn, BookmarkOut, HighlightIn, HighlightOut, HighlightPatch,
)

router = APIRouter(prefix="/api/me", tags=["marcadores"])


@router.get("/annotations", response_model=AnnotationsOut)
def list_annotations(
    book_key: str | None = Query(None, max_length=120, pattern=r"^[^\x00]*$"),
    limit: int = Query(500, ge=1, le=2000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Marcadores e sublinhados — de um livro (book_key) ou os mais recentes de todos."""
    bq = select(Bookmark).where(Bookmark.user_id == user.id)
    hq = select(Highlight).where(Highlight.user_id == user.id)
    if book_key:
        bq, hq = bq.where(Bookmark.book_key == book_key), hq.where(Highlight.book_key == book_key)
    return AnnotationsOut(
        bookmarks=db.scalars(bq.order_by(Bookmark.created_at.desc()).limit(limit)).all(),
        highlights=db.scalars(hq.order_by(Highlight.created_at.desc()).limit(limit)).all(),
    )


def upsert_bookmark(db: Session, user: User, data: BookmarkIn, keep_existing_note: bool = False) -> Bookmark:
    """Cria ou atualiza o marcador da página (seguro contra dois cliques simultâneos)."""
    db.execute(
        pg_insert(Bookmark)
        .values(user_id=user.id, book_key=data.book_key, page=data.page, title=data.title,
                author=data.author, gutenberg_id=data.gutenberg_id, note="")
        .on_conflict_do_nothing(constraint="uq_bookmark_page")
    )
    bm = db.scalar(select(Bookmark).where(
        Bookmark.user_id == user.id, Bookmark.book_key == data.book_key, Bookmark.page == data.page))
    bm.title, bm.author, bm.gutenberg_id = data.title, data.author, data.gutenberg_id
    note = data.note.strip()
    # Na importação do modo visitante, uma anotação vazia não apaga a que já existe na conta.
    if not (keep_existing_note and bm.note and not note):
        bm.note = note
    db.flush()
    return bm


@router.put("/bookmarks", response_model=BookmarkOut)
def put_bookmark(data: BookmarkIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Cria o marcador da página (ou atualiza a anotação, se já existir)."""
    bm = upsert_bookmark(db, user, data)
    db.commit()
    db.refresh(bm)
    return bm


@router.delete("/bookmarks/{bookmark_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bookmark(bookmark_id: int = Path(ge=1, le=2_147_483_647), user: User = Depends(get_current_user), db: Session = Depends(get_db),
                    lang: str = Depends(get_lang)):
    bm = db.get(Bookmark, bookmark_id)
    if not bm or bm.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, msg("not_found", lang))
    db.delete(bm)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def create_highlight(db: Session, user: User, data: HighlightIn) -> Highlight:
    hl = Highlight(user_id=user.id, **data.model_dump())
    hl.text = hl.text.strip()
    db.add(hl)
    db.flush()
    return hl


@router.post("/highlights", response_model=HighlightOut, status_code=status.HTTP_201_CREATED)
def post_highlight(data: HighlightIn, user: User = Depends(get_current_user), db: Session = Depends(get_db),
                   lang: str = Depends(get_lang)):
    if not data.text.strip():
        raise HTTPException(400, msg("bad_highlight", lang))
    hl = create_highlight(db, user, data)
    db.commit()
    db.refresh(hl)
    return hl


def _own_highlight(db: Session, user: User, highlight_id: int, lang: str) -> Highlight:
    hl = db.get(Highlight, highlight_id)
    if not hl or hl.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, msg("not_found", lang))
    return hl


@router.patch("/highlights/{highlight_id}", response_model=HighlightOut)
def patch_highlight(data: HighlightPatch, highlight_id: int = Path(ge=1, le=2_147_483_647), user: User = Depends(get_current_user),
                    db: Session = Depends(get_db), lang: str = Depends(get_lang)):
    hl = _own_highlight(db, user, highlight_id, lang)
    if data.color is not None:
        hl.color = data.color
    if data.note is not None:
        hl.note = data.note.strip()
    db.commit()
    db.refresh(hl)
    return hl


@router.delete("/highlights/{highlight_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_highlight(highlight_id: int = Path(ge=1, le=2_147_483_647), user: User = Depends(get_current_user), db: Session = Depends(get_db),
                     lang: str = Depends(get_lang)):
    db.delete(_own_highlight(db, user, highlight_id, lang))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
