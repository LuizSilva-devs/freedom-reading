from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..deps import get_optional_user
from ..i18n import get_lang, msg
from ..models import Book, SearchLog, User
from ..schemas import BookOut, IdentifyIn, IdentifyOut, MatchOut
from ..services import gutenberg
from ..services.matching import confidence_label, identify
from ..services.text_utils import normalize_text, tidy_text

router = APIRouter(prefix="/api", tags=["identificação"])


@router.get("/books", response_model=list[BookOut])
def list_catalog(db: Session = Depends(get_db)):
    """Livros do acervo de identificação (ingeridos pelo script)."""
    return db.scalars(select(Book).order_by(Book.title)).all()


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

    catalog_size = db.scalar(select(func.count(Book.id))) or 0
    if catalog_size == 0:
        return IdentifyOut(matches=[], threshold=settings.similarity_threshold, catalog_size=0, message="empty_catalog")

    ranked, threshold = identify(db, data.excerpt)

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
