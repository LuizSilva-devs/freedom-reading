"""Correções da segunda revisão completa."""
import uuid
from concurrent.futures import ThreadPoolExecutor

from app import ratelimit
from app.config import get_settings
from app.services import translation

from .conftest import ALICE_ID


def _new_user(client, password="segredo123"):
    email = f"r2-{uuid.uuid4().hex[:8]}@exemplo.com"
    tok = client.post("/api/auth/register", json={"name": "R2", "email": email, "password": password}).json()
    return email, {"Authorization": f"Bearer {tok['access_token']}"}


def test_login_bloqueado_mesmo_com_a_senha_certa(client, monkeypatch):
    email, _ = _new_user(client)
    monkeypatch.setattr(get_settings(), "login_attempts_per_5min", 3)
    ratelimit.clear()
    for _ in range(3):
        assert client.post("/api/auth/login", json={"email": email, "password": "errada"}).status_code == 401
    # 4ª tentativa, agora com a senha CERTA: continua bloqueada
    r = client.post("/api/auth/login", json={"email": email, "password": "segredo123"})
    assert r.status_code == 429
    ratelimit.clear()
    assert client.post("/api/auth/login", json={"email": email, "password": "segredo123"}).status_code == 200


def test_cadastro_limitado_por_ip(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "registrations_per_hour", 2)
    ratelimit.clear()
    codes = [client.post("/api/auth/register", json={"name": "X", "email": f"lim-{i}-{uuid.uuid4().hex[:6]}@exemplo.com",
                                                     "password": "segredo123"}).status_code for i in range(3)]
    ratelimit.clear()
    assert codes == [201, 201, 429]


def test_favoritar_em_paralelo_nao_da_erro_500(client):
    _, headers = _new_user(client)
    ref = {"book_key": "/works/OL999W", "title": "Paralelo", "author": "X"}

    def fav(_):
        return client.put("/api/me/favorites", json=ref, headers=headers).status_code

    def progress(_):
        return client.put("/api/me/progress", json={**ref, "page": 1, "total_pages": 5}, headers=headers).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda i: fav(i) if i % 2 else progress(i), range(16)))
    assert set(codes) == {200}
    books = client.get("/api/me/books", headers=headers).json()
    assert len(books) == 1 and books[0]["is_favorite"]


def test_marcador_em_paralelo_nao_duplica(client):
    _, headers = _new_user(client)
    ref = {"book_key": f"gutenberg:{ALICE_ID}", "title": "Alice", "gutenberg_id": ALICE_ID, "page": 0}
    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(lambda _: client.put("/api/me/bookmarks", json=ref, headers=headers).status_code, range(6)))
    assert set(codes) == {200}
    assert len(client.get("/api/me/annotations", headers=headers).json()["bookmarks"]) == 1


def test_importacao_nao_apaga_anotacao_existente(client):
    _, headers = _new_user(client)
    ref = {"book_key": f"gutenberg:{ALICE_ID}", "title": "Alice", "gutenberg_id": ALICE_ID, "page": 2}
    client.put("/api/me/bookmarks", json={**ref, "note": "minha nota"}, headers=headers)
    client.post("/api/me/import", json={"bookmarks": [ref]}, headers=headers)
    bms = client.get("/api/me/annotations", headers=headers).json()["bookmarks"]
    assert bms[0]["note"] == "minha nota"


def test_mymemory_desfaz_entidades_html(monkeypatch):
    class Resp:
        def json(self):
            return {"responseStatus": 200, "responseData": {"translatedText": "Ela disse &quot;n&#227;o&quot; &amp; foi"}}
    monkeypatch.setattr(translation.httpx, "get", lambda *a, **k: Resp())
    assert translation._mymemory("x", "en", "pt") == 'Ela disse "não" & foi'


def test_id_de_livro_invalido(client):
    assert client.get("/api/reader/0").status_code == 422


def test_titulo_vazio_recusado(client, auth_headers):
    assert client.put("/api/me/favorites", json={"book_key": "/works/OL1W", "title": ""}, headers=auth_headers).status_code == 422


# ------------------------------------------------------------ achados do fuzzing (3ª revisão)
def test_numeros_grandes_demais_sao_recusados_sem_erro_500(client, auth_headers):
    ref = {"book_key": "gutenberg:11", "title": "Alice", "gutenberg_id": 45514899213704990031872, "page": 0}
    assert client.put("/api/me/bookmarks", json=ref, headers=auth_headers).status_code == 422
    hl = {"book_key": "gutenberg:11", "title": "Alice", "gutenberg_id": 11, "page": 0,
          "start": 0, "end": 10**12, "text": "x"}
    assert client.post("/api/me/highlights", json=hl, headers=auth_headers).status_code == 422
    assert client.delete("/api/me/bookmarks/230322555463", headers=auth_headers).status_code == 422
    assert client.get("/api/reader/11", params={"page": 10**12}).status_code == 422


def test_caractere_nulo_e_recusado_sem_erro_500(client, auth_headers):
    assert client.patch("/api/auth/me", json={"name": "a\x00b"}, headers=auth_headers).status_code == 422
    assert client.put("/api/me/favorites", json={"book_key": "x\x00", "title": "t"}, headers=auth_headers).status_code == 422
    assert client.delete("/api/me/favorites", params={"key": "x\x00"}, headers=auth_headers).status_code == 422
    assert client.get("/api/search", params={"q": "a\x00"}).status_code == 422


def test_erros_proprios_seguem_o_formato_documentado(client):
    r = client.post("/api/identify", json={"excerpt": "curto"})
    assert r.status_code == 400 and isinstance(r.json()["detail"], str)


def test_tidy_text_reading_layout():
    from app.services.text_utils import tidy_text
    raw = ("DOM CASMURRO\n\nPOR\n\nMACHADO DE ASSIS\n\nPARIZ\n\nI\n\nDo titulo.\n\n"
           "Uma noite destas, vindo da cidade, encontrei um rapaz.\n\n--Continue, disse eu.\n\n"
           "Chamou-me _Dom Casmurro._ e foi--disse ele.")
    out = tidy_text(raw)
    assert out.startswith("DOM CASMURRO\nPOR\nMACHADO DE ASSIS\nPARIZ\n\nI\nDo titulo.\n\nUma noite")
    assert "\n\n— Continue, disse eu." in out
    assert "Chamou-me Dom Casmurro. e foi—disse ele." in out
    # texto comum e nomes com sublinhado não mudam
    assert tidy_text("Primeiro paragrafo.\n\nSegundo, com snake_case.") == "Primeiro paragrafo.\n\nSegundo, com snake_case."
