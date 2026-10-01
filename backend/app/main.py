"""Freadom Reading — API FastAPI que também serve o frontend.

Rodar (a partir de backend/):  uvicorn app.main:app --reload
Documentação interativa:        http://localhost:8000/docs
"""
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .config import get_settings
from .database import engine, init_db
from .routers import annotations, auth, catalog, identify, me
from .schemas import ErrorOut

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    log = logging.getLogger("uvicorn.error")
    if settings.jwt_secret.startswith("troque"):
        # A chave de exemplo é pública (está no repositório): quem a conhecesse poderia forjar logins.
        # Sem uma chave própria no .env, usamos uma aleatória — os logins expiram quando o servidor reinicia.
        settings.jwt_secret = secrets.token_urlsafe(48)
        log.warning("JWT_SECRET não configurado: usando uma chave temporária. Defina JWT_SECRET no .env "
                    "(gere com: python -c \"import secrets; print(secrets.token_urlsafe(48))\").")
    elif len(settings.jwt_secret) < 32:
        log.warning("JWT_SECRET curto (menos de 32 caracteres). Use uma chave mais longa.")
    if settings.email_provider != "console" and "localhost" in settings.app_url:
        log.warning("APP_URL aponta para localhost: os links dos e-mails não vão funcionar fora desta máquina. "
                    "Defina APP_URL com o endereço público (ex.: http://SEU-IP-DO-EC2:8000).")
    init_db()
    yield


app = FastAPI(title=settings.app_name, version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Códigos de erro documentados no /docs (o fuzzing apontou respostas não documentadas).
def _errors(*codes: int) -> dict:
    names = {400: "Dados inválidos", 401: "Não autenticado", 404: "Não encontrado", 409: "Conflito",
             429: "Muitas tentativas", 502: "Serviço externo indisponível"}
    return {c: {"model": ErrorOut, "description": names[c]} for c in codes}


app.include_router(auth.router, responses=_errors(400, 401, 409, 429))
app.include_router(identify.router, responses=_errors(400))
app.include_router(catalog.router, responses=_errors(400, 404, 502))
app.include_router(me.router, responses=_errors(400, 401))
app.include_router(annotations.router, responses=_errors(400, 401, 404))


@app.get("/api/health", tags=["infra"])
def health():
    """Usado pelo health check do EC2/ALB e pelo frontend para saber se a API está no ar."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}


# O frontend é montado por último para não "engolir" as rotas /api.
if settings.frontend_dir.exists():
    app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
