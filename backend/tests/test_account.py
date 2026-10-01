"""Conta: confirmação de e-mail, esqueci/troca de senha, sair de todos, exclusão."""
import re
import uuid
from concurrent.futures import ThreadPoolExecutor

from app import ratelimit
from app.config import get_settings
from app.services import email

from .conftest import ALICE_ID


def _register(client, password="segredo123", lang="pt"):
    addr = f"acc-{uuid.uuid4().hex[:8]}@exemplo.com"
    r = client.post("/api/auth/register", json={"name": "Conta", "email": addr, "password": password},
                    headers={"Accept-Language": lang})
    assert r.status_code == 201, r.text
    return addr, {"Authorization": f"Bearer {r.json()['access_token']}"}, r.json()


def _last_link(to: str, kind: str) -> str:
    msgs = [m for m in email.outbox if m.to == to]
    assert msgs, "nenhum e-mail enviado"
    m = re.search(rf"#/{kind}\?token=([\w-]+)", msgs[-1].text)
    assert m, msgs[-1].text
    return m.group(1)


# ------------------------------------------------------------ confirmação de e-mail
def test_cadastro_envia_confirmacao_e_link_confirma(client):
    addr, headers, body = _register(client)
    assert body["user"]["email_verified"] is False
    token = _last_link(addr, "confirmar-email")
    r = client.post("/api/auth/verify-email", json={"token": token})
    assert r.status_code == 200 and r.json()["email_verified"] is True
    assert client.get("/api/auth/me", headers=headers).json()["email_verified"] is True
    # o mesmo link não funciona duas vezes
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 400


def test_email_no_idioma_da_pessoa(client):
    addr, _, _ = _register(client, lang="en")
    assert [m for m in email.outbox if m.to == addr][-1].subject.startswith("Confirm your email")


def test_reenviar_confirmacao_invalida_link_antigo(client):
    addr, headers, _ = _register(client)
    old = _last_link(addr, "confirmar-email")
    assert client.post("/api/auth/resend-verification", headers=headers).status_code == 200
    new = _last_link(addr, "confirmar-email")
    assert old != new
    assert client.post("/api/auth/verify-email", json={"token": old}).status_code == 400
    assert client.post("/api/auth/verify-email", json={"token": new}).status_code == 200
    r = client.post("/api/auth/resend-verification", headers=headers)
    assert "já está confirmado" in r.json()["message"]


def test_token_inventado_e_recusado(client):
    assert client.post("/api/auth/verify-email", json={"token": "x" * 43}).status_code == 400


# ------------------------------------------------------------ esqueci minha senha
def test_esqueci_senha_fluxo_completo(client):
    addr, old_headers, _ = _register(client)
    r = client.post("/api/auth/forgot-password", json={"email": addr.upper()})
    assert r.status_code == 200
    token = _last_link(addr, "redefinir-senha")
    r = client.post("/api/auth/reset-password", json={"token": token, "new_password": "novasenha1"})
    assert r.status_code == 200
    new_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert r.json()["user"]["email_verified"] is True
    # senha antiga não entra; a nova entra
    assert client.post("/api/auth/login", json={"email": addr, "password": "segredo123"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": addr, "password": "novasenha1"}).status_code == 200
    # sessões antigas caem; a nova funciona; o link não pode ser reutilizado
    assert client.get("/api/me/books", headers=old_headers).status_code == 401
    assert client.get("/api/me/books", headers=new_headers).status_code == 200
    assert client.post("/api/auth/reset-password", json={"token": token, "new_password": "outra123"}).status_code == 400


def test_esqueci_senha_nao_revela_se_email_existe(client):
    before = len(email.outbox)
    r = client.post("/api/auth/forgot-password", json={"email": f"ninguem-{uuid.uuid4().hex[:6]}@exemplo.com"})
    assert r.status_code == 200 and "Se existir uma conta" in r.json()["message"]
    assert len(email.outbox) == before  # nada enviado


def test_link_de_redefinicao_expirado(client, monkeypatch):
    addr, _, _ = _register(client)
    monkeypatch.setattr(get_settings(), "reset_token_minutes", -1)
    client.post("/api/auth/forgot-password", json={"email": addr})
    token = _last_link(addr, "redefinir-senha")
    assert client.post("/api/auth/reset-password", json={"token": token, "new_password": "novasenha1"}).status_code == 400


def test_link_de_redefinicao_usado_em_paralelo_so_vale_uma_vez(client):
    addr, _, _ = _register(client)
    client.post("/api/auth/forgot-password", json={"email": addr})
    token = _last_link(addr, "redefinir-senha")
    with ThreadPoolExecutor(5) as pool:
        codes = list(pool.map(lambda i: client.post("/api/auth/reset-password",
                                                    json={"token": token, "new_password": f"senha-{i}-ok"}).status_code, range(5)))
    assert sorted(codes) == [200, 400, 400, 400, 400]


def test_limite_por_email_no_esqueci_senha(client):
    addr, _, _ = _register(client)
    ratelimit.clear()
    before = len([m for m in email.outbox if m.to == addr])
    for _ in range(6):
        assert client.post("/api/auth/forgot-password", json={"email": addr}).status_code == 200
    sent = len([m for m in email.outbox if m.to == addr]) - before
    assert sent == 3  # não dá para lotar a caixa de alguém
    ratelimit.clear()


# ------------------------------------------------------------ trocar senha
def test_trocar_senha(client):
    addr, headers, _ = _register(client)
    bad = client.post("/api/auth/change-password", json={"current_password": "errada", "new_password": "novasenha1"}, headers=headers)
    assert bad.status_code == 400
    same = client.post("/api/auth/change-password", json={"current_password": "segredo123", "new_password": "segredo123"}, headers=headers)
    assert same.status_code == 400
    r = client.post("/api/auth/change-password", json={"current_password": "segredo123", "new_password": "novasenha1"}, headers=headers)
    assert r.status_code == 200
    assert client.get("/api/me/books", headers=headers).status_code == 401  # token antigo caiu
    assert client.get("/api/me/books", headers={"Authorization": f"Bearer {r.json()['access_token']}"}).status_code == 200
    assert client.post("/api/auth/login", json={"email": addr, "password": "novasenha1"}).status_code == 200


def test_trocar_senha_tem_limite_de_tentativas(client, monkeypatch):
    _, headers, _ = _register(client)
    monkeypatch.setattr(get_settings(), "login_attempts_per_5min", 2)
    ratelimit.clear()
    for _ in range(2):
        client.post("/api/auth/change-password", json={"current_password": "x", "new_password": "novasenha1"}, headers=headers)
    r = client.post("/api/auth/change-password", json={"current_password": "segredo123", "new_password": "novasenha1"}, headers=headers)
    ratelimit.clear()
    assert r.status_code == 429


# ------------------------------------------------------------ sair de todos
def test_sair_de_todos_os_outros_dispositivos(client):
    addr, device_a, _ = _register(client)
    tok_b = client.post("/api/auth/login", json={"email": addr, "password": "segredo123"}).json()["access_token"]
    device_b = {"Authorization": f"Bearer {tok_b}"}
    r = client.post("/api/auth/logout-all", headers=device_b)
    assert r.status_code == 200
    assert client.get("/api/me/books", headers=device_a).status_code == 401
    assert client.get("/api/me/books", headers=device_b).status_code == 401
    assert client.get("/api/me/books", headers={"Authorization": f"Bearer {r.json()['access_token']}"}).status_code == 200


# ------------------------------------------------------------ excluir conta
def test_excluir_conta_apaga_tudo(client):
    from sqlalchemy import func, select

    from app.database import SessionLocal
    from app.models import AuthToken, Bookmark, Highlight, User, UserBook

    addr, headers, body = _register(client)
    uid = body["user"]["id"]
    ref = {"book_key": f"gutenberg:{ALICE_ID}", "title": "Alice", "gutenberg_id": ALICE_ID}
    client.put("/api/me/favorites", json=ref, headers=headers)
    client.put("/api/me/bookmarks", json={**ref, "page": 0}, headers=headers)
    client.post("/api/me/highlights", json={**ref, "page": 0, "start": 0, "end": 5, "text": "Alice"}, headers=headers)

    assert client.post("/api/auth/delete-account", json={"password": "errada"}, headers=headers).status_code == 400
    assert client.post("/api/auth/delete-account", json={"password": "segredo123"}, headers=headers).status_code == 200

    with SessionLocal() as db:
        assert db.get(User, uid) is None
        for model in (UserBook, Bookmark, Highlight, AuthToken):
            assert db.scalar(select(func.count()).select_from(model).where(model.user_id == uid)) == 0
    assert client.get("/api/me/books", headers=headers).status_code == 401
    assert client.post("/api/auth/login", json={"email": addr, "password": "segredo123"}).status_code == 401
    # o e-mail fica livre para uma conta nova
    assert client.post("/api/auth/register", json={"name": "De novo", "email": addr, "password": "segredo123"}).status_code == 201


def test_token_de_antes_da_atualizacao_continua_valendo(client):
    """Tokens emitidos antes desta versão não têm "ver": valem como versão 0."""
    import jwt
    _, _, body = _register(client)
    s = get_settings()
    legacy = jwt.encode({"sub": str(body["user"]["id"]), "exp": 9999999999}, s.jwt_secret, algorithm=s.jwt_algorithm)
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {legacy}"}).status_code == 200


def test_trocar_senha_anula_link_de_redefinicao_pendente(client):
    addr, headers, _ = _register(client)
    client.post("/api/auth/forgot-password", json={"email": addr})
    token = _last_link(addr, "redefinir-senha")
    client.post("/api/auth/change-password", json={"current_password": "segredo123", "new_password": "novasenha1"}, headers=headers)
    assert client.post("/api/auth/reset-password", json={"token": token, "new_password": "invasor123"}).status_code == 400


def test_smtp_verifica_certificado(monkeypatch):
    """O STARTTLS precisa receber um contexto que confere o certificado."""
    import ssl

    calls = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self, context=None): calls["ctx"] = context
        def login(self, *a): pass
        def send_message(self, m): calls["sent"] = m["To"]

    s = get_settings()
    monkeypatch.setattr(s, "email_provider", "smtp")
    monkeypatch.setattr(s, "smtp_port", 587)
    monkeypatch.setattr(email.smtplib, "SMTP", FakeSMTP)
    email.send(email.build("verify", "pt", "a@exemplo.com", "A", "http://x/#/confirmar-email?token=abc"))
    assert calls["sent"] == "a@exemplo.com"
    assert isinstance(calls["ctx"], ssl.SSLContext) and calls["ctx"].verify_mode == ssl.CERT_REQUIRED


def test_falha_no_envio_de_email_nao_derruba_o_cadastro(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "email_provider", "smtp")
    monkeypatch.setattr(s, "smtp_host", "127.0.0.1")
    monkeypatch.setattr(s, "smtp_port", 1)  # nada escutando: o envio falha
    addr = f"smtpfail-{uuid.uuid4().hex[:6]}@exemplo.com"
    r = client.post("/api/auth/register", json={"name": "X", "email": addr, "password": "segredo123"})
    assert r.status_code == 201
