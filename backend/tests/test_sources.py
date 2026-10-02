"""Outras fontes: identificação externa, Wikisource e arquivos enviados (privados)."""
import io
import json
import uuid
import zipfile

import httpx
import pytest

from app.services import external, wikisource

# ----------------------------------------------------------------------------------- utilidades
PARAGRAPHS = [
    f"Paragrafo numero {i} do romance moderno da Marina, que atravessou a ponte velha da cidade "
    f"carregando a carta do irmao e pensando no mar de Itaguai durante a madrugada fria de inverno {i}."
    for i in range(30)
]
BOOK_TXT = "\n\n".join(PARAGRAPHS)
EXCERPT = "carregando a carta do irmao e pensando no mar de Itaguai durante a madrugada fria"


def _user(client):
    email = f"leitor-{uuid.uuid4().hex[:8]}@exemplo.com"
    r = client.post("/api/auth/register", json={"name": "Leitor", "email": email, "password": "segredo123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _upload(client, headers, name="romance.txt", data=BOOK_TXT.encode(), **form):
    return client.post("/api/me/uploads", headers=headers, files={"file": (name, data)},
                       data={"title": "Romance da Marina", "author": "Autora Moderna", **form})


def _mock_http(monkeypatch, module, handler):
    def factory(*args, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(module, "_client", factory)


def make_pdf(lines: list[str]) -> bytes:
    """PDF mínimo com texto de verdade (sem depender de bibliotecas nos testes)."""
    content = "BT /F1 11 Tf 50 780 Td 14 TL " + " ".join(
        f"({ln.replace('(', '').replace(')', '')}) Tj T*" for ln in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, obj in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{obj}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info << /Title (Livro em PDF) >> >>\n"
              f"startxref\n{xref}\n%%EOF".encode())
    return out.getvalue()


def make_epub() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""")
        z.writestr("OEBPS/content.opf", """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>O Livro do EPUB</dc:title><dc:creator>Escritora EPUB</dc:creator><dc:language>pt-BR</dc:language></metadata>
<manifest><item id="c2" href="text/cap2.xhtml" media-type="application/xhtml+xml"/>
<item id="c1" href="text/cap1.xhtml" media-type="application/xhtml+xml"/></manifest>
<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>""")
        for n, part in ((1, PARAGRAPHS[:15]), (2, PARAGRAPHS[15:])):
            body = "".join(f"<p>{p}</p>" for p in part)
            z.writestr(f"OEBPS/text/cap{n}.xhtml",
                       f'<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Capitulo {n}</h1>{body}</body></html>')
    return buf.getvalue()


# ------------------------------------------------------------------------- arquivos enviados
def test_arquivo_enviado_e_privado_e_identificavel_pelo_dono(client, monkeypatch):
    from app.services import openlibrary

    monkeypatch.setattr(openlibrary, "search", lambda q, limit=20, language=None: [])
    alice, bob = _user(client), _user(client)
    r = _upload(client, alice)
    assert r.status_code == 201, r.text
    book = r.json()
    assert book["private"] and book["source"] == "upload" and book["gutenberg_id"] >= 5_000_000
    gid = book["gutenberg_id"]

    # Dono: identifica, lê e vê na lista.
    top = client.post("/api/identify", json={"excerpt": EXCERPT}, headers=alice).json()["matches"][0]
    assert top["book"]["gutenberg_id"] == gid
    assert client.get(f"/api/reader/{gid}", headers=alice).status_code == 200
    assert [b["gutenberg_id"] for b in client.get("/api/me/uploads", headers=alice).json()] == [gid]
    assert gid in {b["gutenberg_id"] for b in client.get("/api/books", headers=alice).json()}

    # Outra pessoa e visitante: não veem, não leem, não identificam.
    for h in (bob, {}):
        matches = client.post("/api/identify", json={"excerpt": EXCERPT}, headers=h).json()["matches"]
        assert all(m["book"]["gutenberg_id"] != gid for m in matches)
        assert client.get(f"/api/reader/{gid}", headers=h).status_code == 404
        assert client.get("/api/details", params={"key": f"gutenberg:{gid}"}, headers=h).status_code == 404
        assert gid not in {b["gutenberg_id"] for b in client.get("/api/books", headers=h).json()}
        found = client.get("/api/search", params={"q": "Romance da Marina"}, headers=h).json()
        assert all(r["title"] != "Romance da Marina" for r in found)
    mine = client.get("/api/search", params={"q": "Romance da Marina"}, headers=alice).json()
    assert mine[0]["title"] == "Romance da Marina" and mine[0]["source"] == "meus-arquivos"
    assert client.delete(f"/api/me/uploads/{gid}", headers=bob).status_code == 404


def test_apagar_arquivo_remove_livro_e_anotacoes(client):
    h = _user(client)
    gid = _upload(client, h).json()["gutenberg_id"]
    key = f"gutenberg:{gid}"
    client.put("/api/me/favorites", headers=h,
               json={"book_key": key, "title": "Romance da Marina", "author": "", "gutenberg_id": gid})
    client.put("/api/me/bookmarks", headers=h,
               json={"book_key": key, "title": "Romance", "author": "", "gutenberg_id": gid, "page": 0})
    assert client.delete(f"/api/me/uploads/{gid}", headers=h).status_code == 204
    assert client.get(f"/api/reader/{gid}", headers=h).status_code == 404
    ann = client.get("/api/me/annotations", headers=h).json()
    assert all(b["book_key"] != key for b in ann.get("bookmarks", []))
    assert all(b["book_key"] != key for b in client.get("/api/me/books", headers=h).json())


def test_upload_epub_respeita_ordem_do_spine_e_metadados(client):
    h = _user(client)
    r = client.post("/api/me/uploads", headers=h, files={"file": ("livro.epub", make_epub())})
    assert r.status_code == 201, r.text
    b = r.json()
    assert (b["title"], b["author"], b["language"]) == ("O Livro do EPUB", "Escritora EPUB", "pt")
    page = client.get(f"/api/reader/{b['gutenberg_id']}", headers=h).json()["content"]
    assert page.index("Capitulo 1") < page.index("Paragrafo numero 0 ")


def test_upload_pdf_com_texto(client):
    h = _user(client)
    lines = [p[:90] for p in PARAGRAPHS[:40]] + [p[90:] + "." for p in PARAGRAPHS[:10]]
    r = client.post("/api/me/uploads", headers=h, files={"file": ("x.pdf", make_pdf(lines))},
                    data={"language": "pt"})
    assert r.status_code == 201, r.text
    assert r.json()["title"] == "Livro em PDF"


def test_upload_recusa_formatos_e_arquivos_ruins(client):
    h = _user(client)
    assert _upload(client, h, name="foto.png", data=b"\x89PNG").status_code == 400
    r = _upload(client, h, name="vazio.pdf", data=make_pdf(["", ""]))
    assert r.status_code == 400 and "escaneado" in r.json()["detail"]
    assert _upload(client, h, name="quebrado.epub", data=b"nao e zip").status_code == 400
    assert _upload(client, h, name="curto.txt", data=b"oi").status_code == 400
    assert _upload(client, {}).status_code == 401


def test_upload_limite_de_tamanho(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "upload_max_mb", 0.001)
    r = _upload(client, _user(client))
    assert r.status_code == 400 and "MB" in r.json()["detail"]


def test_excluir_conta_apaga_arquivos_enviados(client):
    h = _user(client)
    gid = _upload(client, h).json()["gutenberg_id"]
    assert client.post("/api/auth/delete-account", headers=h, json={"password": "segredo123"}).status_code == 200
    from app.database import SessionLocal
    from app.models import Book

    with SessionLocal() as db:
        assert db.query(Book).filter(Book.gutenberg_id == gid).count() == 0


# ---------------------------------------------------------------------- identificação externa
def test_busca_externa_junta_fontes_e_monta_links(client, monkeypatch):
    external.cache._store.clear()

    def handler(req: httpx.Request):
        if "googleapis" in req.url.host:
            assert req.url.params["q"].startswith('"')
            return httpx.Response(200, json={"items": [{
                "volumeInfo": {"title": "Torto Arado", "authors": ["Itamar Vieira Junior"], "language": "pt",
                               "publishedDate": "2019-08-01", "infoLink": "https://books.google.com/x",
                               "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9786580309313"}],
                               "imageLinks": {"thumbnail": "http://books.google.com/capa.jpg"}},
                "saleInfo": {"saleability": "FOR_SALE", "buyLink": "https://play.google.com/store/books/x"},
                "accessInfo": {"viewability": "PARTIAL", "publicDomain": False},
                "searchInfo": {"textSnippet": "a <b>faca</b> &amp; o rio"},
            }]})
        return httpx.Response(200, json={"hits": {"total": 1, "hits": [{
            "fields": {"identifier": ["tortoarado00"], "meta_title": ["Torto arado"],
                       "meta_creator": ["Vieira Junior, Itamar"]},
            "highlight": {"text": ["{{{faca}}} no rio"]}}]}})

    _mock_http(monkeypatch, external, handler)
    r = client.post("/api/identify/external", json={"excerpt": "Quando retirei a faca da mala de roupas, embrulhada"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["failed"] == [] and len(data["results"]) == 1   # mesmo livro nas duas fontes vira um só
    book = data["results"][0]
    assert book["snippet"] == "a faca & o rio" and book["cover_url"].startswith("https://")
    labels = {link["label"] for link in book["links"]}
    assert {"Google Play Livros", "Amazon", "Estante Virtual (usados)", "Internet Archive (ler/emprestar)"} <= labels
    amazon = next(link["url"] for link in book["links"] if link["label"] == "Amazon")
    assert "9786580309313" in amazon


def test_busca_externa_com_uma_fonte_fora_do_ar(client, monkeypatch):
    external.cache._store.clear()

    def handler(req):
        if "googleapis" in req.url.host:
            return httpx.Response(503)
        return httpx.Response(200, json={"hits": {"total": 0, "hits": []}})

    _mock_http(monkeypatch, external, handler)
    data = client.post("/api/identify/external", json={"excerpt": "um trecho qualquer de um livro moderno aqui"}).json()
    assert data["results"] == [] and data["failed"] == ["google_books"]


def test_busca_externa_tem_limite(client, monkeypatch):
    from app import ratelimit
    from app.config import get_settings

    external.cache._store.clear()
    _mock_http(monkeypatch, external, lambda req: httpx.Response(200, json={"hits": {"total": 0}}))
    monkeypatch.setattr(get_settings(), "external_searches_per_hour", 1)
    ratelimit.clear()
    body = {"excerpt": "outro trecho qualquer de livro para testar o limite"}
    assert client.post("/api/identify/external", json=body).status_code == 200
    assert client.post("/api/identify/external", json=body).status_code == 429
    ratelimit.clear()


def test_frase_escolhida_e_o_meio_do_trecho():
    assert external.pick_phrase("a b c d e f g h i j k l m n", words=4) == "f g h i"
    assert external.pick_phrase("curto demais", words=10) == "curto demais"


# --------------------------------------------------------------------------------- Wikisource
MAIN_HTML = """<div class="mw-parser-output">
<div id="headertemplate" class="ws-noexport"><a href="/wiki/Autor:Machado_de_Assis" title="Autor:Machado de Assis">Machado de Assis</a>
<a href="/wiki/Livro_Teste/I" title="Livro Teste/I">nav</a></div>
<ul><li><a href="/wiki/Livro_Teste/I" title="Livro Teste/I">I</a></li>
<li><a href="/wiki/Livro_Teste/II" title="Livro Teste/II">II</a></li>
<li><a href="/w/index.php?title=Livro_Teste/III&action=edit" class="new" title="Livro Teste/III (página não existe)">III</a></li>
<li><a href="/wiki/Outro" title="Outro">outro livro</a></li></ul></div>"""


def _chapter(n: int) -> str:
    paras = "".join(f"<p>{p} <span class='pagenum ws-pagenum'>[{n}{i}]</span><sup class='reference'>[1]</sup></p>"
                    for i, p in enumerate(PARAGRAPHS[n * 10:(n + 1) * 10]))
    return f"<div class='mw-parser-output'><div class='ws-noexport'>« anterior | próximo »</div><h2>Capítulo {n}</h2>{paras}</div>"


def _wiki_handler(req: httpx.Request):
    p = req.url.params
    if p.get("list") == "search":
        return httpx.Response(200, json={"query": {"search": [
            {"title": "Livro Teste/I", "snippet": "cap"}, {"title": "Livro Teste", "snippet": "<span>obra</span>"}]}})
    page = p.get("page")
    if page == "Livro Teste":
        return httpx.Response(200, json={"parse": {"title": "Livro Teste", "text": MAIN_HTML}})
    if page in ("Livro Teste/I", "Livro Teste/II"):
        n = 1 if page.endswith("/I") else 2
        return httpx.Response(200, json={"parse": {"title": page, "text": _chapter(n)}})
    return httpx.Response(200, json={"error": {"code": "missingtitle", "info": "x"}})


def test_wikisource_texto_limpo_e_capitulos_em_ordem():
    text, subs, author = wikisource.html_to_text(MAIN_HTML, "Livro Teste")
    assert subs == ["Livro Teste/I", "Livro Teste/II"] and author == "Machado de Assis"
    chapter, _, _ = wikisource.html_to_text(_chapter(1))
    assert "anterior" not in chapter and "[10]" not in chapter and "[1]" not in chapter
    assert chapter.startswith("Capítulo 1\n\nParagrafo numero 10")


def test_wikisource_busca_e_importacao(client, monkeypatch):
    _mock_http(monkeypatch, wikisource, _wiki_handler)
    monkeypatch.setattr(wikisource.time, "sleep", lambda s: None)
    h = _user(client)

    results = client.get("/api/wikisource/search", params={"q": "livro teste"}).json()
    assert results[0]["title"] == "Livro Teste" and results[1]["is_chapter"]

    job = client.post("/api/wikisource/import", headers=h, json={"lang": "pt", "title": "Livro Teste"})
    assert job.status_code == 202, job.text
    done = client.get(f"/api/wikisource/jobs/{job.json()['id']}", headers=h).json()
    assert done["status"] == "done", done
    book = done["book"]
    assert book["source"] == "wikisource" and book["author"] == "Machado de Assis" and not book["private"]
    assert book["source_url"] == "https://pt.wikisource.org/wiki/Livro_Teste"

    # Público: qualquer pessoa identifica e lê.
    top = client.post("/api/identify", json={"excerpt": PARAGRAPHS[15][:120]}).json()["matches"][0]
    assert top["book"]["gutenberg_id"] == book["gutenberg_id"]
    assert client.get(f"/api/reader/{book['gutenberg_id']}").status_code == 200

    # Importar de novo não duplica; a busca já marca como "no acervo".
    again = client.post("/api/wikisource/import", headers=h, json={"lang": "pt", "title": "Livro Teste"}).json()
    assert again["status"] == "done" and again["book"]["gutenberg_id"] == book["gutenberg_id"]
    marked = client.get("/api/wikisource/search", params={"q": "livro teste"}).json()
    assert marked[0]["gutenberg_id"] == book["gutenberg_id"]

    # Job de outra pessoa não aparece.
    assert client.get(f"/api/wikisource/jobs/{job.json()['id']}", headers=_user(client)).status_code == 404


def test_wikisource_pagina_inexistente_e_exige_login(client, monkeypatch):
    _mock_http(monkeypatch, wikisource, _wiki_handler)
    assert client.post("/api/wikisource/import", json={"title": "Nada"}).status_code == 401
    h = _user(client)
    job = client.post("/api/wikisource/import", headers=h, json={"lang": "pt", "title": "Nada"}).json()
    done = client.get(f"/api/wikisource/jobs/{job['id']}", headers=h).json()
    assert done["status"] == "error" and done["error"] == "not_found"


@pytest.mark.parametrize("payload", [{"lang": "fr", "title": "X"}, {"lang": "pt", "title": "   "}])
def test_wikisource_import_valida_entrada(client, payload):
    r = client.post("/api/wikisource/import", headers=_user(client), json=payload)
    assert r.status_code in (400, 422), json.dumps(r.json())


def test_id_de_livro_apagado_nao_e_reaproveitado(client):
    h = _user(client)
    first = _upload(client, h).json()["gutenberg_id"]
    assert client.delete(f"/api/me/uploads/{first}", headers=h).status_code == 204
    second = _upload(client, h).json()["gutenberg_id"]
    assert second > first


def test_envio_grande_e_recusado_antes_de_ler_o_corpo(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "upload_max_mb", 0.01)
    big = b"a" * 200_000
    # Sem login: recusado pelo tamanho antes de o formulário ser processado.
    r = client.post("/api/me/uploads", files={"file": ("x.txt", big)})
    assert r.status_code == 413 and "MB" in r.json()["detail"]

    def chunks():   # envio "chunked", sem Content-Length
        yield b'--xyz\r\nContent-Disposition: form-data; name="file"; filename="x.txt"\r\n\r\n'
        for _ in range(20):
            yield b"b" * 20_000
    r = client.post("/api/me/uploads", content=chunks(),
                    headers={"Content-Type": "multipart/form-data; boundary=xyz"})
    assert r.status_code == 413


def test_epub_com_capitulo_repetido_nao_multiplica(client):
    import io
    import zipfile

    from app.services import uploads

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="c.opf"/></rootfiles></container>')
        z.writestr("c.opf", '<package xmlns="http://www.idpf.org/2007/opf"><metadata/><manifest>'
                   '<item id="a" href="a.xhtml"/></manifest><spine>' + '<itemref idref="a"/>' * 300 + '</spine></package>')
        z.writestr("a.xhtml", "<html><head><title>TITULO</title></head><body>" + "<p>texto do capitulo</p>" * 50 + "</body></html>")
    text, _ = uploads.extract("x.epub", buf.getvalue())
    assert text.count("texto do capitulo") == 50 and "TITULO" not in text


def test_link_sem_url_nao_quebra_busca_externa(client, monkeypatch):
    external.cache._store.clear()

    def handler(req):
        if "googleapis" in req.url.host:
            return httpx.Response(200, json={"items": [{"volumeInfo": {"title": "Sem Link"},
                                                        "accessInfo": {"publicDomain": True}}]})
        return httpx.Response(200, json={"hits": {"total": 0, "hits": []}})

    _mock_http(monkeypatch, external, handler)
    r = client.post("/api/identify/external", json={"excerpt": "um trecho bem diferente para nao cair no cache"})
    assert r.status_code == 200 and r.json()["results"][0]["title"] == "Sem Link"


def test_wikisource_importacoes_simultaneas_viram_uma(client, monkeypatch):
    _mock_http(monkeypatch, wikisource, _wiki_handler)
    job1, new1 = wikisource.new_job("pt", "Obra Paralela", 1)
    job2, new2 = wikisource.new_job("pt", "Obra Paralela", 2)
    assert new1 and not new2 and job1["id"] == job2["id"]
    with wikisource._jobs_lock:
        wikisource._jobs[job1["id"]]["status"] = "error"   # encerra o job de teste
