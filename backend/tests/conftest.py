"""Configuração dos testes.

Os testes usam um PostgreSQL real (pg_trgm não existe no SQLite).
Defina TEST_DATABASE_URL apontando para um banco DESCARTÁVEL — ele é limpo a cada execução.
    docker compose up -d db
    TEST_DATABASE_URL=postgresql+psycopg://freedom:freedom@localhost:5432/freedom_test pytest
"""
import os
from pathlib import Path

import pytest

TEST_DB = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://freedom:freedom@localhost:5432/freedom_test"
)
os.environ["DATABASE_URL"] = TEST_DB
os.environ.setdefault("JWT_SECRET", "chave-de-teste-com-pelo-menos-32-bytes-ok")
# Limites altos nos testes (cada teste cria usuários); o limitador tem teste próprio.
os.environ.setdefault("REGISTRATIONS_PER_HOUR", "100000")
os.environ.setdefault("LOGIN_ATTEMPTS_PER_5MIN", "100000")
os.environ.setdefault("EMAIL_REQUESTS_PER_HOUR", "100000")
os.environ["EMAIL_PROVIDER"] = "console"  # nunca envia e-mail de verdade nos testes

FIXTURES = Path(__file__).parent / "fixtures"
ALICE_ID, CASMURRO_ID = 900011, 955752


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.database import Base, engine, init_db
    from app.main import app
    from scripts.ingest_gutenberg import ingest

    Base.metadata.drop_all(engine)
    init_db()
    ingest(ALICE_ID, (FIXTURES / "alice_sample.txt").read_text(encoding="utf-8"))
    ingest(CASMURRO_ID, (FIXTURES / "dom_casmurro_sample.txt").read_text(encoding="utf-8"))

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_headers(client):
    import uuid

    email = f"leitor-{uuid.uuid4().hex[:8]}@exemplo.com"
    r = client.post("/api/auth/register", json={"name": "Leitor", "email": email, "password": "segredo123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
