"""Freadom Reading — API FastAPI que também serve o frontend.

Rodar (a partir de backend/):  uvicorn app.main:app --reload
Documentação interativa:        http://localhost:8000/docs
"""
import json
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .config import get_settings
from .database import engine, init_db
from .routers import annotations, auth, catalog, identify, me, sources
from .i18n import msg
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

class UploadSizeLimit:
    """Recusa envios de arquivo grandes ANTES de o corpo ser lido.

    O FastAPI lê o formulário inteiro (e grava em disco temporário) antes de checar
    o login, então sem isto qualquer pessoa poderia mandar gigabytes para /api/me/uploads.
    Confere o Content-Length e também conta os bytes de envios sem ele (chunked).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/me/uploads") or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        limit = int(settings.upload_max_mb * 1024 * 1024) + 64 * 1024   # + margem dos campos do formulário
        headers = dict(scope.get("headers") or [])
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = 0
        if declared > limit:
            return await self._too_big(scope, send)

        received = 0
        rejected = False

        async def limited_receive():
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Responde 413 agora e diz ao app que o cliente "desconectou": a leitura para aqui.
                    rejected = True
                    await self._too_big(scope, send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            if not rejected:   # depois do 413, ignora a resposta que o app tentar mandar
                await send(message)

        await self.app(scope, limited_receive, guarded_send)

    async def _too_big(self, scope, send):
        from .i18n import get_lang as _lang_of

        lang = _lang_of(Request(scope))
        body = json.dumps({"detail": msg("upload_too_big", lang, n=settings.upload_max_mb)}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(UploadSizeLimit)


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    """Sem isso, o navegador continua usando o app.js/styles.css antigos depois de uma
    atualização (módulos ES ficam em cache mesmo com Ctrl+F5). "no-cache" não desliga o
    cache: só obriga a perguntar ao servidor, que responde 304 (sem reenviar) se nada mudou."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/") and request.url.path != "/docs":
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


# Códigos de erro documentados no /docs (o fuzzing apontou respostas não documentadas).
def _errors(*codes: int) -> dict:
    names = {400: "Dados inválidos", 401: "Não autenticado", 404: "Não encontrado", 409: "Conflito",
             429: "Muitas tentativas", 502: "Serviço externo indisponível"}
    return {c: {"model": ErrorOut, "description": names[c]} for c in codes}


app.include_router(auth.router, responses=_errors(400, 401, 409, 429))
app.include_router(identify.router, responses=_errors(400, 429))
app.include_router(catalog.router, responses=_errors(400, 404, 502))
app.include_router(me.router, responses=_errors(400, 401))
app.include_router(annotations.router, responses=_errors(400, 401, 404))
app.include_router(sources.router, responses=_errors(400, 401, 404, 409, 429, 502))


@app.get("/api/health", tags=["infra"])
def health():
    """Usado pelo health check do EC2/ALB e pelo frontend para saber se a API está no ar."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}


# O frontend é montado por último para não "engolir" as rotas /api.
if settings.frontend_dir.exists():
    app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
