/* Estado do usuário com duas "pontas":
   - Visitante: tudo no localStorage.
   - Conta: tudo na API. No primeiro login, o que o visitante fez é importado.
   O resto do app não precisa saber em qual modo está. */
import { api, auth } from "./api.js";
import { detectLang } from "./i18n.js";

const GUEST_KEY = "freedom_guest_v2";
const LEGACY = { favorites: "freedom_favorites", library: "freedom_library", progress: "freedom_progress", profile: "freedom_profile", settings: "freedom_settings" };
export const DEFAULT_SETTINGS = {
  language: "pt", translate_books: true,
  appearance: "auto", theme: "literatura", font_size: 19, spacing: 1.7, text_width: "medio",
};
const THEMES = ["literatura", "biologia", "terror", "romance", "fantasia", "ficcao", "historia", "misterio"];
const COMPLETED_AT = 99.5;

function lsGet(key, fallback) { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; } }
function lsSet(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); } catch {} }
function lsDel(key) { try { localStorage.removeItem(key); } catch {} }
const now = () => new Date().toISOString();
let guestSeq = 0;
const guestId = () => `g${Date.now().toString(36)}${(guestSeq++).toString(36)}`;

/** Garante que configurações antigas/estranhas não quebrem o app. */
function cleanSettings(raw) {
  const s = { ...DEFAULT_SETTINGS, ...(raw || {}) };
  if (!["pt", "en"].includes(s.language)) s.language = DEFAULT_SETTINGS.language;
  if (!["auto", "claro", "escuro"].includes(s.appearance)) s.appearance = "auto";
  if (!THEMES.includes(s.theme)) s.theme = "literatura";
  if (!["estreito", "medio", "largo"].includes(s.text_width)) s.text_width = "medio";
  s.font_size = Math.min(32, Math.max(12, parseInt(s.font_size, 10) || 19));
  s.spacing = Math.min(2.5, Math.max(1, parseFloat(s.spacing) || 1.7));
  s.translate_books = s.translate_books !== false;
  return s;
}

function blankBook(ref) {
  return {
    book_key: ref.book_key, title: ref.title, author: ref.author || "", cover_url: ref.cover_url || null,
    gutenberg_id: ref.gutenberg_id || null, is_favorite: false, status: null, page: 0, total_pages: 0,
    percent: 0, favorited_at: null, added_at: null, last_read: null,
  };
}
export function toRef(b) {
  return { book_key: b.book_key, title: (b.title || "Sem título").slice(0, 300), author: (b.author || "").slice(0, 300),
           cover_url: b.cover_url || null, gutenberg_id: b.gutenberg_id || null };
}
function annRef(b, page) {
  return { book_key: b.book_key, title: (b.title || "").slice(0, 300) || "—", author: (b.author || "").slice(0, 300),
           gutenberg_id: b.gutenberg_id, page };
}
const stripId = ({ id, created_at, ...rest }) => rest;

/** Converte os dados da versão 100% estática (v1) para o formato novo, uma única vez. */
function migrateLegacy() {
  const favs = lsGet(LEGACY.favorites, null), lib = lsGet(LEGACY.library, null), prog = lsGet(LEGACY.progress, null);
  if (!favs && !lib && !prog) return null;
  const books = {};
  const upsert = (r) => (books[r.book_key] ||= blankBook(r));
  (favs || []).forEach(f => Object.assign(upsert(f), { is_favorite: true, favorited_at: f.added_at || now() }));
  (lib || []).forEach(l => Object.assign(upsert(l), { status: l.status, added_at: l.added_at || now() }));
  Object.values(prog || {}).forEach(p => Object.assign(upsert(p), {
    gutenberg_id: p.gutenberg_id, page: p.page, total_pages: p.total_pages, percent: p.percent, last_read: p.last_read,
  }));
  const data = { books, settings: cleanSettings(lsGet(LEGACY.settings, {})), name: (lsGet(LEGACY.profile, {}) || {}).name || "",
                 bookmarks: [], highlights: [] };
  Object.values(LEGACY).forEach(lsDel);
  return data;
}

class Store {
  constructor() {
    this.user = null;
    this.books = new Map();
    this.bookmarks = [];
    this.highlights = [];
    this.settings = { ...DEFAULT_SETTINGS, language: detectLang() };
    this.guestName = "";
  }

  get isGuest() { return !this.user; }
  get displayName() { return this.user ? this.user.name : this.guestName; }

  emit(kind = "books") { window.dispatchEvent(new CustomEvent("freedom:store", { detail: { kind } })); }

  // ------------------------------------------------------------ carga
  async init() {
    if (auth.token) {
      try { this.user = await api.me(); }
      catch (err) {
        // Só descarta o token se o servidor disser que ele é inválido (401).
        // Servidor fora do ar não pode deslogar a pessoa.
        if (err.status === 401) auth.token = null;
        this.user = null;
      }
    }
    if (this.user) {
      try { await this.loadServer(); }
      catch { this.user = null; this.loadGuest(); } // servidor fora do ar: segue como visitante
    } else this.loadGuest();
  }

  loadGuest() {
    let data = lsGet(GUEST_KEY, null);
    if (!data) {
      data = migrateLegacy() || { books: {}, settings: { ...DEFAULT_SETTINGS, language: detectLang() }, name: "", bookmarks: [], highlights: [] };
      lsSet(GUEST_KEY, data);
    }
    this.books = new Map(Object.entries(data.books || {}));
    this.bookmarks = Array.isArray(data.bookmarks) ? data.bookmarks : [];
    this.highlights = Array.isArray(data.highlights) ? data.highlights : [];
    this.settings = cleanSettings({ language: detectLang(), ...(data.settings || {}) });
    this.guestName = data.name || "";
  }

  saveGuest() {
    lsSet(GUEST_KEY, {
      books: Object.fromEntries(this.books), settings: this.settings, name: this.guestName,
      bookmarks: this.bookmarks, highlights: this.highlights,
    });
  }

  async loadServer() {
    const [books, settings, ann] = await Promise.all([api.myBooks(), api.getSettings(), api.annotations()]);
    this.books = new Map(books.map(b => [b.book_key, b]));
    this.settings = cleanSettings(settings);
    this.bookmarks = ann.bookmarks;
    this.highlights = ann.highlights;
  }

  // ------------------------------------------------------------ sessão
  async afterAuth(tokenOut, isNew) {
    auth.token = tokenOut.access_token;
    this.user = tokenOut.user;
    const guest = lsGet(GUEST_KEY, null);
    const list = Object.values((guest && guest.books) || {});
    const gBookmarks = (guest && guest.bookmarks) || [], gHighlights = (guest && guest.highlights) || [];
    let imported = false;
    if (list.length || gBookmarks.length || gHighlights.length || isNew) {
      try {
        await api.importGuest({
          favorites: list.filter(b => b.is_favorite).map(toRef),
          library: list.filter(b => b.status).map(b => ({ ...toRef(b), status: b.status })),
          progress: list.filter(b => b.last_read && b.total_pages).map(b => ({ ...toRef(b), page: b.page, total_pages: b.total_pages })),
          // Conta nova herda as configurações do visitante (inclusive o idioma escolhido).
          settings: isNew ? cleanSettings(guest ? guest.settings : this.settings) : null,
          bookmarks: gBookmarks.map(stripId),
          highlights: gHighlights.map(stripId),
        });
        imported = list.length + gBookmarks.length + gHighlights.length > 0;
        lsDel(GUEST_KEY);
      } catch {
        // A importação falhou: o login continua valendo e os dados de visitante
        // ficam guardados no navegador para uma nova tentativa no próximo login.
      }
    } else lsDel(GUEST_KEY);
    try { await this.loadServer(); }
    catch { this.books = new Map(); this.bookmarks = []; this.highlights = []; } // recarregar a página resolve
    this.emit("all");
    return imported;
  }

  async login(email, password) { return this.afterAuth(await api.login(email, password), false); }
  async register(name, email, password) { return this.afterAuth(await api.register(name, email, password), true); }

  // ------------------------------------------------------------ conta
  /** Redefine a senha pelo link do e-mail e já entra na conta. */
  async resetPassword(token, password) { return this.afterAuth(await api.resetPassword(token, password), false); }

  async verifyEmail(token) {
    const u = await api.verifyEmail(token);
    if (this.user && this.user.id === u.id) { this.user = u; this.emit("all"); }
    return u;
  }

  async changePassword(current, next) {
    const out = await api.changePassword(current, next);
    auth.token = out.access_token; // os outros aparelhos saem; este continua com um token novo
    this.user = out.user;
  }

  async logoutAll() {
    const out = await api.logoutAll();
    auth.token = out.access_token;
    this.user = out.user;
  }

  async deleteAccount(password) {
    const out = await api.deleteAccount(password);
    this.logout();
    return out;
  }

  logout() {
    clearTimeout(this._settingsTimer);
    this._progressSeq = (this._progressSeq || 0) + 1; // descarta respostas de progresso pendentes
    auth.token = null;
    this.user = null;
    this.loadGuest();
    this.emit("all");
  }

  // ------------------------------------------------------------ livros: leitura
  get(key) { return this.books.get(key) || null; }
  isFavorite(key) { const b = this.books.get(key); return !!(b && b.is_favorite); }
  inLibrary(key) { const b = this.books.get(key); return !!(b && b.status); }
  list(filter) {
    const all = [...this.books.values()];
    const recent = (a, b) => (b.last_read || b.added_at || "").localeCompare(a.last_read || a.added_at || "");
    if (filter === "favoritos") return all.filter(b => b.is_favorite).sort((a, b) => (b.favorited_at || "").localeCompare(a.favorited_at || ""));
    if (filter === "continuar") return all.filter(b => b.last_read && b.gutenberg_id && b.status !== "concluido" && b.percent < COMPLETED_AT).sort(recent);
    if (filter === "biblioteca") return all.filter(b => b.status === "lendo" || b.status === "quero_ler").sort(recent);
    return all.filter(b => b.status === filter).sort(recent);
  }
  counts() {
    const all = [...this.books.values()];
    return {
      favoritos: all.filter(b => b.is_favorite).length,
      quero_ler: all.filter(b => b.status === "quero_ler").length,
      lendo: all.filter(b => b.status === "lendo").length,
      concluido: all.filter(b => b.status === "concluido").length,
      biblioteca: all.filter(b => b.status).length,
      anotacoes: this.bookmarks.length + this.highlights.length,
    };
  }

  // ------------------------------------------------------------ livros: escrita
  _local(ref) {
    let b = this.books.get(ref.book_key);
    if (!b) { b = blankBook(ref); this.books.set(ref.book_key, b); }
    Object.assign(b, { title: ref.title || b.title, author: ref.author || b.author, cover_url: ref.cover_url || b.cover_url, gutenberg_id: ref.gutenberg_id || b.gutenberg_id });
    return b;
  }
  _prune(b) { if (!b.is_favorite && !b.status && !b.last_read) this.books.delete(b.book_key); }
  _replace(serverBook) { this.books.set(serverBook.book_key, serverBook); }

  async toggleFavorite(book) {
    const ref = toRef(book);
    const willFav = !this.isFavorite(ref.book_key);
    if (this.user) {
      if (willFav) this._replace(await api.addFavorite(ref));
      else { await api.removeFavorite(ref.book_key); const b = this.get(ref.book_key); if (b) { b.is_favorite = false; b.favorited_at = null; this._prune(b); } }
    } else {
      const b = this._local(ref);
      b.is_favorite = willFav; b.favorited_at = willFav ? now() : null;
      this._prune(b); this.saveGuest();
    }
    this.emit();
    return willFav;
  }

  async setStatus(book, status) {
    const ref = toRef(book);
    if (this.user) this._replace(await api.setLibrary(ref, status));
    else { const b = this._local(ref); b.status = status; b.added_at ||= now(); this.saveGuest(); }
    this.emit();
  }

  async removeFromLibrary(key) {
    if (this.user) await api.removeLibrary(key);
    const b = this.get(key);
    if (b) { Object.assign(b, { status: null, added_at: null, last_read: null, page: 0, percent: 0 }); this._prune(b); }
    if (!this.user) this.saveGuest();
    this.emit();
  }

  /** Salva o progresso na hora, localmente, e envia ao servidor em FILA.
      Antes, com cliques rápidos em "Próxima", as respostas podiam chegar fora de ordem
      e a página salva no servidor acabava sendo uma anterior. */
  saveProgress(book, page, total) {
    const ref = toRef(book);
    const b = this._local(ref);
    b.page = Math.min(page, total - 1); b.total_pages = total;
    b.percent = Math.round(((b.page + 1) / total) * 1000) / 10;
    b.last_read = now();
    if (b.status !== "concluido") { b.status = b.percent >= COMPLETED_AT ? "concluido" : "lendo"; b.added_at ||= now(); }
    if (this.user) {
      const user = this.user;
      const seq = (this._progressSeq = (this._progressSeq || 0) + 1);
      this._progressChain = (this._progressChain || Promise.resolve())
        .then(() => api.saveProgress(ref, page, total))
        .then(saved => { if (this.user === user && seq === this._progressSeq) this._replace(saved); })
        .catch(() => {});
    } else this.saveGuest();
    return b;
  }

  // ------------------------------------------------------------ marcadores
  bookmarksFor(key) { return this.bookmarks.filter(b => b.book_key === key).sort((a, b) => a.page - b.page); }
  bookmarkAt(key, page) { return this.bookmarks.find(b => b.book_key === key && b.page === page) || null; }

  async toggleBookmark(book, page) {
    const existing = this.bookmarkAt(book.book_key, page);
    if (existing) { await this.removeBookmark(existing.id); return false; }
    const data = { ...annRef(book, page), note: "" };
    const bm = this.user ? await api.putBookmark(data) : { ...data, id: guestId(), created_at: now() };
    this.bookmarks.unshift(bm);
    if (!this.user) this.saveGuest();
    this.emit("notes");
    return true;
  }

  async setBookmarkNote(id, note) {
    const bm = this.bookmarks.find(b => String(b.id) === String(id));
    if (!bm) return;
    const clean = note.trim().slice(0, 200);
    if (this.user) Object.assign(bm, await api.putBookmark({ ...stripId(bm), note: clean }));
    else { bm.note = clean; this.saveGuest(); }
    this.emit("notes");
  }

  async removeBookmark(id) {
    if (this.user) await api.deleteBookmark(id);
    this.bookmarks = this.bookmarks.filter(b => String(b.id) !== String(id));
    if (!this.user) this.saveGuest();
    this.emit("notes");
  }

  // ------------------------------------------------------------ sublinhados
  highlightsFor(key, page, version) {
    return this.highlights.filter(h => h.book_key === key && (page === undefined || h.page === page) &&
                                       (version === undefined || h.version === version));
  }
  getHighlight(id) { return this.highlights.find(h => String(h.id) === String(id)) || null; }

  async addHighlight(book, page, { start, end, text, color, version }) {
    const data = { ...annRef(book, page), start, end, text: text.slice(0, 3000), color, version, note: "" };
    const hl = this.user ? await api.addHighlight(data) : { ...data, id: guestId(), created_at: now() };
    this.highlights.unshift(hl);
    if (!this.user) this.saveGuest();
    this.emit("notes");
    return hl;
  }

  async updateHighlight(id, patch) {
    const hl = this.getHighlight(id);
    if (!hl) return;
    if (this.user) Object.assign(hl, await api.patchHighlight(id, patch));
    else { Object.assign(hl, patch); this.saveGuest(); }
    this.emit("notes");
  }

  async removeHighlight(id) {
    if (this.user) await api.deleteHighlight(id);
    this.highlights = this.highlights.filter(h => String(h.id) !== String(id));
    if (!this.user) this.saveGuest();
    this.emit("notes");
  }

  // ------------------------------------------------------------ configurações / perfil
  async saveSettings(s) {
    clearTimeout(this._settingsTimer);
    this.settings = cleanSettings({ ...this.settings, ...s });
    if (this.user) await api.putSettings(this.settings); else this.saveGuest();
  }

  /** Aplica na hora e envia ao servidor só depois que a pessoa parar de mexer. */
  saveSettingsSoon(s, delay = 600) {
    this.settings = cleanSettings({ ...this.settings, ...s });
    if (!this.user) { this.saveGuest(); return Promise.resolve(); }
    clearTimeout(this._settingsTimer);
    return new Promise((resolve, reject) => {
      this._settingsTimer = setTimeout(() => api.putSettings(this.settings).then(resolve, reject), delay);
    });
  }

  async rename(name) {
    if (this.user) this.user = await api.updateMe(name);
    else { this.guestName = name; this.saveGuest(); }
    this.emit();
  }

  async stats() {
    if (this.user) { try { return await api.stats(); } catch {} }
    const c = this.counts();
    return { favorites: c.favoritos, library: c.biblioteca, reading: c.lendo, completed: c.concluido,
             identifications: null, bookmarks: this.bookmarks.length, highlights: this.highlights.length };
  }
}

export const store = new Store();
