"""Livros enviados pelos usuários (.txt, .epub, .pdf).

O arquivo vira texto, é dividido em trechos e fica PRIVADO: só quem enviou consegue
ler no leitor e ter trechos dele identificados. O arquivo original não é guardado,
só o texto extraído (que é apagado junto com o livro ou com a conta).
"""
import io
import posixpath
import re
import zipfile
from xml.etree.ElementTree import Element, ParseError  # nosec B405 — só tipos; o parse usa defusedxml

from defusedxml.ElementTree import fromstring as safe_fromstring  # EPUB enviado por usuário: XML não confiável

from .text_utils import parse_gutenberg_header, strip_gutenberg_boilerplate
from .wikisource import html_to_text

MAX_EPUB_UNCOMPRESSED = 150 * 1024 * 1024   # proteção contra "zip bomb"
MAX_TEXT_CHARS = 30_000_000                 # ~ 15 mil páginas: nenhum livro de verdade passa disso
EXTENSIONS = (".txt", ".epub", ".pdf")


class UploadError(Exception):
    """`code` vira mensagem traduzida: unsupported | empty | scanned_pdf | bad_file | too_big | too_many."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_PT = {"que", "não", "nao", "uma", "com", "para", "os", "as", "do", "da", "ele", "ela", "mas", "se", "lhe"}
_EN = {"the", "and", "of", "to", "was", "he", "she", "that", "it", "with", "his", "her", "you", "but", "had"}


def guess_language(text: str) -> str:
    words = re.findall(r"[a-záéíóúãõâêôç]+", text[:20000].lower())
    pt = sum(w in _PT for w in words)
    en = sum(w in _EN for w in words)
    return "en" if en > pt else "pt"


def _txt(data: bytes) -> tuple[str, dict]:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            raw = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    meta = {}
    if "PROJECT GUTENBERG" in raw[:20000].upper():
        h = parse_gutenberg_header(raw)
        meta = {"title": h["title"], "author": h["author"], "language": h["language"]}
        raw = strip_gutenberg_boilerplate(raw)
    return raw, meta


def _xml(z: zipfile.ZipFile, name: str) -> Element:
    return safe_fromstring(z.read(name))


def _epub(data: bytes) -> tuple[str, dict]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UploadError("bad_file") from None
    if sum(i.file_size for i in z.infolist()) > MAX_EPUB_UNCOMPRESSED:
        raise UploadError("too_big")
    try:
        container = _xml(z, "META-INF/container.xml")
        rootfile = next(el for el in container.iter() if el.tag.endswith("rootfile")).get("full-path")
        opf = _xml(z, rootfile)
    except (KeyError, StopIteration, ParseError, ValueError):
        raise UploadError("bad_file") from None

    def first(tag: str) -> str:
        el = next((e for e in opf.iter() if e.tag.endswith("}" + tag) or e.tag == tag), None)
        return (el.text or "").strip() if el is not None else ""

    base = posixpath.dirname(rootfile)
    manifest = {e.get("id"): e.get("href") for e in opf.iter() if e.tag.endswith("item") and e.get("href")}
    # Sem repetições: um EPUB malicioso pode listar o mesmo capítulo milhares de vezes.
    spine = list(dict.fromkeys(e.get("idref") for e in opf.iter() if e.tag.endswith("itemref")))
    parts, total = [], 0
    for idref in spine:
        href = manifest.get(idref)
        if not href:
            continue
        path = posixpath.normpath(posixpath.join(base, href.split("#")[0]))
        try:
            html = z.read(path).decode("utf-8", errors="replace")
        except KeyError:
            continue
        text, _, _ = html_to_text(html)
        if text.strip():
            parts.append(text)
            total += len(text)
            if total > MAX_TEXT_CHARS:
                raise UploadError("too_big")
    lang = first("language")[:2].lower()
    return "\n\n".join(parts), {"title": first("title"), "author": first("creator"),
                                 "language": lang if lang in ("pt", "en") else ""}


def _pdf(data: bytes) -> tuple[str, dict]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        pages = [(p.extract_text() or "") for p in reader.pages]
        info = reader.metadata or {}
    except (PdfReadError, ValueError, KeyError, TypeError):
        raise UploadError("bad_file") from None
    except Exception:  # noqa: BLE001 — PDFs malformados levantam erros variados no pypdf
        raise UploadError("bad_file") from None

    cleaned = []
    for page in pages:
        page = re.sub(r"(\w)-\n(\w)", r"\1\2", page)       # junta palavras hifenizadas na quebra de linha
        page = re.sub(r"(?m)^\s*\d{1,4}\s*$", "", page)     # número de página sozinho na linha
        # Linha que termina frase vira fim de parágrafo; as demais são quebras do layout.
        page = re.sub(r"([.!?:»\"”])\s*\n", r"\1\n\n", page)
        cleaned.append(page)
    text = "\n\n".join(cleaned)
    if len(re.sub(r"\W", "", text)) < 500:
        raise UploadError("scanned_pdf")   # PDF de imagens (escaneado): não tem texto para extrair
    return text, {"title": str(info.get("/Title") or "").strip(), "author": str(info.get("/Author") or "").strip()}


def extract(filename: str, data: bytes) -> tuple[str, dict]:
    """Texto do arquivo + metadados que o próprio arquivo trouxer (título, autor, idioma)."""
    name = (filename or "").lower()
    if name.endswith(".txt"):
        text, meta = _txt(data)
    elif name.endswith(".epub"):
        text, meta = _epub(data)
    elif name.endswith(".pdf"):
        text, meta = _pdf(data)
    else:
        raise UploadError("unsupported")
    if len(text) > MAX_TEXT_CHARS:
        raise UploadError("too_big")
    if len(re.sub(r"\W", "", text)) < 200:
        raise UploadError("empty")
    meta.setdefault("language", "")
    meta["language"] = meta.get("language") or guess_language(text)
    return text, meta
