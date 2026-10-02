"""Funções puras de texto: normalização, limpeza do Gutenberg, divisão e paginação.

São usadas tanto pela ingestão (scripts/ingest_gutenberg.py) quanto pela API,
garantindo que o texto indexado e o texto normalizado da busca sigam a MESMA regra.
"""
import re
import unicodedata

_START_RE = re.compile(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG E-?BOOK.*?\*\*\*", re.I | re.S)
_END_RE = re.compile(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG E-?BOOK", re.I)
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.U)

STOPWORDS = {
    "a", "o", "os", "as", "de", "da", "do", "das", "dos", "um", "uma", "e", "que", "em",
    "para", "com", "por", "no", "na", "nos", "nas", "se", "ao", "the", "an", "of", "in",
    "on", "for", "with", "to", "and", "is", "it", "was",
}


def normalize_text(text: str | None) -> str:
    """Minúsculas, sem acentos, sem pontuação e com espaços colapsados.

    Remover acentos e pontuação torna o matching tolerante a quem digita
    "nao" em vez de "não" ou esquece vírgulas.
    """
    if not text:
        return ""
    stripped = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    stripped = stripped.replace("_", " ")
    stripped = _PUNCT_RE.sub(" ", stripped)
    return _WS_RE.sub(" ", stripped).strip().lower()


def strip_gutenberg_boilerplate(raw: str) -> str:
    """Remove o cabeçalho e a licença do Project Gutenberg."""
    raw = raw.replace("\r\n", "\n").lstrip("﻿")
    start = _START_RE.search(raw)
    end = _END_RE.search(raw)
    start_idx = start.end() if start else 0
    end_idx = end.start() if end else len(raw)
    return raw[start_idx:end_idx].strip()


def split_into_chunks(body: str, min_chars: int = 180, max_chars: int = 1200) -> list[str]:
    """Divide o livro em trechos do tamanho de um parágrafo.

    - Parágrafos curtos (diálogos, títulos) são agrupados até `min_chars`,
      senão a busca retornaria trechos de 3 palavras com score alto.
    - Parágrafos gigantes são quebrados em frases até `max_chars`.
    - Quebras de linha internas (texto "hard-wrapped" do Gutenberg) viram espaço.
    """
    paragraphs = [_WS_RE.sub(" ", p).strip() for p in re.split(r"\n\s*\n", body)]
    paragraphs = [p for p in paragraphs if p]

    pieces: list[str] = []
    for p in paragraphs:
        if len(p) <= max_chars:
            pieces.append(p)
            continue
        sentences = []
        for sent in re.split(r"(?<=[.!?;])\s+", p):
            # Texto sem pontuação (PDF ruim, lista) viraria um trecho enorme: corta por palavras.
            while len(sent) > max_chars * 2:
                cut = sent.rfind(" ", 0, max_chars)
                cut = cut if cut > 0 else max_chars
                sentences.append(sent[:cut])
                sent = sent[cut:].lstrip()
            sentences.append(sent)
        buf = ""
        for s in sentences:
            if buf and len(buf) + len(s) + 1 > max_chars:
                pieces.append(buf)
                buf = s
            else:
                buf = f"{buf} {s}".strip()
        if buf:
            pieces.append(buf)

    chunks: list[str] = []
    buf = ""
    for p in pieces:
        buf = f"{buf}\n\n{p}" if buf else p
        if len(buf) >= min_chars:
            chunks.append(buf)
            buf = ""
    if buf:
        if chunks and len(buf) < min_chars:
            chunks[-1] = f"{chunks[-1]}\n\n{buf}"
        else:
            chunks.append(buf)
    return chunks


_DASH_RE = re.compile(r"(?<!-)-{2,3}(?!-)")
_DIALOGUE_RE = re.compile(r"(?:(?<=\n)|^)—(?=\w)", re.M)
_ITALIC_RE = re.compile(r"(?<![\w_])_([^_\n]+?)_(?![\w_])")
_HEADING_RE = re.compile(r"^(?:[IVXLCDM]+|\d+|(?:CAP[IÍ]TULO|CHAPTER|LIVRO|BOOK|PARTE|PART)\b.*)\.?$", re.I)


def _is_caps_line(p: str) -> bool:
    """Linha curta sem letras minúsculas: folha de rosto ("DOM CASMURRO", "POR") ou número de capítulo."""
    return len(p) <= 70 and any(c.isalpha() for c in p) and not any(c.islower() for c in p)


def tidy_text(text: str) -> str:
    """Ajusta o texto do Gutenberg para leitura (não muda a busca, que usa content_normalized).

    - "--" (travessão em ASCII) vira "—";
    - _itálico_ perde os sublinhados de marcação;
    - linhas curtas em maiúsculas seguidas (folha de rosto) e o título do capítulo
      ("I" + "Do titulo.") ficam no mesmo bloco, separados por uma quebra simples,
      em vez de um parágrafo cada.
    """
    text = _DASH_RE.sub("—", text)
    text = _DIALOGUE_RE.sub("— ", text)   # "—Continue" (início de fala) -> "— Continue"
    text = _ITALIC_RE.sub(r"\1", text)
    out: list[str] = []
    for p in text.split("\n\n"):
        prev = out[-1].rsplit("\n", 1)[-1] if out else ""
        if out and _is_caps_line(prev) and len(p) <= 80 and not _HEADING_RE.match(p.strip()):
            out[-1] = f"{out[-1]}\n{p}"
        else:
            out.append(p)
    return "\n\n".join(out)


def paginate_chunks(chunks: list[str], page_chars: int = 2500) -> tuple[list[str], list[int]]:
    """Agrupa trechos em páginas.

    Retorna (páginas, chunk_to_page), onde chunk_to_page[i] é a página que
    contém o trecho i — é isso que permite o botão "abrir no trecho".
    """
    pages: list[str] = []
    mapping: list[int] = []
    current = ""
    for chunk in chunks:
        chunk = tidy_text(chunk)
        if current and len(current) + len(chunk) + 2 > page_chars:
            pages.append(current)
            current = chunk
        else:
            current = f"{current}\n\n{chunk}" if current else chunk
        mapping.append(len(pages))
    if current:
        pages.append(current)
    if not pages:
        pages = ["(Não foi possível extrair o conteúdo textual deste livro.)"]
    return pages, mapping


_LANG_MAP = {"portuguese": "pt", "english": "en", "spanish": "es", "french": "fr",
             "german": "de", "italian": "it", "latin": "la"}


def parse_gutenberg_header(raw: str) -> dict:
    """Lê Title/Author/Language do cabeçalho do arquivo do Gutenberg (funciona offline)."""
    head = raw[:6000]

    def field(name: str) -> str | None:
        m = re.search(rf"^{name}:\s*(.+(?:\n[ \t]+.+)*)", head, re.M | re.I)
        return re.sub(r"\s+", " ", m.group(1)).strip() if m else None

    lang = (field("Language") or "").lower().split(",")[0].strip()
    return {
        "title": field("Title"),
        "author": field("Author"),
        "language": _LANG_MAP.get(lang, lang[:2] if lang else ""),
    }


def keywords(text: str, limit: int = 6) -> str:
    words = [w for w in normalize_text(text).split() if len(w) > 2 and w not in STOPWORDS]
    return " ".join(words[:limit])


def word_overlap(a: str, b: str) -> float:
    """Proporção das palavras de `a` que aparecem em `b` (usada para casar títulos)."""
    aw = set(normalize_text(a).split())
    bw = set(normalize_text(b).split())
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / len(aw)
