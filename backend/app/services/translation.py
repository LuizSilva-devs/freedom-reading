"""Tradução automática das páginas do leitor.

- Cada página é traduzida UMA vez e salva em `page_translations`; as próximas
  leituras (de qualquer usuário) vêm do banco, sem custo e sem espera.
- A página é traduzida por parágrafo (assim as quebras de linha são preservadas)
  e parágrafos longos são divididos em frases, respeitando o limite do provedor.
- O provedor é escolhido no .env (TRANSLATION_PROVIDER). Para testes, basta
  trocar `translate_chunk` por uma função falsa.
"""
import hashlib
import html
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import PageTranslation


class TranslationError(Exception):
    pass


# Limite de caracteres por requisição de cada provedor (com folga).
_LIMITS = {"mymemory": 450, "libretranslate": 1800, "aws": 4500, "none": 10**9}

# Códigos de idioma que cada provedor espera.
_CODES = {
    "mymemory": {"pt": "pt-BR", "en": "en-GB"},
    "libretranslate": {"pt": "pt", "en": "en"},
    "aws": {"pt": "pt", "en": "en"},
}


def _code(provider: str, lang: str) -> str:
    return _CODES.get(provider, {}).get(lang, lang)


# ------------------------------------------------------------------ provedores
def _mymemory(text: str, source: str, target: str) -> str:
    s = get_settings()
    params = {"q": text, "langpair": f"{_code('mymemory', source)}|{_code('mymemory', target)}"}
    if s.translation_email:
        params["de"] = s.translation_email
    try:
        resp = httpx.get("https://api.mymemory.translated.net/get", params=params, timeout=s.http_timeout)
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise TranslationError(str(exc)) from exc
    out = (data.get("responseData") or {}).get("translatedText") or ""
    status = str(data.get("responseStatus"))
    if status != "200" or not out or out.upper().startswith("MYMEMORY WARNING"):
        raise TranslationError(f"MyMemory: {data.get('responseDetails') or out or status}")
    # O MyMemory devolve aspas e apóstrofos como entidades HTML (&#39; &quot;).
    return html.unescape(out)


def _libretranslate(text: str, source: str, target: str) -> str:
    s = get_settings()
    body = {"q": text, "source": _code("libretranslate", source), "target": _code("libretranslate", target), "format": "text"}
    if s.libretranslate_api_key:
        body["api_key"] = s.libretranslate_api_key
    try:
        resp = httpx.post(f"{s.libretranslate_url.rstrip('/')}/translate", json=body, timeout=s.http_timeout)
        resp.raise_for_status()
        return resp.json()["translatedText"]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise TranslationError(f"LibreTranslate: {exc}") from exc


_aws_client = None


def _aws(text: str, source: str, target: str) -> str:
    global _aws_client
    try:
        import boto3  # opcional: só necessário com TRANSLATION_PROVIDER=aws
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:
        raise TranslationError("Instale boto3 para usar o Amazon Translate.") from exc
    if _aws_client is None:
        _aws_client = boto3.client("translate", region_name=get_settings().aws_region)
    try:
        out = _aws_client.translate_text(Text=text, SourceLanguageCode=_code("aws", source),
                                         TargetLanguageCode=_code("aws", target))
        return out["TranslatedText"]
    except (BotoCoreError, ClientError) as exc:
        raise TranslationError(f"Amazon Translate: {exc}") from exc


def translate_chunk(text: str, source: str, target: str) -> str:
    """Traduz um pedaço curto de texto com o provedor configurado."""
    provider = get_settings().translation_provider
    if provider == "none":
        raise TranslationError("Tradução desativada (TRANSLATION_PROVIDER=none).")
    return {"mymemory": _mymemory, "libretranslate": _libretranslate, "aws": _aws}[provider](text, source, target)


# ------------------------------------------------------------------ página
def _split_for_limit(paragraph: str, limit: int) -> list[str]:
    if len(paragraph) <= limit:
        return [paragraph]
    parts, buf = [], ""
    for sentence in re.split(r"(?<=[.!?;:])\s+", paragraph):
        while len(sentence) > limit:  # frase gigante sem pontuação: corta por palavras
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(sentence[:cut])
            sentence = sentence[cut:].lstrip()
        if buf and len(buf) + len(sentence) + 1 > limit:
            parts.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
    if buf:
        parts.append(buf)
    return parts


def translate_text(text: str, source: str, target: str) -> str:
    """Traduz uma página inteira mantendo os parágrafos."""
    s = get_settings()
    limit = _LIMITS.get(s.translation_provider, 450)
    paragraphs = text.split("\n\n")
    pieces: list[tuple[int, str]] = []
    for i, p in enumerate(paragraphs):
        for part in _split_for_limit(p.strip(), limit):
            if part:
                pieces.append((i, part))

    def work(item: tuple[int, str]) -> str:
        _, part = item
        # Trechos sem letras (ex.: "* * *", números de capítulo) não precisam de tradução.
        if not re.search(r"[^\W\d_]{2,}", part):
            return part
        return translate_chunk(part, source, target)

    with ThreadPoolExecutor(max_workers=max(1, s.translation_workers)) as pool:
        translated = list(pool.map(work, pieces))

    out: list[list[str]] = [[] for _ in paragraphs]
    for (i, _), t in zip(pieces, translated, strict=True):
        out[i].append(t.strip())
    return "\n\n".join(" ".join(parts) for parts in out)


# ------------------------------------------------------------------ cache
_locks: dict[tuple, Lock] = {}
_locks_guard = Lock()


def _lock_for(key: tuple) -> Lock:
    with _locks_guard:
        if len(_locks) > 500:
            _locks.clear()
        return _locks.setdefault(key, Lock())


def get_translated_page(db: Session, gutenberg_id: int, page: int, content: str, source: str, target: str) -> str:
    s = get_settings()
    source_hash = hashlib.sha1(content.encode("utf-8"), usedforsecurity=False).hexdigest()  # só detecta mudança no texto
    key = (gutenberg_id, s.reader_page_chars, page, target)

    def cached() -> PageTranslation | None:
        return db.scalar(select(PageTranslation).where(
            PageTranslation.gutenberg_id == gutenberg_id, PageTranslation.page_chars == s.reader_page_chars,
            PageTranslation.page == page, PageTranslation.target_lang == target,
        ))

    row = cached()
    if row and row.source_hash == source_hash:
        return row.content

    # Evita traduzir a mesma página duas vezes em paralelo (ex.: leitura + pré-carregamento).
    with _lock_for(key):
        db.expire_all()
        row = cached()
        if row and row.source_hash == source_hash:
            return row.content
        row_id = row.id if row else None
        # A tradução leva segundos: devolve a conexão ao pool enquanto espera o provedor.
        db.rollback()
        translated = translate_text(content, source, target)
        row = db.get(PageTranslation, row_id) if row_id else None
        if row:
            row.content, row.source_hash, row.provider = translated, source_hash, s.translation_provider
        else:
            db.add(PageTranslation(gutenberg_id=gutenberg_id, page_chars=s.reader_page_chars, page=page,
                                   target_lang=target, source_hash=source_hash, content=translated,
                                   provider=s.translation_provider))
        try:
            db.commit()
        except IntegrityError:  # outro processo salvou primeiro
            db.rollback()
        return translated
