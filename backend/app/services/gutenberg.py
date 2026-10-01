"""Project Gutenberg: busca de versão gratuita (Gutendex) e download do texto.

O download agora é feito pelo SERVIDOR — isso elimina o problema de CORS que
a versão estática tinha (e o proxy público allorigins, que era frágil).
"""
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import Lock

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Book, Excerpt
from . import cache
from .text_utils import (
    normalize_text, paginate_chunks, parse_gutenberg_header, split_into_chunks,
    strip_gutenberg_boilerplate, word_overlap,
)

GUTENDEX_URL = "https://gutendex.com/books"
TEXT_URLS = [
    "https://www.gutenberg.org/cache/epub/{id}/pg{id}.txt",
    "https://www.gutenberg.org/files/{id}/{id}-0.txt",
    "https://www.gutenberg.org/files/{id}/{id}.txt",
]


class GutenbergError(Exception):
    pass


def _client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(timeout=s.http_timeout, follow_redirects=True,
                        headers={"User-Agent": "Freadom-Reading/1.0 (projeto academico)"})


def find_free_version(title: str, author: str | None = None) -> dict | None:
    """Procura no Gutendex um livro com título/autor parecidos."""
    if not title:
        return None
    key = f"gd:free:{normalize_text(title)}:{normalize_text(author or '')}"
    cached = cache.get(key)
    if not cache.is_miss(cached):
        return cached

    try:
        with _client() as client:
            resp = client.get(GUTENDEX_URL, params={"search": title})
            resp.raise_for_status()
            results = resp.json().get("results", [])
    except (httpx.HTTPError, ValueError):
        return None  # sem versão gratuita conhecida não é erro fatal

    best, best_score = None, 0.0
    for book in results:
        b_authors = ", ".join(a.get("name", "") for a in book.get("authors", []))
        score = word_overlap(title, book.get("title", ""))
        if author:
            score += 0.3 * word_overlap(author, b_authors)
        if score > best_score:
            best, best_score = book, score

    result = None
    if best and best_score >= 0.35:
        result = {
            "gutenberg_id": best["id"],
            "title": best.get("title"),
            "authors": [a.get("name") for a in best.get("authors", [])],
            "languages": best.get("languages", []),
        }
    cache.set(key, result, ttl=3600)
    return result


def _cache_path(gutenberg_id: int) -> Path:
    d = get_settings().cache_dir
    d.mkdir(parents=True, exist_ok=True)
    return d / f"pg{gutenberg_id}.txt"


def download_raw(gutenberg_id: int) -> str:
    """Baixa o .txt do Gutenberg (com cache em disco)."""
    path = _cache_path(gutenberg_id)
    if path.exists():
        return path.read_text(encoding="utf-8")

    last_error = None
    with _client() as client:
        for tpl in TEXT_URLS:
            url = tpl.format(id=gutenberg_id)
            try:
                resp = client.get(url)
                if resp.is_success and len(resp.content) > 500:
                    resp.encoding = resp.encoding or "utf-8"
                    raw = resp.text
                    # Grava em arquivo temporário e renomeia: outra requisição nunca lê um arquivo pela metade.
                    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
                    tmp.write_text(raw, encoding="utf-8")
                    tmp.replace(path)
                    return raw
                last_error = f"{url} -> HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                last_error = f"{url} -> {exc}"
    raise GutenbergError(f"Não foi possível baixar o livro {gutenberg_id}: {last_error}")


@lru_cache(maxsize=8)
def _parsed_download(gutenberg_id: int) -> tuple[tuple[str, ...], str]:
    raw = download_raw(gutenberg_id)
    language = parse_gutenberg_header(raw)["language"]
    return tuple(split_into_chunks(strip_gutenberg_boilerplate(raw))), language


@dataclass(frozen=True)
class PagedBook:
    pages: tuple[str, ...]
    chunk_to_page: tuple[int, ...]
    language: str          # código de 2 letras ("pt", "en") ou "" se desconhecido


_page_cache: OrderedDict[tuple, PagedBook] = OrderedDict()
_page_lock = Lock()
_PAGE_CACHE_SIZE = 24


def get_paged_book(db: Session, gutenberg_id: int) -> PagedBook:
    """Livro já dividido em páginas do leitor, com cache em memória.

    Livros do acervo vêm do banco (a chave do cache inclui a quantidade e o maior id
    dos trechos, então uma reingestão com --replace invalida o cache sozinha).
    Os demais são baixados do Gutenberg.
    """
    page_chars = get_settings().reader_page_chars
    book = db.scalar(select(Book).where(Book.gutenberg_id == gutenberg_id))
    if book:
        count, max_id = db.execute(
            select(func.count(Excerpt.id), func.max(Excerpt.id)).where(Excerpt.book_id == book.id)
        ).one()
        key = ("db", gutenberg_id, page_chars, count, max_id)
    else:
        key = ("dl", gutenberg_id, page_chars)

    with _page_lock:
        cached = _page_cache.get(key)
        if cached:
            _page_cache.move_to_end(key)
            return cached

    if book and count:
        chunks = db.scalars(
            select(Excerpt.content).where(Excerpt.book_id == book.id).order_by(Excerpt.position)
        ).all()
        language = (book.language or "")[:2]
    else:
        # O download pode levar vários segundos: devolve a conexão ao pool antes,
        # para não segurar uma conexão do banco parada esperando a internet.
        db.rollback()
        chunks, language = _parsed_download(gutenberg_id)

    pages, mapping = paginate_chunks(list(chunks), page_chars)
    paged = PagedBook(tuple(pages), tuple(mapping), language)
    with _page_lock:
        _page_cache[key] = paged
        while len(_page_cache) > _PAGE_CACHE_SIZE:
            _page_cache.popitem(last=False)
    return paged


def clear_page_cache() -> None:
    with _page_lock:
        _page_cache.clear()
