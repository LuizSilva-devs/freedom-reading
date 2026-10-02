"""Configuração central da aplicação (lida do .env)."""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent          # backend/
PROJECT_DIR = BASE_DIR.parent                               # raiz do repositório


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    app_name: str = "Freadom Reading API"
    database_url: str = "postgresql+psycopg://freedom:freedom@localhost:5432/freedom"

    # Autenticação
    jwt_secret: str = "troque-esta-chave-no-.env"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # 7 dias
    # Limite de tentativas (por IP): login errado em 5 minutos e cadastros por hora
    login_attempts_per_5min: int = 10
    registrations_per_hour: int = 20
    # Pedidos de "esqueci minha senha" / reenvio de confirmação (por IP e por e-mail, por hora)
    email_requests_per_hour: int = 5

    # Links de confirmação e de redefinição de senha
    app_url: str = "http://localhost:8000"     # endereço público do site (usado nos links do e-mail)
    verify_token_hours: int = 48
    reset_token_minutes: int = 60

    # Envio de e-mail
    #   console -> não envia: imprime o e-mail no terminal do servidor (desenvolvimento/demonstração)
    #   smtp    -> qualquer servidor SMTP (Gmail com senha de app, Outlook, Mailtrap, SMTP do Amazon SES)
    #   ses     -> API do Amazon SES (usa o LabRole/credenciais da instância; precisa de boto3)
    email_provider: Literal["console", "smtp", "ses"] = "console"
    email_from: str = "Freadom Reading <no-reply@freedom.local>"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True

    # Matching (pg_trgm). word_similarity: 0..1 — quanto maior, mais exigente.
    similarity_threshold: float = 0.45
    min_excerpt_chars: int = 25
    max_excerpt_chars: int = 2000

    # Leitor
    reader_page_chars: int = 2500
    # Livro aberto no leitor que não está no acervo é indexado em segundo plano
    # e passa a ser reconhecido por trecho (até este tamanho de arquivo).
    auto_ingest_on_read: bool = True
    auto_ingest_max_mb: float = 6.0

    # Identificação fora do acervo (livros modernos/pagos): Google Books + Internet Archive.
    # Sem chave o Google Books funciona com uma cota diária menor; crie uma grátis no Google Cloud se precisar.
    google_books_api_key: str = ""
    external_archive: bool = True            # busca no texto do Internet Archive (mais lenta)
    external_archive_timeout: float = 25.0
    external_searches_per_hour: int = 30     # por IP

    # Outras fontes: importações da Wikisource (por usuário) e livros enviados pelos usuários
    wikisource_imports_per_hour: int = 10
    upload_max_mb: float = 20.0
    uploads_per_user: int = 50
    uploads_per_hour: int = 20

    # Tradução automática no leitor
    #   mymemory       -> gratuito, sem chave (limite diário; informe TRANSLATION_EMAIL para 50 mil caracteres/dia)
    #   libretranslate -> servidor próprio ou pago (LIBRETRANSLATE_URL / LIBRETRANSLATE_API_KEY)
    #   aws            -> Amazon Translate (usa o LabRole/credenciais da instância EC2)
    #   none           -> desliga a tradução
    translation_provider: Literal["mymemory", "libretranslate", "aws", "none"] = "mymemory"
    translation_email: str = ""
    libretranslate_url: str = "http://localhost:5000"
    libretranslate_api_key: str = ""
    aws_region: str = "us-east-1"
    translation_workers: int = 4

    # Cache em disco dos textos do Gutenberg baixados sob demanda
    cache_dir: Path = BASE_DIR / "data" / "cache"
    http_timeout: float = 15.0

    # Frontend servido pela própria API
    frontend_dir: Path = PROJECT_DIR / "frontend"
    cors_origins: list[str] = ["http://localhost:5500", "http://127.0.0.1:5500"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
