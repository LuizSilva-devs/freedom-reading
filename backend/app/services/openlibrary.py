"""Integração com a Open Library (catálogo, capas e detalhes).

Antes essas chamadas eram feitas direto pelo navegador. Agora passam pelo
backend: dá para cachear, padronizar o formato e cruzar com o acervo local.
"""
import logging

import httpx

from ..config import get_settings
from . import cache

SEARCH_URL = "https://openlibrary.org/search.json"
BASE_URL = "https://openlibrary.org"
FIELDS = "key,title,author_name,first_publish_year,cover_i,ebook_access,has_fulltext,subject,language"


log = logging.getLogger(__name__)


class UpstreamError(Exception):
    """A API externa não respondeu como esperado."""


def cover_url(cover_id: int | None) -> str | None:
    return f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else None


def _client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(timeout=s.http_timeout, headers={"User-Agent": "Freadom-Reading/1.0 (projeto academico)"})


def _get(client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    """GET com UMA nova tentativa: a Open Library às vezes demora ou responde 5xx
    na primeira chamada (servidor "frio") e funciona logo em seguida."""
    for attempt in (1, 2):
        try:
            resp = client.get(url, **kwargs)
            if resp.status_code >= 500 and attempt == 1:
                continue
            resp.raise_for_status()
            return resp
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt == 2:
                raise
            log.info("Open Library falhou (%s); tentando de novo", exc.__class__.__name__)
    raise AssertionError("inalcançável")


# Códigos de idioma da Open Library (MARC).
OL_LANG = {"pt": "por", "en": "eng"}


def search(query: str, limit: int = 20, language: str | None = None) -> list[dict]:
    query = query.strip()
    key = f"ol:search:{query.lower()}:{limit}:{language or ''}"
    cached = cache.get(key)
    if not cache.is_miss(cached):
        return cached
    try:
        with _client() as client:
            q = f"{query} language:{OL_LANG[language]}" if language in OL_LANG else query
            resp = _get(client, SEARCH_URL, params={"q": q, "limit": limit, "fields": FIELDS})
            docs = resp.json().get("docs", [])
    except (httpx.HTTPError, ValueError) as exc:
        raise UpstreamError(str(exc)) from exc

    results = []
    for doc in docs[:limit]:
        authors = doc.get("author_name") or []
        results.append({
            "book_key": doc.get("key"),
            "title": doc.get("title") or "?",
            "author": ", ".join(authors),
            "cover_url": cover_url(doc.get("cover_i")),
            "year": doc.get("first_publish_year"),
            "has_fulltext": bool(doc.get("has_fulltext")) and doc.get("ebook_access") in ("public", "borrowable"),
            "subjects": (doc.get("subject") or [])[:5],
            "source": "openlibrary",
        })
    cache.set(key, results)
    return results


def work_details(work_key: str) -> dict:
    key = f"ol:work:{work_key}"
    cached = cache.get(key)
    if not cache.is_miss(cached):
        return cached

    detail = {
        "book_key": work_key, "title": "?", "author": "",
        "cover_url": None, "year": None, "description": "", "subjects": [],
    }
    try:
        with _client() as client:
            resp = _get(client, f"{BASE_URL}{work_key}.json")
            data = resp.json()
            detail["title"] = data.get("title") or detail["title"]
            desc = data.get("description")
            if isinstance(desc, dict):
                desc = desc.get("value", "")
            detail["description"] = desc or ""
            detail["subjects"] = (data.get("subjects") or [])[:8]
            if data.get("covers"):
                detail["cover_url"] = cover_url(data["covers"][0])
            if data.get("first_publish_date"):
                detail["year"] = data["first_publish_date"]

            names = []
            for a in (data.get("authors") or [])[:3]:
                a_key = (a.get("author") or {}).get("key")
                if not a_key:
                    continue
                try:
                    ar = client.get(f"{BASE_URL}{a_key}.json")
                    if ar.is_success and ar.json().get("name"):
                        names.append(ar.json()["name"])
                except httpx.HTTPError:
                    pass
            if names:
                detail["author"] = ", ".join(names)
    except (httpx.HTTPError, ValueError) as exc:
        raise UpstreamError(str(exc)) from exc

    cache.set(key, detail, ttl=3600)
    return detail
