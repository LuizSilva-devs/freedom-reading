"""Marcadores, sublinhados, tradução, idioma e correções de bugs."""
from app.config import get_settings
from app.models import PageTranslation
from app.services import openlibrary, translation
from app.services.text_utils import parse_gutenberg_header

from .conftest import ALICE_ID, CASMURRO_ID

ALICE_KEY = f"gutenberg:{ALICE_ID}"


def fake_translate(text, source, target):
    return f"[{target}] {text}"


# ------------------------------------------------------------ idioma / tradução
def test_idioma_do_livro_vem_do_cabecalho():
    raw = "Title: X\nAuthor: Y\nLanguage: Portuguese\n\n*** START OF THE PROJECT GUTENBERG EBOOK X ***"
    assert parse_gutenberg_header(raw)["language"] == "pt"


def test_leitor_informa_idioma_original(client):
    page = client.get(f"/api/reader/{ALICE_ID}").json()
    assert page["original_language"] == "en" and page["translated"] is False


def test_leitor_traduz_livro_em_ingles(client, monkeypatch):
    monkeypatch.setattr(translation, "translate_chunk", fake_translate)
    page = client.get(f"/api/reader/{ALICE_ID}", params={"page": 0, "lang": "pt"}).json()
    assert page["translated"] and page["language"] == "pt"
    paragraphs = page["content"].split("\n\n")
    assert all(p.startswith("[pt] ") for p in paragraphs if any(c.isalpha() for c in p))
    # parágrafos preservados: mesma quantidade que o original
    original = client.get(f"/api/reader/{ALICE_ID}", params={"page": 0}).json()["content"]
    assert len(paragraphs) == len(original.split("\n\n"))


def test_traducao_fica_em_cache(client, monkeypatch):
    from sqlalchemy import delete

    from app.database import SessionLocal
    with SessionLocal() as db:
        db.execute(delete(PageTranslation))
        db.commit()
    calls = []
    monkeypatch.setattr(translation, "translate_chunk", lambda t, s, g: calls.append(t) or f"PT {t}")
    client.get(f"/api/reader/{ALICE_ID}", params={"page": 0, "lang": "pt"})
    first = len(calls)
    client.get(f"/api/reader/{ALICE_ID}", params={"page": 0, "lang": "pt"})
    assert first > 0 and len(calls) == first  # segunda leitura veio do banco


def test_livro_no_mesmo_idioma_nao_traduz(client, monkeypatch):
    monkeypatch.setattr(translation, "translate_chunk", lambda *a: (_ for _ in ()).throw(AssertionError("não deveria traduzir")))
    page = client.get(f"/api/reader/{CASMURRO_ID}", params={"lang": "pt"}).json()
    assert page["translated"] is False and page["language"] == "pt"


def test_falha_na_traducao_devolve_original(client, monkeypatch):
    def boom(*_):
        raise translation.TranslationError("sem cota")
    monkeypatch.setattr(translation, "translate_chunk", boom)
    page = client.get(f"/api/reader/{CASMURRO_ID}", params={"lang": "en"},
                      headers={"Accept-Language": "en-US,en;q=0.9"}).json()
    assert page["translated"] is False and "original" in page["translation_error"].lower()


def test_divisao_respeita_limite_do_provedor():
    long = ("Palavra " * 200).strip() + ". " + "Outra frase curta."
    parts = translation._split_for_limit(long, 450)
    assert all(len(p) <= 450 for p in parts) and " ".join(parts).split() == long.split()


def test_cache_da_traducao_invalida_quando_texto_muda(client, monkeypatch):
    from app.database import SessionLocal
    monkeypatch.setattr(translation, "translate_chunk", fake_translate)
    with SessionLocal() as db:
        s = get_settings()
        db.add(PageTranslation(gutenberg_id=ALICE_ID, page_chars=s.reader_page_chars, page=5, target_lang="pt",
                               source_hash="antigo", content="velho", provider="x"))
        db.commit()
        out = translation.get_translated_page(db, ALICE_ID, 5, "Texto novo.", "en", "pt")
    assert out == "[pt] Texto novo."


# ------------------------------------------------------------ mensagens em inglês
def test_mensagens_da_api_em_ingles(client):
    r = client.post("/api/auth/login", json={"email": "x@exemplo.com", "password": "x"},
                    headers={"Accept-Language": "en"})
    assert r.json()["detail"] == "Incorrect email or password."
    r = client.post("/api/identify", json={"excerpt": "Alice"}, headers={"Accept-Language": "en"})
    assert "longer excerpt" in r.json()["detail"]


# ------------------------------------------------------------ busca
def test_busca_filtra_por_idioma(client, monkeypatch):
    seen = {}

    def fake(q, limit=20, language=None):
        seen["language"] = language
        return []
    monkeypatch.setattr(openlibrary, "search", fake)
    pt = client.get("/api/search", params={"q": "a", "language": "pt"}).json()
    assert seen["language"] == "pt"
    assert all(r["gutenberg_id"] != ALICE_ID for r in pt)


def test_busca_so_com_espacos_da_erro_claro(client):
    assert client.get("/api/search", params={"q": "   "}).status_code == 400


def test_busca_com_curinga_nao_retorna_tudo(client, monkeypatch):
    monkeypatch.setattr(openlibrary, "search", lambda q, limit=20, language=None: [])
    assert client.get("/api/search", params={"q": "%"}).json() == []


# ------------------------------------------------------------ marcadores
def _ref(page=0):
    return {"book_key": ALICE_KEY, "title": "Alice", "author": "Carroll", "gutenberg_id": ALICE_ID, "page": page}


def test_marcadores(client, auth_headers):
    b1 = client.put("/api/me/bookmarks", json=_ref(0), headers=auth_headers).json()
    again = client.put("/api/me/bookmarks", json={**_ref(0), "note": "  começo  "}, headers=auth_headers).json()
    assert again["id"] == b1["id"] and again["note"] == "começo"  # mesma página = mesmo marcador
    client.put("/api/me/bookmarks", json=_ref(1), headers=auth_headers)

    ann = client.get("/api/me/annotations", params={"book_key": ALICE_KEY}, headers=auth_headers).json()
    assert sorted(b["page"] for b in ann["bookmarks"]) == [0, 1]

    assert client.delete(f"/api/me/bookmarks/{b1['id']}", headers=auth_headers).status_code == 204
    assert client.delete(f"/api/me/bookmarks/{b1['id']}", headers=auth_headers).status_code == 404


def test_nao_apaga_marcador_de_outro_usuario(client, auth_headers):
    import uuid
    other = client.post("/api/auth/register", json={"name": "Outro", "email": f"o-{uuid.uuid4().hex[:6]}@exemplo.com",
                                                    "password": "segredo123"}).json()
    b = client.put("/api/me/bookmarks", json=_ref(3), headers=auth_headers).json()
    r = client.delete(f"/api/me/bookmarks/{b['id']}", headers={"Authorization": f"Bearer {other['access_token']}"})
    assert r.status_code == 404


# ------------------------------------------------------------ sublinhados
def test_sublinhados(client, auth_headers):
    body = {**_ref(0), "start": 10, "end": 30, "text": "trecho sublinhado", "color": "green"}
    h = client.post("/api/me/highlights", json=body, headers=auth_headers)
    assert h.status_code == 201
    hid = h.json()["id"]
    p = client.patch(f"/api/me/highlights/{hid}", json={"color": "pink", "note": "gostei"}, headers=auth_headers).json()
    assert p["color"] == "pink" and p["note"] == "gostei"

    stats = client.get("/api/me/stats", headers=auth_headers).json()
    assert stats["highlights"] == 1

    assert client.delete(f"/api/me/highlights/{hid}", headers=auth_headers).status_code == 204


def test_sublinhado_invalido(client, auth_headers):
    bad = {**_ref(0), "start": 30, "end": 10, "text": "x"}
    assert client.post("/api/me/highlights", json=bad, headers=auth_headers).status_code == 422
    bad_color = {**_ref(0), "start": 0, "end": 5, "text": "x", "color": "roxo"}
    assert client.post("/api/me/highlights", json=bad_color, headers=auth_headers).status_code == 422


def test_import_traz_marcadores_e_sublinhados_sem_duplicar(client, auth_headers):
    hl = {**_ref(2), "start": 0, "end": 5, "text": "Alice", "version": "pt"}
    payload = {"bookmarks": [_ref(2)], "highlights": [hl]}
    client.post("/api/me/import", json=payload, headers=auth_headers)
    client.post("/api/me/import", json=payload, headers=auth_headers)
    ann = client.get("/api/me/annotations", headers=auth_headers).json()
    assert len(ann["bookmarks"]) == 1 and len(ann["highlights"]) == 1
    assert ann["highlights"][0]["version"] == "pt"


# ------------------------------------------------------------ correções
def test_renomear_com_espacos_e_recusado(client, auth_headers):
    assert client.patch("/api/auth/me", json={"name": "   "}, headers=auth_headers).status_code == 422


def test_estatistica_conta_so_identificacoes_com_resultado(client, auth_headers):
    client.post("/api/identify", json={"excerpt": "quantum chromodynamics lattice gauge renormalization"},
                headers=auth_headers)
    assert client.get("/api/me/stats", headers=auth_headers).json()["identifications"] == 0
    client.post("/api/identify", json={"excerpt": "a White Rabbit with pink eyes ran close by her"},
                headers=auth_headers)
    assert client.get("/api/me/stats", headers=auth_headers).json()["identifications"] == 1


def test_configuracao_antiga_invalida_nao_quebra(client, auth_headers):
    from app.database import SessionLocal
    from app.models import User
    me = client.get("/api/auth/me", headers=auth_headers).json()
    with SessionLocal() as db:
        u = db.get(User, me["id"])
        u.settings = {"theme": "tema-que-nao-existe", "font_size": 20, "chave_velha": 1}
        db.commit()
    s = client.get("/api/me/settings", headers=auth_headers).json()
    assert s["theme"] == "literatura" and s["font_size"] == 20
