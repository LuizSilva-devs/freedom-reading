"""Identificação fora do acervo: livros modernos, pagos ou que ainda não temos.

Quando o trecho não está no acervo, procuramos a frase dentro do texto de livros de
duas bases públicas — sem guardar nenhum texto, só o nome do livro:

- Google Books: busca no texto completo de milhões de livros (inclusive com direitos
  autorais). Devolve título, autor, capa, um pedacinho do texto e, quando o livro está
  à venda, o link da Google Play Livros.
- Internet Archive (pela Open Library): busca no texto dos livros digitalizados.
  Muitos podem ser emprestados de graça no archive.org. É mais lenta (10–30 s).

Para cada livro encontrado devolvemos "onde conseguir": comprar (Google Play, Amazon,
Estante Virtual para usados), emprestar/ler (Internet Archive, Google Books quando
liberado) — e o frontend ainda oferece "tenho o arquivo: adicionar à minha conta".
"""
import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import quote_plus

import httpx

from ..config import get_settings
from . import cache
from .text_utils import normalize_text, word_overlap

log = logging.getLogger(__name__)

GOOGLE_URL = "https://www.googleapis.com/books/v1/volumes"
INSIDE_URL = "https://openlibrary.org/search/inside.json"
_TAG_RE = re.compile(r"<[^>]+>")
_WORD_RE = re.compile(r"[\w'’-]+", re.U)
# Um grupo de threads por fonte: o Internet Archive lento não segura as buscas do Google Books.
_pools = {"google_books": ThreadPoolExecutor(max_workers=4, thread_name_prefix="google"),
          "internet_archive": ThreadPoolExecutor(max_workers=4, thread_name_prefix="archive")}


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True,
                        headers={"User-Agent": "Freadom-Reading/1.0 (projeto academico)"})


def pick_phrase(excerpt: str, words: int = 10) -> str:
    """Escolhe um pedaço do meio do trecho para a busca exata.

    O começo e o fim costumam ter palavras cortadas pela seleção; um erro de
    digitação só atrapalha se cair dentro desse pedaço.
    """
    tokens = _WORD_RE.findall(excerpt)
    if len(tokens) <= words:
        return " ".join(tokens)
    start = (len(tokens) - words) // 2
    return " ".join(tokens[start:start + words])


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub("", text or ""))).strip()


def store_links(title: str, author: str, isbn: str | None = None) -> list[dict]:
    """Lojas onde procurar o livro (links de busca, sem afiliação)."""
    q = quote_plus(isbn or f"{title} {author}".strip())
    q_text = quote_plus(f"{title} {author}".strip())
    return [
        {"kind": "buy", "label": "Amazon", "url": f"https://www.amazon.com.br/s?k={q}&i=stripbooks"},
        {"kind": "buy", "label": "Estante Virtual (usados)", "url": f"https://www.estantevirtual.com.br/busca?q={q_text}"},
    ]


def _google(phrase: str, s) -> list[dict]:
    params = {"q": f'"{phrase}"', "maxResults": 6, "printType": "books"}
    if s.google_books_api_key:
        params["key"] = s.google_books_api_key
    with _client(s.http_timeout) as client:
        resp = client.get(GOOGLE_URL, params=params)
        resp.raise_for_status()
        items = resp.json().get("items") or []

    out = []
    for item in items:
        info = item.get("volumeInfo") or {}
        title = info.get("title")
        if not title:
            continue
        if info.get("subtitle"):
            title = f"{title}: {info['subtitle']}"
        author = ", ".join(info.get("authors") or [])
        access, sale = item.get("accessInfo") or {}, item.get("saleInfo") or {}
        isbn = next((i.get("identifier") for i in info.get("industryIdentifiers") or []
                     if i.get("type") == "ISBN_13"), None)
        links = []
        if sale.get("saleability") == "FOR_SALE" and sale.get("buyLink"):
            links.append({"kind": "buy", "label": "Google Play Livros", "url": sale["buyLink"]})
        links += store_links(info.get("title", ""), author, isbn)
        reader_url = access.get("webReaderLink") or info.get("infoLink")
        if (access.get("viewability") == "ALL_PAGES" or access.get("publicDomain")) and reader_url:
            links.append({"kind": "read", "label": "Ler no Google Books", "url": reader_url})
        if info.get("infoLink"):
            links.append({"kind": "info", "label": "Google Books", "url": info["infoLink"]})
        images = info.get("imageLinks") or {}
        cover = images.get("thumbnail") or images.get("smallThumbnail")
        out.append({
            "title": title[:300], "author": author[:300],
            "year": (info.get("publishedDate") or "")[:4] or None,
            "language": (info.get("language") or "")[:5],
            "cover_url": cover.replace("http://", "https://") if cover else None,
            "snippet": _clean((item.get("searchInfo") or {}).get("textSnippet"))[:400],
            "source": "google_books",
            "public_domain": bool(access.get("publicDomain")),
            "links": links,
        })
    return out


def _archive(phrase: str, s) -> list[dict]:
    with _client(s.external_archive_timeout) as client:
        resp = client.get(INSIDE_URL, params={"q": f'"{phrase}"', "limit": 6})
        resp.raise_for_status()
        hits = (resp.json() or {}).get("hits")
    if not isinstance(hits, dict) or not isinstance(hits.get("total"), int):
        raise ValueError("Internet Archive respondeu sem resultados válidos")

    out = []
    for hit in hits.get("hits") or []:
        fields = hit.get("fields") or {}
        ident = (fields.get("identifier") or [None])[0]
        title = (fields.get("meta_title") or [None])[0]
        if not ident or not title:
            continue
        author = (fields.get("meta_creator") or [""])[0] or ""
        snippet = " … ".join(_clean(t) for t in ((hit.get("highlight") or {}).get("text") or [])[:2])
        snippet = snippet.replace("{{{", "").replace("}}}", "")
        links = [{"kind": "borrow", "label": "Internet Archive (ler/emprestar)",
                  "url": f"https://archive.org/details/{quote_plus(ident)}"}]
        links += store_links(title, author)
        out.append({
            "title": title[:300], "author": author[:300], "year": None, "language": "",
            "cover_url": f"https://archive.org/services/img/{quote_plus(ident)}",
            "snippet": snippet[:400], "source": "internet_archive", "public_domain": False, "links": links,
        })
    return out


def _safe_links(links: list[dict]) -> list[dict]:
    return [link for link in links if isinstance(link.get("url"), str) and link["url"].startswith(("https://", "http://"))]


def _same_book(a: dict, b: dict) -> bool:
    return (word_overlap(a["title"].split(":")[0], b["title"]) >= 0.8
            and (not a["author"] or not b["author"] or word_overlap(a["author"], b["author"]) >= 0.3))


def search(excerpt: str) -> dict:
    """Procura o trecho nas fontes externas. Fontes fora do ar não derrubam a busca."""
    s = get_settings()
    phrase = pick_phrase(excerpt)
    key = f"ext:{normalize_text(phrase)}"
    cached = cache.get(key)
    if not cache.is_miss(cached):
        return cached

    jobs = {"google_books": _pools["google_books"].submit(_google, phrase, s)}
    if s.external_archive:
        jobs["internet_archive"] = _pools["internet_archive"].submit(_archive, phrase, s)
    wait(jobs.values(), timeout=s.external_archive_timeout + 2)

    results: list[dict] = []
    failed: list[str] = []
    for name, fut in jobs.items():
        if not fut.done():
            failed.append(name)
            continue
        try:
            for r in fut.result():
                r["links"] = _safe_links(r["links"])
                dup = next((x for x in results if _same_book(x, r)), None)
                if dup:
                    # Mesmo livro nas duas fontes: junta os links que faltam.
                    have = {(link["kind"], link["label"]) for link in dup["links"]}
                    dup["links"] += [link for link in r["links"] if (link["kind"], link["label"]) not in have]
                    dup["cover_url"] = dup["cover_url"] or r["cover_url"]
                else:
                    results.append(r)
        except Exception as exc:  # noqa: BLE001 — uma fonte fora do ar não derruba a outra
            log.info("Busca externa em %s falhou: %s", name, exc)
            failed.append(name)

    out = {"phrase": phrase, "results": results[:8], "failed": failed}
    if not failed:
        cache.set(key, out, ttl=6 * 3600)
    return out
