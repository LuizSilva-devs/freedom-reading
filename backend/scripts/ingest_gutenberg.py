"""Ingestão de livros do Project Gutenberg no acervo de identificação.

Uso (a partir de backend/):
    python -m scripts.ingest_gutenberg                  # coleção completa (~70 clássicos em PT e EN)
    python -m scripts.ingest_gutenberg --basico         # só os 4 livros iniciais (rápido, para testar)
    python -m scripts.ingest_gutenberg --lista          # mostra a coleção sem baixar nada
    python -m scripts.ingest_gutenberg 55752 1342       # IDs específicos
    python -m scripts.ingest_gutenberg --file livro.txt --id 99999 --title "X" --author "Y"
    python -m scripts.ingest_gutenberg --replace 55752  # reindexa um livro já ingerido
    python -m scripts.ingest_gutenberg --wikisource "Senhora"   # importa uma obra da Wikisource

Fluxo: baixa o .txt -> remove o cabeçalho/licença do Gutenberg -> divide em
trechos do tamanho de parágrafos -> grava Book + Excerpt (texto original e normalizado).

Livros que já estão no acervo são pulados SEM baixar de novo, então dá para rodar
o comando várias vezes (por exemplo, se a internet cair no meio).

Além desta coleção, o acervo cresce sozinho: todo livro do Gutenberg aberto no
leitor é indexado em segundo plano (AUTO_INGEST_ON_READ no .env).
"""
import argparse
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, init_db
from app.models import Book
from app.services.gutenberg import GUTENDEX_URL, GutenbergError, _client, download_raw
from app.services import wikisource
from app.services.ingest import ingest_raw
from app.services.text_utils import normalize_text, word_overlap

BASIC_BOOKS = [
    55752,  # Dom Casmurro — Machado de Assis
    54829,  # Memórias Póstumas de Brás Cubas — Machado de Assis
    11,     # Alice's Adventures in Wonderland — Lewis Carroll
    1342,   # Pride and Prejudice — Jane Austen
]

# Clássicos em português. Os IDs do Gutenberg são descobertos pelo título/autor no
# Gutendex na hora da ingestão (assim nenhum ID fica errado na lista). Os que não
# existirem no Gutenberg são procurados na Wikisource.
PT_BOOKS = [
    ("Quincas Borba", "Machado de Assis"),
    ("Esaú e Jacó", "Machado de Assis"),
    ("Memorial de Aires", "Machado de Assis"),
    ("Helena", "Machado de Assis"),
    ("Iaiá Garcia", "Machado de Assis"),
    ("A Mão e a Luva", "Machado de Assis"),
    ("Ressurreição", "Machado de Assis"),
    ("Papéis Avulsos", "Machado de Assis"),
    ("Várias Histórias", "Machado de Assis"),
    ("Histórias sem Data", "Machado de Assis"),
    ("Iracema", "José de Alencar"),
    ("O Guarani", "José de Alencar"),
    ("Senhora", "José de Alencar"),
    ("Lucíola", "José de Alencar"),
    ("Diva", "José de Alencar"),
    ("Ubirajara", "José de Alencar"),
    ("Cinco Minutos", "José de Alencar"),
    ("O Cortiço", "Aluísio Azevedo"),
    ("O Mulato", "Aluísio Azevedo"),
    ("Casa de Pensão", "Aluísio Azevedo"),
    ("Memórias de um Sargento de Milícias", "Manuel Antônio de Almeida"),
    ("O Ateneu", "Raul Pompeia"),
    ("A Escrava Isaura", "Bernardo Guimarães"),
    ("A Moreninha", "Joaquim Manuel de Macedo"),
    ("Inocência", "Visconde de Taunay"),
    ("Triste Fim de Policarpo Quaresma", "Lima Barreto"),
    ("Os Sertões", "Euclides da Cunha"),
    ("Os Maias", "Eça de Queirós"),
    ("O Primo Basílio", "Eça de Queirós"),
    ("O Crime do Padre Amaro", "Eça de Queirós"),
    ("A Cidade e as Serras", "Eça de Queirós"),
    ("A Relíquia", "Eça de Queirós"),
    ("O Mandarim", "Eça de Queirós"),
    ("Amor de Perdição", "Camilo Castelo Branco"),
    ("As Pupilas do Senhor Reitor", "Júlio Dinis"),
    ("Viagens na Minha Terra", "Almeida Garrett"),
    ("Os Lusíadas", "Luís de Camões"),
]

# Clássicos em inglês (IDs fixos e conhecidos do Gutenberg).
EN_BOOKS = [
    84,     # Frankenstein — Mary Shelley
    345,    # Dracula — Bram Stoker
    1661,   # The Adventures of Sherlock Holmes — Arthur Conan Doyle
    2701,   # Moby Dick — Herman Melville
    98,     # A Tale of Two Cities — Charles Dickens
    1400,   # Great Expectations — Charles Dickens
    46,     # A Christmas Carol — Charles Dickens
    1260,   # Jane Eyre — Charlotte Brontë
    768,    # Wuthering Heights — Emily Brontë
    158,    # Emma — Jane Austen
    161,    # Sense and Sensibility — Jane Austen
    174,    # The Picture of Dorian Gray — Oscar Wilde
    74,     # The Adventures of Tom Sawyer — Mark Twain
    76,     # Adventures of Huckleberry Finn — Mark Twain
    120,    # Treasure Island — Robert Louis Stevenson
    43,     # The Strange Case of Dr. Jekyll and Mr. Hyde — Robert Louis Stevenson
    35,     # The Time Machine — H. G. Wells
    36,     # The War of the Worlds — H. G. Wells
    16,     # Peter Pan — J. M. Barrie
    55,     # The Wonderful Wizard of Oz — L. Frank Baum
    219,    # Heart of Darkness — Joseph Conrad
    2814,   # Dubliners — James Joyce
    1952,   # The Yellow Wallpaper — Charlotte Perkins Gilman
    25344,  # The Scarlet Letter — Nathaniel Hawthorne
    64317,  # The Great Gatsby — F. Scott Fitzgerald
    5200,   # Metamorphosis — Franz Kafka
    2554,   # Crime and Punishment — Fiódor Dostoiévski
    1184,   # The Count of Monte Cristo — Alexandre Dumas
    1232,   # The Prince — Nicolau Maquiavel
    205,    # Walden — Henry David Thoreau
]


def ingest(gutenberg_id: int, raw: str, title: str | None = None, author: str | None = None,
           replace: bool = False) -> None:
    """Indexa um texto já baixado (também usado pelos testes)."""
    with SessionLocal() as db:
        r = ingest_raw(db, gutenberg_id, raw, title, author, replace)
    if r.status == "exists":
        print(f"  = {gutenberg_id} ({r.title}) já está no acervo. Use --replace para reindexar.")
    elif r.status == "empty":
        print(f"  ! {gutenberg_id}: nenhum trecho extraído, pulando.")
    else:
        print(f"  + {gutenberg_id}: {r.title} — {r.author} ({r.chunks} trechos)")


def resolve_pt(title: str, author: str) -> int | None:
    """Acha o ID do Gutenberg de um livro em português pelo título e autor (Gutendex).

    Tenta "título + sobrenome", depois só o título e por fim só o autor (a busca do
    Gutendex é por palavras, e acentos/grafia antiga às vezes atrapalham uma delas).
    """
    surname = author.split()[-1]
    for query in (f"{title} {surname}", title, surname):
        try:
            with _client() as client:
                resp = client.get(GUTENDEX_URL, params={"search": query, "languages": "pt"})
                resp.raise_for_status()
                results = resp.json().get("results", [])
        except (httpx.HTTPError, ValueError) as exc:
            raise GutenbergError(f"Gutendex indisponível: {exc}") from exc
        gid = pick_match(results, title, author)
        if gid:
            return gid
    return None


def pick_match(results: list[dict], title: str, author: str) -> int | None:
    """Escolhe o resultado com título e autor mais parecidos (exige os dois)."""
    best, best_score = None, 0.0
    for book in results:
        names = " ".join(a.get("name", "") for a in book.get("authors", []))
        t_score = word_overlap(title, book.get("title", "").split(";")[0])
        a_score = word_overlap(author, names)
        # "Helena" não pode casar com "Helena e outros contos" de outro autor.
        if t_score < 0.6 or a_score < 0.3:
            continue
        # Entre edições, prefere o título mais curto (o livro, não uma coletânea que o contém).
        score = t_score + 0.5 * a_score - 0.01 * len(normalize_text(book.get("title", "")).split())
        if score > best_score:
            best, best_score = book, score
    return best["id"] if best else None


def already_in_catalog(gutenberg_id: int) -> str | None:
    with SessionLocal() as db:
        return db.scalar(select(Book.title).where(Book.gutenberg_id == gutenberg_id))


def from_wikisource(title: str, author: str, lang: str = "pt") -> str:
    """Procura a obra na Wikisource e importa. Devolve "added", "exists" ou "missing"."""
    try:
        results = [r for r in wikisource.search(title, lang) if not r["is_chapter"]]
        best = next((r for r in results if word_overlap(title, r["title"]) >= 0.8
                     and word_overlap(r["title"], title) >= 0.6), None)
        if not best:
            return "missing"
        with SessionLocal() as db:
            book, new = wikisource.import_work(
                db, lang, best["title"],
                progress=lambda d, t: print(f"\r         capítulos: {d}/{t}", end="", flush=True))
        print()
        print(f"         {'+' if new else '='} {book.title} — {book.author} ({book.excerpt_count} trechos, Wikisource)")
        return "added" if new else "exists"
    except Exception as exc:  # noqa: BLE001 — Wikisource fora do ar não para a coleção
        print(f"\n         ! Wikisource: {getattr(exc, 'code', exc)}")
        return "missing"


def run_collection(ids: list[int], pt_titles: list[tuple[str, str]], replace: bool,
                   use_wikisource: bool = True) -> int:
    total = len(ids) + len(pt_titles)
    added = skipped = 0
    failures: list[str] = []
    n = 0

    def one(gid: int, label: str) -> None:
        nonlocal added, skipped
        existing = already_in_catalog(gid)
        if existing and not replace:
            skipped += 1
            print(f"[{n}/{total}] = {existing} (já está no acervo)")
            return
        print(f"[{n}/{total}] baixando {label}...", flush=True)
        with SessionLocal() as db:
            r = ingest_raw(db, gid, download_raw(gid), replace=replace)
        if r.status in ("added", "replaced"):
            added += 1
            print(f"         + {r.title} — {r.author} ({r.chunks} trechos)")
        else:
            failures.append(f"{label}: sem texto aproveitável")

    for gid in ids:
        n += 1
        try:
            one(gid, f"#{gid}")
        except GutenbergError as exc:
            failures.append(f"#{gid}: {exc}")
            print(f"         ! {exc}")

    for title, author in pt_titles:
        n += 1
        try:
            gid = resolve_pt(title, author)
            if gid is None:
                # Não está no Gutenberg: tenta a Wikisource, que é bem mais completa em português.
                if use_wikisource:
                    print(f"[{n}/{total}] {title}: não está no Gutenberg, procurando na Wikisource...", flush=True)
                    result = from_wikisource(title, author)
                    if result == "added":
                        added += 1
                        continue
                    if result == "exists":
                        skipped += 1
                        continue
                failures.append(f"{title} ({author}): não encontrado no Gutenberg nem na Wikisource")
                print(f"[{n}/{total}] - {title} ({author}): não encontrado, pulando")
                continue
            one(gid, f"{title} (#{gid})")
            time.sleep(0.3)  # gentileza com o Gutendex, que é um serviço gratuito
        except GutenbergError as exc:
            failures.append(f"{title}: {exc}")
            print(f"         ! {exc}")

    print(f"\nPronto: {added} adicionado(s), {skipped} já estavam no acervo, {len(failures)} não entraram.")
    for f in failures:
        print(f"  - {f}")
    if failures:
        print("Rodar o comando de novo tenta só os que faltaram.")
    with SessionLocal() as db:
        print(f"Acervo agora: {len(db.scalars(select(Book.id)).all())} livros.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*", type=int, help="IDs do Project Gutenberg")
    parser.add_argument("--basico", action="store_true", help="Só os 4 livros iniciais")
    parser.add_argument("--lista", action="store_true", help="Mostra a coleção e sai")
    parser.add_argument("--file", type=Path, help="Ingerir um .txt local em vez de baixar")
    parser.add_argument("--id", type=int, help="ID a usar com --file")
    parser.add_argument("--title")
    parser.add_argument("--author")
    parser.add_argument("--replace", action="store_true", help="Reindexa livros já existentes")
    parser.add_argument("--sem-wikisource", action="store_true",
                        help="Não procurar na Wikisource os livros em português que faltam no Gutenberg")
    parser.add_argument("--wikisource", metavar="TITULO", action="append",
                        help='Importa uma obra da Wikisource pelo título (ex.: --wikisource "Dom Casmurro")')
    parser.add_argument("--wikisource-lang", default="pt", choices=["pt", "en"])
    args = parser.parse_args()

    if args.lista:
        print(f"Básicos ({len(BASIC_BOOKS)}): " + ", ".join(map(str, BASIC_BOOKS)))
        print(f"\nPortuguês ({len(PT_BOOKS)}, ID descoberto no Gutendex):")
        for title, author in PT_BOOKS:
            print(f"  {title} — {author}")
        print(f"\nInglês ({len(EN_BOOKS)}): " + ", ".join(map(str, EN_BOOKS)))
        return 0

    init_db()

    if args.file:
        if not args.id:
            parser.error("--file exige --id")
        ingest(args.id, args.file.read_text(encoding="utf-8"), args.title, args.author, args.replace)
        return 0

    if args.wikisource:
        for title in args.wikisource:
            print(f"Wikisource: {title}")
            if from_wikisource(title, "", args.wikisource_lang) == "missing":
                print("  - não encontrado (confira o título exato na Wikisource)")
        return 0
    if args.ids:
        return run_collection(args.ids, [], args.replace)
    if args.basico:
        return run_collection(BASIC_BOOKS, [], args.replace)
    return run_collection(BASIC_BOOKS + EN_BOOKS, PT_BOOKS, args.replace, use_wikisource=not args.sem_wikisource)


if __name__ == "__main__":
    raise SystemExit(main())
