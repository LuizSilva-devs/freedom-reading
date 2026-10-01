"""Identificação de livro a partir de um trecho (o "Shazam dos livros").

Estratégia:
1. Normaliza o trecho do usuário com a mesma regra usada na ingestão.
2. Usa `word_similarity` do pg_trgm (operador `<%`) — diferente de `similarity`,
   ele compara o trecho do usuário com a MELHOR sub-extensão do parágrafo,
   então um pedaço curto dentro de um parágrafo longo ainda pontua alto.
3. Trechos longos são quebrados em janelas, porque o usuário pode ter copiado
   algo que atravessa dois parágrafos do acervo.
4. Agrega por livro e devolve os candidatos ordenados por confiança.

IMPORTANTE (bug já visto no projeto): o limite do pg_trgm é uma variável de
SESSÃO. Ele precisa ser definido na mesma conexão, logo antes da consulta.
"""
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import get_settings
from .text_utils import normalize_text

WINDOW_CHARS = 260
MAX_WINDOWS = 4


@dataclass
class ExcerptHit:
    excerpt_id: int
    book_id: int
    position: int
    content: str
    score: float


@dataclass
class BookMatch:
    book_id: int
    score: float
    best: ExcerptHit
    hits: int = 1
    windows_matched: set[int] = field(default_factory=set)


def build_windows(normalized: str) -> list[str]:
    if len(normalized) <= WINDOW_CHARS + 60:
        return [normalized]
    words = normalized.split()
    windows, buf = [], []
    for w in words:
        buf.append(w)
        if sum(len(x) + 1 for x in buf) >= WINDOW_CHARS:
            windows.append(" ".join(buf))
            buf = []
    if buf and len(" ".join(buf)) > 60:
        windows.append(" ".join(buf))
    return windows[:MAX_WINDOWS]


def _query_window(db: Session, window: str, threshold: float, limit: int) -> list[ExcerptHit]:
    db.execute(
        text("SELECT set_config('pg_trgm.word_similarity_threshold', :t, false)"),
        {"t": str(threshold)},
    )
    rows = db.execute(
        text(
            """
            SELECT e.id, e.book_id, e.position, e.content,
                   word_similarity(:q, e.content_normalized) AS score
            FROM excerpts e
            WHERE :q <% e.content_normalized
            ORDER BY score DESC
            LIMIT :limit
            """
        ),
        {"q": window, "limit": limit},
    ).all()
    return [ExcerptHit(r.id, r.book_id, r.position, r.content, float(r.score)) for r in rows]


def identify(db: Session, excerpt: str, threshold: float | None = None, top: int = 5) -> tuple[list[BookMatch], float]:
    settings = get_settings()
    threshold = settings.similarity_threshold if threshold is None else threshold
    normalized = normalize_text(excerpt)[: settings.max_excerpt_chars]
    windows = build_windows(normalized)

    by_book: dict[int, BookMatch] = {}
    for w_idx, window in enumerate(windows):
        for hit in _query_window(db, window, threshold, limit=20):
            match = by_book.get(hit.book_id)
            if match is None:
                by_book[hit.book_id] = BookMatch(hit.book_id, hit.score, hit, 1, {w_idx})
                continue
            match.hits += 1
            match.windows_matched.add(w_idx)
            if hit.score > match.best.score:
                match.best = hit
                match.score = hit.score

    # Bônus pequeno quando várias janelas do trecho apontam para o mesmo livro.
    for m in by_book.values():
        coverage = len(m.windows_matched) / len(windows)
        m.score = min(1.0, m.best.score * (0.85 + 0.15 * coverage))

    ranked = sorted(by_book.values(), key=lambda m: m.score, reverse=True)[:top]
    return ranked, threshold


def confidence_label(score: float) -> str:
    if score >= 0.8:
        return "alta"
    if score >= 0.6:
        return "média"
    return "baixa"
