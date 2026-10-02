from app.services import openlibrary
from app.services.text_utils import (
    normalize_text, paginate_chunks, split_into_chunks, strip_gutenberg_boilerplate,
)

from .conftest import ALICE_ID, CASMURRO_ID


# ------------------------------------------------------------ funções puras
def test_normalize_remove_acentos_e_pontuacao():
    assert normalize_text("  Não, SENHOR!  Coração ") == "nao senhor coracao"


def test_strip_boilerplate_e_chunks():
    raw = "Header\n*** START OF THE PROJECT GUTENBERG EBOOK X ***\n" + ("Parágrafo longo. " * 20 + "\n\n") * 5 \
          + "*** END OF THE PROJECT GUTENBERG EBOOK X ***\nLicença"
    body = strip_gutenberg_boilerplate(raw)
    assert "Header" not in body and "Licença" not in body
    chunks = split_into_chunks(body)
    assert len(chunks) == 5
    pages, mapping = paginate_chunks(chunks, page_chars=700)
    assert len(mapping) == len(chunks) and mapping[-1] == len(pages) - 1


def test_dialogos_curtos_sao_agrupados():
    chunks = split_into_chunks("--Oi.\n\n--Tudo bem?\n\n--Sim.\n\n" + "Texto " * 50)
    assert all(len(c) >= 30 for c in chunks)


# ------------------------------------------------------------ identificação
def test_catalogo_lista_livros_ingeridos(client):
    books = client.get("/api/books").json()
    assert {b["gutenberg_id"] for b in books} >= {ALICE_ID, CASMURRO_ID}
    assert all(b["excerpt_count"] > 0 for b in books)


def test_identifica_trecho_exato(client):
    r = client.post("/api/identify", json={"excerpt": "a White Rabbit with pink eyes ran close by her"})
    data = r.json()
    assert r.status_code == 200
    assert data["matches"][0]["book"]["gutenberg_id"] == ALICE_ID
    assert data["matches"][0]["confidence"] == "alta"


def test_identifica_sem_acentos_e_com_erros(client):
    # sem acento, sem pontuação, com dois erros de digitação
    trecho = "vindo da cidade para o engenho novo encontrei no trem da centrl um rapaz aqui do bairo"
    data = client.post("/api/identify", json={"excerpt": trecho}).json()
    assert data["matches"][0]["book"]["gutenberg_id"] == CASMURRO_ID
    assert data["matches"][0]["score"] >= 0.6


def test_trecho_que_atravessa_paragrafos(client):
    trecho = ("fechei os olhos tres ou quatro vezes tanto bastou para que ele interrompesse a leitura "
              "e metesse os versos no bolso continue disse eu acordando ja acabei murmurou ele "
              "sao muito bonitos vi-lhe fazer um gesto para tira-los outra vez do bolso mas nao passou "
              "do gesto estava amuado no dia seguinte entrou a dizer de mim nomes feios")
    data = client.post("/api/identify", json={"excerpt": trecho}).json()
    assert data["matches"][0]["book"]["gutenberg_id"] == CASMURRO_ID


def test_trecho_inexistente_nao_retorna_nada(client):
    data = client.post("/api/identify", json={
        "excerpt": "quantum chromodynamics lattice gauge renormalization group flow parameters"}).json()
    assert data["matches"] == []


def test_trecho_curto_demais(client):
    assert client.post("/api/identify", json={"excerpt": "Alice"}).status_code == 400


def test_resultado_aponta_pagina_do_leitor(client):
    data = client.post("/api/identify", json={"excerpt": "Down, down, down. Would the fall never come to an end?"}).json()
    m = data["matches"][0]
    page = client.get(f"/api/reader/{ALICE_ID}", params={"page": m["page"]}).json()
    assert "Would the fall never come to an end" in page["content"]


# ------------------------------------------------------------ catálogo
def test_detalhes_de_livro_do_acervo(client):
    d = client.get("/api/details", params={"key": f"gutenberg:{CASMURRO_ID}"}).json()
    assert d["title"] == "Dom Casmurro" and d["free_version"]["gutenberg_id"] == CASMURRO_ID


def test_busca_inclui_acervo_e_open_library(client, monkeypatch):
    monkeypatch.setattr(openlibrary, "search", lambda q, limit=20, language=None: [{
        "book_key": "/works/OL1W", "title": "Outro livro", "author": "Fulano", "cover_url": None,
        "year": 1900, "has_fulltext": False, "subjects": [], "source": "openlibrary"}])
    results = client.get("/api/search", params={"q": "Casmurro"}).json()
    assert results[0]["source"] == "acervo" and results[0]["in_catalog"]
    assert results[1]["book_key"] == "/works/OL1W"


def test_busca_com_open_library_fora_do_ar(client, monkeypatch):
    def boom(q, limit=20, language=None):
        raise openlibrary.UpstreamError("timeout")
    monkeypatch.setattr(openlibrary, "search", boom)
    assert client.get("/api/search", params={"q": "zzz"}).status_code == 502


# ------------------------------------------------------------ usuário
def test_login_errado(client):
    r = client.post("/api/auth/login", json={"email": "ninguem@exemplo.com", "password": "x"})
    assert r.status_code == 401


def test_rotas_pessoais_exigem_login(client):
    assert client.get("/api/me/books").status_code == 401


def test_fluxo_favorito_biblioteca_progresso(client, auth_headers):
    ref = {"book_key": f"gutenberg:{ALICE_ID}", "title": "Alice", "author": "Carroll", "gutenberg_id": ALICE_ID}
    assert client.put("/api/me/favorites", json=ref, headers=auth_headers).json()["is_favorite"]
    assert client.put("/api/me/library", json={**ref, "status": "quero_ler"}, headers=auth_headers).status_code == 200

    p = client.put("/api/me/progress", json={**ref, "page": 1, "total_pages": 4}, headers=auth_headers).json()
    assert p["status"] == "lendo" and p["percent"] == 50.0
    p = client.put("/api/me/progress", json={**ref, "page": 3, "total_pages": 4}, headers=auth_headers).json()
    assert p["status"] == "concluido"

    stats = client.get("/api/me/stats", headers=auth_headers).json()
    assert stats == {"favorites": 1, "library": 1, "reading": 0, "completed": 1, "identifications": 0,
                     "bookmarks": 0, "highlights": 0}

    client.delete("/api/me/favorites", params={"key": ref["book_key"]}, headers=auth_headers)
    client.delete("/api/me/library", params={"key": ref["book_key"]}, headers=auth_headers)
    assert client.get("/api/me/books", headers=auth_headers).json() == []


def test_configuracoes_validadas(client, auth_headers):
    ok = {"language": "en", "translate_books": False, "appearance": "escuro", "theme": "terror",
          "font_size": 20, "spacing": 1.8, "text_width": "largo"}
    assert client.put("/api/me/settings", json=ok, headers=auth_headers).json() == ok
    assert client.put("/api/me/settings", json={**ok, "font_size": 99}, headers=auth_headers).status_code == 422


def test_importa_dados_do_visitante(client, auth_headers):
    ref = {"book_key": "/works/OL2W", "title": "Livro X", "author": "Y"}
    payload = {"favorites": [ref], "library": [{**ref, "status": "quero_ler"}],
               "progress": [{**ref, "page": 2, "total_pages": 10}]}
    books = client.post("/api/me/import", json=payload, headers=auth_headers).json()
    assert len(books) == 1
    assert books[0]["is_favorite"] and books[0]["status"] == "lendo" and books[0]["page"] == 2
