"""Regras comuns sobre os livros do acervo: quem pode ver cada livro e IDs locais."""
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from ..models import Book, User


def visible_to(user: User | None):
    """Condição SQL: livros públicos + os arquivos enviados pela própria pessoa."""
    if user is None:
        return Book.owner_id.is_(None)
    return or_(Book.owner_id.is_(None), Book.owner_id == user.id)


def get_visible(db: Session, gutenberg_id: int, user: User | None) -> Book | None:
    return db.scalar(select(Book).where(Book.gutenberg_id == gutenberg_id, visible_to(user)))


def hidden_from(db: Session, gutenberg_id: int, user: User | None) -> bool:
    """True quando o livro existe mas é um arquivo particular de outra pessoa."""
    book = db.scalar(select(Book).where(Book.gutenberg_id == gutenberg_id))
    return book is not None and book.owner_id is not None and (user is None or book.owner_id != user.id)


def next_local_id(db: Session) -> int:
    """Próximo ID para um livro que não vem do Gutenberg (Wikisource, arquivo enviado).

    Vem de uma sequência do PostgreSQL: dois envios ao mesmo tempo nunca recebem o mesmo
    número, e o ID de um livro apagado não volta a ser usado.
    """
    return int(db.scalar(text("SELECT nextval('local_book_id_seq')")))
