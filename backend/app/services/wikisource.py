"""Wikisource: biblioteca livre da Wikimedia, com muitas obras em domínio público
(muito forte em português: Machado, Alencar, Eça, Aluísio, Lima Barreto...).

Usamos a API oficial do MediaWiki (sem raspar o site):
- `list=search` para procurar obras pelo título;
- `action=parse` para pegar o HTML de cada página.

Na Wikisource um livro costuma ser uma página-índice com links para os capítulos,
que são subpáginas ("Dom Casmurro/I", "Dom Casmurro/II"...). Seguimos esses links
na ordem em que aparecem e juntamos o texto, descartando cabeçalhos de navegação,
notas e numeração de páginas.
"""
import logging
import re
import time
import uuid
from collections.abc import Callable
from html.parser import HTMLParser
from threading import Lock
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Book
from .books import next_local_id
from .ingest import store_book
from .text_utils import split_into_chunks

log = logging.getLogger(__name__)

LANGS = ("pt", "en")
MAX_PAGES = 400            # capítulos por obra (proteção contra páginas gigantes de índice)
MIN_CHUNKS = 5
AUTHOR_NS = ("Autor:", "Author:")

# Partes da página que não são texto do livro.
SKIP_CLASSES = {
    "ws-noexport", "noprint", "mw-editsection", "reference", "references", "mw-references-wrap",
    "navbox", "ws-header", "headertemplate", "catlinks", "ws-pagenum", "pagenum", "toc", "mw-empty-elt",
    "wst-header", "wst-sister", "sisitem", "metadata", "mw-cite-backlink",
}
SKIP_IDS = {"headertemplate", "toc", "nofooter", "footertemplate"}
SKIP_TAGS = {"script", "style", "sup", "noscript", "head", "title"}
BLOCK_TAGS = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "dd", "dt", "tr",
              "section", "center", "poem", "pre"}
VOID_TAGS = {"br", "img", "hr", "meta", "link", "input", "wbr", "source"}


class WikisourceError(Exception):
    """`code` vira mensagem traduzida no site: not_found | too_short | save_failed | unavailable."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def api_url(lang: str) -> str:
    if lang not in LANGS:
        raise WikisourceError("not_found")
    return f"https://{lang}.wikisource.org/w/api.php"


def page_url(lang: str, title: str) -> str:
    return f"https://{lang}.wikisource.org/wiki/{quote(title.replace(' ', '_'))}"


def _client() -> httpx.Client:
    s = get_settings()
    # A Wikimedia pede um User-Agent que identifique o projeto.
    return httpx.Client(timeout=s.http_timeout, follow_redirects=True,
                        headers={"User-Agent": "Freadom-Reading/1.0 (projeto academico; FastAPI)"})


def _api(client: httpx.Client, lang: str, **params) -> dict:
    params.update(format="json", formatversion=2)
    for attempt in (1, 2):
        try:
            resp = client.get(api_url(lang), params=params)
            if resp.status_code >= 500 and attempt == 1:
                continue
            resp.raise_for_status()
            data = resp.json()
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == 2:
                raise
            continue
        if "error" in data:
            code = data["error"].get("code", "")
            raise WikisourceError("not_found" if code in ("missingtitle", "invalidtitle") else "unavailable")
        return data
    raise WikisourceError("unavailable")


# --------------------------------------------------------------------------- HTML -> texto
class _TextExtractor(HTMLParser):
    def __init__(self, base_title: str = ""):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []   # (tag, abriu um trecho ignorado?)
        self.skip = 0
        self.base = base_title
        self.subpages: list[str] = []
        self.author: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        if tag == "a" and self.skip == 0:
            self._link(a, classes)
        if tag == "a" and self.author is None:
            t = a.get("title") or ""
            if t.startswith(AUTHOR_NS):   # o autor aparece no cabeçalho (que o texto ignora)
                self.author = t.split(":", 1)[1].strip()
        if tag in VOID_TAGS:
            if tag == "br" and not self.skip:
                self.parts.append("\n")
            return
        skipping = tag in SKIP_TAGS or bool(classes & SKIP_CLASSES) or a.get("id") in SKIP_IDS
        if skipping:
            self.skip += 1
        if tag in BLOCK_TAGS and not self.skip:
            self.parts.append("\n\n")
        self.stack.append((tag, skipping))

    def handle_endtag(self, tag):
        # HTML da Wikisource é bem formado, mas fecha com tolerância a tags soltas.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for _, was_skip in self.stack[i:]:
                    if was_skip:
                        self.skip -= 1
                del self.stack[i:]
                break
        if tag in BLOCK_TAGS and not self.skip:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def _link(self, a: dict, classes: set[str]):
        t = a.get("title") or ""
        if "new" in classes or not self.base:          # link vermelho: página ainda não existe
            return
        if t.startswith(self.base + "/") and t not in self.subpages:
            self.subpages.append(t)

    def text(self) -> str:
        raw = "".join(self.parts).replace("\xa0", " ")
        paragraphs = [re.sub(r"[ \t]+", " ", p).strip() for p in re.split(r"\n\s*\n", raw)]
        return "\n\n".join(p for p in paragraphs if p)


def html_to_text(html: str, base_title: str = "") -> tuple[str, list[str], str | None]:
    """Devolve (texto, subpáginas na ordem do documento, autor)."""
    p = _TextExtractor(base_title)
    p.feed(html)
    p.close()
    return p.text(), p.subpages, p.author


# --------------------------------------------------------------------------- busca e download
def search(query: str, lang: str = "pt", limit: int = 10) -> list[dict]:
    with _client() as client:
        data = _api(client, lang, action="query", list="search", srsearch=query,
                    srnamespace=0, srlimit=limit, srprop="snippet|wordcount")
    out = []
    for r in data.get("query", {}).get("search", []):
        title = r.get("title", "")
        out.append({
            "title": title,
            "lang": lang,
            "url": page_url(lang, title),
            "snippet": re.sub(r"<[^>]+>", "", r.get("snippet", ""))[:200],
            "is_chapter": "/" in title,   # subpágina (um capítulo), não a obra inteira
        })
    # Obras inteiras primeiro.
    return sorted(out, key=lambda r: r["is_chapter"])


def _parse(client: httpx.Client, lang: str, title: str) -> tuple[str, str]:
    data = _api(client, lang, action="parse", page=title, prop="text|displaytitle",
                redirects=1, disableeditsection=1, disabletoc=1)
    parsed = data.get("parse") or {}
    return parsed.get("text") or "", parsed.get("title") or title


def fetch_work(lang: str, title: str, progress: Callable[[int, int], None] | None = None) -> dict:
    """Baixa uma obra inteira (página-índice + capítulos)."""
    with _client() as client:
        html, real_title = _parse(client, lang, title)
        main_text, subpages, author = html_to_text(html, real_title)
        subpages = subpages[:MAX_PAGES]
        texts: list[str] = []
        if subpages:
            queue = list(subpages)
            done = 0
            while queue and done < MAX_PAGES:
                sub = queue.pop(0)
                sub_html, _ = _parse(client, lang, sub)
                text, deeper, _ = html_to_text(sub_html, sub)
                done += 1
                # Volume que é só um índice de capítulos: entra nos capítulos dele.
                if deeper and len(text) < 1500:
                    queue[:0] = [d for d in deeper if d not in subpages]
                    subpages += deeper
                else:
                    texts.append(text)
                if progress:
                    progress(done, done + len(queue))
                time.sleep(0.05)   # gentileza com os servidores da Wikimedia
        else:
            texts.append(main_text)
            if progress:
                progress(1, 1)
    full = "\n\n".join(t for t in texts if t.strip())
    return {"title": real_title.split("/")[0], "author": author or "", "text": full,
            "url": page_url(lang, real_title), "lang": lang}


def find_in_catalog(db: Session, url: str) -> Book | None:
    return db.scalar(select(Book).where(Book.source_url == url, Book.owner_id.is_(None)))


def import_work(db: Session, lang: str, title: str,
                progress: Callable[[int, int], None] | None = None) -> tuple[Book, bool]:
    """Importa uma obra para o acervo público. Devolve (livro, era_novo)."""
    existing = find_in_catalog(db, page_url(lang, title))
    if existing:
        return existing, False
    work = fetch_work(lang, title, progress)
    existing = find_in_catalog(db, work["url"])   # o título pode ter sido um redirecionamento
    if existing:
        return existing, False
    chunks = split_into_chunks(work["text"])
    if len(chunks) < MIN_CHUNKS:
        raise WikisourceError("too_short")
    try:
        book = store_book(db, gutenberg_id=next_local_id(db), chunks=chunks, title=work["title"],
                          author=work["author"], language=lang, source="wikisource", source_url=work["url"])
        return book, True
    except IntegrityError:
        # Índice único em source_url: outra importação da mesma obra terminou primeiro.
        db.rollback()
        existing = find_in_catalog(db, work["url"])
        if existing:
            return existing, False
        raise WikisourceError("save_failed") from None


# --------------------------------------------------------------------------- importações em segundo plano
_jobs: dict[str, dict] = {}
_jobs_lock = Lock()


def new_job(lang: str, title: str, user_id: int) -> tuple[dict, bool]:
    """Cria a importação. Se a mesma obra já está sendo importada, devolve essa (era_nova=False)."""
    url = page_url(lang, title)
    with _jobs_lock:
        old = [k for k, j in _jobs.items()
               if j["status"] in ("done", "error") and time.time() - j["created"] > 3600]
        for k in old:
            _jobs.pop(k, None)
        running = next((j for j in _jobs.values() if j["url"] == url and j["status"] in ("queued", "running")), None)
        if running:
            # Outra pessoa já pediu esta obra: acompanha a mesma importação (sem baixar duas vezes).
            running.setdefault("watchers", set()).add(user_id)
            return dict(running), False
        job = {"id": uuid.uuid4().hex, "lang": lang, "title": title, "url": url, "user_id": user_id,
               "watchers": {user_id}, "status": "queued", "done": 0, "total": 0, "book": None, "error": None,
               "created": time.time()}
        _jobs[job["id"]] = job
        return dict(job), True


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return {**job, "watchers": set(job.get("watchers", ()))} if job else None


def _update(job_id: str, **fields) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def run_job(job_id: str) -> None:
    """Executa a importação (BackgroundTasks). Nunca levanta exceção."""
    from ..database import SessionLocal

    job = get_job(job_id)
    if not job:
        return
    _update(job_id, status="running")
    try:
        with SessionLocal() as db:
            book, _ = import_work(db, job["lang"], job["title"],
                                  progress=lambda d, t: _update(job_id, done=d, total=t))
            _update(job_id, status="done", book={
                "id": book.id, "gutenberg_id": book.gutenberg_id, "title": book.title, "author": book.author,
                "language": book.language, "cover_url": book.cover_url, "excerpt_count": book.excerpt_count,
                "source": book.source, "source_url": book.source_url, "private": False,
            })
    except WikisourceError as exc:
        _update(job_id, status="error", error=exc.code)
    except Exception:  # noqa: BLE001
        log.exception("Falha ao importar %s da Wikisource", job["title"])
        _update(job_id, status="error", error="unavailable")
