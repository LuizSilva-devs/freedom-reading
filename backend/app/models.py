from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Acervo de identificação (livros ingeridos do Project Gutenberg)
# --------------------------------------------------------------------------
class Book(Base):
    __tablename__ = "books"

    id: Mapped[int] = mapped_column(primary_key=True)
    gutenberg_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    author: Mapped[str] = mapped_column(String(300))
    language: Mapped[str] = mapped_column(String(10), default="pt")
    cover_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    excerpt_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    excerpts: Mapped[list["Excerpt"]] = relationship(
        back_populates="book", cascade="all, delete-orphan", order_by="Excerpt.position"
    )


class Excerpt(Base):
    __tablename__ = "excerpts"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("books.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)  # ordem do trecho no livro
    content: Mapped[str] = mapped_column(Text)
    content_normalized: Mapped[str] = mapped_column(Text)  # indexado com GIN/pg_trgm

    book: Mapped[Book] = relationship(back_populates="excerpts")

    __table_args__ = (UniqueConstraint("book_id", "position", name="uq_excerpt_book_pos"),)


# --------------------------------------------------------------------------
# Usuários e dados pessoais (antes ficavam no localStorage)
# --------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(255))
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Vai no token de login. Aumentar este número desloga todos os aparelhos
    # ("sair de todos os dispositivos", troca e redefinição de senha).
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class AuthToken(Base):
    """Token de uso único enviado por e-mail (confirmação de e-mail ou redefinição de senha).

    Só o hash SHA-256 é guardado: quem tiver acesso ao banco não consegue usar os links.
    """
    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(20))            # "verify" | "reset"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserBook(Base):
    """Um livro na vida do usuário: favorito e/ou status na biblioteca.

    `book_key` é a chave da Open Library (ex.: /works/OL123W) ou `gutenberg:<id>`.
    """
    __tablename__ = "user_books"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    book_key: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(300))
    author: Mapped[str] = mapped_column(String(300), default="")
    cover_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    gutenberg_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    is_favorite: Mapped[bool] = mapped_column(default=False)
    favorited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)  # quero_ler|lendo|concluido
    added_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    page: Mapped[int] = mapped_column(Integer, default=0)
    total_pages: Mapped[int] = mapped_column(Integer, default=0)
    percent: Mapped[float] = mapped_column(Float, default=0.0)
    last_read: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "book_key", name="uq_user_book"),)


class Bookmark(Base):
    """Marcador de página (um por página de cada livro)."""
    __tablename__ = "bookmarks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    book_key: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(300))
    author: Mapped[str] = mapped_column(String(300), default="")
    gutenberg_id: Mapped[int] = mapped_column(Integer)
    page: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("user_id", "book_key", "page", name="uq_bookmark_page"),)


class Highlight(Base):
    """Trecho sublinhado. `start`/`end` são posições de caractere dentro da página.

    `version` diz em qual texto o sublinhado foi feito: "original" ou o código do
    idioma da tradução (ex.: "pt") — a posição só vale para aquele texto.
    """
    __tablename__ = "highlights"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    book_key: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(300))
    author: Mapped[str] = mapped_column(String(300), default="")
    gutenberg_id: Mapped[int] = mapped_column(Integer)
    page: Mapped[int] = mapped_column(Integer)
    version: Mapped[str] = mapped_column(String(10), default="original")
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    color: Mapped[str] = mapped_column(String(10), default="yellow")
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PageTranslation(Base):
    """Cache de páginas traduzidas: cada página é traduzida uma vez e reaproveitada por todos."""
    __tablename__ = "page_translations"

    id: Mapped[int] = mapped_column(primary_key=True)
    gutenberg_id: Mapped[int] = mapped_column(Integer)
    page_chars: Mapped[int] = mapped_column(Integer)
    page: Mapped[int] = mapped_column(Integer)
    target_lang: Mapped[str] = mapped_column(String(10))
    source_hash: Mapped[str] = mapped_column(String(40))
    content: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("gutenberg_id", "page_chars", "page", "target_lang", name="uq_page_translation"),
    )


class SearchLog(Base):
    """Registro das identificações — útil para calibrar o threshold com dados reais."""
    __tablename__ = "search_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    query_chars: Mapped[int] = mapped_column(Integer)
    top_book_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    top_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
