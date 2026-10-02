/* Freadom Reading — aplicação (SPA com rotas por hash).
   Rotas:  #/  #/explorar  #/busca?q=&mode=&lang=&genre=  #/livro?key=
           #/ler/<gutenbergId>?key=&p=&hl=  #/biblioteca  #/perfil  #/ajustes          */
import { api } from "./api.js";
import { t, setLang, getLang } from "./i18n.js";
import { store, DEFAULT_SETTINGS } from "./store.js";
import {
  $, $$, esc, icon, toast, register, lookup, detailsHref, coverHTML, bookCardHTML,
  skeletonRow, emptyState, loading, highlight, openModal, closeModal,
} from "./ui.js";

const GENRES = [
  { key: "literatura", query: "literatura brasileira" },
  { key: "biologia", query: "biology science" },
  { key: "terror", query: "horror" },
  { key: "romance", query: "romance" },
  { key: "fantasia", query: "fantasy" },
  { key: "ficcao", query: "science fiction" },
  { key: "historia", query: "history" },
  { key: "filosofia", query: "philosophy", theme: "literatura" },
  { key: "misterio", query: "mystery" },
  { key: "aventura", query: "adventure", theme: "historia" },
];
const STATUSES = ["quero_ler", "lendo", "concluido"];
const MIN_EXCERPT = 25;
const letters = (s) => s.replace(/[^\p{L}\p{N}]/gu, "").length;

const state = {
  route: { view: "home", params: {} },
  previewTheme: null,         // tema temporário ao explorar um gênero
  currentBook: null,          // livro aberto nos detalhes/leitor
  libraryTab: "favoritos",
  authMode: "login",
  catalog: null,              // livros do acervo (cache para re-renderizar sem rede)
  classics: null,
  lastIdentify: null,         // { res, query } para re-renderizar ao trocar de idioma
  identifying: false,
  pendingToken: null,         // token do link do e-mail (tirado da URL assim que a tela abre)
  accountResult: null,        // resultado da confirmação de e-mail, para redesenhar a tela sem perder o estado
  uploads: null,              // livros enviados pela pessoa (aba "Meus arquivos")
  userId: undefined,          // detecta troca de conta para limpar o que é particular
};

// Estado do leitor
const r = {
  gid: null, key: null, page: 0, total: 0, origLang: "",
  cache: new Map(),           // "página|idioma" -> dados já carregados
  inflight: new Map(),        // "página|idioma" -> promessa (evita pedir a mesma página 2x)
  seq: 0,                     // descarta respostas atrasadas (cliques rápidos em "Próxima")
  data: null, painted: false,
  paper: "papel", translateOn: true,
  target: null,               // trecho encontrado pela identificação, para destacar
  flashHid: null,             // destaque a piscar depois de navegar até ele
};

/* =========================================================================
   APARÊNCIA E IDIOMA
========================================================================= */
const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
function applyAppearance() {
  const s = store.settings;
  document.body.dataset.theme = state.previewTheme || s.theme || "literatura";
  const dark = s.appearance === "escuro" || (s.appearance === "auto" && darkQuery.matches);
  if (dark) document.body.dataset.mode = "dark"; else delete document.body.dataset.mode;
}
darkQuery.addEventListener?.("change", applyAppearance);

function previewGenre(genre) {
  state.previewTheme = genre ? (genre.theme || genre.key) : null;
  applyAppearance();
}

function applyLanguage() {
  setLang(store.settings.language);
  $$("[data-action=set-lang]").forEach(b => b.setAttribute("aria-pressed", b.dataset.lang === getLang()));
}

async function changeLanguage(lang) {
  if (lang === store.settings.language) return;
  try { await store.saveSettings({ language: lang }); } catch (err) { toast(err.message); }
  applyLanguage();
  renderAccount();
  route({ keepScroll: true });
}

/* =========================================================================
   ROTEAMENTO
   O índice salvo em history.state diz se há uma tela anterior DENTRO do app,
   para o botão "Voltar" não sair do site quando a pessoa abriu um link direto.
========================================================================= */
const ROUTES = { "": "home", acervo: "collection", explorar: "explore", busca: "search", livro: "details", ler: "reader", biblioteca: "library",
                 perfil: "profile", ajustes: "settings", "redefinir-senha": "account", "confirmar-email": "account" };
let navIdx = 0;

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, qs] = raw.split("?");
  const [head, arg] = path.split("/");
  return { view: ROUTES[head] || "home", arg, params: Object.fromEntries(new URLSearchParams(qs || "")) };
}

function syncHistoryIndex() {
  if (history.state && typeof history.state.idx === "number") navIdx = history.state.idx;
  else { navIdx += 1; history.replaceState({ idx: navIdx }, ""); }
}

export function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }
function back() { if (history.state && history.state.idx > 0) history.back(); else go("#/"); }

function route({ keepScroll = false } = {}) {
  const rt = parseHash();
  const prevView = state.route.view;
  state.route = rt;
  if (!["explore", "search"].includes(rt.view)) previewGenre(null);
  hidePop();
  if (rt.view !== "reader") closeNotes();

  $$(".view").forEach(v => v.classList.toggle("active", v.id === `view-${rt.view}`));
  const navKey = ["search", "details", "reader", "account"].includes(rt.view) ? null : rt.view;
  $$(".nav-item[data-nav]").forEach(el => {
    const on = el.dataset.nav === navKey;
    el.classList.toggle("active", on);
    on ? el.setAttribute("aria-current", "page") : el.removeAttribute("aria-current");
  });
  if (!keepScroll && (prevView !== rt.view || rt.view !== "reader")) window.scrollTo({ top: 0 });

  ({
    home: renderHome, collection: renderCollection, explore: renderExplore, search: () => renderSearch(rt.params),
    details: () => renderDetails(rt.params.key), reader: () => openReader(rt.arg, rt.params),
    library: renderLibrary, profile: renderProfile, settings: renderSettings,
    account: () => renderAccountAction(location.hash.replace(/^#\/?/, "").split("?")[0], rt.params.token),
  })[rt.view]();
}

/* =========================================================================
   CARDS
========================================================================= */
const cardFor = (b, opts = {}) => bookCardHTML(b, {
  isFavorite: store.isFavorite(b.book_key), inLibrary: store.inLibrary(b.book_key), ...opts,
});

function catalogToCard(b) {
  return { book_key: `gutenberg:${b.gutenberg_id}`, title: b.title, author: b.author, cover_url: b.cover_url,
           gutenberg_id: b.gutenberg_id, in_catalog: true, source: b.private ? "meus-arquivos" : "acervo",
           language: b.language, private: !!b.private };
}

const readerHref = (gid, key, page, extra = {}) =>
  `#/ler/${gid}?${new URLSearchParams({ key, ...(page !== undefined ? { p: page } : {}), ...extra })}`;

/* =========================================================================
   INÍCIO
========================================================================= */
function renderHome() {
  $("#genre-scroll-home").innerHTML = GENRES.map(g =>
    `<button class="genre-chip" data-action="genre-search" data-genre="${g.key}">${esc(t("genre." + g.key))}</button>`).join("");
  updateExcerptCount();
  renderHomePersonal();
  if (state.lastIdentify) $("#identify-result").innerHTML = renderMatches(state.lastIdentify.res, state.lastIdentify.query);

  if (state.catalog) paintCatalogRow(); else loadCatalogRow();
  if (state.classics) paintClassics(); else loadClassics();
}

async function loadCatalogRow() {
  $("#catalog-books").innerHTML = skeletonRow(4);
  try { state.catalog = await api.catalog(); paintCatalogRow(); }
  catch (err) { $("#catalog-books").innerHTML = `<div class="notice">${esc(err.message)}</div>`; }
}

const HOME_CATALOG_MAX = 12;
const LOCAL_ID_START = 5_000_000;   // livros da Wikisource e arquivos enviados (não existem no Gutenberg)

// Na página inicial: até 12 livros, primeiro os do idioma do site; o resto fica em "Ver todos".
function paintCatalogRow() {
  const books = state.catalog;
  const lang = store.settings.language;
  $("#catalog-note").textContent = !books.length ? "" : books.length === 1 ? t("home.catalogNote1") : t("home.catalogNote", { n: books.length });
  const all = $("#catalog-all");
  all.hidden = books.length <= HOME_CATALOG_MAX;
  all.textContent = t("coll.seeAll");
  const shown = [...books].sort((a, b) => (b.language === lang) - (a.language === lang)).slice(0, HOME_CATALOG_MAX);
  $("#catalog-books").innerHTML = books.length
    ? shown.map(b => cardFor(catalogToCard(b))).join("")
    : `<div class="notice">${t("home.catalogEmpty")}</div>`;
}

/* ---------------------------------------------------------------- acervo completo */
const fold = (s) => (s || "").normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
const collection = { lang: "", text: "" };

async function renderCollection() {
  $$("#collection-lang .filter-chip").forEach(c => {
    const on = c.dataset.lang === collection.lang;
    c.classList.toggle("active", on);
    c.setAttribute("aria-pressed", on);
  });
  $("#collection-filter").value = collection.text;
  if (state.route.params.add) setTimeout(() => $(".add-books")?.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
  if (!state.catalog) {
    $("#collection-books").innerHTML = skeletonRow(10);
    try { state.catalog = await api.catalog(); }
    catch (err) { $("#collection-books").innerHTML = `<div class="notice">${esc(err.message)}</div>`; return; }
  }
  paintCollection();
}

function paintCollection() {
  const books = state.catalog || [];
  const words = fold(collection.text).split(/\s+/).filter(Boolean);
  const list = books.filter(b =>
    (!collection.lang || b.language === collection.lang) &&
    words.every(w => fold(`${b.title} ${b.author}`).includes(w)));
  $("#collection-count").textContent = t("coll.count", { n: list.length, total: books.length });
  $("#collection-books").innerHTML = !books.length
    ? `<div class="notice">${t("home.catalogEmpty")}</div>`
    : list.length ? list.map(b => cardFor(catalogToCard(b))).join("") : emptyState(t("coll.none"));
}

async function loadClassics() {
  $("#popular-books").innerHTML = skeletonRow(6);
  try { state.classics = await api.search("classic literature", { limit: 12 }); paintClassics(); }
  catch (err) { $("#popular-books").innerHTML = `<div class="notice">${esc(err.message)}</div>`; }
}

function paintClassics() {
  $("#popular-books").innerHTML = state.classics.length
    ? state.classics.map(b => cardFor(b)).join("") : `<div class="notice">${esc(t("home.noSuggestions"))}</div>`;
}

/** Área pessoal da tela inicial: continuar lendo, favoritos, biblioteca e destaques. */
function renderHomePersonal() {
  const cont = store.list("continuar").slice(0, 3);
  const favs = store.list("favoritos").slice(0, 12);
  const lib = store.list("biblioteca").slice(0, 12);
  const hls = store.highlights.slice(0, 3);
  let html = "";

  if (cont.length) {
    html += `<div class="section-title"><h2>${esc(t("home.continue"))}</h2></div>
      <div class="continue-list">${cont.map(b => `
        <div class="continue-card">
          ${coverHTML(b)}
          <div class="c-body">
            <div class="c-title">${esc(b.title)}</div>
            <div class="c-meta">${esc(t("home.pageOf", { p: b.page + 1, t: b.total_pages, pct: b.percent }))}</div>
            <div class="progress-bar-wrap"><div class="progress-bar-fill" style="width:${b.percent}%"></div></div>
          </div>
          <a class="btn small" href="${readerHref(b.gutenberg_id, b.book_key)}">${esc(t("home.continueBtn"))}</a>
        </div>`).join("")}</div>`;
  }

  html += `<div class="section-title"><h2>${esc(t("home.favorites"))}</h2>
      ${favs.length ? `<a class="link" href="#/biblioteca" data-action="lib-open" data-tab="favoritos">${esc(t("home.seeAll"))}</a>` : ""}</div>`;
  html += favs.length
    ? `<div class="book-row">${favs.map(b => cardFor(b)).join("")}</div>`
    : `<div class="empty-inline">${icon("heart")}<span>${esc(t("home.favoritesEmpty"))}</span></div>`;

  if (lib.length) {
    html += `<div class="section-title"><h2>${esc(t("home.fromLibrary"))}</h2>
        <a class="link" href="#/biblioteca" data-action="lib-open" data-tab="lendo">${esc(t("home.seeAll"))}</a></div>
      <div class="book-row">${lib.map(b => cardFor(b, { statusLabel: t("status." + b.status) })).join("")}</div>`;
  }

  if (hls.length) {
    html += `<div class="section-title"><h2>${esc(t("home.highlights"))}</h2>
        <a class="link" href="#/biblioteca" data-action="lib-open" data-tab="anotacoes">${esc(t("home.seeAll"))}</a></div>
      <div class="quote-list">${hls.map(h => `
        <a class="quote-card c-${h.color}" href="${readerHref(h.gutenberg_id, h.book_key, h.page, { hl: h.id })}">
          <span class="q-text">${esc(h.text.length > 220 ? h.text.slice(0, 220) + "…" : h.text)}</span>
          <span class="q-meta">${esc(h.title)} · ${esc(t("notes.page", { p: h.page + 1 }))}</span>
        </a>`).join("")}</div>`;
  }
  $("#home-personal").innerHTML = html;
}

/* =========================================================================
   IDENTIFICAR TRECHO (o "Shazam")
========================================================================= */
function updateExcerptCount() {
  const n = letters($("#identify-input").value);
  const el = $("#identify-count");
  if (n === 0) { el.textContent = t("home.minLetters", { n: MIN_EXCERPT }); el.classList.remove("ok"); }
  else if (n < MIN_EXCERPT) { el.textContent = t("home.missingLetters", { n: MIN_EXCERPT - n }); el.classList.remove("ok"); }
  else { el.textContent = t("home.ready"); el.classList.add("ok"); }
}

async function identify(text) {
  if (state.identifying) return;   // Ctrl+Enter repetido não dispara requisições em paralelo
  const excerpt = text.trim();
  if (letters(excerpt) < MIN_EXCERPT) { toast(t("identify.minToast", { n: MIN_EXCERPT })); return; }
  const target = $("#identify-result");
  const btn = $("#identify-btn");
  state.identifying = true;
  btn.disabled = true;
  target.innerHTML = loading(t("identify.searching"));
  try {
    const res = await api.identify(excerpt);
    state.lastIdentify = { res, query: excerpt, ext: null };
    target.innerHTML = renderMatches(res, excerpt);
    target.scrollIntoView({ behavior: "smooth", block: "nearest" });
    // Não achou no acervo (ou achou com pouca confiança): procura sozinho em outras fontes.
    if (!res.matches.length || res.message === "low_confidence") runExternal(excerpt);
  } catch (err) {
    state.lastIdentify = null;
    target.innerHTML = `<div class="notice">${esc(err.message)}</div>`;
  } finally {
    state.identifying = false;
    btn.disabled = false;
  }
}

function renderMatches(res, query) {
  return renderLocalMatches(res, query) + `<div id="external-result" class="external" aria-live="polite">${externalHTML(query)}</div>`;
}

function renderLocalMatches(res, query) {
  if (!res.catalog_size) {
    return `<div class="notice"><strong>${esc(t("identify.emptyCatalog"))}</strong> ${esc(t("identify.emptyCatalogText"))}</div>`;
  }
  if (!res.matches.length) {
    return `<div class="notice"><strong>${esc(t("identify.noneTitle", { n: res.catalog_size }))}</strong>
      ${esc(t("identify.noneText"))}
      <div><button class="btn secondary small" data-action="search-fallback">${icon("search")}${esc(t("identify.searchWords"))}</button></div></div>`;
  }
  const [top, ...others] = res.matches;
  const book = catalogToCard(top.book);
  const bid = register({ ...book, _highlight: top.excerpt });
  const pct = Math.round(top.score * 100);
  const fav = store.isFavorite(book.book_key);
  return `
    <article class="match">
      <a href="${detailsHref(book.book_key)}">${coverHTML(book)}</a>
      <div class="match-body">
        <h2>${esc(top.book.title)}</h2>
        <p class="match-author">${esc(top.book.author)}</p>
        <div class="meter" title="${esc(t("identify.similarityTitle"))}">
          <div class="meter-track"><div class="meter-fill" style="width:${pct}%"></div></div>
          <span><strong>${esc(t("identify.conf." + top.confidence))}</strong> · ${esc(t("identify.similarity", { pct }))}</span>
        </div>
        <blockquote class="quote">${highlight(top.excerpt, query)}</blockquote>
        <div class="match-actions">
          <button class="btn" data-action="read-at" data-bid="${bid}" data-page="${top.page}">${icon("open")}${esc(t("identify.readFrom"))}</button>
          <button class="btn ${fav ? "is-on" : "ghost"}" data-action="fav" data-bid="${bid}" aria-pressed="${fav}">${icon("heart")}${esc(t(fav ? "book.favorited" : "book.favorite"))}</button>
        </div>
      </div>
    </article>
    ${others.length ? `<div class="others"><h3>${esc(t("identify.others"))}</h3>
      ${others.map(o => `<a href="${detailsHref(`gutenberg:${o.book.gutenberg_id}`)}"><span>${esc(o.book.title)} — ${esc(o.book.author)}</span><span>${Math.round(o.score * 100)}%</span></a>`).join("")}
    </div>` : ""}`;
}


/* ------------------------------------------------ outras fontes (livros modernos ou pagos) */
let extCtrl = null;

function externalHTML(query) {
  const li = state.lastIdentify;
  if (li?.ext === "loading") return loading(t("ext.searching"));
  if (li?.ext?.error) return `<div class="notice">${esc(li.ext.error)}</div>`;
  if (li?.ext) return renderExternal(li.ext, query);
  // Achou no acervo com boa confiança: a busca externa fica a um clique.
  return li?.res?.matches?.length
    ? `<p class="ext-more">${esc(t("ext.notThis"))} <button class="link" data-action="search-external">${esc(t("ext.btn"))}</button></p>` : "";
}

async function runExternal(excerpt) {
  const li = state.lastIdentify;
  if (!li) return;
  extCtrl?.abort();
  const ctrl = extCtrl = new AbortController();
  li.ext = "loading";
  paintExternal();
  try {
    const res = await api.identifyExternal(excerpt, ctrl.signal);
    if (state.lastIdentify !== li) return;      // outro trecho foi identificado enquanto esperava
    li.ext = res;
  } catch (err) {
    if (err.name === "AbortError" || state.lastIdentify !== li) return;
    li.ext = { error: err.message };
  }
  paintExternal();
}

function paintExternal() {
  const el = $("#external-result");
  if (el && state.lastIdentify) el.innerHTML = externalHTML(state.lastIdentify.query);
}

function renderExternal(res, query) {
  const failed = (res.failed || []).map(f => `<p class="hint">${esc(t("ext.failed." + f))}</p>`).join("");
  if (!res.results.length) {
    return `<div class="notice ext-none"><strong>${esc(t("ext.none"))}</strong> ${esc(t("ext.noneText"))}
      <div class="ext-none-actions">
        <button class="btn secondary small" data-action="open-upload">${icon("upload")}${esc(t("add.fileBtn"))}</button>
        <a class="btn secondary small" href="#/acervo?add=1">${icon("plus")}${esc(t("add.wsTitle"))}</a>
      </div>${failed}</div>`;
  }
  const linkBtn = (l) => `<a class="btn small ${l.kind === "buy" ? "secondary" : "ghost"}" href="${esc(/^https?:\/\//i.test(l.url) ? l.url : "#")}" target="_blank" rel="noopener noreferrer">
      ${icon(l.kind === "buy" ? "cart" : l.kind === "info" ? "external" : "open")}${esc(l.label)}</a>`;
  return `<section class="ext-section">
    <h2>${esc(t("ext.title"))}</h2>
    <p class="hint">${esc(t("ext.note"))}</p>
    ${res.results.map(b => {
      const buy = b.links.filter(l => l.kind === "buy");
      const read = b.links.filter(l => l.kind !== "buy");
      return `<article class="ext-card">
        ${coverHTML({ title: b.title, author: b.author, cover_url: b.cover_url }, "small")}
        <div class="ext-body">
          <h3>${esc(b.title)}</h3>
          <p class="match-author">${esc(b.author || t("book.unknownAuthor"))}${b.year ? ` · ${esc(b.year)}` : ""}</p>
          <p class="ext-tags"><span class="tag">${esc(t("ext.src." + b.source))}</span>${b.public_domain ? `<span class="tag free">${esc(t("ext.publicDomain"))}</span>` : ""}</p>
          ${b.snippet ? `<blockquote class="quote small">…${highlight(b.snippet, query)}…</blockquote>` : ""}
          ${buy.length ? `<div class="ext-links"><span class="ext-label">${esc(t("ext.buy"))}</span>${buy.map(linkBtn).join("")}</div>` : ""}
          ${read.length ? `<div class="ext-links"><span class="ext-label">${esc(t("ext.borrow"))}</span>${read.map(linkBtn).join("")}</div>` : ""}
          ${b.catalog_gutenberg_id ? `<div class="ext-links"><a class="btn small" href="${detailsHref(`gutenberg:${b.catalog_gutenberg_id}`)}">${icon("check")}${esc(t("ext.inFreadom"))}</a></div>` : ""}
          <div class="ext-actions">
            <button class="link" data-action="open-upload" data-title="${esc(b.title)}" data-author="${esc(b.author)}">${icon("upload")}${esc(t("ext.haveFile"))}</button>
            ${b.public_domain && !b.catalog_gutenberg_id ? `<a class="link" href="${searchHash(b.title, "text")}">${icon("search")}${esc(t("ext.findFree"))}</a>` : ""}
          </div>
        </div>
      </article>`;
    }).join("")}
    ${failed}
  </section>`;
}

/* ------------------------------------------------ enviar arquivo (livro particular) */
function openUpload({ title = "", author = "" } = {}) {
  if (store.isGuest) {
    toast(t("up.loginNeeded"));
    setAuthMode("login");
    openModal("modal-auth");
    return;
  }
  const form = $("#upload-form");
  form.reset();
  $("#up-title").value = title;
  $("#up-author").value = author;
  $("#up-file-name").textContent = t("up.choose");
  $("#up-error").textContent = "";
  openModal("modal-upload");
}

const UPLOAD_EXT = /\.(txt|epub|pdf)$/i;
async function submitUpload(e) {
  e.preventDefault();
  const form = e.target;
  const file = $("#up-file").files[0];
  const errEl = $("#up-error");
  errEl.textContent = "";
  if (!file) { errEl.textContent = t("up.pickFile"); return; }
  if (!UPLOAD_EXT.test(file.name)) { errEl.textContent = t("up.badExt"); return; }
  if (file.size > 20 * 1024 * 1024) { errEl.textContent = t("up.tooBig"); return; }
  const btn = $("#up-submit");
  const label = $("span", btn);
  btn.disabled = true;
  label.textContent = t("up.sending");
  try {
    const book = await api.uploadBook(new FormData(form));
    closeModal("modal-upload");
    toast(t("up.done", { title: book.title }), "check");
    state.catalog = null;
    state.uploads = null;
    go(detailsHref(`gutenberg:${book.gutenberg_id}`));
  } catch (err) {
    errEl.textContent = err.message;
  } finally {
    btn.disabled = false;
    label.textContent = t("up.submit");
  }
}

async function loadUploads() {
  try { state.uploads = await api.uploads(); }
  catch (err) { state.uploads = { error: err.message }; }
  if (state.route.view === "library") renderLibrary();
}

async function deleteUpload(gid, title) {
  if (!confirm(t("lib.deleteConfirm", { title }))) return;
  try {
    await api.deleteUpload(gid);
    toast(t("lib.fileDeleted"));
    state.catalog = null;
    state.uploads = null;
    await store.loadServer().catch(() => {});
    store.emit("all");
    renderLibrary();
  } catch (err) { toast(err.message); }
}

/* ------------------------------------------------ Wikisource (acervo) */
const wsJobs = new Map();   // título -> estado da importação, para redesenhar a lista

async function searchWikisource(q, lang) {
  const box = $("#ws-results");
  box.innerHTML = loading(t("ws.searching"));
  try {
    const results = await api.wikisourceSearch(q, lang);
    box.dataset.lang = lang;
    state.wsResults = results;
    paintWikisource();
  } catch (err) { box.innerHTML = `<div class="notice">${esc(err.message)}</div>`; }
}

function paintWikisource() {
  const box = $("#ws-results");
  const results = state.wsResults || [];
  if (!results.length) { box.innerHTML = `<p class="hint">${esc(t("ws.none"))}</p>`; return; }
  box.innerHTML = `<ul class="ws-list">${results.map(w => {
    const job = wsJobs.get(`${w.lang}:${w.title}`);
    const gid = w.gutenberg_id || job?.book?.gutenberg_id;
    let action;
    if (gid) action = `<a class="btn small secondary" href="${detailsHref(`gutenberg:${gid}`)}">${icon("check")}${esc(t("ws.open"))}</a>`;
    else if (job && (job.status === "queued" || job.status === "running"))
      action = `<span class="ws-status">${esc(job.total ? t("ws.importing", { d: job.done, t: job.total }) : t("ws.queued"))}</span>`;
    else action = `<button class="btn small" data-action="ws-import" data-title="${esc(w.title)}" data-lang="${esc(w.lang)}">${icon("plus")}${esc(t("ws.add"))}</button>`;
    const err = job?.status === "error" ? `<p class="form-error">${esc(t("ws.err." + job.error))}</p>` : "";
    return `<li><div><a href="${esc(w.url)}" target="_blank" rel="noopener noreferrer"><strong>${esc(w.title)}</strong></a>
        ${w.is_chapter ? `<span class="tag">${esc(t("ws.chapter"))}</span>` : ""}
        ${w.snippet ? `<small>${esc(w.snippet)}…</small>` : ""}${err}</div>${action}</li>`;
  }).join("")}</ul>`;
}

async function importWikisource(lang, title) {
  if (store.isGuest) { toast(t("ws.loginNeeded")); setAuthMode("login"); openModal("modal-auth"); return; }
  const key = `${lang}:${title}`;
  try {
    let job = await api.wikisourceImport(lang, title);
    wsJobs.set(key, job);
    paintWikisource();
    while (job.status === "queued" || job.status === "running") {
      await new Promise(res => setTimeout(res, 1500));
      job = await api.wikisourceJob(job.id);
      wsJobs.set(key, job);
      if (state.route.view === "collection") paintWikisource();
    }
    if (job.status === "done") {
      toast(t("ws.done", { title: job.book.title }), "check");
      state.catalog = null;
      if (state.route.view === "collection") renderCollection();
    }
  } catch (err) {
    wsJobs.set(key, { status: "error", error: "unavailable" });
    toast(err.message);
  }
  if (state.route.view === "collection") paintWikisource();
}

/* =========================================================================
   EXPLORAR / BUSCA
========================================================================= */
let exploreSeq = 0;
let exploreGenreKey = null;
function renderExplore() {
  $("#genre-scroll-explore").innerHTML = GENRES.map(g =>
    `<button class="genre-chip ${g.key === exploreGenreKey ? "active" : ""}" data-action="genre-explore" data-genre="${g.key}">${esc(t("genre." + g.key))}</button>`).join("");
  if (exploreGenreKey) exploreGenre(GENRES.find(g => g.key === exploreGenreKey));
  else $("#explore-results").innerHTML = emptyState(t("explore.pick"), t("explore.pickText"));
}

async function exploreGenre(genre) {
  exploreGenreKey = genre.key;
  const seq = ++exploreSeq;
  $$("#genre-scroll-explore .genre-chip").forEach(c => c.classList.toggle("active", c.dataset.genre === genre.key));
  previewGenre(genre);
  const el = $("#explore-results");
  el.innerHTML = skeletonRow(10);
  try {
    const results = await api.search(genre.query, { limit: 24 });
    if (seq !== exploreSeq) return; // outro gênero foi clicado enquanto carregava
    el.innerHTML = results.length ? results.map(b => cardFor(b)).join("") : emptyState(t("explore.none"));
  } catch (err) {
    if (seq === exploreSeq) el.innerHTML = `<div class="notice">${esc(err.message)}</div>`;
  }
}

let searchCtrl = null;
async function renderSearch({ q = "", mode = "text", genre, lang = "" }) {
  const input = $("#search-input");
  input.value = q;
  $$("#search-lang-filter .filter-chip").forEach(c => {
    const on = c.dataset.lang === lang;
    c.classList.toggle("active", on);
    c.setAttribute("aria-pressed", on);
  });
  const titles = { text: t("search.resultsFor", { q }), phrase: t("search.phrase"), description: t("search.description") };
  $("#search-title").textContent = !q ? "" : genre ? t("genre." + genre) : (titles[mode] || titles.text);
  if (genre) previewGenre(GENRES.find(g => g.key === genre));

  const el = $("#search-results");
  searchCtrl?.abort();
  if (!q) { el.innerHTML = emptyState(t("search.typeSomething")); setTimeout(() => input.focus(), 50); return; }
  el.innerHTML = skeletonRow(10);
  searchCtrl = new AbortController();
  try {
    const results = await api.search(q, { mode, limit: 24, language: lang || undefined, signal: searchCtrl.signal });
    el.innerHTML = results.length ? results.map(b => cardFor(b)).join("")
      : emptyState(t("search.none"), lang ? t("search.noneLang") : mode === "text" ? t("search.noneText") : t("search.noneDesc"));
  } catch (err) {
    if (err.name !== "AbortError") el.innerHTML = `<div class="notice">${esc(err.message)}</div>`;
  }
}

function searchHash(q, mode = "text", extra = {}) {
  const params = { q, mode, ...extra };
  Object.keys(params).forEach(k => !params[k] && delete params[k]);
  return `#/busca?${new URLSearchParams(params)}`;
}

/* =========================================================================
   DETALHES
========================================================================= */
let detailsSeq = 0;
async function renderDetails(key) {
  const el = $("#details-content");
  if (!key) { el.innerHTML = emptyState(t("book.notFound")); return; }
  const seq = ++detailsSeq;
  state.currentBook = null; // evita repintar o livro anterior enquanto este carrega
  el.innerHTML = loading(t("book.loading"));
  try {
    const book = await api.details(key);
    if (seq !== detailsSeq) return;
    state.currentBook = { ...book, gutenberg_id: book.free_version?.gutenberg_id || null };
    paintDetails();
  } catch (err) {
    if (seq === detailsSeq) el.innerHTML = `<div class="notice">${esc(err.message)}</div>`;
  }
}

function paintDetails() {
  const book = state.currentBook;
  if (!book || state.route.view !== "details" || book.book_key !== state.route.params.key) return;
  const saved = store.get(book.book_key);
  const fav = store.isFavorite(book.book_key);
  const bid = register(book);
  const free = book.free_version;
  const status = saved?.status;
  const bookLang = (free?.languages || [])[0] || "";
  const translatable = free && bookLang && bookLang !== store.settings.language && store.settings.translate_books;
  $("#details-content").innerHTML = `
    <div class="detail">
      ${coverHTML(book)}
      <div>
        <h1>${esc(book.title)}</h1>
        <p class="d-author">${esc(book.author || t("book.unknownAuthor"))}</p>
        <div class="detail-meta">
          ${book.private ? `<span class="tag private" title="${esc(t("book.yourFileTitle"))}">${icon("file")}${esc(t("book.yourFile"))}</span>` : ""}
          ${book.in_catalog ? `<span class="tag catalog">${icon("check")}${esc(t("book.recognized"))}</span>` : ""}
          ${book.source === "wikisource" && book.source_url ? `<a class="meta-chip" href="${esc(book.source_url)}" target="_blank" rel="noopener noreferrer">${icon("external")}${esc(t("book.fromWikisource"))}</a>` : ""}
          ${free && !book.private ? `<span class="tag free">${esc(t("book.freeReading"))}</span>` : ""}
          ${bookLang === "pt" || bookLang === "en" ? `<span class="meta-chip">${esc(t("book.language." + bookLang))}</span>` : ""}
          ${translatable ? `<span class="meta-chip">${icon("translate")}${esc(t("reader.translateTo", { lang: t("lang.name." + store.settings.language) }))}</span>` : ""}
          ${book.year ? `<span class="meta-chip">${esc(String(book.year))}</span>` : ""}
          ${(book.subjects || []).slice(0, 4).map(s => `<span class="meta-chip">${esc(s)}</span>`).join("")}
        </div>
        <p class="detail-desc">${esc((book.description || t("book.noDescription")).slice(0, 900))}</p>
        <div class="detail-actions">
          ${free
            ? `<a class="btn" href="${readerHref(free.gutenberg_id, book.book_key)}">${icon("open")}${esc(saved?.last_read && saved.gutenberg_id === free.gutenberg_id ? t("book.continueAt", { p: saved.page + 1 }) : t("book.readNow"))}</a>`
            : `<span class="unavailable">${esc(t("book.noFree"))}</span>`}
          <button class="btn ${fav ? "is-on" : "ghost"}" data-action="fav" data-bid="${bid}" aria-pressed="${fav}">${icon("heart")}${esc(t(fav ? "book.favorited" : "book.favorite"))}</button>
        </div>
        ${free ? "" : whereToGetHTML(book)}
        <div class="detail-actions" style="margin-top:14px">
          <div class="status-select" role="group" aria-label="${esc(t("book.statusGroup"))}">
            ${STATUSES.map(k =>
              `<button class="${status === k ? "active" : ""}" data-action="set-status" data-status="${k}" data-bid="${bid}" aria-pressed="${status === k}">${esc(t("status." + k))}</button>`).join("")}
          </div>
          ${status ? `<button class="link" data-action="remove-lib" data-key="${esc(book.book_key)}">${esc(t("book.removeLib"))}</button>` : ""}
        </div>
        ${annotationsBlock(book.book_key, t("book.yourNotes"))}
      </div>
    </div>`;
}

/** Livro sem versão gratuita (moderno/pago): onde comprar ou emprestar, ou adicionar o próprio arquivo. */
function whereToGetHTML(book) {
  const q = encodeURIComponent(`${book.title} ${(book.author || "").split(",")[0]}`.trim());
  const stores = [
    ["buy", "Amazon", `https://www.amazon.com.br/s?k=${q}&i=stripbooks`],
    ["buy", "Estante Virtual (usados)", `https://www.estantevirtual.com.br/busca?q=${q}`],
    ["buy", "Google Play Livros", `https://play.google.com/store/search?q=${q}&c=books`],
    ["borrow", "Internet Archive (ler/emprestar)", `https://archive.org/search?query=${q}`],
  ];
  const btn = ([kind, label, url]) => `<a class="btn small ${kind === "buy" ? "secondary" : "ghost"}" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${icon(kind === "buy" ? "cart" : "open")}${esc(label)}</a>`;
  return `<section class="where-to-get" aria-labelledby="wtg-h">
    <h2 id="wtg-h">${esc(t("book.whereToGet"))}</h2>
    <div class="ext-links"><span class="ext-label">${esc(t("ext.buy"))}</span>${stores.filter(x => x[0] === "buy").map(btn).join("")}</div>
    <div class="ext-links"><span class="ext-label">${esc(t("ext.borrow"))}</span>${stores.filter(x => x[0] !== "buy").map(btn).join("")}</div>
    <div class="ext-actions"><button class="link" data-action="open-upload" data-title="${esc(book.title)}" data-author="${esc((book.author || "").split(",")[0])}">${icon("upload")}${esc(t("ext.haveFile"))}</button></div>
  </section>`;
}

/** Marcadores e destaques de um livro, como links para o leitor. */
function annotationsBlock(bookKey, heading) {
  const bms = store.bookmarksFor(bookKey);
  const hls = store.highlightsFor(bookKey).sort((a, b) => a.page - b.page || a.start - b.start);
  if (!bms.length && !hls.length) return "";
  return `<div class="ann-block">
    ${heading ? `<h2>${esc(heading)}</h2>` : ""}
    ${bms.length ? `<div class="bm-chips">${bms.map(b => `
      <a class="bm-chip" href="${readerHref(b.gutenberg_id, b.book_key, b.page)}" title="${esc(b.note)}">
        ${icon("bookmark")}${esc(t("notes.page", { p: b.page + 1 }))}${b.note ? `<small>${esc(b.note)}</small>` : ""}</a>`).join("")}</div>` : ""}
    ${hls.map(h => `
      <a class="quote-card c-${h.color}" href="${readerHref(h.gutenberg_id, h.book_key, h.page, { hl: h.id })}">
        <span class="q-text">${esc(h.text.length > 260 ? h.text.slice(0, 260) + "…" : h.text)}</span>
        <span class="q-meta">${esc(t("notes.page", { p: h.page + 1 }))}${h.version !== "original" ? " · " + esc(t("hl.onTranslation")) : ""}</span>
      </a>`).join("")}
  </div>`;
}

/* =========================================================================
   LEITOR
========================================================================= */
const wantLang = () => (r.translateOn ? store.settings.language : null);
const pageKey = (page, lang) => `${page}|${lang || ""}`;

async function openReader(gidRaw, params) {
  const gid = parseInt(gidRaw, 10);
  const pageEl = $("#reader-page");
  if (!gid || gid < 1) { pageEl.innerHTML = emptyState(t("reader.invalid")); return; }
  const key = params.key || `gutenberg:${gid}`;
  if (r.gid !== gid || r.key !== key) {
    Object.assign(r, { cache: new Map(), inflight: new Map(), total: 0, origLang: "", data: null, painted: false });
    r.translateOn = store.settings.translate_books;
    $("#reader-page-indicator").textContent = "";
    $("#reader-progress-label").textContent = "";
    $("#reader-progress-fill").style.width = "0";
    $("#reader-notice").innerHTML = "";
  }
  r.gid = gid; r.key = key;
  if (params.orig) r.translateOn = false;
  // Livro fora do acervo: o servidor vai indexá-lo agora; recarrega a lista na próxima visita.
  if (state.catalog && !state.catalog.some(b => b.gutenberg_id === gid)) state.catalog = null;

  // Ir direto para um destaque: usa a versão (original/tradução) em que ele foi feito.
  if (params.hl) {
    const h = store.getHighlight(params.hl);
    if (h) { r.translateOn = h.version !== "original"; r.flashHid = String(h.id); }
  }

  // Metadados do livro (título/autor) — do estado, da biblioteca ou da API.
  let meta = state.currentBook?.book_key === key ? state.currentBook : store.get(key);
  if (!meta) { try { meta = await api.details(key); } catch { meta = { book_key: key, title: `Gutenberg #${gid}`, author: "" }; } }
  if (r.gid !== gid) return; // a pessoa já abriu outro livro
  state.currentBook = { ...meta, book_key: key, gutenberg_id: gid };
  $("#reader-title").textContent = meta.title;
  $("#reader-author").textContent = (meta.author || "").split(",")[0];

  const saved = store.get(key);
  const start = params.p !== undefined ? parseInt(params.p, 10) : (saved?.gutenberg_id === gid ? saved.page : 0);
  await showReaderPage(Number.isFinite(start) && start >= 0 ? start : 0);
}

function fetchPage(page, lang) {
  const k = pageKey(page, lang);
  if (r.cache.has(k)) return Promise.resolve(r.cache.get(k));
  if (r.inflight.has(k)) return r.inflight.get(k);
  const gid = r.gid;
  const p = api.readerPage(gid, page, lang).then(data => {
    if (r.gid === gid) {
      r.cache.set(k, data);
      r.cache.set(pageKey(data.page, lang), data);
      r.total = data.total_pages;
      r.origLang = data.original_language || r.origLang;
    }
    return data;
  }).finally(() => r.inflight.delete(k));
  r.inflight.set(k, p);
  return p;
}

async function showReaderPage(page) {
  const seq = ++r.seq;
  const pageEl = $("#reader-page");
  hidePop();
  applyReaderSettings();
  const lang = wantLang();
  if (!r.cache.has(pageKey(page, lang))) {
    const translating = lang && r.origLang && r.origLang !== lang;
    pageEl.innerHTML = loading(t(translating ? "reader.translating" : "reader.loading"));
    r.painted = false;
  }
  try {
    const data = await fetchPage(page, lang);
    if (seq !== r.seq) return;   // resposta atrasada de um clique anterior
    r.page = data.page;
    r.data = data;
    paintReaderPage();
    updateReaderChrome();
    history.replaceState(history.state, "", readerHref(r.gid, r.key, r.page));
    if (data.page + 1 < r.total) fetchPage(data.page + 1, lang).catch(() => {}); // pré-carrega (e pré-traduz) a próxima
    const saved = await store.saveProgress(state.currentBook, r.page, r.total);
    if (seq !== r.seq) return;
    $("#reader-progress-fill").style.width = saved.percent + "%";
    $("#reader-progress-label").textContent = t("reader.done", { pct: saved.percent });
  } catch (err) {
    if (seq !== r.seq) return;
    r.painted = false;
    pageEl.innerHTML = `<div class="notice"><strong>${esc(t("reader.cantOpen"))}</strong> ${esc(err.message)}
      ${r.gid < LOCAL_ID_START ? `<div><a class="btn secondary small" href="https://www.gutenberg.org/ebooks/${r.gid}" target="_blank" rel="noopener">${esc(t("reader.openGutenberg"))}</a></div>` : ""}</div>`;
  }
}

const currentVersion = () => (r.data && r.data.translated ? r.data.language : "original");

/** Encontra o destaque no texto: pela posição salva ou, se o texto mudou, pelo conteúdo. */
function locate(content, h) {
  if (content.slice(h.start, h.end) === h.text) return { start: h.start, end: h.end };
  const i = content.indexOf(h.text);
  return i >= 0 ? { start: i, end: i + h.text.length } : null;
}

function renderRanges(content, ranges, from = 0, to = content.length) {
  let out = "", pos = from;
  for (const rg of ranges) {
    const s = Math.max(rg.start, from), e = Math.min(rg.end, to);
    if (e <= s || s < pos) continue;
    out += esc(content.slice(pos, s)) + `<mark class="${rg.cls}" ${rg.attrs}>${esc(content.slice(s, e))}</mark>`;
    pos = e;
  }
  return out + esc(content.slice(pos, to));
}

// Folha de rosto ("DOM CASMURRO / POR / MACHADO DE ASSIS") e títulos de capítulo ("I / Do titulo.")
// ficam centralizados. Só embrulha o bloco num <span>: o texto continua idêntico,
// então os deslocamentos dos sublinhados não mudam.
const CHAPTER_RE = /^(?:[IVXLCDM]+|\d+|(?:cap[ií]tulo|chapter|livro|book|parte|part)\b.*)\.?$/i;
function blockKind(block) {
  const lines = block.split("\n");
  if (!lines[0] || lines.some(l => l.length > 80)) return "";
  const first = lines[0].trim();
  if (!/\p{L}/u.test(first) || /\p{Ll}/u.test(first)) return "";
  return CHAPTER_RE.test(first) ? "rd-chapter" : "rd-title";
}

function renderPage(content, ranges) {
  // Remove sobreposições uma vez (mantém o primeiro), depois desenha bloco a bloco.
  ranges.sort((a, b) => a.start - b.start);
  const clean = [];
  for (const rg of ranges) if (!clean.length || rg.start >= clean[clean.length - 1].end) clean.push(rg);
  let out = "", pos = 0;
  const sep = /\n\n/g;
  for (;;) {
    const m = sep.exec(content);
    const end = m ? m.index : content.length;
    const kind = blockKind(content.slice(pos, end));
    const inner = renderRanges(content, clean, pos, end);
    out += kind ? `<span class="${kind}">${inner}</span>` : inner;
    if (!m) break;
    out += renderRanges(content, clean, end, end + 2);
    pos = end + 2;
  }
  return out;
}

function paintReaderPage() {
  const pageEl = $("#reader-page");
  const data = r.data;
  if (!data) return;
  const content = data.content;
  const version = currentVersion();
  const ranges = [];
  for (const h of store.highlightsFor(r.key, data.page, version)) {
    const pos = locate(content, h);
    if (pos) ranges.push({ ...pos, cls: `uh c-${h.color}`, attrs: `data-hid="${esc(String(h.id))}" tabindex="0"` });
  }
  let targetFound = false;
  if (r.target && version === "original") {
    const i = content.indexOf(r.target);
    if (i >= 0 && !ranges.some(rg => rg.start < i + r.target.length && i < rg.end)) {
      ranges.push({ start: i, end: i + r.target.length, cls: "hl", attrs: 'id="reader-hl"' });
      targetFound = true;
    }
  }
  pageEl.innerHTML = renderPage(content, ranges);
  r.painted = true;
  r.target = null; // vale só para a página aberta pela identificação
  if (targetFound) {
    setTimeout(() => $("#reader-hl")?.scrollIntoView({ behavior: "smooth", block: "center" }), 60);
  }
  if (r.flashHid) {
    const mark = pageEl.querySelector(`mark[data-hid="${CSS.escape(r.flashHid)}"]`);
    r.flashHid = null;
    if (mark) {
      mark.classList.add("flash");
      setTimeout(() => mark.scrollIntoView({ behavior: "smooth", block: "center" }), 60);
      setTimeout(() => mark.classList.remove("flash"), 2200);
    }
  }
}

function updateReaderChrome() {
  const data = r.data;
  if (!data) return;
  $("#reader-page-indicator").textContent = t("reader.pageOf", { p: r.page + 1, t: r.total });
  $$("[data-action=reader-prev]").forEach(b => (b.disabled = r.page === 0));
  $$("[data-action=reader-next]").forEach(b => (b.disabled = r.page >= r.total - 1));

  const marked = !!store.bookmarkAt(r.key, r.page);
  const bmBtn = $("#reader-bookmark-btn");
  bmBtn.classList.toggle("active", marked);
  bmBtn.setAttribute("aria-pressed", marked);
  bmBtn.setAttribute("aria-label", t(marked ? "reader.unbookmark" : "reader.bookmark"));
  bmBtn.title = t(marked ? "reader.unbookmark" : "reader.bookmark");

  const n = store.bookmarksFor(r.key).length + store.highlightsFor(r.key).length;
  const badge = $("#reader-notes-count");
  badge.hidden = !n;
  badge.textContent = n;

  const uiLang = store.settings.language;
  const canTranslate = !!r.origLang && r.origLang !== uiLang;
  const trBtn = $("#reader-translate-btn");
  trBtn.hidden = !canTranslate;
  trBtn.classList.toggle("active", !!data.translated);
  trBtn.setAttribute("aria-pressed", !!data.translated);
  $("span", trBtn).textContent = t(data.translated ? "reader.original" : "reader.translate");
  trBtn.title = data.translated ? t("reader.showOriginal") : t("reader.translateTo", { lang: t("lang.name." + uiLang) });

  const notice = $("#reader-notice");
  if (data.translation_error) {
    notice.innerHTML = `<div class="reader-note warn">${esc(data.translation_error)}</div>`;
  } else if (data.translated) {
    notice.innerHTML = `<div class="reader-note">${icon("translate")}<span>${esc(t("reader.translatedFrom", { lang: t("lang.from." + r.origLang) }))}</span>
      <button class="link" data-action="reader-translate">${esc(t("reader.showOriginal"))}</button></div>`;
  } else notice.innerHTML = "";

  if (!$("#notes-drawer").hidden) renderNotes();
}

function applyReaderSettings() {
  const s = store.settings, el = $("#reader-page");
  el.style.fontSize = s.font_size + "px";
  el.style.lineHeight = s.spacing;
  el.classList.toggle("narrow", s.text_width === "estreito");
  el.classList.toggle("wide", s.text_width === "largo");
  el.classList.toggle("paper-sepia", r.paper === "sepia");
  el.classList.toggle("paper-night", r.paper === "noite");
  el.lang = r.data?.language || "";
  $("#reader-paper-btn").textContent = t("reader.paper." + r.paper);
}

function readerStep(delta) {
  const next = r.page + delta;
  if (next < 0 || next >= r.total) return;
  showReaderPage(next).then(() => window.scrollTo({ top: 0, behavior: "smooth" }));
}

/* ------------------------------------------------ painel de marcadores e destaques */
function openNotes() { $("#notes-drawer").hidden = false; document.body.classList.add("notes-open"); renderNotes(); $("#notes-drawer .modal-close").focus(); }
function closeNotes() { $("#notes-drawer").hidden = true; document.body.classList.remove("notes-open"); }

function renderNotes() {
  const bms = store.bookmarksFor(r.key);
  const hls = store.highlightsFor(r.key).sort((a, b) => a.page - b.page || a.start - b.start);
  $("#notes-body").innerHTML = `
    <h3>${esc(t("notes.bookmarks"))} <span class="count">${bms.length || ""}</span></h3>
    ${bms.length ? `<ul class="note-list">${bms.map(b => `
      <li class="bm-item ${b.page === r.page ? "current" : ""}">
        <button class="bm-go" data-action="goto-page" data-page="${b.page}" aria-label="${esc(t("notes.go", { p: b.page + 1 }))}">${icon("bookmark")}<span>${esc(t("notes.page", { p: b.page + 1 }))}</span></button>
        <input class="bm-note" data-bmid="${esc(String(b.id))}" value="${esc(b.note)}" maxlength="200" placeholder="${esc(t("notes.notePh"))}" aria-label="${esc(t("notes.notePh"))}">
        <button class="icon-btn" data-action="delete-bookmark" data-id="${esc(String(b.id))}" aria-label="${esc(t("notes.delete"))}" title="${esc(t("notes.delete"))}">${icon("trash")}</button>
      </li>`).join("")}</ul>` : `<p class="hint">${esc(t("notes.noBookmarks"))}</p>`}
    <h3>${esc(t("notes.highlights"))} <span class="count">${hls.length || ""}</span></h3>
    ${hls.length ? `<ul class="note-list">${hls.map(h => `
      <li class="hl-item c-${h.color}">
        <button class="hl-quote" data-action="goto-highlight" data-id="${esc(String(h.id))}">${esc(h.text.length > 240 ? h.text.slice(0, 240) + "…" : h.text)}</button>
        <div class="hl-meta"><span>${esc(t("notes.page", { p: h.page + 1 }))}${h.version !== "original" ? " · " + esc(t("hl.onTranslation")) : ""}</span>
          <button class="link" data-action="delete-highlight" data-id="${esc(String(h.id))}">${esc(t("notes.delete"))}</button></div>
      </li>`).join("")}</ul>` : `<p class="hint">${esc(t("notes.noHighlights"))}</p>`}`;
}

/* ------------------------------------------------ sublinhar (seleção de texto) */
const pop = { mode: null, sel: null, hid: null };

function selectionInfo() {
  if (!r.painted || !r.data) return null;
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  const pageEl = $("#reader-page");
  if (!pageEl.contains(range.startContainer) || !pageEl.contains(range.endContainer)) return null;
  const pre = document.createRange();
  pre.selectNodeContents(pageEl);
  pre.setEnd(range.startContainer, range.startOffset);
  let start = pre.toString().length;
  const raw = range.toString();
  const text = raw.trim();
  if (letters(text) < 2) return null;
  start += raw.length - raw.trimStart().length;
  const content = r.data.content;
  if (content.slice(start, start + text.length) !== text) {
    const i = content.indexOf(text);
    if (i < 0) return null;
    start = i;
  }
  return { start, end: start + text.length, text, rect: range.getBoundingClientRect() };
}

function showPop(rect, mode, activeColor) {
  const el = $("#hl-pop");
  pop.mode = mode;
  el.hidden = false;
  $(".pop-remove", el).hidden = mode !== "edit";
  $$(".swatch", el).forEach(s => s.classList.toggle("active", s.dataset.color === activeColor));
  const w = el.offsetWidth, h = el.offsetHeight;
  // Abaixo da seleção (no celular, o menu nativo de copiar fica em cima).
  let top = rect.bottom + 10;
  if (top + h > window.innerHeight - 84) top = Math.max(8, rect.top - h - 10);
  const left = Math.min(Math.max(8, rect.left + rect.width / 2 - w / 2), window.innerWidth - w - 8);
  el.style.top = `${top}px`;
  el.style.left = `${left}px`;
}

function hidePop() {
  const el = $("#hl-pop");
  if (el) el.hidden = true;
  pop.mode = pop.sel = pop.hid = null;
}

let selTimer = null;
document.addEventListener("selectionchange", () => {
  if (state.route.view !== "reader") return;
  clearTimeout(selTimer);
  selTimer = setTimeout(() => {
    if (pop.mode === "edit") return;
    const info = selectionInfo();
    if (info) { pop.sel = info; showPop(info.rect, "new"); }
  }, 180);
});
// Clicar fora esconde o menu; clicar no menu não pode desfazer a seleção.
document.addEventListener("pointerdown", (e) => {
  if (e.target.closest("#hl-pop")) { e.preventDefault(); return; }
  if (!$("#hl-pop").hidden) hidePop();
});
// Ao rolar, o menu acompanha a seleção (antes ficava parado no lugar antigo da tela).
window.addEventListener("scroll", () => {
  if (pop.mode === "edit") { hidePop(); return; }
  if (pop.mode === "new") {
    const info = selectionInfo();
    if (info) { pop.sel = info; showPop(info.rect, "new"); } else hidePop();
  }
}, { passive: true });

async function applyHighlightColor(color) {
  try {
    if (pop.mode === "new" && pop.sel) {
      let { start, end } = pop.sel;
      hidePop();
      window.getSelection()?.removeAllRanges();
      const version = currentVersion();
      const content = r.data.content;
      // Sublinhados que se sobrepõem ao novo viram um só (antes o novo era salvo mas não aparecia).
      const overlapping = store.highlightsFor(r.key, r.page, version)
        .map(h => ({ h, pos: locate(content, h) }))
        .filter(({ pos }) => pos && pos.start < end && start < pos.end);
      for (const { pos } of overlapping) { start = Math.min(start, pos.start); end = Math.max(end, pos.end); }
      for (const { h } of overlapping) await store.removeHighlight(h.id);
      const text = content.slice(start, end);
      await store.addHighlight(state.currentBook, r.page, { start, end, text, color, version });
      toast(t("hl.added"), "check");
    } else if (pop.mode === "edit" && pop.hid) {
      const id = pop.hid;
      hidePop();
      await store.updateHighlight(id, { color });
    }
  } catch (err) { toast(err.message); }
}

/* =========================================================================
   BIBLIOTECA / PERFIL
========================================================================= */
function renderLibrary() {
  const counts = store.counts();
  $$("[data-count]").forEach(el => (el.textContent = counts[el.dataset.count] || ""));
  $$(".tab-btn").forEach(b => {
    const on = b.dataset.tab === state.libraryTab;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on);
  });
  $("#library-guest-note").innerHTML = store.isGuest ? guestNote(t("lib.guestNote")) : "";
  const tab = state.libraryTab;
  const box = $("#library-content");
  box.classList.toggle("book-grid", tab !== "anotacoes");
  const files = Array.isArray(state.uploads) ? state.uploads : null;
  $("[data-count=arquivos]").textContent = files?.length || "";
  if (!store.isGuest && state.uploads === null) { state.uploads = "loading"; loadUploads(); }

  if (tab === "arquivos") {
    const bar = `<div class="files-bar"><button class="btn small" data-action="open-upload">${icon("upload")}${esc(t("add.fileBtn"))}</button>
      <span class="hint">${esc(t("add.fileHint"))}</span></div>`;
    if (store.isGuest) { box.innerHTML = emptyState(t("lib.empty.arquivos"), t("lib.filesGuest"), `<button class="btn small" data-action="open-auth">${esc(t("guest.signin"))}</button>`); return; }
    if (!files) { box.innerHTML = state.uploads?.error ? `<div class="notice">${esc(state.uploads.error)}</div>` : skeletonRow(4); return; }
    box.innerHTML = bar + (files.length ? files.map(b => `<div class="file-card">${cardFor(catalogToCard(b))}
        <button class="link danger" data-action="delete-upload" data-gid="${b.gutenberg_id}" data-title="${esc(b.title)}">${icon("trash")}${esc(t("lib.deleteFile"))}</button></div>`).join("")
      : emptyState(t("lib.empty.arquivos"), t("lib.emptyText.arquivos")));
    return;
  }

  if (tab === "anotacoes") {
    const keys = [...new Set([...store.bookmarks, ...store.highlights].map(a => a.book_key))];
    box.innerHTML = keys.length ? keys.map(k => {
      const any = store.bookmarks.find(a => a.book_key === k) || store.highlights.find(a => a.book_key === k);
      return `<section class="ann-book"><h3><a href="${detailsHref(k)}">${esc(any.title)}</a>${any.author ? ` <small>${esc(any.author.split(",")[0])}</small>` : ""}</h3>
        ${annotationsBlock(k, "")}</section>`;
    }).join("") : emptyState(t("lib.empty.anotacoes"), t("lib.emptyText.anotacoes"));
    return;
  }
  const items = store.list(tab);
  box.innerHTML = items.length ? items.map(b => cardFor(b)).join("")
    : emptyState(t("lib.empty." + tab), t("lib.emptyText." + tab), `<a class="btn secondary small" href="#/explorar">${esc(t("lib.explore"))}</a>`);
}

function guestNote(text) {
  return `<div class="sync-note"><span><strong>${esc(text)}</strong> ${esc(t("guest.cta"))}</span>
    <button class="btn small" data-action="open-auth">${esc(t("guest.signin"))}</button></div>`;
}

let profileSeq = 0;
async function renderProfile() {
  const seq = ++profileSeq;
  const el = $("#profile-content");
  const name = store.displayName || t("account.guest");
  el.innerHTML = `
    <div class="profile-head">
      <div class="avatar">${esc((name[0] || "?").toUpperCase())}</div>
      <div><h2>${esc(name)}</h2><p class="hint">${store.user ? esc(store.user.email) : esc(t("profile.guestMode"))}
        ${store.user ? (store.user.email_verified
          ? `<span class="tag free">${icon("check")}${esc(t("acc.verified"))}</span>`
          : `<span class="tag warn">${esc(t("acc.unverified"))}</span>`) : ""}</p></div>
    </div>
    ${store.isGuest ? guestNote(t("profile.youAreGuest")) : ""}
    ${store.user && !store.user.email_verified ? `
      <div class="verify-banner" role="status">
        <div><strong>${esc(t("acc.verifyTitle"))}</strong><span>${esc(t("acc.verifyText", { email: store.user.email }))}</span></div>
        <button class="btn secondary small" data-action="resend-verification">${esc(t("acc.resend"))}</button>
      </div>` : ""}
    <div class="profile-stats" id="profile-stats">${[1, 2, 3, 4].map(() => `<div class="stat skeleton" style="height:78px"></div>`).join("")}</div>
    <!-- Duas colunas que se empilham: cartões de alturas diferentes não deixam buracos -->
    <div class="profile-cols">
      <div class="profile-col">
        <section class="panel">
          <h3>${esc(t("profile.nameTitle"))}</h3>
          <form class="inline-form" id="profile-form">
            <label class="sr-only" for="profile-name">${esc(t("profile.nameLabel"))}</label>
            <input type="text" id="profile-name" maxlength="80" value="${esc(store.user ? store.user.name : store.guestName)}" placeholder="${esc(t("profile.namePh"))}">
            <button class="btn" type="submit">${esc(t("profile.saveName"))}</button>
          </form>
        </section>
        ${store.user ? passwordPanelHTML() : ""}
      </div>
      ${store.user ? `<div class="profile-col">${sessionsPanelHTML()}${deletePanelHTML()}</div>` : ""}
    </div>`;
  const s = await store.stats();
  if (seq !== profileSeq) return;
  const items = [["stats.favorites", s.favorites], ["stats.library", s.library], ["stats.reading", s.reading],
                 ["stats.completed", s.completed], ["stats.bookmarks", s.bookmarks], ["stats.highlights", s.highlights]];
  if (s.identifications !== null && s.identifications !== undefined) items.push(["stats.identifications", s.identifications]);
  const box = $("#profile-stats");
  if (box) box.innerHTML = items.map(([l, n]) => `<div class="stat"><div class="num">${n ?? 0}</div><div class="label">${esc(t(l))}</div></div>`).join("");
}

/** Painéis de conta e segurança (só para quem está logado). */
function passwordPanelHTML() {
  return `
    <section class="panel">
      <h3>${esc(t("acc.changeTitle"))}</h3>
      <form id="password-form" novalidate>
        <input type="text" autocomplete="username" value="${esc(store.user.email)}" hidden>
        <div class="form-group"><label for="pw-current">${esc(t("acc.current"))}</label>
          <input type="password" id="pw-current" autocomplete="current-password" required maxlength="128"></div>
        <div class="form-group"><label for="pw-new">${esc(t("acc.new"))}</label>
          <input type="password" id="pw-new" autocomplete="new-password" required minlength="6" maxlength="128"></div>
        <div class="form-group"><label for="pw-confirm">${esc(t("acc.confirm"))}</label>
          <input type="password" id="pw-confirm" autocomplete="new-password" required minlength="6" maxlength="128"></div>
        <p class="form-error" id="pw-error" role="alert"></p>
        <button class="btn small" type="submit">${esc(t("acc.changeSubmit"))}</button>
      </form>
    </section>`;
}

function sessionsPanelHTML() {
  return `
    <section class="panel">
      <h3>${esc(t("acc.sessionsTitle"))}</h3>
      <p class="hint">${esc(t("acc.sessionsText"))}</p>
      <div class="stack-actions">
        <button class="btn ghost small" data-action="logout-all">${esc(t("acc.logoutAll"))}</button>
        <button class="btn ghost small" data-action="logout">${icon("logout")}${esc(t("profile.logout"))}</button>
      </div>
    </section>`;
}

function deletePanelHTML() {
  return `
    <section class="panel danger-panel">
      <h3>${esc(t("acc.deleteTitle"))}</h3>
      <p class="hint">${esc(t("acc.deleteText"))}</p>
      <button class="btn danger small" data-action="open-delete">${icon("trash")}${esc(t("acc.deleteBtn"))}</button>
    </section>`;
}

/** Telas abertas pelos links do e-mail: #/confirmar-email?token= e #/redefinir-senha?token= */
async function renderAccountAction(kind, token) {
  const box = $("#account-action");
  // O token não deve ficar no endereço (histórico, captura de tela, compartilhamento).
  if (token) { state.pendingToken = token; state.accountResult = null; history.replaceState(history.state, "", `#/${kind}`); }
  token = token || state.pendingToken;
  const home = `<a class="btn secondary small" href="#/">${esc(t("acc.goHome"))}</a>`;
  // Já confirmou nesta sessão: redesenha o resultado (antes, trocar o idioma mostrava "Link inválido").
  if (!token && kind === "confirmar-email" && state.accountResult) {
    const res = state.accountResult;
    box.innerHTML = res.ok
      ? `<h1>${icon("check")} ${esc(t("acc.verifiedTitle"))}</h1><p class="hint">${esc(t("acc.verifiedText"))}</p>${home}`
      : `<h1>${esc(t("acc.linkTitle"))}</h1><p class="form-error">${esc(res.message)}</p><p class="hint">${esc(t("acc.verifyExpired"))}</p>${home}`;
    return;
  }
  if (!token) { box.innerHTML = `<h1>${esc(t("acc.linkTitle"))}</h1><p class="hint">${esc(t("acc.noToken"))}</p>${home}`; return; }

  if (kind === "confirmar-email") {
    box.innerHTML = `<h1>${esc(t("acc.verifyingTitle"))}</h1>${loading(t("acc.verifying"))}`;
    try {
      await store.verifyEmail(token);
      state.pendingToken = null;
      state.accountResult = { ok: true };
      box.innerHTML = `<h1>${icon("check")} ${esc(t("acc.verifiedTitle"))}</h1><p class="hint">${esc(t("acc.verifiedText"))}</p>${home}`;
      renderAccount();
    } catch (err) {
      state.pendingToken = null;
      state.accountResult = { ok: false, message: err.message };
      box.innerHTML = `<h1>${esc(t("acc.linkTitle"))}</h1><p class="form-error">${esc(err.message)}</p>
        <p class="hint">${esc(t("acc.verifyExpired"))}</p>${home}`;
    }
    return;
  }

  box.innerHTML = `
    <h1>${esc(t("acc.resetTitle"))}</h1>
    <p class="hint">${esc(t("acc.resetText"))}</p>
    <form id="reset-form" novalidate>
      <div class="form-group"><label for="reset-new">${esc(t("acc.new"))}</label>
        <input type="password" id="reset-new" autocomplete="new-password" required minlength="6" maxlength="128"></div>
      <div class="form-group"><label for="reset-confirm">${esc(t("acc.confirm"))}</label>
        <input type="password" id="reset-confirm" autocomplete="new-password" required minlength="6" maxlength="128"></div>
      <p class="form-error" id="reset-error" role="alert"></p>
      <button class="btn full" type="submit">${esc(t("acc.resetSubmit"))}</button>
    </form>`;
  setTimeout(() => $("#reset-new")?.focus(), 50);
}

/** Confere nova senha + confirmação; devolve a mensagem de erro ou "". */
function checkNewPassword(pw, confirm) {
  if (pw.length < 6) return t("err.password");
  if (pw.length > 128) return t("acc.tooLong");
  if (pw !== confirm) return t("acc.mismatch");
  return "";
}

document.addEventListener("submit", async (e) => {
  const id = e.target.id;
  if (!["password-form", "reset-form", "delete-form"].includes(id)) return;
  e.preventDefault();
  const btn = $("button[type=submit]", e.target);

  if (id === "password-form") {
    const errEl = $("#pw-error");
    const current = $("#pw-current").value, next = $("#pw-new").value;
    const problem = !current ? t("acc.needCurrent") : checkNewPassword(next, $("#pw-confirm").value);
    errEl.textContent = problem;
    if (problem) return;
    btn.disabled = true;
    try { await store.changePassword(current, next); e.target.reset(); toast(t("acc.changed"), "check"); }
    catch (err) { errEl.textContent = err.message; }
    finally { btn.disabled = false; }
  }

  if (id === "reset-form") {
    const errEl = $("#reset-error");
    const next = $("#reset-new").value;
    const problem = checkNewPassword(next, $("#reset-confirm").value);
    errEl.textContent = problem;
    if (problem) return;
    btn.disabled = true;
    try {
      await store.resetPassword(state.pendingToken, next);
      state.pendingToken = null;
      applyAppearance(); applyLanguage();
      toast(t("acc.resetDone", { name: store.user.name }), "check");
      go("#/");
    } catch (err) { errEl.textContent = err.message; }
    finally { btn.disabled = false; }
  }

  if (id === "delete-form") {
    const errEl = $("#delete-error");
    const pw = $("#delete-password").value;
    if (!pw) { errEl.textContent = t("acc.needCurrent"); return; }
    btn.disabled = true;
    try {
      const out = await store.deleteAccount(pw);
      closeModal("modal-delete");
      applyAppearance(); applyLanguage();
      toast(out.message, "check");
      go("#/");
    } catch (err) { errEl.textContent = err.message; }
    finally { btn.disabled = false; }
  }
});

/* =========================================================================
   CONFIGURAÇÕES
========================================================================= */
function renderSettings() {
  const s = store.settings;
  $("#settings-sync-note").innerHTML = store.isGuest
    ? guestNote(t("settings.guestNote"))
    : `<div class="sync-note"><span>${t("settings.accountNote", { email: esc(store.user.email) })}</span></div>`;
  $("#setting-language").value = s.language;
  $("#setting-translate").checked = s.translate_books;
  $("#setting-appearance").value = s.appearance;
  $("#setting-theme").value = s.theme;
  $("#setting-font-size").value = s.font_size;
  $("#setting-spacing").value = s.spacing;
  $("#setting-text-width").value = s.text_width;
  paintSettingsPreview();
}

function readSettingsForm() {
  return {
    language: $("#setting-language").value,
    translate_books: $("#setting-translate").checked,
    appearance: $("#setting-appearance").value,
    theme: $("#setting-theme").value,
    font_size: parseInt($("#setting-font-size").value, 10) || DEFAULT_SETTINGS.font_size,
    spacing: parseFloat($("#setting-spacing").value) || DEFAULT_SETTINGS.spacing,
    text_width: $("#setting-text-width").value,
  };
}

function paintSettingsPreview() {
  const s = readSettingsForm();
  $("#out-font-size").textContent = `${s.font_size}px`;
  $("#out-spacing").textContent = s.spacing.toFixed(1);
  const p = $("#reading-preview");
  p.style.fontSize = s.font_size + "px";
  p.style.lineHeight = s.spacing;
}

let settingsTimer = null;
function onSettingsInput(e) {
  const form = readSettingsForm();
  if (e.target.id === "setting-language") { changeLanguage(form.language); return; }
  paintSettingsPreview();
  store.settings = { ...store.settings, ...form };
  applyAppearance();
  clearTimeout(settingsTimer);
  settingsTimer = setTimeout(async () => {
    try { await store.saveSettings(readSettingsForm()); toast(t("settings.saved"), "check"); }
    catch (err) { toast(err.message); }
  }, 600);
}

/* =========================================================================
   CONTA
========================================================================= */
function renderAccount() {
  const box = $("#account-box");
  const topBtn = $("#topbar-account");
  if (store.user) {
    box.innerHTML = `<div class="who"><div class="avatar">${esc((store.user.name[0] || "?").toUpperCase())}</div>
      <div><div class="name">${esc(store.user.name)}</div><div class="sub">${store.user.email_verified
        ? esc(t("account.synced")) : `<a class="link warn-link" href="#/perfil">${esc(t("acc.unverified"))}</a>`}</div></div></div>`;
    topBtn.removeAttribute("data-i18n");
    topBtn.textContent = store.user.name.split(" ")[0];
    topBtn.dataset.action = "go-profile";
  } else {
    box.innerHTML = `<div class="who"><div class="avatar">${icon("user")}</div>
      <div><div class="name">${esc(t("account.guest"))}</div><div class="sub">${esc(t("account.guestSub"))}</div></div></div>
      <button class="btn secondary small full" data-action="open-auth">${esc(t("account.signinOrUp"))}</button>`;
    topBtn.textContent = t("account.signin");
    topBtn.dataset.action = "open-auth";
  }
}

function setAuthMode(mode) {
  state.authMode = mode;
  const reg = mode === "register", forgot = mode === "forgot";
  $("#ma-h").textContent = t(reg ? "auth.signup" : forgot ? "auth.forgotTitle" : "auth.signin");
  $("#auth-form .hint").textContent = t(forgot ? "auth.forgotHint" : "auth.hint");
  $("#auth-name-group").hidden = !reg;
  $("#auth-password-group").hidden = forgot;
  $("#auth-forgot-btn").hidden = mode !== "login";
  $("#auth-submit").textContent = t(reg ? "auth.signup" : forgot ? "auth.forgotSubmit" : "auth.signin");
  $("#auth-switch-text").textContent = t(reg || forgot ? "auth.haveAccount" : "auth.noAccount");
  $("#auth-switch-btn").textContent = t(reg || forgot ? "auth.signin" : "auth.signup");
  $("#auth-password").autocomplete = reg ? "new-password" : "current-password";
  $("#auth-error").textContent = "";
  $("#auth-info").textContent = "";
}

async function submitAuth(e) {
  e.preventDefault();
  const name = $("#auth-name").value.trim(), email = $("#auth-email").value.trim(), password = $("#auth-password").value;
  const errEl = $("#auth-error");
  errEl.textContent = "";
  if (state.authMode === "forgot") {
    if (!email) { errEl.textContent = t("err.email"); return; }
    const btn = $("#auth-submit");
    btn.disabled = true;
    try { $("#auth-info").textContent = (await api.forgotPassword(email)).message; }
    catch (err) { errEl.textContent = err.message; }
    finally { btn.disabled = false; }
    return;
  }
  if (state.authMode === "register" && !name) { errEl.textContent = t("auth.needName"); return; }
  if (!email || !password) { errEl.textContent = t("auth.needFields"); return; }
  const btn = $("#auth-submit");
  btn.disabled = true;
  try {
    const imported = state.authMode === "register" ? await store.register(name, email, password) : await store.login(email, password);
    closeModal("modal-auth");
    $("#auth-password").value = "";
    applyAppearance();
    applyLanguage();
    toast(t(imported ? "auth.welcomeImported" : "auth.welcome", { name: store.user.name }), "check");
    if (state.authMode === "register") setTimeout(() => toast(t("acc.checkInbox", { email: store.user.email })), 400);
    route({ keepScroll: true });
  } catch (err) {
    errEl.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

/* =========================================================================
   FOTO (OCR no navegador com Tesseract.js, carregado sob demanda)
========================================================================= */
let tesseractPromise = null;
function loadTesseract() {
  if (window.Tesseract) return Promise.resolve(window.Tesseract);
  tesseractPromise ||= new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://cdn.jsdelivr.net/npm/tesseract.js@5/dist/tesseract.min.js";
    s.onload = () => resolve(window.Tesseract);
    s.onerror = () => { tesseractPromise = null; s.remove(); reject(new Error(t("photo.ocrFail"))); };
    document.head.appendChild(s);
  });
  return tesseractPromise;
}

const manualPhotoForm = () => `<form class="inline-form" data-form="title" style="margin-top:12px">
  <input type="text" name="q" placeholder="${esc(t("photo.manualPh"))}" required><button class="btn small" type="submit">${esc(t("search.submit"))}</button></form>`;

let photoUrl = null;
async function handlePhoto(file) {
  const status = $("#photo-status");
  if (!file) return;
  if (!/^image\/(jpeg|png|webp)$/.test(file.type)) { status.innerHTML = `<p class="form-error">${esc(t("photo.badFormat"))}</p>`; return; }
  if (file.size > 8 * 1024 * 1024) { status.innerHTML = `<p class="form-error">${esc(t("photo.tooBig"))}</p>`; return; }
  const prev = $("#photo-preview");
  if (photoUrl) URL.revokeObjectURL(photoUrl);
  photoUrl = URL.createObjectURL(file);
  prev.src = photoUrl;
  prev.hidden = false;

  status.innerHTML = loading(t("photo.reading"));
  let text = "";
  try {
    const T = await loadTesseract();
    const out = await T.recognize(file, "por+eng");
    text = (out?.data?.text || "").trim();
  } catch (err) {
    status.innerHTML = `<p class="hint">${esc(err.message)} ${esc(t("photo.typeInstead"))}</p>` + manualPhotoForm();
    return;
  }
  if (!text) { status.innerHTML = `<p class="hint">${esc(t("photo.noText"))}</p>` + manualPhotoForm(); return; }

  // Página de livro → tenta identificar o trecho. Capa → busca pelas linhas mais longas.
  status.innerHTML = loading(t("photo.searching"));
  let html = "";
  if (letters(text) >= 80) {
    try {
      const res = await api.identify(text);
      if (res.matches.length) {
        const m = res.matches[0];
        const bid = register({ ...catalogToCard(m.book), _highlight: m.excerpt });
        html += `<p class="hint ok" style="margin-bottom:8px">${t("photo.looksLike", { title: esc(m.book.title), pct: Math.round(m.score * 100) })}</p>
          <button class="btn small" data-action="read-at" data-bid="${bid}" data-page="${m.page}">${icon("open")}${esc(t("photo.openPage"))}</button>`;
      }
    } catch {}
  }
  const lines = text.split("\n").map(l => l.trim()).filter(l => letters(l) > 3).sort((a, b) => b.length - a.length).slice(0, 2);
  try {
    const seen = new Set(), found = [];
    for (const line of lines) {
      for (const b of await api.search(line.slice(0, 200), { mode: "phrase", limit: 6 })) {
        if (!seen.has(b.book_key)) { seen.add(b.book_key); found.push(b); }
      }
    }
    if (found.length) html += `<p class="hint" style="margin-top:16px">${esc(t("photo.similar"))}</p><div class="photo-results">${found.slice(0, 6).map(b => cardFor(b)).join("")}</div>`;
  } catch {}
  status.innerHTML = html || `<p class="hint">${esc(t("photo.none"))}</p>` + manualPhotoForm();
}



/* =========================================================================
   EVENTOS
========================================================================= */
// Ações que gravam dados: trava contra clique duplo.
const busy = new Set();
const WRITE_ACTIONS = new Set(["fav", "want", "set-status", "remove-lib", "reader-bookmark", "logout-all", "resend-verification"]);

document.addEventListener("click", async (e) => {
  // Clique num trecho sublinhado abre o menu de cor/remover.
  const mark = e.target.closest("#reader-page mark.uh");
  if (mark && window.getSelection()?.isCollapsed !== false) {
    const h = store.getHighlight(mark.dataset.hid);
    if (h) { showPop(mark.getBoundingClientRect(), "edit", h.color); pop.hid = String(h.id); }
    return;
  }

  const el = e.target.closest("[data-action]");
  if (!el) return;
  const a = el.dataset.action;
  const book = el.dataset.bid ? lookup(el.dataset.bid) : null;

  // Clique duplo em favoritar/marcar não pode disparar duas gravações opostas.
  if (WRITE_ACTIONS.has(a)) {
    const lockKey = `${a}:${book?.book_key || r.key}:${a === "reader-bookmark" ? r.page : ""}`;
    if (busy.has(lockKey)) return;
    busy.add(lockKey);
    try { await handleAction(e, el, a, book); } finally { busy.delete(lockKey); }
    return;
  }
  await handleAction(e, el, a, book);
});


async function handleAction(e, el, a, book) {
  switch (a) {
    case "open-modal":
      if (el.dataset.modal === "modal-photo") { $("#photo-status").innerHTML = ""; $("#photo-preview").hidden = true; }
      openModal(el.dataset.modal); break;
    case "close-modal": closeModal(); break;
    case "open-auth": setAuthMode("login"); openModal("modal-auth"); break;
    case "auth-switch": setAuthMode(state.authMode === "login" ? "register" : "login"); break;
    case "auth-forgot": setAuthMode("forgot"); $("#auth-email").focus(); break;
    case "resend-verification":
      try { toast((await api.resendVerification()).message, "check"); } catch (err) { toast(err.message); }
      break;
    case "logout-all":
      try { await store.logoutAll(); toast(t("acc.loggedOutAll"), "check"); } catch (err) { toast(err.message); }
      break;
    case "open-delete":
      $("#delete-password").value = ""; $("#delete-error").textContent = "";
      openModal("modal-delete");
      break;
    case "go-profile": go("#/perfil"); break;
    case "logout": store.logout(); applyAppearance(); applyLanguage(); toast(t("auth.loggedOut")); route(); break;
    case "back": back(); break;
    case "set-lang": changeLanguage(el.dataset.lang); break;
    case "genre-search": go(searchHash(GENRES.find(x => x.key === el.dataset.genre).query, "text", { genre: el.dataset.genre })); break;
    case "genre-explore": exploreGenre(GENRES.find(x => x.key === el.dataset.genre)); break;
    case "search-lang": {
      const p = state.route.params;
      go(searchHash(p.q || "", p.mode || "text", { genre: p.genre, lang: el.dataset.lang }));
      break;
    }
    case "collection-lang": collection.lang = el.dataset.lang; renderCollection(); break;
    case "search-external": if (state.lastIdentify) runExternal(state.lastIdentify.query); break;
    case "open-upload": openUpload({ title: el.dataset.title || "", author: el.dataset.author || "" }); break;
    case "delete-upload": deleteUpload(parseInt(el.dataset.gid, 10), el.dataset.title); break;
    case "ws-import": importWikisource(el.dataset.lang, el.dataset.title); break;
    case "search-fallback": go(searchHash($("#identify-input").value.trim().slice(0, 300), "phrase")); break;
    case "lib-tab": state.libraryTab = el.dataset.tab; renderLibrary(); break;
    case "lib-open": state.libraryTab = el.dataset.tab; break; // o link segue para #/biblioteca
    case "fav": {
      e.preventDefault();
      try {
        const on = await store.toggleFavorite(book);
        toast(t(on ? "toast.favAdded" : "toast.favRemoved"), on ? "heart" : null);
      } catch (err) { toast(err.message); }
      break;
    }
    case "want":
      if (store.inLibrary(book.book_key)) { toast(t("book.inLibrary"), "check"); break; }
      try { await store.setStatus(book, "quero_ler"); toast(t("toast.wantAdded"), "check"); } catch (err) { toast(err.message); }
      break;
    case "set-status":
      try { await store.setStatus(book, el.dataset.status); toast(t("toast.statusSet", { status: t("status." + el.dataset.status) }), "check"); } catch (err) { toast(err.message); }
      break;
    case "remove-lib":
      try { await store.removeFromLibrary(el.dataset.key); toast(t("toast.libRemoved")); } catch (err) { toast(err.message); }
      break;
    case "read-at":
      closeModal("modal-photo");
      state.currentBook = { ...book };
      r.target = book._highlight || null;
      // "orig=1": abre no texto original, que é onde está o trecho encontrado.
      go(readerHref(book.gutenberg_id, book.book_key, el.dataset.page, { orig: 1 }));
      break;
    case "font": {
      const size = Math.max(12, Math.min(32, store.settings.font_size + parseInt(el.dataset.delta, 10)));
      store.saveSettingsSoon({ font_size: size }).catch(() => {});
      applyReaderSettings();
      break;
    }
    case "reader-width": {
      const order = ["estreito", "medio", "largo"];
      store.saveSettingsSoon({ text_width: order[(order.indexOf(store.settings.text_width) + 1) % 3] }).catch(() => {});
      applyReaderSettings();
      break;
    }
    case "reader-paper": {
      const order = ["papel", "sepia", "noite"];
      r.paper = order[(order.indexOf(r.paper) + 1) % 3];
      try { localStorage.setItem("freedom_paper", r.paper); } catch {}
      applyReaderSettings();
      break;
    }
    case "reader-translate":
      r.translateOn = !(r.data && r.data.translated);
      showReaderPage(r.page);
      break;
    case "reader-bookmark":
      if (!r.data) break;
      try {
        const on = await store.toggleBookmark(state.currentBook, r.page);
        toast(t(on ? "reader.bookmarkAdded" : "reader.bookmarkRemoved"), on ? "bookmark" : null);
      } catch (err) { toast(err.message); }
      break;
    case "open-notes": openNotes(); break;
    case "close-notes": closeNotes(); $("#reader-notes-btn").focus(); break;
    case "goto-page": {
      if (window.innerWidth < 900) closeNotes();
      showReaderPage(parseInt(el.dataset.page, 10)).then(() => window.scrollTo({ top: 0, behavior: "smooth" }));
      break;
    }
    case "goto-highlight": {
      const h = store.getHighlight(el.dataset.id);
      if (!h) break;
      if (window.innerWidth < 900) closeNotes();
      r.translateOn = h.version !== "original";
      r.flashHid = String(h.id);
      showReaderPage(h.page);
      break;
    }
    case "delete-bookmark":
      try { await store.removeBookmark(el.dataset.id); toast(t("reader.bookmarkRemoved")); } catch (err) { toast(err.message); }
      break;
    case "delete-highlight":
      try { await store.removeHighlight(el.dataset.id); toast(t("hl.removed")); } catch (err) { toast(err.message); }
      break;
    case "hl-color": applyHighlightColor(el.dataset.color); break;
    case "hl-remove": {
      const id = pop.hid;
      hidePop();
      if (id) { try { await store.removeHighlight(id); toast(t("hl.removed")); } catch (err) { toast(err.message); } }
      break;
    }
    case "reader-prev": readerStep(-1); break;
    case "reader-next": readerStep(1); break;
  }
}

// Anotação do marcador: salva ao sair do campo ou apertar Enter.
document.addEventListener("change", async (e) => {
  const input = e.target.closest(".bm-note");
  if (!input) return;
  try { await store.setBookmarkNote(input.dataset.bmid, input.value); toast(t("notes.saved"), "check"); }
  catch (err) { toast(err.message); }
});

// Formulários de busca (barra lateral, Explorar, Busca, descrição, foto)
document.addEventListener("submit", (e) => {
  const form = e.target.closest("[data-form]");
  if (!form) return;
  e.preventDefault();
  const q = new FormData(form).get("q")?.toString().trim();
  if (!q) return;
  closeModal();
  const kind = form.dataset.form;
  const lang = kind === "search" && state.route.view === "search" ? state.route.params.lang : "";
  go(searchHash(q.slice(0, 500), kind === "description" ? "description" : "text", { lang }));
  if (kind !== "search") form.reset();
  if (form.classList.contains("side-search")) form.reset();
});

$("#identify-form").addEventListener("submit", (e) => { e.preventDefault(); identify($("#identify-input").value); });
$("#identify-input").addEventListener("input", updateExcerptCount);
$("#collection-filter").addEventListener("input", (e) => { collection.text = e.target.value; paintCollection(); });
$("#upload-form").addEventListener("submit", submitUpload);
$("#up-file").addEventListener("change", (e) => {
  const f = e.target.files[0];
  $("#up-file-name").textContent = f ? f.name : t("up.choose");
  if (f && !$("#up-title").value) $("#up-title").value = f.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ");
});
$("#ws-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#ws-q").value.trim();
  if (q.length >= 2) searchWikisource(q, $("#ws-lang").value);
});
$("#identify-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); identify(e.target.value); } });
$("#auth-form").addEventListener("submit", submitAuth);
$("#photo-input").addEventListener("change", (e) => { handlePhoto(e.target.files[0]); e.target.value = ""; });
$("#settings-form").addEventListener("input", onSettingsInput);
$("#settings-form").addEventListener("submit", (e) => e.preventDefault());
document.addEventListener("submit", async (e) => {
  if (e.target.id !== "profile-form") return;
  e.preventDefault();
  const name = $("#profile-name").value.trim();
  if (!name) { toast(t("profile.typeName")); return; }
  try { await store.rename(name.slice(0, 80)); toast(t("profile.nameSaved"), "check"); } catch (err) { toast(err.message); }
});

$$(".overlay").forEach(ov => ov.addEventListener("click", (e) => { if (e.target === ov) closeModal(ov.id); }));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!$("#hl-pop").hidden) { hidePop(); return; }
    if ($(".overlay.active")) { closeModal(); return; }
    if (!$("#notes-drawer").hidden) { closeNotes(); return; }
  }
  // Enter/Espaço num trecho sublinhado (teclado) abre o menu dele.
  if ((e.key === "Enter" || e.key === " ") && e.target.matches?.("#reader-page mark.uh")) {
    e.preventDefault();
    const h = store.getHighlight(e.target.dataset.hid);
    if (h) { showPop(e.target.getBoundingClientRect(), "edit", h.color); pop.hid = String(h.id); }
    return;
  }
  if (state.route.view === "reader" && !e.target.closest("input, textarea, select, [contenteditable]") && !$(".overlay.active")) {
    if (e.key === "ArrowRight") readerStep(1);
    if (e.key === "ArrowLeft") readerStep(-1);
  }
});

// Quando favoritos/biblioteca/anotações mudam, atualiza o que estiver na tela.
window.addEventListener("freedom:store", () => {
  const uid = store.user?.id ?? null;
  if (uid !== state.userId) {
    const first = state.userId === undefined;
    state.userId = uid;
    state.uploads = null;
    if (!first) {   // entrou/saiu: acervo visível e resultados podem incluir arquivos particulares
      state.catalog = null;
      state.lastIdentify = null;
      $("#identify-result").innerHTML = "";
      if (state.route.view === "home") loadCatalogRow();
      if (state.route.view === "collection") renderCollection();
    }
  }
  renderAccount();
  $$("[data-action=fav][data-bid]").forEach(btn => {
    const b = lookup(btn.dataset.bid); if (!b) return;
    const on = store.isFavorite(b.book_key);
    btn.setAttribute("aria-pressed", on);
    if (btn.classList.contains("icon-btn")) { btn.classList.toggle("active", on); btn.setAttribute("aria-label", t(on ? "book.unfavorite" : "book.favorite")); }
    else { btn.classList.toggle("is-on", on); btn.classList.toggle("ghost", !on); btn.innerHTML = icon("heart") + esc(t(on ? "book.favorited" : "book.favorite")); }
  });
  $$("[data-action=want][data-bid]").forEach(btn => {
    const b = lookup(btn.dataset.bid); if (!b) return;
    const on = store.inLibrary(b.book_key);
    btn.classList.toggle("active", on);
    btn.innerHTML = icon(on ? "check" : "plus");
    btn.setAttribute("aria-label", t(on ? "book.inLibrary" : "book.addWant"));
    btn.title = t(on ? "book.inLibrary" : "book.addWant");
  });
  const v = state.route.view;
  if (v === "home") renderHomePersonal();
  if (v === "library") renderLibrary();
  if (v === "details") paintDetails();
  if (v === "profile") renderProfile();
  if (v === "reader" && r.data) { paintReaderPage(); updateReaderChrome(); }
});
window.addEventListener("freedom:logout", (e) => {
  store.logout(); applyAppearance(); applyLanguage();
  if (e.detail?.expired) toast(t("auth.expired"));
});
window.addEventListener("hashchange", () => { syncHistoryIndex(); route(); });

// Login/logout em OUTRA aba do mesmo navegador (o token fica no localStorage compartilhado).
window.addEventListener("storage", (e) => {
  if (e.key !== "freedom_token") return;
  if (!e.newValue && store.user) { store.logout(); applyAppearance(); applyLanguage(); toast(t("auth.loggedOut")); route({ keepScroll: true }); }
  else if (e.newValue && !e.oldValue) location.reload(); // entrou em outra aba: recarrega já logado
});

/* =========================================================================
   INIT
========================================================================= */
(async function init() {
  if (!history.state || typeof history.state.idx !== "number") history.replaceState({ idx: 0 }, "");
  navIdx = history.state.idx;
  try { r.paper = localStorage.getItem("freedom_paper") || "papel"; } catch {}
  if (!["papel", "sepia", "noite"].includes(r.paper)) r.paper = "papel";
  applyLanguage(); // textos iniciais no idioma do navegador enquanto carrega
  await store.init();
  applyAppearance();
  applyLanguage();
  renderAccount();
  route();
})();
