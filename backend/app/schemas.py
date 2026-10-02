from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

LibraryStatus = Literal["quero_ler", "lendo", "concluido"]

# Limites que cabem nas colunas INTEGER do PostgreSQL (antes, números enormes davam erro 500).
MAX_GUTENBERG_ID = 9_999_999   # Gutenberg (< 1 milhão) + livros de outras fontes (a partir de 5 milhões)
MAX_PAGE = 100_000
MAX_OFFSET = 1_000_000


class CleanModel(BaseModel):
    """Modelo de entrada que recusa o caractere nulo (\\0) em textos:
    o PostgreSQL não aceita esse caractere e a requisição terminava em erro 500."""

    @field_validator("*", mode="before")
    @classmethod
    def no_nul(cls, v):
        if isinstance(v, str) and "\x00" in v:
            raise ValueError("texto com caractere inválido")
        return v


class ErrorOut(BaseModel):
    """Formato dos erros da API: {"detail": "mensagem já traduzida"}."""
    detail: str


# ---------------- Auth ----------------
class RegisterIn(CleanModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Informe um nome.")
        return v


class LoginIn(CleanModel):
    email: EmailStr
    password: str = Field(max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str
    email_verified: bool = False
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class ProfileIn(CleanModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Informe um nome.")
        return v


PasswordStr = Field(min_length=6, max_length=128)


class ForgotPasswordIn(CleanModel):
    email: EmailStr


class ResetPasswordIn(CleanModel):
    token: str = Field(min_length=10, max_length=200)
    new_password: str = PasswordStr


class VerifyEmailIn(CleanModel):
    token: str = Field(min_length=10, max_length=200)


class ChangePasswordIn(CleanModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = PasswordStr


class DeleteAccountIn(CleanModel):
    password: str = Field(min_length=1, max_length=128)


class MessageOut(BaseModel):
    message: str


# ---------------- Identificação ----------------
class IdentifyIn(CleanModel):
    excerpt: str = Field(max_length=5000)


class ExternalLink(BaseModel):
    kind: Literal["buy", "borrow", "read", "info"]
    label: str
    url: str


class ExternalBook(BaseModel):
    title: str
    author: str = ""
    year: str | None = None
    language: str = ""
    cover_url: str | None = None
    snippet: str = ""
    source: Literal["google_books", "internet_archive"]
    public_domain: bool = False
    links: list[ExternalLink] = []
    catalog_gutenberg_id: int | None = None   # o mesmo livro já está no acervo: dá para ler aqui


class ExternalOut(BaseModel):
    phrase: str
    results: list[ExternalBook]
    failed: list[str] = []


class BookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    gutenberg_id: int
    title: str
    author: str
    language: str
    cover_url: str | None = None
    excerpt_count: int
    source: str = "gutenberg"
    source_url: str | None = None
    private: bool = False


class WikisourceResult(BaseModel):
    title: str
    lang: str
    url: str
    snippet: str = ""
    is_chapter: bool = False
    gutenberg_id: int | None = None   # preenchido quando a obra já está no acervo


class WikisourceImportIn(CleanModel):
    lang: Literal["pt", "en"] = "pt"
    title: str = Field(min_length=1, max_length=300)

    @field_validator("title")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("título vazio")
        return v


class WikisourceJob(BaseModel):
    id: str
    lang: str
    title: str
    status: Literal["queued", "running", "done", "error"]
    done: int = 0
    total: int = 0
    book: BookOut | None = None
    error: str | None = None


class MatchOut(BaseModel):
    book: BookOut
    score: float
    confidence: str
    excerpt: str
    excerpt_position: int
    page: int


class IdentifyOut(BaseModel):
    matches: list[MatchOut]
    threshold: float
    catalog_size: int
    message: str


# ---------------- Catálogo ----------------
class SearchResult(BaseModel):
    book_key: str
    title: str
    author: str
    cover_url: str | None = None
    year: int | str | None = None
    has_fulltext: bool = False
    subjects: list[str] = []
    source: str = "openlibrary"
    gutenberg_id: int | None = None
    in_catalog: bool = False


class FreeVersion(BaseModel):
    gutenberg_id: int
    title: str | None = None
    authors: list[str] = []
    languages: list[str] = []


class BookDetail(BaseModel):
    book_key: str
    title: str
    author: str
    cover_url: str | None = None
    year: int | str | None = None
    description: str = ""
    subjects: list[str] = []
    free_version: FreeVersion | None = None
    in_catalog: bool = False
    source: str = "openlibrary"          # gutenberg | wikisource | upload | openlibrary
    source_url: str | None = None
    private: bool = False


class ReaderPage(BaseModel):
    gutenberg_id: int
    page: int
    total_pages: int
    content: str
    original_language: str = ""      # idioma do livro ("pt", "en", ...)
    language: str = ""               # idioma do texto devolvido
    translated: bool = False
    translation_error: str | None = None


# ---------------- Dados do usuário ----------------
class BookRef(CleanModel):
    book_key: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    author: str = Field(default="", max_length=300)
    cover_url: str | None = Field(default=None, max_length=500)
    gutenberg_id: int | None = Field(default=None, ge=1, le=MAX_GUTENBERG_ID)


class LibraryIn(BookRef):
    status: LibraryStatus


class ProgressIn(BookRef):
    page: int = Field(ge=0, le=MAX_PAGE)
    total_pages: int = Field(ge=1, le=MAX_PAGE)


class UserBookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    book_key: str
    title: str
    author: str
    cover_url: str | None
    gutenberg_id: int | None
    is_favorite: bool
    status: str | None
    page: int
    total_pages: int
    percent: float
    favorited_at: datetime | None
    added_at: datetime | None
    last_read: datetime | None


class SettingsIO(CleanModel):
    language: Literal["pt", "en"] = "pt"
    translate_books: bool = True
    appearance: Literal["auto", "claro", "escuro"] = "auto"
    theme: Literal["literatura", "biologia", "terror", "romance", "fantasia", "ficcao", "historia", "misterio"] = "literatura"
    font_size: int = Field(default=19, ge=12, le=32)
    spacing: float = Field(default=1.7, ge=1.0, le=2.5)
    text_width: Literal["estreito", "medio", "largo"] = "medio"


class StatsOut(BaseModel):
    favorites: int
    library: int
    reading: int
    completed: int
    identifications: int
    bookmarks: int = 0
    highlights: int = 0


# ---------------- Marcadores e sublinhados ----------------
HighlightColor = Literal["yellow", "green", "pink", "blue"]


class AnnotationRef(CleanModel):
    book_key: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    author: str = Field(default="", max_length=300)
    gutenberg_id: int = Field(ge=1, le=MAX_GUTENBERG_ID)
    page: int = Field(ge=0, le=MAX_PAGE)


class BookmarkIn(AnnotationRef):
    note: str = Field(default="", max_length=200)


class BookmarkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    book_key: str
    title: str
    author: str
    gutenberg_id: int
    page: int
    note: str
    created_at: datetime


class HighlightIn(AnnotationRef):
    version: str = Field(default="original", pattern=r"^(original|[a-z]{2})$")
    start: int = Field(ge=0, le=MAX_OFFSET)
    end: int = Field(ge=1, le=MAX_OFFSET)
    text: str = Field(min_length=1, max_length=3000)
    color: HighlightColor = "yellow"
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def check_range(self):
        if self.end <= self.start:
            raise ValueError("end deve ser maior que start")
        return self


class HighlightPatch(CleanModel):
    color: HighlightColor | None = None
    note: str | None = Field(default=None, max_length=500)


class HighlightOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    book_key: str
    title: str
    author: str
    gutenberg_id: int
    page: int
    version: str
    start: int
    end: int
    text: str
    color: str
    note: str
    created_at: datetime


class AnnotationsOut(BaseModel):
    bookmarks: list[BookmarkOut]
    highlights: list[HighlightOut]


class ImportIn(CleanModel):
    """Dados do modo visitante (localStorage) enviados no primeiro login."""
    favorites: list[BookRef] = []
    library: list[LibraryIn] = []
    progress: list[ProgressIn] = []
    settings: SettingsIO | None = None
    bookmarks: list[BookmarkIn] = Field(default=[], max_length=2000)
    highlights: list[HighlightIn] = Field(default=[], max_length=2000)
