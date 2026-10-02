"""Outras fontes de livros além do Project Gutenberg.

- Wikisource: procurar e importar obras para o acervo público.
- Arquivos do usuário (.txt, .epub, .pdf): livros particulares, só para quem enviou.
"""
from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Path, Query, Request, Response, UploadFile,
    status,
)
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import ratelimit
from ..config import get_settings
from ..database import get_db
from ..deps import get_current_user
from ..i18n import get_lang, msg
from ..models import Book, Bookmark, Highlight, PageTranslation, User, UserBook
from ..schemas import BookOut, WikisourceJob, WikisourceImportIn, WikisourceResult
from ..services import gutenberg, uploads, wikisource
from ..services.books import next_local_id
from ..services.ingest import store_book
from ..services.text_utils import split_into_chunks

router = APIRouter(prefix="/api", tags=["outras fontes"])


# ------------------------------------------------------------------------------------- Wikisource
@router.get("/wikisource/search", response_model=list[WikisourceResult])
def wikisource_search(request: Request, q: str = Query(min_length=2, max_length=200, pattern=r"^[^\x00]*$"),
                      lang: str = Query("pt", pattern="^(pt|en)$"),
                      db: Session = Depends(get_db), ui_lang: str = Depends(get_lang)):
    """Procura obras na Wikisource (pt ou en) e diz quais já estão no acervo."""
    ip = request.client.host if request.client else "?"
    if not ratelimit.allow(f"ws-search:{ip}", 60, 3600):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, msg("too_many_attempts", ui_lang))
    try:
        results = wikisource.search(q.strip(), lang)
    except Exception:  # noqa: BLE001 — Wikisource fora do ar
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, msg("wikisource_down", ui_lang)) from None
    urls = [r["url"] for r in results]
    in_catalog = dict(db.execute(
        select(Book.source_url, Book.gutenberg_id).where(Book.source_url.in_(urls), Book.owner_id.is_(None))
    ).all())
    return [WikisourceResult(**r, gutenberg_id=in_catalog.get(r["url"])) for r in results]


@router.post("/wikisource/import", response_model=WikisourceJob, status_code=status.HTTP_202_ACCEPTED)
def wikisource_import(data: WikisourceImportIn, background: BackgroundTasks,
                      user: User = Depends(get_current_user), db: Session = Depends(get_db),
                      lang: str = Depends(get_lang)):
    """Importa uma obra da Wikisource para o acervo público (em segundo plano).

    Exige conta (evita abuso). Acompanhe em GET /api/wikisource/jobs/{id}.
    """
    existing = wikisource.find_in_catalog(db, wikisource.page_url(data.lang, data.title))
    if existing:
        return {"id": "0" * 32, "lang": data.lang, "title": data.title, "status": "done",
                "book": BookOut.model_validate(existing).model_dump()}
    if not ratelimit.allow(f"wikisource:{user.id}", get_settings().wikisource_imports_per_hour, 3600):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, msg("too_many_attempts", lang))
    job, is_new = wikisource.new_job(data.lang, data.title, user.id)
    if is_new:
        background.add_task(wikisource.run_job, job["id"])
    return job


@router.get("/wikisource/jobs/{job_id}", response_model=WikisourceJob)
def wikisource_job(job_id: str = Path(pattern="^[0-9a-f]{32}$"), user: User = Depends(get_current_user),
                   lang: str = Depends(get_lang)):
    job = wikisource.get_job(job_id)
    if not job or user.id not in job["watchers"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, msg("job_not_found", lang))
    return job


# ------------------------------------------------------------------------------- arquivos do usuário
@router.get("/me/uploads", response_model=list[BookOut])
def list_uploads(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.scalars(select(Book).where(Book.owner_id == user.id).order_by(Book.created_at.desc())).all()


@router.post("/me/uploads", response_model=BookOut, status_code=status.HTTP_201_CREATED)
async def upload_book(
    file: UploadFile = File(...),
    title: str = Form("", max_length=300),
    author: str = Form("", max_length=300),
    language: str = Form("", pattern="^(pt|en|)$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    lang: str = Depends(get_lang),
):
    """Envia um livro seu (.txt, .epub ou .pdf com texto). Ele fica particular:
    só você lê e só os seus trechos são comparados com ele."""
    s = get_settings()
    if not (file.filename or "").lower().endswith(uploads.EXTENSIONS):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg("upload_unsupported", lang))
    count = db.scalar(select(func.count(Book.id)).where(Book.owner_id == user.id)) or 0
    if count >= s.uploads_per_user:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg("upload_too_many", lang, n=s.uploads_per_user))
    if not ratelimit.allow(f"upload:{user.id}", s.uploads_per_hour, 3600):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, msg("too_many_attempts", lang))

    max_bytes = int(s.upload_max_mb * 1024 * 1024)
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg("upload_too_big", lang, n=s.upload_max_mb))
    try:
        # Extrair texto de PDF/EPUB grande leva alguns segundos: roda fora do loop do servidor.
        from starlette.concurrency import run_in_threadpool
        text, meta = await run_in_threadpool(uploads.extract, file.filename or "", data)
    except uploads.UploadError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg(f"upload_{exc.code}", lang, n=s.upload_max_mb)) from None

    chunks = split_into_chunks(text)
    name = (file.filename or "").rsplit(".", 1)[0].replace("_", " ").strip()
    for _ in range(3):
        try:
            book = store_book(
                db, gutenberg_id=next_local_id(db), chunks=chunks,
                title=title.strip() or meta.get("title") or name or "Sem título",
                author=author.strip() or meta.get("author") or "",
                language=language or meta.get("language") or "pt",
                source="upload", source_url=None, owner_id=user.id,
            )
            return book
        except IntegrityError:
            db.rollback()
    raise HTTPException(status.HTTP_409_CONFLICT, msg("upload_save_failed", lang))


@router.delete("/me/uploads/{gutenberg_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_upload(gutenberg_id: int = Path(ge=1, le=9_999_999), user: User = Depends(get_current_user),
                  db: Session = Depends(get_db), lang: str = Depends(get_lang)):
    """Apaga o livro enviado, o texto extraído, as traduções e as anotações dele."""
    book = db.scalar(select(Book).where(Book.gutenberg_id == gutenberg_id, Book.owner_id == user.id))
    if not book:
        raise HTTPException(status.HTTP_404_NOT_FOUND, msg("not_in_catalog", lang))
    purge_book(db, book)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def purge_book(db: Session, book: Book) -> None:
    key = f"gutenberg:{book.gutenberg_id}"
    db.execute(delete(PageTranslation).where(PageTranslation.gutenberg_id == book.gutenberg_id))
    if book.owner_id:
        for model in (UserBook, Bookmark, Highlight):
            db.execute(delete(model).where(model.user_id == book.owner_id, model.book_key == key))
    db.delete(book)
    gutenberg.clear_page_cache()
