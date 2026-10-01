/* Cliente HTTP da API Freadom Reading.
   Por padrão usa a mesma origem (o FastAPI serve o frontend). Para rodar o
   frontend em outro servidor (ex.: Live Server na porta 5500), defina
   window.FREEDOM_API_BASE = "http://localhost:8000" antes de carregar o app. */
import { getLang, t } from "./i18n.js";

const API_BASE = (window.FREEDOM_API_BASE || "").replace(/\/$/, "");
const TOKEN_KEY = "freedom_token";

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

export const auth = {
  get token() { try { return localStorage.getItem(TOKEN_KEY); } catch { return null; } },
  set token(v) {
    try { v ? localStorage.setItem(TOKEN_KEY, v) : localStorage.removeItem(TOKEN_KEY); } catch {}
  },
};

function detailMessage(body, status) {
  const d = body && body.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d.length) {
    const field = d[0].loc ? d[0].loc[d[0].loc.length - 1] : "";
    const map = { email: "err.email", password: "err.password", new_password: "err.password", name: "err.name" };
    return t(map[field] || "err.invalid");
  }
  if (status >= 500) return t("err.server");
  return t("err.generic");
}

export async function request(path, { method = "GET", body, params, signal } = {}) {
  let url = API_BASE + path;
  if (params) {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => v !== undefined && v !== null && v !== "" && qs.set(k, v));
    const s = qs.toString();
    if (s) url += "?" + s;
  }
  const headers = { Accept: "application/json", "Accept-Language": getLang() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = auth.token;
  if (token) headers.Authorization = `Bearer ${token}`;

  let resp;
  try {
    resp = await fetch(url, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined, signal });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError(t("err.network"), 0);
  }
  if (resp.status === 204) return null;
  let data = null;
  try { data = await resp.json(); } catch {}
  if (!resp.ok) {
    // Token expirado em qualquer rota pessoal: volta ao modo visitante.
    // (Compara com o token usado na requisição para não deslogar quem acabou de entrar de novo.)
    // Rotas que exigem login: um 401 nelas significa que a sessão caiu (antes, só algumas eram tratadas
    // e a tela continuava mostrando a pessoa como logada depois de "sair de todos" em outro aparelho).
    const PUBLIC_AUTH = ["/api/auth/login", "/api/auth/register", "/api/auth/forgot-password",
                         "/api/auth/reset-password", "/api/auth/verify-email"];
    const personal = path.startsWith("/api/me") || (path.startsWith("/api/auth/") && !PUBLIC_AUTH.includes(path));
    if (resp.status === 401 && token && personal && auth.token === token) {
      auth.token = null;
      window.dispatchEvent(new CustomEvent("freedom:logout", { detail: { expired: true } }));
      // Mesma mensagem do aviso de sessão expirada: o toast repetido é descartado (antes apareciam duas).
      throw new ApiError(t("auth.expired"), 401);
    }
    throw new ApiError(detailMessage(data, resp.status), resp.status);
  }
  return data;
}

export const api = {
  health: () => request("/api/health"),
  // auth
  register: (name, email, password) => request("/api/auth/register", { method: "POST", body: { name, email, password } }),
  login: (email, password) => request("/api/auth/login", { method: "POST", body: { email, password } }),
  me: () => request("/api/auth/me"),
  updateMe: (name) => request("/api/auth/me", { method: "PATCH", body: { name } }),
  verifyEmail: (token) => request("/api/auth/verify-email", { method: "POST", body: { token } }),
  resendVerification: () => request("/api/auth/resend-verification", { method: "POST" }),
  forgotPassword: (email) => request("/api/auth/forgot-password", { method: "POST", body: { email } }),
  resetPassword: (token, new_password) => request("/api/auth/reset-password", { method: "POST", body: { token, new_password } }),
  changePassword: (current_password, new_password) =>
    request("/api/auth/change-password", { method: "POST", body: { current_password, new_password } }),
  logoutAll: () => request("/api/auth/logout-all", { method: "POST" }),
  deleteAccount: (password) => request("/api/auth/delete-account", { method: "POST", body: { password } }),
  // identificação e catálogo
  identify: (excerpt) => request("/api/identify", { method: "POST", body: { excerpt } }),
  catalog: () => request("/api/books"),
  search: (q, { mode = "text", limit = 20, language, signal } = {}) =>
    request("/api/search", { params: { q, mode, limit, language }, signal }),
  details: (key) => request("/api/details", { params: { key } }),
  readerPage: (gid, page, lang) => request(`/api/reader/${gid}`, { params: { page, lang } }),
  // dados do usuário
  myBooks: () => request("/api/me/books"),
  addFavorite: (ref) => request("/api/me/favorites", { method: "PUT", body: ref }),
  removeFavorite: (key) => request("/api/me/favorites", { method: "DELETE", params: { key } }),
  setLibrary: (ref, status) => request("/api/me/library", { method: "PUT", body: { ...ref, status } }),
  removeLibrary: (key) => request("/api/me/library", { method: "DELETE", params: { key } }),
  saveProgress: (ref, page, total_pages) => request("/api/me/progress", { method: "PUT", body: { ...ref, page, total_pages } }),
  getSettings: () => request("/api/me/settings"),
  putSettings: (s) => request("/api/me/settings", { method: "PUT", body: s }),
  stats: () => request("/api/me/stats"),
  importGuest: (payload) => request("/api/me/import", { method: "POST", body: payload }),
  // marcadores e sublinhados
  annotations: (book_key) => request("/api/me/annotations", { params: { book_key, limit: 2000 } }),
  putBookmark: (data) => request("/api/me/bookmarks", { method: "PUT", body: data }),
  deleteBookmark: (id) => request(`/api/me/bookmarks/${id}`, { method: "DELETE" }),
  addHighlight: (data) => request("/api/me/highlights", { method: "POST", body: data }),
  patchHighlight: (id, patch) => request(`/api/me/highlights/${id}`, { method: "PATCH", body: patch }),
  deleteHighlight: (id) => request(`/api/me/highlights/${id}`, { method: "DELETE" }),
};
