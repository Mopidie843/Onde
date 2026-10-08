// Console développeur (F7) — partie toujours active, chargée AVANT tout le reste : elle note ce qui
// se passe dans la page (messages, erreurs, requêtes) sans rien changer à son fonctionnement.
// L'affichage (devconsole.js) n'est chargé qu'à la première ouverture.
const MAX_LOGS = 1500;
const MAX_NET = 400;

export const dev = {
  logs: [],          // { id, ts, level, source: "page", message }
  net: [],           // { id, ts, method, url, status, ms, size, error, type }
  errors: 0,
  ctx: {},           // { player, api } : donnés par app.js pour les commandes de la console
  listeners: new Set(),
};
let seq = 0;
// identifiant de cette page : ses propres erreurs, renvoyées par le serveur, ne s'affichent pas deux fois
export const pageId = Math.random().toString(36).slice(2, 10);
const nativeFetch = window.fetch.bind(window);
export const rawFetch = nativeFetch;

function emit(kind, item) {
  for (const fn of dev.listeners) { try { fn(kind, item); } catch { /* ignore */ } }
}

export function fmt(v, depth = 0) {
  if (typeof v === "string") return v;
  if (v instanceof Error) return `${v.name}: ${v.message}${v.stack ? `\n${String(v.stack).split("\n").slice(1, 6).join("\n")}` : ""}`;
  if (v === undefined) return "undefined";
  if (v === null || typeof v !== "object") return String(v);
  if (typeof Node !== "undefined" && v instanceof Node) return `<${(v.nodeName || "").toLowerCase()}${v.id ? `#${v.id}` : ""}>`;
  try {
    const seen = new WeakSet();
    return JSON.stringify(v, (k, x) => {
      if (x && typeof x === "object") { if (seen.has(x)) return "[circulaire]"; seen.add(x); }
      if (typeof x === "function") return `ƒ ${x.name || ""}()`;
      return x;
    }, depth ? 0 : 2)?.slice(0, 20000) ?? String(v);
  } catch { return String(v); }
}

let last = null;
export function log(level, message, { local = false } = {}) {
  const text = String(message).slice(0, 20000);
  if (last && last.message === text && last.level === level && Date.now() - last.ts < 5000) {
    last.count = (last.count || 1) + 1;
    last.ts = Date.now();
    emit("update", last);
    return last;
  }
  const item = { id: ++seq, ts: Date.now(), level, source: "page", message: text };
  dev.logs.push(item);
  if (dev.logs.length > MAX_LOGS) dev.logs.splice(0, dev.logs.length - MAX_LOGS);
  if (level === "error") dev.errors++;
  last = item;
  emit("log", item);
  if (!local && (level === "error" || level === "warn")) report(item);
  return item;
}

// ------------------------------------------------------------------ console.*
for (const level of ["log", "info", "warn", "error", "debug"]) {
  const orig = console[level];
  console[level] = function (...args) {
    try { log(level === "log" ? "info" : level, args.map((a) => fmt(a)).join(" ")); } catch { /* ignore */ }
    return orig.apply(this, args);
  };
}

// ------------------------------------------------------------------ erreurs
window.addEventListener("error", (e) => {
  const t = e.target;
  if (t && t !== window && t.nodeType === 1) { // fichier qui ne charge pas (image, son, script…)
    const tag = t.tagName.toLowerCase();
    const src = t.currentSrc || t.src || t.href || "";
    if (src.startsWith("data:")) return;
    const media = t.error ? ` (code ${t.error.code}${t.error.message ? ` : ${t.error.message}` : ""})` : "";
    log(tag === "img" ? "info" : "warn", `Impossible de charger <${tag}> ${src}${media}`);
    return;
  }
  const where = e.filename ? ` (${e.filename.replace(location.origin, "")}:${e.lineno}:${e.colno})` : "";
  log("error", `${e.error ? fmt(e.error) : e.message}${where}`);
}, true);
window.addEventListener("unhandledrejection", (e) => log("error", `Promesse rejetée : ${fmt(e.reason)}`));

// ------------------------------------------------------------------ réseau
function netStart(method, url) {
  const item = { id: ++seq, ts: Date.now(), method: (method || "GET").toUpperCase(), url: String(url), status: null, ms: null, size: null, error: null, type: "" };
  const own = item.url.includes("/api/dev/");
  if (!own) {
    dev.net.push(item);
    if (dev.net.length > MAX_NET) dev.net.splice(0, dev.net.length - MAX_NET);
    emit("net", item);
  }
  return { item, own, t0: performance.now() };
}
function netEnd(rec, status, size, type, error) {
  const it = rec.item;
  it.ms = Math.round(performance.now() - rec.t0);
  it.status = status;
  it.size = size;
  it.type = type || "";
  it.error = error || null;
  if (!rec.own) emit("netdone", it);
}

window.fetch = async function (input, init) {
  const url = typeof input === "string" ? input : (input && input.url) || String(input);
  const method = (init && init.method) || (input && input.method) || "GET";
  const rec = netStart(method, url);
  try {
    const res = await nativeFetch(input, init);
    const len = +res.headers.get("content-length") || null;
    netEnd(rec, res.status, len, res.headers.get("content-type"));
    return res;
  } catch (err) {
    netEnd(rec, 0, null, "", (err && err.message) || String(err));
    throw err;
  }
};

const xOpen = XMLHttpRequest.prototype.open;
const xSend = XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open = function (method, url, ...rest) {
  this.__dev = { method, url };
  return xOpen.call(this, method, url, ...rest);
};
XMLHttpRequest.prototype.send = function (body) {
  const d = this.__dev;
  if (d) {
    const rec = netStart(d.method, d.url);
    this.addEventListener("loadend", () => {
      netEnd(rec, this.status, +(this.getResponseHeader?.("content-length") || 0) || null,
        this.getResponseHeader?.("content-type"), this.status ? null : "échec réseau ou annulé");
    }, { once: true });
  }
  return xSend.call(this, body);
};

// ------------------------------------------------------------------ envoi des erreurs au serveur
// (pour voir depuis un PC ce qui se passe sur le téléphone ou l'appli)
let queue = [];
let timer = 0;
let sentThisMinute = 0;
let minuteStart = Date.now();

export function deviceName() {
  const ua = navigator.userAgent;
  try { if (window.OndeApp) return `Appli Android ${window.OndeApp.version()}`; } catch { /* ignore */ }
  const standalone = window.matchMedia?.("(display-mode: standalone)").matches || navigator.standalone;
  let os = /iPhone/.test(ua) ? "iPhone" : /iPad|Macintosh.*Mobile/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1) ? "iPad"
    : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows" : /Mac OS X/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : "Appareil";
  const br = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /CriOS|Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "navigateur";
  return `${os} · ${standalone ? "écran d'accueil" : br}`;
}

function report(item) {
  if (item.message.startsWith("Impossible de charger <img>")) return;
  queue.push({ level: item.level, message: item.message.slice(0, 3000), page: location.hash || "#/" });
  if (queue.length > 30) queue = queue.slice(-30);
  if (!timer) timer = setTimeout(flush, 3000);
}

function flush() {
  timer = 0;
  if (!queue.length) return;
  if (Date.now() - minuteStart > 60_000) { minuteStart = Date.now(); sentThisMinute = 0; }
  const room = 30 - sentThisMinute;
  if (room <= 0) { queue = []; return; }
  const items = queue.splice(0, room);
  sentThisMinute += items.length;
  queue = [];
  nativeFetch("/api/dev/client", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device: deviceName(), page: pageId, items }),
  }).catch(() => { /* serveur injoignable : tant pis, c'est noté ici */ });
}

// ------------------------------------------------------------------ ouverture (F7)
let loading = null;
export function toggleConsole(force) {
  loading = loading || import("./devconsole.js");
  return loading.then((m) => m.toggle(force)).catch((e) => { loading = null; console.error("Console développeur : chargement impossible", e); });
}
window.addEventListener("keydown", (e) => {
  if (e.key === "F7" && !e.altKey && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    e.stopPropagation();
    toggleConsole();
  }
}, true);
document.addEventListener("click", (e) => {
  if (e.target.closest?.("[data-devconsole]")) toggleConsole(true);
});
window.OndeConsole = { open: () => toggleConsole(true), close: () => toggleConsole(false), dev };
