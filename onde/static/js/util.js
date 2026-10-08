// Petites fonctions utilitaires partagées.
export const $ = (sel, el = document) => el.querySelector(sel);
export const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
export const enc = (s) => encodeURIComponent(s ?? "");

export const icon = (name, cls = "") => `<svg class="ic ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;

const pad = (n) => String(n).padStart(2, "0");
export function fmtTime(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}
export function fmtLong(sec) {
  sec = Math.round(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min ${pad(s)} s`;
  return `${s} s`;
}
export function fmtSize(bytes) {
  const u = ["o", "Ko", "Mo", "Go", "To"];
  let i = 0, n = bytes || 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0).replace(".", ",")} ${u[i]}`;
}
const rtf = new Intl.RelativeTimeFormat("fr", { numeric: "auto" });
const dtf = new Intl.DateTimeFormat("fr", { day: "numeric", month: "short", year: "numeric" });
export function fmtRel(ts) {
  if (!ts) return "";
  const diff = (ts * 1000 - Date.now()) / 1000;
  const a = Math.abs(diff);
  if (a < 60) return "à l'instant";
  if (a < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (a < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  if (a < 86400 * 7) return rtf.format(Math.round(diff / 86400), "day");
  if (a < 86400 * 35) return rtf.format(Math.round(diff / 604800), "week");
  return dtf.format(new Date(ts * 1000));
}
export const plural = (n, one, many) => `${n.toLocaleString("fr")} ${n > 1 ? many : one}`;

export function hue(str) {
  let h = 0;
  for (const ch of String(str || "")) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return h % 360;
}

// --- pochettes -----------------------------------------------------------
const SIZES = [96, 192, 384, 768, 1000];
export const coverUrl = (id, size = 384) => (id ? `/api/cover/${id}/${size}` : "");
// pochettes gardées sur l'appareil (écoute hors connexion) : (id, taille) -> URL locale ou null
let localCover = () => null;
let offlineNow = () => false;
export function setCoverResolver(fn, isOffline = () => false) { localCover = fn; offlineNow = isOffline; }
/** Réseau lent (VPN, 4G faible, économie de données) : on charge des pochettes plus légères. */
export function slowNet() {
  const c = navigator.connection;
  return !!c && (c.saveData || /(^|-)(2g|3g)$/.test(c.effectiveType || "") || (c.rtt || 0) > 350);
}
export function coverImg(id, size, alt = "", cls = "cover-img", eager = false) {
  if (!id) return "";
  const local = localCover(id, size);
  if (local) return `<img class="${cls}" src="${local}" alt="${esc(alt)}" decoding="async" draggable="false">`;
  if (offlineNow()) return placeholder(alt || id, "note"); // hors connexion : pas d'image cassée
  const next = SIZES[SIZES.indexOf(size) + 1] || size;
  const set = slowNet() ? "" : ` srcset="${coverUrl(id, size)} 1x, ${coverUrl(id, next)} 2x"`;
  return `<img class="${cls}" src="${coverUrl(id, size)}"${set} alt="${esc(alt)}" ${eager ? "" : 'loading="lazy" fetchpriority="low"'} decoding="async" draggable="false">`;
}
/** URL d'une pochette pour l'écran de verrouillage (locale si elle est sur l'appareil). */
export const artworkUrl = (id, size) => (offlineNow() && localCover(id, size)) || `${coverUrl(id, size)}?f=jpg`;
export function placeholder(seed, kind = "note") {
  return `<div class="ph" style="--h:${hue(seed)}">${icon(kind)}</div>`;
}
export function cover(id, size, seed, kind = "note", alt = "") {
  return id ? coverImg(id, size, alt) : placeholder(seed, kind);
}
export function mosaic(ids, size, seed) {
  if (!ids || !ids.length) return placeholder(seed, "note");
  if (ids.length < 4) return coverImg(ids[0], size);
  const half = size >= 384 ? 192 : 96;
  return `<div class="mosaic">${ids.slice(0, 4).map((id) => coverImg(id, half)).join("")}</div>`;
}
export const likedCover = () => `<div class="ph liked">${icon("heart-fill")}</div>`;

// --- divers ---------------------------------------------------------------
export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
export function shuffleArr(a) {
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}
export function greeting() {
  const h = new Date().getHours();
  if (h < 5) return "Bonne nuit";
  if (h < 18) return "Bonjour";
  return "Bonsoir";
}
export const isTouch = () => matchMedia("(hover: none)").matches;
// vrai téléphone : petit écran, ou écran court + tactile (téléphone à l'horizontale) — garde la mise
// en page mobile (barre d'onglets, menus en feuille) en cohérence avec le point de rupture CSS.
export const isMobile = () => matchMedia("(max-width: 760px), (max-height: 500px) and (pointer: coarse)").matches;
export const AUDIO_EXT = /\.(mp3|m4a|aac|flac|ogg|oga|opus|wav)$/i;
// formats audio -> type MIME, et « ce navigateur sait-il lire ce format ? » (sinon : converti par le Pi)
export const AUDIO_MIME = {
  mp3: "audio/mpeg", m4a: "audio/mp4", aac: "audio/aac", flac: "audio/flac", ogg: "audio/ogg",
  oga: "audio/ogg", opus: 'audio/ogg; codecs="opus"', wav: "audio/wav", aif: "audio/aiff", aiff: "audio/aiff",
};
const playable = {};
let probe = null;
export function canPlayExt(ext) {
  if (!ext) return true;
  if (!(ext in playable)) {
    try { probe ||= document.createElement("audio"); playable[ext] = !!AUDIO_MIME[ext] && probe.canPlayType(AUDIO_MIME[ext]) !== ""; } catch { playable[ext] = false; }
  }
  return playable[ext];
}
export function markUnplayable(ext) { if (ext) playable[ext] = false; }
export const storage = {
  get(k, d = null) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* navigation privée */ } },
};
