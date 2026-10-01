"""Calibração do SIMILARITY_THRESHOLD com dados do próprio acervo.

Sorteia trechos reais, "estraga" como um usuário faria (tira acentos, erros de
digitação, corta pedaços) e mede, para vários thresholds, quantas vezes o livro
certo ficou em 1º lugar e quantas vezes a busca voltou vazia.

Uso (a partir de backend/):  python -m scripts.test_matching --samples 60
"""
import argparse
import random
import sys
import unicodedata
from pathlib import Path

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal
from app.models import Excerpt
from app.services.matching import identify


def degrade(text: str, rng: random.Random) -> str:
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    words = text.split()
    if len(words) > 30:  # trecho parcial, como alguém que copiou só uma parte
        start = rng.randint(0, len(words) - 25)
        words = words[start:start + rng.randint(15, 25)]
    for _ in range(max(1, len(words) // 12)):  # ~1 erro de digitação a cada 12 palavras
        i = rng.randrange(len(words))
        w = words[i]
        if len(w) > 3:
            j = rng.randrange(1, len(w) - 1)
            words[i] = w[:j] + w[j + 1:]
    return " ".join(words)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=40)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--thresholds", default="0.3,0.35,0.4,0.45,0.5,0.55,0.6")
    args = ap.parse_args()
    rng = random.Random(args.seed)

    with SessionLocal() as db:
        rows = db.execute(
            select(Excerpt.book_id, Excerpt.content)
            .where(func.length(Excerpt.content) > 150)
            .order_by(func.random()).limit(args.samples)
        ).all()
        if not rows:
            print("Acervo vazio — rode scripts.ingest_gutenberg primeiro.")
            return
        samples = [(book_id, degrade(content, rng)) for book_id, content in rows]

        print(f"{len(samples)} trechos degradados\n")
        print(f"{'threshold':>9} | {'acerto top-1':>12} | {'sem resultado':>13} | {'livro errado':>12}")
        print("-" * 56)
        for t in (float(x) for x in args.thresholds.split(",")):
            hit = empty = wrong = 0
            for book_id, query in samples:
                ranked, _ = identify(db, query, threshold=t, top=1)
                if not ranked:
                    empty += 1
                elif ranked[0].book_id == book_id:
                    hit += 1
                else:
                    wrong += 1
            n = len(samples)
            print(f"{t:>9.2f} | {hit / n:>11.0%} | {empty / n:>12.0%} | {wrong / n:>11.0%}")
    print("\nEscolha o maior threshold que mantém o acerto alto e 'livro errado' perto de zero.")


if __name__ == "__main__":
    main()
