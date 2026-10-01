"""Ingestão de livros do Project Gutenberg no acervo de identificação.

Uso (a partir de backend/):
    python -m scripts.ingest_gutenberg                  # lista padrão do projeto
    python -m scripts.ingest_gutenberg 55752 1342       # IDs específicos
    python -m scripts.ingest_gutenberg --file livro.txt --id 99999 --title "X" --author "Y"
    python -m scripts.ingest_gutenberg --replace 55752  # reindexa um livro já ingerido

Fluxo: baixa o .txt -> remove o cabeçalho/licença do Gutenberg -> divide em
trechos do tamanho de parágrafos -> grava Book + Excerpt (texto original e normalizado).
"""
import argparse
import sys
from pathlib import Path

from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, init_db
from app.models import Book, Excerpt
from app.services.gutenberg import GutenbergError, download_raw
from app.services.text_utils import (
    normalize_text, parse_gutenberg_header, split_into_chunks, strip_gutenberg_boilerplate,
)

DEFAULT_BOOKS = [
    55752,  # Dom Casmurro — Machado de Assis
    54829,  # Memórias Póstumas de Brás Cubas — Machado de Assis
    11,     # Alice's Adventures in Wonderland — Lewis Carroll
    1342,   # Pride and Prejudice — Jane Austen
]


def ingest(gutenberg_id: int, raw: str, title: str | None = None, author: str | None = None,
           replace: bool = False) -> None:
    meta = parse_gutenberg_header(raw)
    title = title or meta["title"] or f"Livro {gutenberg_id}"
    author = author or meta["author"] or "Autor desconhecido"
    chunks = split_into_chunks(strip_gutenberg_boilerplate(raw))
    if not chunks:
        print(f"  ! {gutenberg_id}: nenhum trecho extraído, pulando.")
        return

    with SessionLocal() as db:
        book = db.scalar(select(Book).where(Book.gutenberg_id == gutenberg_id))
        if book and not replace:
            print(f"  = {gutenberg_id} ({book.title}) já está no acervo. Use --replace para reindexar.")
            return
        if book:
            db.execute(delete(Excerpt).where(Excerpt.book_id == book.id))
        else:
            book = Book(gutenberg_id=gutenberg_id)
            db.add(book)
        book.title, book.author, book.language = title[:300], author[:300], (meta["language"] or "pt")[:10]
        book.excerpt_count = len(chunks)
        db.flush()

        db.add_all(
            Excerpt(book_id=book.id, position=i, content=c, content_normalized=normalize_text(c))
            for i, c in enumerate(chunks)
        )
        db.commit()
    print(f"  + {gutenberg_id}: {title} — {author} ({len(chunks)} trechos)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*", type=int, help="IDs do Project Gutenberg")
    parser.add_argument("--file", type=Path, help="Ingerir um .txt local em vez de baixar")
    parser.add_argument("--id", type=int, help="ID a usar com --file")
    parser.add_argument("--title")
    parser.add_argument("--author")
    parser.add_argument("--replace", action="store_true", help="Reindexa livros já existentes")
    args = parser.parse_args()

    init_db()

    if args.file:
        if not args.id:
            parser.error("--file exige --id")
        ingest(args.id, args.file.read_text(encoding="utf-8"), args.title, args.author, args.replace)
        return 0

    ids = args.ids or DEFAULT_BOOKS
    print(f"Ingerindo {len(ids)} livro(s)...")
    failures = 0
    for gid in ids:
        try:
            ingest(gid, download_raw(gid), replace=args.replace)
        except GutenbergError as exc:
            failures += 1
            print(f"  ! {gid}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
