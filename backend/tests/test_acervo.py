"""Acervo maior: coleção do script e indexação automática ao ler."""
from app.services import gutenberg, ingest

FRANK_ID = 900084
FRANK_TXT = """The Project Gutenberg eBook of Frankenstein
Title: Frankenstein
Author: Mary Wollstonecraft Shelley
Language: English

*** START OF THE PROJECT GUTENBERG EBOOK FRANKENSTEIN ***

Letter 1

You will rejoice to hear that no disaster has accompanied the commencement of an enterprise
which you have regarded with such evil forebodings. I arrived here yesterday, and my first task
is to assure my dear sister of my welfare and increasing confidence in the success of my undertaking.

I am already far north of London, and as I walk in the streets of Petersburgh, I feel a cold
northern breeze play upon my cheeks, which braces my nerves and fills me with delight.

*** END OF THE PROJECT GUTENBERG EBOOK FRANKENSTEIN ***
"""


def _fake_download(monkeypatch, text):
    calls = []
    monkeypatch.setattr(gutenberg, "download_raw", lambda gid: calls.append(gid) or text)
    gutenberg._parsed_download.cache_clear()
    gutenberg.clear_page_cache()
    return calls


def test_livro_lido_entra_no_acervo_e_passa_a_ser_identificado(client, monkeypatch):
    _fake_download(monkeypatch, FRANK_TXT)
    monkeypatch.setattr(ingest, "MIN_CHUNKS", 1)
    excerpt = "as I walk in the streets of Petersburgh I feel a cold northern breeze play upon my cheeks"

    before = client.post("/api/identify", json={"excerpt": excerpt}).json()
    assert all(m["book"]["gutenberg_id"] != FRANK_ID for m in before["matches"])

    page_before = client.get(f"/api/reader/{FRANK_ID}").json()   # abre no leitor -> indexa em segundo plano

    books = {b["gutenberg_id"]: b for b in client.get("/api/books").json()}
    assert books[FRANK_ID]["title"] == "Frankenstein"
    assert books[FRANK_ID]["language"] == "en"
    top = client.post("/api/identify", json={"excerpt": excerpt}).json()["matches"][0]
    assert top["book"]["gutenberg_id"] == FRANK_ID

    # As páginas continuam iguais depois de o livro entrar no acervo (marcadores não mudam de lugar).
    gutenberg.clear_page_cache()
    page_after = client.get(f"/api/reader/{FRANK_ID}").json()
    assert page_after["content"] == page_before["content"]
    assert page_after["total_pages"] == page_before["total_pages"]


def test_indexacao_automatica_roda_uma_vez_por_livro(client, monkeypatch):
    calls = _fake_download(monkeypatch, FRANK_TXT.replace("Frankenstein", "Outro"))
    ran = []
    monkeypatch.setattr(ingest, "ingest_after_read", lambda gid: ran.append(gid))
    for p in range(3):
        client.get("/api/reader/900085", params={"page": p})
    assert ran == [900085]
    assert calls  # o leitor baixou o texto


def test_indexacao_automatica_pode_ser_desligada(client, monkeypatch):
    from app.config import get_settings

    _fake_download(monkeypatch, FRANK_TXT)
    monkeypatch.setattr(get_settings(), "auto_ingest_on_read", False)
    ran = []
    monkeypatch.setattr(ingest, "ingest_after_read", lambda gid: ran.append(gid))
    client.get("/api/reader/900086")
    assert ran == []


def test_textos_pequenos_ou_grandes_nao_entram(client, monkeypatch):
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import Book

    _fake_download(monkeypatch, FRANK_TXT)
    ingest.ingest_after_read(900087)                         # 1 trecho < MIN_CHUNKS
    monkeypatch.setattr(ingest, "MIN_CHUNKS", 1)
    monkeypatch.setattr(get_settings(), "auto_ingest_max_mb", 0.0001)
    ingest.ingest_after_read(900088)                         # maior que o limite
    with SessionLocal() as db:
        assert not db.query(Book).filter(Book.gutenberg_id.in_([900087, 900088])).count()


def test_livro_do_acervo_nao_dispara_indexacao(client, monkeypatch):
    from .conftest import ALICE_ID

    ran = []
    monkeypatch.setattr(ingest, "ingest_after_read", lambda gid: ran.append(gid))
    client.get(f"/api/reader/{ALICE_ID}")
    assert ran == []


def test_script_escolhe_a_edicao_certa_pelo_titulo_e_autor():
    from scripts.ingest_gutenberg import pick_match

    results = [
        {"id": 1, "title": "Helena e outros contos", "authors": [{"name": "Silva, João"}]},
        {"id": 2, "title": "Helena", "authors": [{"name": "Assis, Machado de"}]},
        {"id": 3, "title": "Contos escolhidos: Helena e mais", "authors": [{"name": "Assis, Machado de"}]},
    ]
    assert pick_match(results, "Helena", "Machado de Assis") == 2
    assert pick_match(results, "Iracema", "José de Alencar") is None
    assert pick_match([{"id": 9, "title": "Os Maias: episodios da vida romantica",
                        "authors": [{"name": "Queiroz, Eça de"}]}], "Os Maias", "Eça de Queirós") == 9


def test_script_pula_livros_que_ja_estao_no_acervo(client, monkeypatch, capsys):
    from .conftest import ALICE_ID
    from scripts import ingest_gutenberg as script

    monkeypatch.setattr(script, "download_raw", lambda gid: (_ for _ in ()).throw(AssertionError("não devia baixar")))
    script.run_collection([ALICE_ID], [], replace=False)
    assert "já está no acervo" in capsys.readouterr().out
