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
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_excerpts_content_norm_trgm "
            "ON excerpts USING gin (content_normalized gin_trgm_ops)"
        ))
