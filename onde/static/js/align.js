// Réglages : paroles synchronisées par IA (stable-whisper) — installation, état, modèle, langue.
import { api } from "./api.js";
import { $, esc, icon, plural } from "./util.js";
import { toast, toastError } from "./ui.js";

const LANGS = [["auto", "Devinée"], ["fr", "Français"], ["en", "Anglais"], ["es", "Espagnol"],
  ["de", "Allemand"], ["it", "Italien"], ["pt", "Portugais"]];
const PHASES = { audio: "lecture du son", model: "chargement du modèle", language: "langue", align: "calage" };

const sw = (id, on) =>
  `<label class="switch"><input type="checkbox" id="${id}"${on ? " checked" : ""}><span class="sw-track"><span class="sw-thumb"></span></span></label>`;

export function alignSection() {
  return `<h2 class="set-title">Paroles synchronisées par IA</h2><div id="align-box"><div class="settings-card"><div class="sc-text"><p class="dim">Chargement…</p></div></div></div>`;
}

function summary(st) {
  const parts = [];
  if (st.current) {
    const p = st.current.phase === "align" && st.current.progress ? ` ${Math.round(st.current.progress * 100)} %` : "";
    parts.push(`<span class="spinner sm"></span> En cours : <b>${esc(st.current.title || "")}</b> — ${esc(PHASES[st.current.phase] || "")}${p}`);
  }
  parts.push(`${plural(st.done, "titre synchronisé", "titres synchronisés")}`);
  if (st.pending) parts.push(`${st.pending} en attente${st.auto ? "" : " (synchronisation automatique désactivée)"}`);
  if (st.failed) parts.push(`${st.failed} impossible${st.failed > 1 ? "s" : ""}`);
  let html = `<p style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center">${parts.join(" · ")}</p>`;
  if (st.last) html += `<p class="dim" style="margin-top:4px;font-size:12px">Dernier : ${esc(st.last.title || "")} — ${esc(st.last.message || "")}</p>`;
  return html;
}

function render(box, st) {
  const intro = "Quand Onde n'a que des paroles non synchronisées (LRCLIB, tags du fichier, ou collées à la main), il les cale tout seul sur la voix, ligne par ligne, avec stable-whisper — sur le serveur, sans Internet une fois installé. Un fichier .lrc est aussi écrit à côté du morceau.";
  if (!st.available) {
    const ins = st.install || {};
    let right = "";
    let extra = "";
    if (st.frozen) extra = `<p class="dim" style="margin-top:8px">${esc(st.reason)}.</p>`;
    else if (ins.running) extra = `<p style="margin-top:8px;display:flex;gap:8px;align-items:center"><span class="spinner sm"></span> ${esc(ins.step || "Installation…")}</p>`;
    else {
      right = `<button class="btn" data-align-install>${icon("download")} Installer (≈ 1 Go)</button>`;
      extra = `<p class="dim" style="margin-top:8px">${ins.error ? `Échec : ${esc(ins.error)}` : `Non installé (${esc(st.reason || "")}). Il faut Internet le temps de l'installation, puis tout se fait sur place.`}</p>`;
    }
    box.innerHTML = `<div class="settings-card align-card"><div class="sc-text"><h3>Synchroniser les paroles sur la voix</h3><p>${intro}</p>${extra}</div>${right}</div>`;
    return;
  }
  const custom = st.models.some((m) => m.name === st.model) ? "" : `<option selected>${esc(st.model.split(/[\\/]/).pop())}</option>`;
  const models = custom + st.models.map((m) => `<option value="${m.name}"${m.name === st.model ? " selected" : ""}${!m.fits && m.name !== st.model ? " disabled" : ""}>${m.name} — ${m.memory.toString().replace(".", ",")} Go${m.fits ? "" : " (mémoire insuffisante)"}</option>`).join("");
  const langs = LANGS.map(([v, l]) => `<option value="${v}"${v === (st.language || "auto") ? " selected" : ""}>${l}</option>`).join("");
  box.innerHTML = `<div class="settings-card align-card">
      <div class="sc-text"><h3>Synchroniser les paroles sur la voix</h3><p>${intro}</p><div data-align-sum>${summary(st)}</div></div>
      ${sw("align-auto", st.auto)}
    </div>
    <div class="settings-card">
      <div class="sc-text"><h3>Modèle d'IA</h3><p>Plus gros = plus précis, mais plus lent et plus gourmand en mémoire. « base » convient au Raspberry Pi 5${st.memory ? ` (${st.memory.toString().replace(".", ",")} Go libres en ce moment)` : ""}. Un nouveau modèle se télécharge à sa première utilisation.</p></div>
      <select class="select" id="align-model">${models}</select>
    </div>
    <div class="settings-card">
      <div class="sc-text"><h3>Langue des paroles</h3><p>« Devinée » : d'après les mots des paroles, sinon à l'écoute du morceau.</p></div>
      <select class="select" id="align-lang">${langs}</select>
    </div>
    <div class="settings-card">
      <div class="sc-text"><h3>Écrire le fichier .lrc à côté du morceau</h3><p>Pour retrouver la synchro dans tes autres lecteurs. Un .lrc synchronisé existant n'est jamais remplacé.</p></div>
      ${sw("align-lrc", st.writeLrc)}
    </div>`;
}

export function bindAlign(el) {
  let timer = 0;
  let alive = true;
  let shown = "";
  const poll = async () => {
    const box = $("#align-box", el);
    if (!alive || !box) return;
    let st;
    try {
      st = await api.get("/api/align/status");
    } catch { timer = setTimeout(poll, 10000); return; }
    // ne reconstruit pas les menus pendant qu'on s'en sert : seul le résumé bouge
    const key = JSON.stringify([st.available, st.frozen, st.install, st.model, st.language, st.auto, st.writeLrc, st.memory != null]);
    if (key !== shown) { render(box, st); shown = key; } else {
      const sum = $("[data-align-sum]", box);
      if (sum) sum.innerHTML = summary(st);
    }
    timer = setTimeout(poll, st.install?.running || st.current ? 2500 : 10000);
  };
  const put = async (values, msg) => {
    try { await api.put("/api/settings", values); toast(msg); shown = ""; clearTimeout(timer); poll(); } catch (e) { toastError(e); }
  };
  el.addEventListener("change", (e) => {
    if (e.target.id === "align-auto") put({ auto_align: e.target.checked }, `Synchronisation automatique : ${e.target.checked ? "activée" : "désactivée"}`);
    if (e.target.id === "align-lrc") put({ align_write_lrc: e.target.checked }, e.target.checked ? "Les fichiers .lrc seront écrits" : "Plus de fichiers .lrc écrits");
    if (e.target.id === "align-model") put({ align_model: e.target.value }, `Modèle : ${e.target.value}`);
    if (e.target.id === "align-lang") put({ align_language: e.target.value }, "Langue des paroles modifiée");
  });
  el.addEventListener("click", async (e) => {
    if (!e.target.closest("[data-align-install]")) return;
    try { await api.post("/api/align/install"); toast("Installation lancée — quelques minutes"); shown = ""; clearTimeout(timer); poll(); } catch (err) { toastError(err); }
  });
  poll();
  return () => { alive = false; clearTimeout(timer); };
}
