from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..i18n import get_lang, msg
from ..models import Book
from ..schemas import BookDetail, FreeVersion, ReaderPage, SearchResult
from ..services import gutenberg, openlibrary, translation
from ..services.text_utils import keywords, normalize_text, word_overlap

router = APIRouter(prefix="/api", tags=["catálogo"])

GUTENBERG_PREFIX = "gutenberg:"


def _catalog_result(book: Book) -> SearchResult:
    return SearchResult(
        book_key=f"{GUTENBERG_PREFIX}{book.gutenberg_id}", title=book.title, author=book.author,
        cover_url=book.cover_url, has_fulltext=True, source="acervo",
        gutenberg_id=book.gutenberg_id, in_catalog=True,
    )


def _find_catalog_book(books: list[dict], title: str) -> dict | None:
    for book in books:
        if word_overlap(book["title"], title) >= 0.8:
            return book
    return None


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/search", response_model=list[SearchResult])
def search(
    q: str = Query(min_length=1, max_length=500, pattern=r"^[^\x00]*$"),
    mode: str = Query("text", pattern="^(text|phrase|description)$"),
    language: str | None = Query(None, pattern="^(pt|en)$"),
    limit: int = Query(20, ge=1, le=40),
    db: Session = Depends(get_db),
    lang: str = Depends(get_lang),
):
    """Busca no acervo local + Open Library.

    - `language` filtra por idioma do livro (pt/en).
    - Para frases e descrições longas, se a busca literal não achar nada,
      tenta de novo só com as palavras-chave (sem artigos/preposições).
    """
    q = q.strip()
    if not q:
        raise HTTPException(400, msg("empty_query", lang))

    like = f"%{_escape_like(q)}%"
    local_q = select(Book).where(or_(Book.title.ilike(like, escape="\\"), Book.author.ilike(like, escape="\\")))
    if language:
        local_q = local_q.where(Book.language == language)
    local = db.scalars(local_q.limit(5)).all()
    results = [_catalog_result(b) for b in local]
    seen_titles = {normalize_text(b.title) for b in local}
    db.rollback()  # libera a conexão antes de chamar a Open Library

    try:
        remote = openlibrary.search(q, limit, language=language)
        if not remote and mode in ("phrase", "description"):
            kw = keywords(q)
            if kw and kw != normalize_text(q):
                remote = openlibrary.search(kw, limit, language=language)
    except openlibrary.UpstreamError:
        if results:
            return results
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, msg("openlibrary_down", lang)) from None

    for r in remote:
        if normalize_text(r["title"]) in seen_titles:
            continue
        results.append(SearchResult(**r))
    return results[:limit]


@router.get("/details", response_model=BookDetail)
def details(key: str = Query(min_length=2, max_length=120, pattern=r"^[^\x00]*$"), db: Session = Depends(get_db),
            lang: str = Depends(get_lang)):
    """Detalhes de um livro. `key` é /works/OL...W (Open Library) ou gutenberg:<id> (acervo)."""
    if key.startswith(GUTENBERG_PREFIX):
        try:
            gid = int(key.removeprefix(GUTENBERG_PREFIX))
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, msg("bad_key", lang)) from None
        book = db.scalar(select(Book).where(Book.gutenberg_id == gid))
        if not book:
            raise HTTPException(status.HTTP_404_NOT_FOUND, msg("not_in_catalog", lang))
        return BookDetail(
            book_key=key, title=book.title, author=book.author, cover_url=book.cover_url,
            description=msg("catalog_description", lang, n=book.excerpt_count),
            subjects=[], in_catalog=True,
            free_version=FreeVersion(gutenberg_id=gid, title=book.title, authors=[book.author],
                                     languages=[book.language]),
        )

    if not key.startswith("/works/"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, msg("bad_key", lang))
    # Copia só os campos necessários: depois do rollback os objetos do ORM ficam expirados.
    catalog_books = [{"gutenberg_id": b.gutenberg_id, "title": b.title, "author": b.author, "language": b.language}
                     for b in db.scalars(select(Book)).all()]
    db.rollback()  # libera a conexão antes das chamadas externas
    try:
        detail = openlibrary.work_details(key)
    except openlibrary.UpstreamError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, msg("details_down", lang)) from None

    catalog_book = _find_catalog_book(catalog_books, detail["title"])
    if catalog_book:
        free = FreeVersion(gutenberg_id=catalog_book["gutenberg_id"], title=catalog_book["title"],
                           authors=[catalog_book["author"]], languages=[catalog_book["language"]])
    else:
        found = gutenberg.find_free_version(detail["title"], detail["author"])
        free = FreeVersion(**found) if found else None
    return BookDetail(**detail, free_version=free, in_catalog=catalog_book is not None)


@router.get("/reader/{gutenberg_id}", response_model=ReaderPage)
def reader_page(
    gutenberg_id: int = Path(ge=1, le=999999),
    page: int = Query(0, ge=0, le=100_000),
    translate_to: str | None = Query(None, alias="lang", pattern="^(pt|en)$"),
    db: Session = Depends(get_db),
    lang: str = Depends(get_lang),
):
    """Uma página do livro.

    O servidor baixa e pagina o texto (sem problema de CORS). Com `lang=pt|en`,
    se o livro estiver em outro idioma, a página volta traduzida (e fica em cache).
    """
    try:
        book = gutenberg.get_paged_book(db, gutenberg_id)
    except gutenberg.GutenbergError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, msg("gutenberg_down", lang)) from None

    page = min(page, len(book.pages) - 1)
    content = book.pages[page]
    out = ReaderPage(gutenberg_id=gutenberg_id, page=page, total_pages=len(book.pages), content=content,
                     original_language=book.language, language=book.language)

    if translate_to and book.language and book.language != translate_to:
        try:
            out.content = translation.get_translated_page(db, gutenberg_id, page, content, book.language, translate_to)
            out.language, out.translated = translate_to, True
        except translation.TranslationError:
            out.translation_error = msg("translation_failed", lang)
    return out
