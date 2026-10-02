/* Utilitários de interface: escape, toasts, capas, cards e modais. */
import { t } from "./i18n.js";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function esc(str) {
  if (str === null || str === undefined) return "";
  return String(str).replace(/[&<>"']/g, s => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[s]));
}

export const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;

const recentToasts = new Map();
export function toast(msg, iconName) {
  if (!msg) return;
  // Não repete a mesma mensagem em sequência (ex.: sessão expirada durante várias ações).
  const now = Date.now();
  if (now - (recentToasts.get(msg) || 0) < 2000) return;
  recentToasts.set(msg, now);
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = (iconName ? icon(iconName) : "") + `<span>${esc(msg)}</span>`;
  $("#toast-container").appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

// ------------------------------------------------------------ registro de livros
// Cada card guarda só um id; o objeto completo fica aqui (evita JSON em atributos HTML).
const registry = new Map();
let seq = 0;
export function register(book) { const id = `b${++seq}`; registry.set(id, book); return id; }
export function lookup(id) { return registry.get(id); }

export const detailsHref = (key) => `#/livro?key=${encodeURIComponent(key)}`;

// ------------------------------------------------------------ capas
// Tecidos de encadernação: cor do tecido + cor da tinta.
const CLOTHS = [
  ["#6b2d2a", "#f1dcc4"], ["#2f4a3a", "#e7dfc6"], ["#23344f", "#e9dcc0"], ["#8a6a2c", "#fbf1dc"],
  ["#4d2f4f", "#efdbe6"], ["#3c4043", "#e9e2d2"], ["#7a3b1d", "#f6e3cc"], ["#1f4d52", "#dcebe4"],
];
function hash(s) { let h = 0; for (const c of s || "") h = (h * 31 + c.charCodeAt(0)) >>> 0; return h; }

export function generatedCover(title, author, extraClass = "") {
  const [cloth, ink] = CLOTHS[hash(title) % CLOTHS.length];
  const shortAuthor = (author || "").split(",")[0];
  return `<div class="cover generated ${extraClass}" style="--cloth:${cloth}; --cloth-ink:${ink}" aria-hidden="true">
    <span class="g-title">${esc(title)}</span>
    <span><span class="g-rule" style="display:block"></span><span class="g-author">${esc(shortAuthor)}</span></span>
  </div>`;
}

export function coverHTML(book, extraClass = "") {
  if (book.cover_url) {
    return `<div class="cover ${extraClass}" data-title="${esc(book.title)}" data-author="${esc(book.author || "")}">
      <img src="${esc(book.cover_url)}" alt="" loading="lazy"></div>`;
  }
  return generatedCover(book.title, book.author, extraClass);
}

// Se a capa da Open Library não carregar, troca pela capa tipográfica.
document.addEventListener("error", (e) => {
  const img = e.target;
  if (!(img instanceof HTMLImageElement)) return;
  const box = img.closest(".cover");
  if (!box || box.classList.contains("generated")) return;
  const extra = [...box.classList].filter(c => c !== "cover").join(" ");
  box.outerHTML = generatedCover(box.dataset.title, box.dataset.author, extra);
}, true);

// ------------------------------------------------------------ cards
export function bookCardHTML(book, { isFavorite = false, inLibrary = false, statusLabel = "" } = {}) {
  const bid = register(book);
  const href = detailsHref(book.book_key);
  const tag = statusLabel ? `<span class="tag status">${esc(statusLabel)}</span>`
    : book.private ? `<span class="tag private" title="${esc(t("book.yourFileTitle"))}">${icon("file")}${esc(t("book.yourFile"))}</span>`
    : book.in_catalog || book.source === "acervo" || String(book.book_key || "").startsWith("gutenberg:")
      ? `<span class="tag catalog" title="${esc(t("book.inCatalogTitle"))}">${icon("check")}${esc(t("book.inCatalog"))}</span>`
      : book.gutenberg_id ? `<span class="tag free">${esc(t("book.free"))}</span>` : "";
  return `<article class="book-card">
    <a class="cover-link" href="${href}" aria-label="${esc(book.title)}">${coverHTML(book)}</a>
    <h3 class="b-title"><a href="${href}">${esc(book.title)}</a></h3>
    <p class="b-author">${esc(book.author || t("book.unknownAuthor"))}</p>
    <div class="b-foot">
      <button class="icon-btn ${isFavorite ? "active" : ""}" data-action="fav" data-bid="${bid}"
        aria-pressed="${isFavorite}" aria-label="${esc(t(isFavorite ? "book.unfavorite" : "book.favorite"))}">${icon("heart")}</button>
      <button class="icon-btn ${inLibrary ? "active" : ""}" data-action="want" data-bid="${bid}"
        aria-label="${esc(t(inLibrary ? "book.inLibrary" : "book.addWant"))}" title="${esc(t(inLibrary ? "book.inLibrary" : "book.addWant"))}">${icon(inLibrary ? "check" : "plus")}</button>
      ${tag}
    </div>
  </article>`;
}

export function skeletonRow(n = 6) {
  return Array.from({ length: n }, () =>
    `<div class="sk-card" aria-hidden="true"><div class="skeleton sk-cover"></div><div class="skeleton sk-line"></div><div class="skeleton sk-line short"></div></div>`
  ).join("");
}

export function emptyState(title, text = "", actionHTML = "") {
  return `<div class="empty-state"><h3>${esc(title)}</h3>${text ? `<p>${esc(text)}</p>` : ""}${actionHTML}</div>`;
}

export const loading = (msg) => `<div class="loading"><div class="spinner"></div>${esc(msg)}</div>`;

// ------------------------------------------------------------ destaque de palavras
const strip = (s) => s.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase();

/** Marca no trecho do livro as palavras (com 4+ letras) que o usuário digitou.
    Separa as palavras ANTES de escapar o HTML (antes, "&quot;" podia virar "&<mark>quot</mark>;"). */
export function highlight(text, query) {
  const words = new Set(strip(query).split(/[^a-z0-9]+/).filter(w => w.length >= 4));
  return String(text).split(/([\p{L}\p{N}]+)/u)
    .map((part, i) => (i % 2 === 1 && words.has(strip(part)) ? `<mark>${esc(part)}</mark>` : esc(part)))
    .join("");
}

// ------------------------------------------------------------ modais
let lastFocus = null;
export function openModal(id) {
  lastFocus = document.activeElement;
  const ov = document.getElementById(id);
  ov.classList.add("active");
  const first = ov.querySelector("input:not([type=file]):not([hidden]), textarea, button.btn");
  setTimeout(() => first && first.focus(), 30);
}
// Mantém o foco do teclado dentro do modal aberto (antes o Tab ia para a página por trás).
document.addEventListener("keydown", (e) => {
  if (e.key !== "Tab") return;
  const ov = $(".overlay.active");
  if (!ov) return;
  const items = $$("a[href], button:not([disabled]), input:not([type=hidden]):not([hidden]), textarea, select", ov)
    .filter(el => el.offsetParent !== null);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  else if (!ov.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
});

export function closeModal(id) {
  const ov = id ? document.getElementById(id) : $(".overlay.active");
  if (!ov) return;
  ov.classList.remove("active");
  if (lastFocus) lastFocus.focus();
}
