// Console développeur (F7, ou Réglages → Console développeur sur téléphone) : journal du serveur et de
// la page, requêtes, paroles, IA, système, lecteur, et une ligne de commande JavaScript.
import { dev, log as devLog, fmt, rawFetch, deviceName, pageId } from "./devhooks.js";

const log = (level, message) => devLog(level, message, { local: true }); // (la console ne se signale pas elle-même au serveur)

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem(`onde.dev.${k}`); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(`onde.dev.${k}`, JSON.stringify(v)); } catch { /* navigation privée */ } },
};
const TABS = [["journal", "Journal"], ["reseau", "Réseau"], ["paroles", "Paroles"], ["ia", "IA"], ["systeme", "Système"], ["lecteur", "Lecteur"]];
const SOURCES = {
  "onde.lyrics": "paroles", "onde.align": "IA", "onde.library": "bibliothèque", "onde.download": "téléchargement",
  "onde.covers": "pochettes", "onde.net": "réseau", "onde.notify": "notifications", "onde.pi": "lecteur Pi",
  "onde.pi_player": "lecteur Pi", "onde.tls": "https", "onde.remote": "distance", "onde.discover": "découverte",
  "onde.smart": "playlists", "onde": "onde", "uvicorn.error": "serveur", "uvicorn": "serveur", page: "appareil",
};
const LEVELS = { DEBUG: 0, debug: 0, INFO: 1, info: 1, cmd: 1, result: 1, WARNING: 2, warn: 2, ERROR: 3, error: 3, CRITICAL: 3 };
const LV_LABEL = (lv) => ({ DEBUG: "DBG", debug: "DBG", INFO: "INF", info: "INF", WARNING: "AVT", warn: "AVT", ERROR: "ERR", error: "ERR", CRITICAL: "ERR", cmd: "›", result: "‹" }[lv] || lv);
const MAX_SERVER = 3000;
const MAX_ROWS = 900;

let el = null;
let open = false;
let tab = store.get("tab", "journal");
let filt = { level: store.get("level", 1), source: store.get("source", "all"), text: "" };
let server = [];          // journal du serveur
let serverLast = null;
let polling = false;
let pollCtl = null;
let clearedAt = 0;
let follow = true;
let tabTimer = 0;
let netMode = "page";
let serverHttp = [];
let serverHttpLast = null;
let history = store.get("history", []);
let hpos = -1;
let debugOn = false;
let authProblem = "";

const time = (ts) => { const d = new Date(ts); return `${d.toLocaleTimeString("fr-FR", { hour12: false })}.${String(d.getMilliseconds()).padStart(3, "0")}`; };
const size = (n) => (n == null ? "" : n < 1024 ? `${n} o` : n < 1048576 ? `${(n / 1024).toFixed(1)} Ko` : `${(n / 1048576).toFixed(1)} Mo`);
const dur = (s) => { s = Math.round(s || 0); const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return d ? `${d} j ${h} h` : h ? `${h} h ${m} min` : m ? `${m} min ${s % 60} s` : `${s} s`; };
const ago = (ts) => dur(Date.now() / 1000 - ts) ;

async function getJSON(url, init) {
  const r = await rawFetch(url, { cache: "no-store", ...init, headers: { "Content-Type": "application/json", ...(init?.headers || {}) } });
  if (r.status === 401) { authProblem = "Connexion requise : recharge la page et entre le mot de passe."; throw new Error("401"); }
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

// ------------------------------------------------------------------ construction
function build() {
  el = document.createElement("div");
  el.id = "devc";
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-label", "Console développeur");
  el.innerHTML = `
    <div class="dc-grip" title="Glisser pour redimensionner"></div>
    <div class="dc-bar">
      <b class="dc-title">Console</b>
      <nav class="dc-tabs">${TABS.map(([k, t]) => `<button data-tab="${k}">${t}</button>`).join("")}</nav>
      <div class="dc-acts">
        <label class="dc-chk" title="Journal détaillé du serveur (chaque requête vers LRCLIB, chaque nouvel essai…)"><input type="checkbox" data-act="debug"> détaillé</label>
        <button data-act="copy" title="Copier">Copier</button>
        <button data-act="save" title="Télécharger le journal">Télécharger</button>
        <button data-act="clear" title="Effacer l'affichage">Effacer</button>
        <button data-act="close" class="dc-x" title="Fermer (F7 / Échap)" aria-label="Fermer">✕</button>
      </div>
    </div>
    <div class="dc-tools"></div>
    <div class="dc-body" tabindex="-1"></div>
    <form class="dc-cmd" autocomplete="off"><span>›</span><input name="cmd" spellcheck="false" autocapitalize="off" placeholder="Commande JavaScript — player, api, dev… (tape help)"></form>`;
  document.body.append(el);
  const h = store.get("height", 0);
  if (h) el.style.setProperty("--devc-h", `${h}px`);
  el.addEventListener("click", onClick);
  el.addEventListener("change", onChange);
  el.addEventListener("input", (e) => { if (e.target.name === "q") { filt.text = e.target.value.toLowerCase(); renderJournal(); } });
  el.querySelector(".dc-cmd").addEventListener("submit", (e) => { e.preventDefault(); const i = e.target.cmd; const code = i.value.trim(); if (code) { i.value = ""; run(code); } });
  el.querySelector(".dc-cmd input").addEventListener("keydown", onCmdKey);
  el.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); toggle(false); } });
  const body = el.querySelector(".dc-body");
  body.addEventListener("scroll", () => { if (tab === "journal") follow = body.scrollHeight - body.scrollTop - body.clientHeight < 30; }, { passive: true });
  grip(el.querySelector(".dc-grip"));
  dev.listeners.add(onDev);
}

function grip(g) {
  g.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    g.setPointerCapture(e.pointerId);
    const move = (ev) => {
      const hgt = Math.max(160, Math.min(window.innerHeight - 60, window.innerHeight - ev.clientY));
      el.style.setProperty("--devc-h", `${hgt}px`);
      store.set("height", hgt);
    };
    const up = () => { g.removeEventListener("pointermove", move); g.removeEventListener("pointerup", up); };
    g.addEventListener("pointermove", move);
    g.addEventListener("pointerup", up);
  });
}

export function toggle(force) {
  const want = force === undefined ? !open : !!force;
  if (want === open) { if (want) el.querySelector(".dc-cmd input")?.focus({ preventScroll: true }); return; }
  open = want;
  if (open) {
    if (!el) build();
    el.hidden = false;
    document.documentElement.classList.add("devc-open");
    showTab(tab);
    startPolling();
    getJSON("/api/dev/info").then((d) => { debugOn = !!d.debug; const c = el.querySelector("[data-act=debug]"); if (c) c.checked = debugOn; }).catch(() => {});
  } else if (el) {
    el.hidden = true;
    document.documentElement.classList.remove("devc-open");
    stopPolling();
    clearTimeout(tabTimer);
  }
}

// ------------------------------------------------------------------ journal du serveur
function startPolling() {
  if (polling) return;
  polling = true;
  (async () => {
    while (polling) {
      try {
        pollCtl = new AbortController();
        const after = serverLast == null ? "" : serverLast;
        const r = await getJSON(`/api/dev/logs?after=${after}&wait=${serverLast == null ? 0 : 25}`, { signal: pollCtl.signal });
        authProblem = "";
        if (r.reset) server = [];
        const fresh = r.items.filter((i) => i.pageId !== pageId);
        for (const i of fresh) { i.ts *= 1000; i.where = i.source === "page" ? "device" : "server"; }
        r.items = fresh;
        server.push(...fresh);
        if (server.length > MAX_SERVER) server.splice(0, server.length - MAX_SERVER);
        serverLast = r.last;
        if (tab === "journal") { if (r.reset || r.items.length > 50) renderJournal(); else r.items.forEach((i) => appendRow(i)); }
      } catch (e) {
        if (!polling) break;
        if (tab === "journal" && authProblem) renderJournal();
        await new Promise((res) => setTimeout(res, 3000));
      }
    }
  })();
}
function stopPolling() { polling = false; pollCtl?.abort(); }

function onDev(kind, item) {
  if (!open) return;
  if (tab === "journal" && kind === "log") appendRow(item);
  else if (tab === "journal" && kind === "update") renderJournal();
  else if (tab === "reseau" && netMode === "page" && (kind === "net" || kind === "netdone")) renderNet();
}

function tagged() {
  for (const i of dev.logs) i.where = "local";
  return [...server, ...dev.logs].sort((a, b) => a.ts - b.ts);
}

function matches(i) {
  if (i.id <= 0) return false;
  if ((LEVELS[i.level] ?? 1) < filt.level) return false;
  const src = i.where;
  if (filt.source === "server" && src !== "server") return false;
  if (filt.source === "local" && src !== "local") return false;
  if (filt.source === "device" && src !== "device") return false;
  if (filt.source.startsWith("src:") && (SOURCES[i.source] || i.source) !== filt.source.slice(4)) return false;
  if (filt.text && !`${i.message} ${SOURCES[i.source] || i.source}`.toLowerCase().includes(filt.text)) return false;
  return i.ts > clearedAt;
}

function row(i) {
  const lv = String(i.level);
  const src = i.where === "local" ? "cette page" : (SOURCES[i.source] || i.source);
  const msg = String(i.message);
  const nl = msg.indexOf("\n");
  const body = nl > 0 && msg.length > 160
    ? `<details><summary>${esc(msg.slice(0, nl))}</summary><pre>${esc(msg.slice(nl + 1))}</pre></details>`
    : `<span class="dc-msg">${esc(msg)}</span>`;
  return `<div class="dc-row lv-${LEVELS[lv] ?? 1}${lv === "cmd" ? " is-cmd" : ""}"><time>${time(i.ts)}</time><span class="dc-lv">${esc(LV_LABEL(lv))}</span><span class="dc-src">${esc(src)}${i.count > 1 ? ` ×${i.count}` : ""}</span>${body}</div>`;
}

function journalTools() {
  const srcs = [...new Set(server.map((i) => SOURCES[i.source] || i.source))].filter((s) => s !== "appareil").sort();
  return `<select name="level" aria-label="Niveau">
      <option value="0">tout (détails compris)</option><option value="1">infos et plus</option><option value="2">avertissements et erreurs</option><option value="3">erreurs</option></select>
    <select name="source" aria-label="Provenance">
      <option value="all">tout</option><option value="server">serveur</option><option value="local">cette page</option><option value="device">autres appareils</option>
      ${srcs.map((s) => `<option value="src:${esc(s)}">${esc(s)}</option>`).join("")}</select>
    <input name="q" type="search" placeholder="Filtrer…" value="${esc(filt.text)}" aria-label="Filtrer le journal">
    <span class="dc-dim" data-count></span>`;
}

function renderJournal() {
  if (!open || tab !== "journal") return;
  const body = el.querySelector(".dc-body");
  const items = tagged().filter(matches);
  const shown = items.slice(-MAX_ROWS);
  body.innerHTML = (authProblem ? `<div class="dc-note err">${esc(authProblem)}</div>` : "")
    + (items.length > shown.length ? `<div class="dc-note">… ${items.length - shown.length} messages plus anciens masqués (filtre pour les voir)</div>` : "")
    + (shown.length ? shown.map(row).join("") : `<div class="dc-note">Rien pour l'instant. Les messages du serveur et de la page s'affichent ici en direct.</div>`);
  const c = el.querySelector("[data-count]");
  if (c) c.textContent = `${items.length} message${items.length > 1 ? "s" : ""}`;
  if (follow) body.scrollTop = body.scrollHeight;
}

function appendRow(i) {
  if (!open || tab !== "journal") return;
  const it = i;
  if (!it.where) it.where = "local";
  if (!matches(it)) return;
  const body = el.querySelector(".dc-body");
  body.querySelector(".dc-note:not(.err)")?.remove();
  body.insertAdjacentHTML("beforeend", row(it));
  while (body.children.length > MAX_ROWS) body.firstElementChild.remove();
  if (follow) body.scrollTop = body.scrollHeight;
}

// ------------------------------------------------------------------ onglets
function showTab(k) {
  tab = TABS.some(([t]) => t === k) ? k : "journal";
  store.set("tab", tab);
  clearTimeout(tabTimer);
  el.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  el.querySelector(".dc-cmd").hidden = tab !== "journal";
  const tools = el.querySelector(".dc-tools");
  const body = el.querySelector(".dc-body");
  body.innerHTML = "";
  if (tab === "journal") {
    tools.innerHTML = journalTools();
    tools.querySelector("[name=level]").value = String(filt.level);
    tools.querySelector("[name=source]").value = filt.source;
    if (tools.querySelector("[name=source]").value !== filt.source) { filt.source = "all"; tools.querySelector("[name=source]").value = "all"; }
    follow = true;
    renderJournal();
  } else if (tab === "reseau") {
    tools.innerHTML = `<div class="dc-seg"><button data-net="page">Cette page</button><button data-net="server">Serveur (tous les appareils)</button></div><span class="dc-dim" data-count></span>`;
    renderNet();
  } else if (tab === "paroles") { tools.innerHTML = `<button data-act="lyr-retry">Tout rechercher maintenant</button><button data-act="lyr-now">Chercher le titre en cours</button><span class="dc-dim" data-count></span>`; tickLyrics(); }
  else if (tab === "ia") { tools.innerHTML = `<button data-act="ia-now">Caler le titre en cours</button><span class="dc-dim" data-count></span>`; tickAI(); }
  else if (tab === "systeme") { tools.innerHTML = `<button data-act="sys-refresh">Rafraîchir</button><button data-act="sys-copy">Copier le rapport</button><span class="dc-dim" data-count></span>`; renderSystem(); }
  else if (tab === "lecteur") { tools.innerHTML = `<span class="dc-dim">Mis à jour chaque seconde</span>`; tickPlayer(); }
}

// réseau
function renderNet() {
  if (!open || tab !== "reseau") return;
  el.querySelectorAll("[data-net]").forEach((b) => b.classList.toggle("on", b.dataset.net === netMode));
  const body = el.querySelector(".dc-body");
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 30;
  const list = netMode === "page" ? dev.net : serverHttp;
  const rows = list.slice(-400).map((n) => {
    const st = n.status === null ? "…" : n.status === 0 ? "échec" : n.status;
    const cls = n.status === null ? "" : n.status === 0 || n.status >= 500 ? "lv-3" : n.status >= 400 ? "lv-2" : "";
    const url = netMode === "page" ? String(n.url).replace(location.origin, "") : `${n.path}${n.query ? `?${n.query}` : ""}`;
    const ts = netMode === "page" ? n.ts : n.ts * 1000;
    return `<div class="dc-net ${cls}"><time>${time(ts)}</time><b>${esc(n.method)}</b><span class="dc-st">${esc(st)}</span><span class="dc-ms">${n.ms == null ? "" : `${n.ms} ms`}</span><span class="dc-sz">${size(n.size)}</span><span class="dc-url" title="${esc(url)}">${esc(url)}${n.error ? ` — ${esc(n.error)}` : ""}${netMode === "server" ? ` <i class="dc-dim">${esc(n.client)}</i>` : ""}</span></div>`;
  });
  body.innerHTML = `<div class="dc-net dc-head"><time>heure</time><b>méth.</b><span class="dc-st">état</span><span class="dc-ms">durée</span><span class="dc-sz">taille</span><span class="dc-url">adresse</span></div>${rows.join("") || `<div class="dc-note">Aucune requête pour l'instant.</div>`}`;
  const c = el.querySelector("[data-count]");
  if (c) c.textContent = `${list.length} requête${list.length > 1 ? "s" : ""}`;
  if (atBottom || body.scrollTop === 0) body.scrollTop = body.scrollHeight;
  if (netMode === "server") {
    clearTimeout(tabTimer);
    tabTimer = setTimeout(async () => {
      try {
        const r = await getJSON(`/api/dev/http${serverHttpLast == null ? "" : `?after=${serverHttpLast}`}`);
        if (r.reset) serverHttp = [];
        serverHttp.push(...r.items);
        serverHttp = serverHttp.slice(-600);
        serverHttpLast = r.last;
      } catch { /* ignore */ }
      if (netMode === "server") renderNet();
    }, serverHttpLast == null ? 0 : 3000);
  }
}

// paroles
const RESULT = { synced: ["synchronisées", "ok"], plain: ["non synchronisées", "mid"], instrumental: ["instrumental", "mid"], missing: ["introuvables", "bad"], offline: ["LRCLIB injoignable", "bad"], error: ["erreur", "bad"] };
async function tickLyrics() {
  if (!open || tab !== "paroles") return;
  const body = el.querySelector(".dc-body");
  try {
    const s = await getJSON("/api/lyrics/status");
    if (tab !== "paroles") return;
    const c = s.counts;
    const pct = c.tracks ? Math.round((c.synced + c.plain + c.instrumental) / c.tracks * 100) : 0;
    body.innerHTML = `<div class="dc-cards">
      ${card("Titres", c.tracks)}${card("Synchronisées", c.synced, "ok")}${card("Non synchronisées", c.plain, "mid")}${card("Instrumentaux", c.instrumental)}
      ${card("Introuvables (réessai auto)", c.missing, c.missing ? "bad" : "")}${card("Jamais cherchés", c.never)}${card("À chercher maintenant", s.pending)}${card("Couverture", `${pct} %`)}
    </div>
    <p class="dc-line">${!s.enabled ? `<b class="bad">Recherche en ligne désactivée</b> (Réglages → Paroles en ligne).` : s.current
      ? `<span class="dc-spin"></span> Recherche : <b title="${esc(`${s.current.artist} — ${s.current.title}`)}">${esc(s.current.query || `${s.current.artist} — ${s.current.title}`)}</b> (${esc(s.current.why)})`
      : s.offlineSince ? `<b class="bad">LRCLIB injoignable</b> depuis ${ago(s.offlineSince)} — reprise automatique.`
      : s.nextRound ? `En pause — prochaine tournée dans ${dur(s.nextRound - Date.now() / 1000)} (ou dès qu'un titre est lancé / ajouté).` : s.running ? (s.session.searches ? "En cours…" : "Démarre dans un instant (Onde finit d'abord de démarrer).") : "Arrêtée."}
      ${s.session.searches ? ` · depuis le démarrage : ${s.session.searches} recherche(s), ${s.session.requests || 0} requête(s) LRCLIB` : ""}${s.priority.length ? ` · ${s.priority.length} en priorité` : ""}</p>
    <h4>Dernières recherches</h4>
    ${s.recent.length ? `<div class="dc-table">${s.recent.map((r) => `<div class="dc-tr"><time>${time(r.ts * 1000)}</time><span class="${RESULT[r.result]?.[1] || ""}">${esc(RESULT[r.result]?.[0] || r.result)}</span><span title="${esc(`${r.artist} — ${r.title}`)}">${esc(r.query || `${r.artist} — ${r.title}`)}${r.detail ? `<i class="dc-dim"> ← ${esc(r.detail)}</i>` : ""}</span><span class="dc-dim">${r.requests} req · ${r.ms} ms · ${esc(r.why)}</span></div>`).join("")}</div>` : `<p class="dc-line dc-dim">Aucune recherche depuis le démarrage d'Onde.</p>`}`;
  } catch (e) { body.innerHTML = `<div class="dc-note err">${esc(authProblem || `Statut indisponible : ${e.message}`)}</div>`; }
  clearTimeout(tabTimer);
  tabTimer = setTimeout(tickLyrics, 3000);
}
const card = (label, value, cls = "") => `<div class="dc-card ${cls}"><b>${esc(value)}</b><span>${esc(label)}</span></div>`;

// IA
async function tickAI() {
  if (!open || tab !== "ia") return;
  const body = el.querySelector(".dc-body");
  try {
    const s = await getJSON("/api/align/status");
    if (tab !== "ia") return;
    body.innerHTML = `<div class="dc-cards">${card("Installée", s.available ? "oui" : "non", s.available ? "ok" : "bad")}${card("Automatique", s.auto ? "oui" : "non")}${card("Modèle", s.model || "—")}${card("Synchronisés", s.done ?? 0, "ok")}${card("En attente", s.pending ?? 0)}${card("Impossibles", s.failed ?? 0, s.failed ? "bad" : "")}${card("Mémoire libre", s.memory != null ? `${String(s.memory).replace(".", ",")} Go` : "—")}</div>
      ${!s.available ? `<p class="dc-line">${esc(s.reason || "")}</p>` : ""}
      ${s.current ? `<p class="dc-line"><span class="dc-spin"></span> En cours : <b>${esc(s.current.title || "")}</b> — ${esc(s.current.phase || "")}${s.current.progress ? ` ${Math.round(s.current.progress * 100)} %` : ""}</p>` : ""}
      ${s.last ? `<p class="dc-line">Dernier : ${esc(s.last.title || "")} — ${esc(s.last.message || "")}</p>` : ""}
      ${s.install && (s.install.running || s.install.error) ? `<p class="dc-line">Installation : ${esc(s.install.step || s.install.error || "")}</p>` : ""}
      <h4>État complet</h4><pre class="dc-pre">${esc(fmt(s))}</pre>`;
  } catch (e) { body.innerHTML = `<div class="dc-note err">${esc(authProblem || `Statut indisponible : ${e.message}`)}</div>`; }
  clearTimeout(tabTimer);
  tabTimer = setTimeout(tickAI, 3000);
}

// système
let lastInfo = null;
function pageInfo() {
  const p = dev.ctx.player;
  return {
    appareil: deviceName(), navigateur: navigator.userAgent, adresse: location.origin,
    écran: `${window.innerWidth}×${window.innerHeight} (×${window.devicePixelRatio || 1})`,
    "en ligne": navigator.onLine, "contexte sécurisé (https)": window.isSecureContext,
    "service worker": navigator.serviceWorker?.controller ? "actif" : "non",
    "appli Android": (() => { try { return window.OndeApp ? window.OndeApp.version() : "non"; } catch { return "?"; } })(),
    "mode du lecteur": p ? p.mode : "?", "erreurs de la page": dev.errors,
  };
}
const kv = (obj) => `<div class="dc-kv">${Object.entries(obj).map(([k, v]) => `<span>${esc(k)}</span><b>${esc(typeof v === "object" && v !== null ? fmt(v, 1) : v)}</b>`).join("")}</div>`;
async function renderSystem() {
  if (!open || tab !== "systeme") return;
  const body = el.querySelector(".dc-body");
  body.innerHTML = `<h4>Cette page</h4>${kv(pageInfo())}<p class="dc-dim">Chargement du serveur…</p>`;
  try {
    const d = lastInfo = await getJSON("/api/dev/info");
    if (tab !== "systeme") return;
    const m = d.memory || {};
    const gb = (n) => (n ? `${(n / 1073741824).toFixed(2).replace(".", ",")} Go` : "—");
    body.innerHTML = `<h4>Cette page</h4>${kv(pageInfo())}
      <h4>Serveur</h4>${kv({ version: d.version, machine: d.model || d.machine, système: d.platform, python: d.python, nom: d.hostname, "démarré depuis": dur(d.uptime), "allumé depuis": d.systemUptime ? dur(d.systemUptime) : "—",
        processeurs: d.cpus, charge: d.load ? d.load.map((x) => x.toFixed(2)).join(" / ") : "—", température: d.temperature != null ? `${d.temperature.toFixed(1)} °C` : "—",
        mémoire: `${gb(m.available)} libres sur ${gb(m.total)} — Onde : ${gb(m.onde)}`, https: d.https ? "oui" : "non", "mot de passe": d.password ? "oui" : "non", port: d.port, "lecteur Pi (mpv)": d.pi ? "oui" : "non" })}
      <h4>Disques</h4>${kv(Object.fromEntries((d.disks || []).map((x) => [x.path, `${gb(x.free)} libres sur ${gb(x.total)}`])))}
      <h4>Bibliothèque</h4>${kv({ ...d.library, "surveillance des dossiers": d.watching ? "oui" : "non", musique: d.paths.music, données: d.paths.data, base: `${d.paths.db} (${size(d.paths.dbSize)})` })}
      <h4>Outils</h4>${kv({ ffmpeg: d.tools.ffmpeg || "absent", "yt-dlp": d.tools.ytdlp || "absent", "IA (paroles)": d.tools.ai.available ? `oui (${d.tools.ai.model})` : `non — ${d.tools.ai.reason || ""}` })}
      <h4>Adresses</h4>${kv(Object.fromEntries((d.urls || []).map((u, i) => [`réseau local ${i + 1}`, u])))}
      <h4>Réglages</h4>${kv(d.settings || {})}
      <h4>Variables ONDE_*</h4>${kv(d.env || {})}
      <h4>Tâches en cours (${d.threads.length})</h4><p class="dc-line">${esc(d.threads.join(" · "))}</p>`;
    debugOn = !!d.debug;
    const chk = el.querySelector("[data-act=debug]");
    if (chk) chk.checked = debugOn;
  } catch (e) { body.insertAdjacentHTML("beforeend", `<div class="dc-note err">${esc(authProblem || `Serveur injoignable : ${e.message}`)}</div>`); }
}

// lecteur
function tickPlayer() {
  if (!open || tab !== "lecteur") return;
  const p = dev.ctx.player;
  const body = el.querySelector(".dc-body");
  if (!p) { body.innerHTML = `<div class="dc-note">Lecteur pas encore prêt.</div>`; }
  else {
    const a = p.audio || document.querySelector("#audio");
    const t = p.current;
    const buf = [];
    try { for (let i = 0; a && i < a.buffered.length; i++) buf.push(`${a.buffered.start(i).toFixed(0)}–${a.buffered.end(i).toFixed(0)} s`); } catch { /* ignore */ }
    const RS = ["rien", "métadonnées", "image courante", "un peu d'avance", "assez d'avance"];
    const NS = ["vide", "inactif", "téléchargement", "source introuvable"];
    body.innerHTML = `<h4>Titre</h4>${kv(t ? { id: t.id, titre: t.title, artiste: t.artist, album: t.album, durée: `${Math.round(t.duration || 0)} s`, format: t.ext || "" } : { titre: "aucun" })}
      <h4>Lecture</h4>${kv({ mode: p.mode === "pi" ? "sur le Raspberry Pi" : "sur cet appareil", "en pause": p.paused, position: `${(p.position || 0).toFixed(1)} s / ${(p.duration || 0).toFixed(1)} s`, volume: `${Math.round((p.volume ?? 1) * 100)} %${p.muted ? " (coupé)" : ""}`, aléatoire: p.shuffle, répétition: p.repeat, "file d'attente": `${(p.index ?? -1) + 1} / ${p.queue?.length ?? 0}`, minuterie: p.sleepLeft == null ? "non" : p.sleepLeft === "end" ? "fin du titre" : `${Math.round(p.sleepLeft)} s` })}
      <h4>Élément audio</h4>${kv(a ? { source: (a.currentSrc || "").replace(location.origin, ""), état: RS[a.readyState] || a.readyState, réseau: NS[a.networkState] || a.networkState, "en mémoire": buf.join(", ") || "—", vitesse: a.playbackRate, erreur: a.error ? `code ${a.error.code} ${a.error.message || ""}` : "aucune" } : { audio: "absent" })}`;
  }
  clearTimeout(tabTimer);
  tabTimer = setTimeout(tickPlayer, 1000);
}

// ------------------------------------------------------------------ actions
function journalText() {
  return tagged().filter(matches)
    .map((i) => `${new Date(i.ts).toISOString()} ${LV_LABEL(i.level)} [${i.where === "local" ? "cette page" : SOURCES[i.source] || i.source}] ${i.message}`).join("\n");
}
function report() {
  return `Onde — rapport du ${new Date().toLocaleString("fr-FR")}\n\n== Page ==\n${fmt(pageInfo())}\n\n== Serveur ==\n${fmt(lastInfo || {})}\n\n== Journal ==\n${journalText()}`;
}
async function copy(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.append(ta); ta.select();
    try { document.execCommand("copy"); } catch { /* ignore */ }
    ta.remove();
  }
  flash("Copié");
}
function flash(msg) {
  const b = document.createElement("div");
  b.className = "dc-flash"; b.textContent = msg;
  el.append(b);
  setTimeout(() => b.remove(), 1600);
}
function download(text, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  a.download = name;
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

async function onClick(e) {
  const t = e.target.closest("[data-tab],[data-act],[data-net]");
  if (!t || t.tagName === "LABEL") return;
  if (t.dataset.tab) return showTab(t.dataset.tab);
  if (t.dataset.net) { netMode = t.dataset.net; return renderNet(); }
  const act = t.dataset.act;
  if (act === "close") toggle(false);
  else if (act === "clear") { clearedAt = Date.now(); if (tab === "journal") renderJournal(); else if (tab === "reseau") { if (netMode === "page") dev.net.length = 0; else serverHttp = []; renderNet(); } }
  else if (act === "copy") copy(tab === "systeme" ? report() : journalText());
  else if (act === "save") download(report(), `onde-journal-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.txt`);
  else if (act === "sys-refresh") renderSystem();
  else if (act === "sys-copy") copy(report());
  else if (act === "lyr-retry") {
    try { const r = await getJSON("/api/lyrics/retry", { method: "POST" }); flash(`${r.count} titre(s) relancé(s)`); tickLyrics(); } catch (err) { flash(`Échec : ${err.message}`); }
  } else if (act === "lyr-now" || act === "ia-now") {
    const id = dev.ctx.player?.currentId;
    if (!id) return flash("Aucun titre en cours");
    try {
      if (act === "lyr-now") {
        flash("Recherche…");
        const d = await getJSON(`/api/tracks/${id}/lyrics?force=1`);
        log("info", `Paroles du titre ${id} : ${d.synced ? "synchronisées" : d.plain ? "non synchronisées" : d.instrumental ? "instrumental" : d.offline ? "LRCLIB injoignable" : "introuvables"}${d.source ? ` (${d.source})` : ""}`);
        tickLyrics();
      } else {
        const d = await getJSON(`/api/tracks/${id}/lyrics/align`, { method: "POST", body: "{}" });
        flash(d.align?.state === "running" || d.align?.queued ? "Calage lancé" : "Demandé");
        tickAI();
      }
    } catch (err) { flash(`Échec : ${err.message}`); }
  }
}

async function onChange(e) {
  const t = e.target;
  if (t.name === "level") { filt.level = +t.value; store.set("level", filt.level); renderJournal(); }
  else if (t.name === "source") { filt.source = t.value; store.set("source", filt.source); renderJournal(); }
  else if (t.dataset.act === "debug") {
    try { const r = await getJSON("/api/dev/debug", { method: "PUT", body: JSON.stringify({ on: t.checked }) }); debugOn = r.debug; t.checked = debugOn; if (debugOn && filt.level > 0 && tab === "journal") { filt.level = 0; showTab("journal"); } }
    catch (err) { t.checked = !t.checked; flash(`Échec : ${err.message}`); }
  }
}

// ------------------------------------------------------------------ commandes
const AsyncFunction = Object.getPrototypeOf(async function () { /* */ }).constructor;
const HELP = `Commandes : n'importe quel JavaScript, résultat affiché ici. Raccourcis disponibles :
  player            le lecteur (player.current, player.queue, player.next(), player.seek(30)…)
  api               l'API d'Onde (await api.get("/api/info"), await api.get("/api/lyrics/status"))
  dev               ce que note la console (dev.logs, dev.net)
  clear             efface l'affichage`;

async function run(code) {
  history = [code, ...history.filter((h) => h !== code)].slice(0, 50);
  store.set("history", history);
  hpos = -1;
  log("cmd", code);
  if (code === "help") return log("result", HELP);
  if (code === "clear") { clearedAt = Date.now(); return renderJournal(); }
  let fn;
  try { fn = new AsyncFunction("player", "api", "dev", `return (${code}\n);`); }
  catch {
    try { fn = new AsyncFunction("player", "api", "dev", code); } catch (err) { return log("error", fmt(err)); }
  }
  try { log("result", fmt(await fn(dev.ctx.player, dev.ctx.api, dev))); }
  catch (err) { log("error", fmt(err)); }
}

function onCmdKey(e) {
  if (e.key === "ArrowUp" && history.length) { e.preventDefault(); hpos = Math.min(history.length - 1, hpos + 1); e.target.value = history[hpos]; }
  else if (e.key === "ArrowDown") { e.preventDefault(); hpos = Math.max(-1, hpos - 1); e.target.value = hpos < 0 ? "" : history[hpos]; }
}
