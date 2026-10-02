from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from .. import ratelimit
from ..deps import get_optional_user
from ..i18n import get_lang, msg
from ..models import Book, SearchLog, User
from ..schemas import BookOut, ExternalOut, IdentifyIn, IdentifyOut, MatchOut
from ..services import external, gutenberg
from ..services.books import visible_to
from ..services.matching import confidence_label, identify
from ..services.text_utils import normalize_text, tidy_text, word_overlap

router = APIRouter(prefix="/api", tags=["identificação"])


@router.get("/books", response_model=list[BookOut])
def list_catalog(db: Session = Depends(get_db), user: User | None = Depends(get_optional_user)):
    """Livros do acervo de identificação: os públicos + os arquivos que a própria pessoa enviou."""
    return db.scalars(select(Book).where(visible_to(user)).order_by(Book.title)).all()


@router.post("/identify", response_model=IdentifyOut)
def identify_excerpt(
    data: IdentifyIn,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_user),
    lang: str = Depends(get_lang),
):
    settings = get_settings()
    normalized = normalize_text(data.excerpt)
    if len(normalized.replace(" ", "")) < settings.min_excerpt_chars:
        raise HTTPException(400, msg("excerpt_too_short", lang, n=settings.min_excerpt_chars))

    catalog_size = db.scalar(select(func.count(Book.id)).where(visible_to(user))) or 0
    if catalog_size == 0:
        return IdentifyOut(matches=[], threshold=settings.similarity_threshold, catalog_size=0, message="empty_catalog")

    ranked, threshold = identify(db, data.excerpt, user_id=user.id if user else None)

    matches: list[MatchOut] = []
    for m in ranked:
        book = db.get(Book, m.book_id)
        # Paginação vem do cache (antes relia o livro inteiro do banco a cada resultado).
        mapping = gutenberg.get_paged_book(db, book.gutenberg_id).chunk_to_page
        page = mapping[m.best.position] if m.best.position < len(mapping) else 0
        matches.append(MatchOut(
            book=BookOut.model_validate(book),
            score=round(m.score, 3),
            confidence=confidence_label(m.score),
            excerpt=tidy_text(m.best.content),
            excerpt_position=m.best.position,
            page=page,
        ))

    db.add(SearchLog(
        user_id=user.id if user else None,
        query_chars=len(normalized),
        top_book_id=matches[0].book.id if matches else None,
        top_score=matches[0].score if matches else None,
        threshold=threshold,
    ))
    db.commit()

    message = ("found" if matches and matches[0].confidence != "baixa"
               else "low_confidence" if matches else "not_found")
    return IdentifyOut(matches=matches, threshold=threshold, catalog_size=catalog_size, message=message)


@router.post("/identify/external", response_model=ExternalOut)
def identify_external(data: IdentifyIn, request: Request, db: Session = Depends(get_db),
                      user: User | None = Depends(get_optional_user), lang: str = Depends(get_lang)):
    """Procura o trecho FORA do acervo (Google Books + Internet Archive).

    Serve para livros modernos ou pagos: devolve título, autor e onde comprar ou
    emprestar. Nenhum texto desses livros é guardado. É mais lenta (até ~30 s),
    por isso fica separada de /identify e o site só chama quando o acervo não achou.
    """
    settings = get_settings()
    if len(normalize_text(data.excerpt).replace(" ", "")) < settings.min_excerpt_chars:
        raise HTTPException(400, msg("excerpt_too_short", lang, n=settings.min_excerpt_chars))
    ip = request.client.host if request.client else "?"
    if not ratelimit.allow(f"external:{ip}", settings.external_searches_per_hour, 3600):
        raise HTTPException(429, msg("too_many_attempts", lang))
    out = external.search(data.excerpt)
    # Algum resultado já está no acervo (ex.: um clássico)? Aponta para o leitor do Freadom.
    books = db.execute(select(Book.gutenberg_id, Book.title, Book.author).where(visible_to(user))).all()
    results = []
    for r in out["results"]:
        match = next((b for b in books if word_overlap(r["title"].split(":")[0], b.title) >= 0.8
                      and (not r["author"] or word_overlap(r["author"], b.author) >= 0.3)), None)
        results.append({**r, "catalog_gutenberg_id": match.gutenberg_id if match else None})
    return {**out, "results": results}
