"""Ingestão de livros no acervo de identificação.

Usada em dois lugares:
- pelo script `scripts/ingest_gutenberg.py` (coleção inicial e livros avulsos);
- pelo leitor: quando alguém abre um livro do Gutenberg que ainda não está no acervo,
  ele é indexado em segundo plano e passa a ser reconhecido por trecho
  ("acervo que cresce com a leitura", ligado por AUTO_INGEST_ON_READ).

A divisão em trechos é a MESMA do leitor (split_into_chunks), então as páginas
de um livro continuam iguais antes e depois de ele entrar no acervo — marcadores
e sublinhados não mudam de lugar.
"""
import logging
from dataclasses import dataclass
from threading import Lock

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Book, Excerpt
from .text_utils import normalize_text, parse_gutenberg_header, split_into_chunks, strip_gutenberg_boilerplate

log = logging.getLogger(__name__)

# Abaixo disso quase sempre não é um livro (índices, catálogos, arquivos de áudio).
MIN_CHUNKS = 20


# Livros que já tiveram a indexação automática disparada neste processo: cada página
# aberta chama o leitor, mas a tarefa só precisa rodar uma vez por livro.
_claimed: set[int] = set()
_claim_lock = Lock()


def claim(gutenberg_id: int) -> bool:
    with _claim_lock:
        if gutenberg_id in _claimed:
            return False
        _claimed.add(gutenberg_id)
        return True


@dataclass
class IngestResult:
    status: str            # "added" | "replaced" | "exists" | "empty" | "too_small" | "too_big"
    title: str = ""
    author: str = ""
    chunks: int = 0


def ingest_raw(db: Session, gutenberg_id: int, raw: str, title: str | None = None, author: str | None = None,
               replace: bool = False, min_chunks: int = 1) -> IngestResult:
    meta = parse_gutenberg_header(raw)
    title = (title or meta["title"] or f"Livro {gutenberg_id}")[:300]
    author = (author or meta["author"] or "Autor desconhecido")[:300]

    book = db.scalar(select(Book).where(Book.gutenberg_id == gutenberg_id))
    if book and not replace:
        return IngestResult("exists", book.title, book.author, book.excerpt_count)

    chunks = split_into_chunks(strip_gutenberg_boilerplate(raw))
    if not chunks:
        return IngestResult("empty", title, author)
    if len(chunks) < min_chunks:
        return IngestResult("too_small", title, author, len(chunks))

    status = "replaced" if book else "added"
    store_book(db, gutenberg_id=gutenberg_id, chunks=chunks, title=title, author=author,
               language=meta["language"] or "pt", book=book)
    return IngestResult(status, title, author, len(chunks))


def store_book(db: Session, *, gutenberg_id: int, chunks: list[str], title: str, author: str, language: str,
               source: str = "gutenberg", source_url: str | None = None, owner_id: int | None = None,
               book: Book | None = None) -> Book:
    """Grava (ou regrava) um livro e seus trechos. Serve para todas as fontes."""
    if book:
        db.execute(delete(Excerpt).where(Excerpt.book_id == book.id))
    else:
        book = Book(gutenberg_id=gutenberg_id)
        db.add(book)
    book.title, book.author = title[:300], (author or "Autor desconhecido")[:300]
    book.language = (language or "pt")[:10]
    book.source, book.source_url, book.owner_id = source, source_url, owner_id
    book.excerpt_count = len(chunks)
    db.flush()
    db.add_all(
        Excerpt(book_id=book.id, position=i, content=c, content_normalized=normalize_text(c))
        for i, c in enumerate(chunks)
    )
    db.commit()
    return book


def ingest_after_read(gutenberg_id: int) -> None:
    """Tarefa em segundo plano disparada pelo leitor. Nunca levanta exceção."""
    from ..database import SessionLocal
    from . import gutenberg

    s = get_settings()
    if not s.auto_ingest_on_read:
        return
    try:
        raw = gutenberg.download_raw(gutenberg_id)   # já está no cache em disco: o leitor acabou de baixar
        if len(raw.encode("utf-8")) > s.auto_ingest_max_mb * 1024 * 1024:
            log.info("Livro %s grande demais para o acervo automático", gutenberg_id)
            return
        with SessionLocal() as db:
            result = ingest_raw(db, gutenberg_id, raw, min_chunks=MIN_CHUNKS)
        if result.status == "added":
            log.info("Acervo: + %s %s (%s trechos, via leitura)", gutenberg_id, result.title, result.chunks)
    except IntegrityError:
        pass  # duas pessoas abriram o mesmo livro ao mesmo tempo: a outra tarefa já indexou
    except Exception:  # noqa: BLE001 — tarefa de fundo: registra e segue
        log.exception("Falha ao indexar o livro %s depois da leitura", gutenberg_id)
