from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

settings = get_settings()

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Cria a extensão pg_trgm, as tabelas e o índice GIN (idempotente)."""
    from . import models  # noqa: F401  (necessário: registra as tabelas no metadata)

    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # create_all não altera tabelas que já existem: colunas adicionadas depois entram aqui.
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified boolean NOT NULL DEFAULT false"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version integer NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE books ADD COLUMN IF NOT EXISTS source varchar(20) NOT NULL DEFAULT 'gutenberg'"))
        conn.execute(text("ALTER TABLE books ADD COLUMN IF NOT EXISTS source_url varchar(500)"))
        conn.execute(text(
            "ALTER TABLE books ADD COLUMN IF NOT EXISTS owner_id integer REFERENCES users(id) ON DELETE CASCADE"
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_books_owner_id ON books (owner_id)"))
        # A mesma obra da Wikisource entra uma vez só no acervo público.
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_books_public_source_url ON books (source_url) "
            "WHERE owner_id IS NULL AND source_url IS NOT NULL"
        ))
        # IDs dos livros que não vêm do Gutenberg (Wikisource, arquivos enviados). Uma sequência
        # garante que um ID apagado nunca é reaproveitado por outro livro.
        conn.execute(text("CREATE SEQUENCE IF NOT EXISTS local_book_id_seq MINVALUE 1 START 5000000"))
        conn.execute(text(
            "SELECT setval('local_book_id_seq', GREATEST("
            "(SELECT COALESCE(MAX(gutenberg_id), 4999999) FROM books WHERE gutenberg_id >= 5000000),"
            "(SELECT CASE WHEN is_called THEN last_value ELSE last_value - 1 END FROM local_book_id_seq)))"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_excerpts_content_norm_trgm "
            "ON excerpts USING gin (content_normalized gin_trgm_ops)"
        ))
