// Paroles : affichage synchronisé façon karaoké (LRC), défilement automatique, clic = aller à la ligne.
import { api } from "./api.js";
import { $, esc, icon } from "./util.js";
import { openModal, toast, toastError } from "./ui.js";

const cache = new Map(); // id du titre -> paroles
let player = null;
let box = null;
let lines = [];        // [{ t, text }] (synchronisé) ou []
let current = -1;
let trackId = null;
let userScrollAt = 0;
let raf = 0;
let visible = false;
let recheck = 0;       // nouvel essai tant que les paroles ne sont pas trouvées (panneau ouvert)
const RECHECK_MS = 90_000;
const RECHECK_OFFLINE_MS = 30_000;
const RECHECK_ALIGN_MS = 4_000;        // calage sur la voix en cours : on suit l'avancement
const RECHECK_ALIGN_QUEUED_MS = 20_000;

// paroles brutes en train d'être calées sur la voix (ou en attente) : à redemander plus tard
const aligning = (d) => !!(d && d.align && d.align.available && (d.align.state === "running" || d.align.queued));
const settled = (d) => d && d.found && !aligning(d);

export function bindLyrics(p, container) {
  player = p;
  box = container;
  box.addEventListener("wheel", () => { userScrollAt = performance.now(); }, { passive: true });
  box.addEventListener("touchmove", () => { userScrollAt = performance.now(); }, { passive: true });
  box.addEventListener("click", (e) => {
    const line = e.target.closest(".lyr-line[data-t]");
    if (line) { player.seek(+line.dataset.t); userScrollAt = 0; return; }
    if (e.target.closest("[data-lyr-edit]")) editLyrics();
    if (e.target.closest("[data-lyr-retry]")) { cache.delete(trackId); load(trackId, true, true); }
    if (e.target.closest("[data-lyr-align]")) startAlign(trackId);
    if (e.target.closest("[data-lyr-sing]")) openKaraoke();
  });
  player.addEventListener("track", () => { if (visible || kara) load(player.currentId); });
  player.addEventListener("state", tick);
  player.addEventListener("time", tick);
}

export function parseLRC(text) {
  const out = [];
  let offset = 0;
  for (const raw of (text || "").split(/\r?\n/)) {
    const off = raw.match(/^\[offset:\s*([+-]?\d+)\]/i);
    if (off) { offset = +off[1] / 1000; continue; }
    const stamps = [...raw.matchAll(/\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]/g)];
    if (!stamps.length) continue;
    // le texte : sans les dates de ligne [mm:ss.xx] ni les dates mot à mot <mm:ss.xx> (LRC « enrichi »)
    const words = raw.replace(/\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]/g, "").replace(/<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>/g, "").replace(/\s+/g, " ").trim();
    for (const m of stamps) {
      const frac = m[3] ? +m[3] / 10 ** m[3].length : 0;
      out.push({ t: Math.max(0, +m[1] * 60 + +m[2] + frac - offset), text: words });
    }
  }
  return out.sort((a, b) => a.t - b.t);
}

export function setVisible(v) {
  visible = v;
  if (v) load(player.currentId);
  else { cancelAnimationFrame(raf); clearTimeout(recheck); }
}

// spinner=false : nouvel essai discret (le panneau reste tel quel pendant la recherche)
async function load(id, force = false, spinner = !force) {
  clearTimeout(recheck);
  const same = id === trackId;
  trackId = id;
  if (!id) { box.innerHTML = ""; return; }
  let data = cache.get(id);
  if (!data) {
    if (spinner || !same) { current = -1; box.innerHTML = `<div class="lyr-state"><div class="spinner"></div></div>`; }
    try {
      data = await api.get(`/api/tracks/${id}/lyrics${force ? "?force=1" : ""}`);
      if (settled(data)) cache.set(id, data); // pas encore trouvées (ou en cours de calage) : on redemandera
    } catch (e) {
      data = { found: false, error: e.message };
    }
  }
  if (id !== trackId) return;
  render(data);
  if (!settled(data) && visible) {
    const wait = aligning(data) ? (data.align.state === "running" ? RECHECK_ALIGN_MS : RECHECK_ALIGN_QUEUED_MS)
      : data.offline || data.error ? RECHECK_OFFLINE_MS : RECHECK_MS;
    recheck = setTimeout(() => { if (visible && trackId === id) load(id, false, false); }, wait);
  }
}

function when(ts) {
  const s = Math.max(0, ts - Date.now() / 1000);
  if (s < 90) return "dans un instant";
  if (s < 3600) return `dans ${Math.round(s / 60)} min`;
  if (s < 86400 * 1.5) return `dans ${Math.round(s / 3600)} h`;
  return `dans ${Math.round(s / 86400)} jours`;
}

function render(d) {
  const t = player.current;
  lines = d.synced ? parseLRC(d.synced) : [];
  const edit = `<button class="lyr-link" data-lyr-edit>${icon("edit", "sm")} ${d.found ? "Modifier" : "Ajouter des paroles"}</button>`;
  if (lines.length) {
    box.innerHTML = `<div class="lyr" data-synced="1">${lines.map((l, i) =>
      `<p class="lyr-line" data-i="${i}" data-t="${l.t.toFixed(2)}">${l.text ? esc(l.text) : "♪"}</p>`).join("")}</div>
      <div class="lyr-foot">Paroles synchronisées${d.source ? ` · ${esc(d.source)}` : ""} <button class="lyr-link" data-lyr-sing>${icon("lyrics", "sm")} Mode karaoké</button> ${edit}</div>`;
    current = -1;
    userScrollAt = 0;
    tick();
  } else if (d.plain) {
    box.innerHTML = `<div class="lyr plain">${d.plain.split(/\r?\n/).map((l) => `<p class="lyr-line">${esc(l) || "&nbsp;"}</p>`).join("")}</div>
      <div class="lyr-foot">Paroles non synchronisées${d.source ? ` · ${esc(d.source)}` : ""} ${edit}${alignNote(d.align)}</div>`;
  } else if (d.instrumental) {
    box.innerHTML = `<div class="lyr-state"><svg class="ic lyr-ic"><use href="#i-note"/></svg><b>Morceau instrumental</b><p>Profite de la musique ♪</p></div>`;
  } else {
    const auto = d.next_try ? `Onde continue de chercher tout seul (prochain essai ${when(d.next_try)}).` : "";
    const why = d.offline ? "Le Pi n'a pas accès à Internet pour l'instant : Onde cherchera les paroles dès que la connexion reviendra." :
      d.error ? esc(d.error) : `Pas encore trouvées pour « ${esc(t?.title || "")} ». ${auto}`;
    box.innerHTML = `<div class="lyr-state" data-missing><svg class="ic lyr-ic"><use href="#i-lyrics"/></svg><b>Pas de paroles</b><p>${why}</p>
      <div class="lyr-actions"><button class="btn sm" data-lyr-retry>Chercher maintenant</button>${edit}</div>
      <p class="dim" style="font-size:12px;margin-top:14px">Astuce : un fichier .lrc portant le même nom que le morceau est utilisé automatiquement.</p></div>`;
  }
}

const PHASES = { audio: "lecture du son", model: "chargement du modèle", language: "reconnaissance de la langue", align: "calage" };

// état du calage sur la voix, sous des paroles non synchronisées
function alignNote(a) {
  if (!a) return "";
  const btn = (label) => `<button class="lyr-link" data-lyr-align>${icon("sparkles", "sm")} ${label}</button>`;
  if (!a.available) return `<div class="lyr-align dim">Synchronisation automatique par IA : ${esc(a.reason || "indisponible")}${a.reason && a.reason.includes("installé") ? " — à installer dans les Réglages" : ""}.</div>`;
  if (a.state === "running") {
    const pct = a.phase === "align" && a.progress ? ` ${Math.round(a.progress * 100)} %` : "";
    return `<div class="lyr-align"><span class="spinner sm"></span> Synchronisation sur la voix… ${esc(PHASES[a.phase] || "")}${pct}</div>`;
  }
  if (a.queued) return `<div class="lyr-align dim">${icon("sparkles", "sm")} En attente de synchronisation sur la voix (IA).</div>`;
  if (a.error) {
    const later = a.state === "retry" && a.next_try && a.auto ? ` Nouvel essai automatique ${when(a.next_try)}.` : "";
    return `<div class="lyr-align"><span class="dim">${esc(a.error)}.${later}</span> ${btn("Réessayer maintenant")}</div>`;
  }
  return `<div class="lyr-align">${btn("Synchroniser sur la voix (IA)")}</div>`;
}

async function startAlign(id, text) {
  try {
    const res = await api.post(`/api/tracks/${id}/lyrics/align`, text ? { text } : {});
    cache.delete(id);
    toast("Synchronisation sur la voix lancée — quelques minutes");
    if (id === trackId) { render(res); load(id, false, false); }
    return res;
  } catch (e) { toastError(e); return null; }
}

function tick() {
  if (!visible || !lines.length) return;
  cancelAnimationFrame(raf);
  raf = requestAnimationFrame(() => {
    const pos = player.position + 0.25; // petite avance : la ligne s'allume au bon moment
    let lo = 0, hi = lines.length - 1, idx = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (lines[mid].t <= pos) { idx = mid; lo = mid + 1; } else hi = mid - 1;
    }
    if (idx !== current) {
      const els = box.querySelectorAll(".lyr-line");
      els.forEach((el, i) => {
        el.classList.toggle("cur", i === idx);
        el.classList.toggle("past", i < idx);
      });
      current = idx;
      const el = els[Math.max(0, idx)];
      if (el && performance.now() - userScrollAt > 4000) {
        const target = el.offsetTop - box.clientHeight * 0.38;
        box.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
      }
    }
    if (!player.paused) raf = requestAnimationFrame(tick);
  });
}

function editLyrics() {
  const id = trackId;
  const d = cache.get(id) || {};
  const t = player.current;
  openModal({
    title: "Paroles",
    wide: true,
    body: `<p class="muted" style="margin:0 0 10px;font-size:13px">Colle les paroles de « ${esc(t?.title || "")} ». Le format LRC (<code>[01:23.45] ligne</code>) les synchronise avec la musique.</p>
      <textarea class="field" id="lyr-text" style="width:100%;min-height:320px;font-family:ui-monospace,Menlo,monospace;font-size:13px" spellcheck="false">${esc(d.synced || d.plain || "")}</textarea>`,
    foot: `<button class="btn ghost" data-close>Annuler</button><button class="btn" data-save-align hidden>${icon("sparkles", "sm")} Enregistrer et synchroniser</button><button class="btn white" data-save>Enregistrer</button>`,
    onMount: (m, close) => {
      const both = $("[data-save-align]", m);
      const area = $("#lyr-text", m);
      let canAlign = false;
      const refresh = () => { both.hidden = !canAlign || !area.value.trim() || parseLRC(area.value).length > 0; };
      area.addEventListener("input", refresh);
      api.get("/api/align/status").then((st) => { canAlign = st.available; refresh(); }).catch(() => {});
      both.addEventListener("click", async () => {
        const res = await startAlign(id, area.value);
        if (res) close();
      });
      $("[data-save]", m).addEventListener("click", async () => {
        try {
          const res = await api.put(`/api/tracks/${id}/lyrics`, { text: $("#lyr-text", m).value });
          cache.set(id, res);
          close();
          toast("Paroles enregistrées");
          if (id === trackId) render(res);
        } catch (e) { toastError(e); }
      });
    },
  });
}


// ---------------------------------------------------------------- mode karaoké (plein écran, pour chanter)
let kara = null;

export function openKaraoke() {
  if (kara) return;
  const el = document.createElement("div");
  el.id = "karaoke";
  el.innerHTML = `<div class="k-bg"></div>
    <button class="icon-btn k-close" aria-label="Fermer">${icon("close")}</button>
    <div class="k-meta"></div>
    <div class="k-lines"><p class="k-prev"></p><p class="k-cur"><span class="k-fill"></span><span class="k-text"></span></p><p class="k-next"></p><p class="k-next2"></p></div>
    <div class="k-ctrl"><button class="icon-btn big" data-k="prev" aria-label="Précédent">${icon("prev")}</button>
      <button class="play-btn huge" data-k="toggle" aria-label="Lecture">${icon("play")}</button>
      <button class="icon-btn big" data-k="next" aria-label="Suivant">${icon("next")}</button></div>`;
  document.body.append(el);
  kara = { el, raf: 0, idx: -2 };
  const wasVisible = visible;
  visible = true;
  load(player.currentId);
  const close = () => {
    cancelAnimationFrame(kara.raf);
    el.remove();
    kara = null;
    visible = wasVisible;
    removeEventListener("keydown", onKey, true);
    try { document.exitFullscreen?.(); } catch { /* ignore */ }
  };
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  addEventListener("keydown", onKey, true);
  el.querySelector(".k-close").addEventListener("click", close);
  el.addEventListener("click", (e) => {
    const b = e.target.closest("[data-k]");
    if (!b) return;
    if (b.dataset.k === "toggle") player.toggle(); else if (b.dataset.k === "next") player.next(); else player.prev();
  });
  try { el.requestFullscreen?.().catch(() => {}); } catch { /* ignore */ }
  const loop = () => {
    if (!kara) return;
    karaTick();
    kara.raf = requestAnimationFrame(loop);
  };
  loop();
}

function karaTick() {
  const el = kara.el;
  const t = player.current;
  const meta = el.querySelector(".k-meta");
  const label = t ? `${t.title} — ${t.artist}` : "";
  if (meta.textContent !== label) {
    meta.textContent = label;
    el.querySelector(".k-bg").style.backgroundImage = t?.cover ? `url(/api/cover/${t.cover}/768)` : "";
    kara.idx = -2;
  }
  el.querySelector("[data-k=toggle]").innerHTML = icon(player.paused ? "play" : "pause");
  const cur = el.querySelector(".k-cur");
  if (!lines.length) {
    if (kara.idx !== -3) {
      kara.idx = -3;
      el.querySelector(".k-text").textContent = "Pas de paroles synchronisées pour ce titre";
      ["k-prev", "k-next", "k-next2"].forEach((c) => { el.querySelector("." + c).textContent = ""; });
      el.querySelector(".k-fill").style.width = "0";
    }
    return;
  }
  const pos = player.position + 0.25;
  let lo = 0, hi = lines.length - 1, idx = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (lines[mid].t <= pos) { idx = mid; lo = mid + 1; } else hi = mid - 1; }
  const txt = (i) => (i >= 0 && i < lines.length ? lines[i].text || "♪" : "");
  if (idx !== kara.idx) {
    kara.idx = idx;
    el.querySelector(".k-prev").textContent = txt(idx - 1);
    el.querySelector(".k-text").textContent = idx < 0 ? "♪" : txt(idx);
    el.querySelector(".k-next").textContent = txt(idx + 1);
    el.querySelector(".k-next2").textContent = txt(idx + 2);
    cur.classList.remove("pop"); void cur.offsetWidth; cur.classList.add("pop");
  }
  // remplissage de la ligne en cours, du début de la ligne au début de la suivante
  const start = idx >= 0 ? lines[idx].t : 0;
  const end = idx + 1 < lines.length ? lines[idx + 1].t : start + 4;
  const k = Math.max(0, Math.min(1, (pos - start) / Math.max(0.5, end - start)));
  el.querySelector(".k-fill").style.width = `${(k * 100).toFixed(1)}%`;
}
