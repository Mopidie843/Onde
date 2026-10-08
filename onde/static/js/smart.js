// Playlists intelligentes : la page, l'éditeur de règles (avec aperçu en direct) et des modèles prêts
// à l'emploi. Les règles sont évaluées par le Pi (onde/smart.py) : la playlist suit la bibliothèque.
import { api, emit } from "./api.js";
import { $, $$, esc, icon, fmtLong, plural, mosaic, debounce } from "./util.js";
import { hero, actionBar, trackList, emptyState } from "./components.js";
import { openModal, openMenu, toast, toastError, confirmDialog } from "./ui.js";
import { offline } from "./offline.js";

let page = null;
export function bindSmart(ctx) { page = ctx; }

const FIELDS = [
  ["genre", "Genre", "text"], ["artist", "Artiste", "text"], ["album", "Album", "text"], ["title", "Titre", "text"],
  ["year", "Année", "number"], ["plays", "Nombre d'écoutes", "number"], ["duration", "Durée (minutes)", "number"],
  ["added", "Ajouté", "date"], ["played", "Dernière écoute", "date"], ["liked", "Liké", "bool"], ["format", "Format", "enum"],
];
const KIND = Object.fromEntries(FIELDS.map(([k, , kind]) => [k, kind]));
const LABEL = Object.fromEntries(FIELDS.map(([k, l]) => [k, l]));
const OPS = {
  text: [["contains", "contient"], ["not_contains", "ne contient pas"], ["is", "est"], ["is_not", "n'est pas"], ["starts", "commence par"]],
  number: [["gt", "plus de"], ["lt", "moins de"], ["eq", "égal à"], ["ne", "différent de"], ["between", "entre"]],
  date: [["in_last", "dans les derniers"], ["not_in_last", "pas dans les derniers"], ["never", "jamais"]],
  bool: [["is_true", "oui"], ["is_false", "non"]],
  enum: [["is", "est"], ["is_not", "n'est pas"]],
};
const OPLABEL = Object.fromEntries(Object.values(OPS).flat());
const SORTS = [
  ["random", "Aléatoire (renouvelé chaque semaine)"], ["plays", "Les plus écoutés"], ["added", "Ajoutés récemment"],
  ["played", "Écoutés récemment"], ["title", "Titre (A → Z)"], ["artist", "Artiste puis album"],
  ["year", "Les plus récents (année)"], ["year_asc", "Les plus anciens (année)"], ["duration", "Les plus longs"],
];
const FORMATS = ["mp3", "flac", "m4a", "aac", "ogg", "opus", "wav", "wma", "aiff", "ape"];

export const TEMPLATES = [
  { name: "Jamais écoutés", spec: { match: "all", rules: [{ field: "played", op: "never" }], sort: "random", limit: 100 } },
  { name: "Les plus écoutés", spec: { match: "all", rules: [{ field: "plays", op: "gt", value: 0 }], sort: "plays", limit: 50 } },
  { name: "Ajoutés ce mois-ci", spec: { match: "all", rules: [{ field: "added", op: "in_last", value: 30 }], sort: "added", limit: null } },
  { name: "À redécouvrir", spec: { match: "all", rules: [{ field: "plays", op: "gt", value: 2 }, { field: "played", op: "not_in_last", value: 180 }], sort: "random", limit: 50 } },
  { name: "Likés, pas écoutés depuis 1 mois", spec: { match: "all", rules: [{ field: "liked", op: "is_true" }, { field: "played", op: "not_in_last", value: 30 }], sort: "random", limit: 100 } },
  { name: "Années 80", spec: { match: "all", rules: [{ field: "year", op: "between", value: [1980, 1989] }], sort: "random", limit: 100 } },
  { name: "Années 2000", spec: { match: "all", rules: [{ field: "year", op: "between", value: [2000, 2009] }], sort: "random", limit: 100 } },
  { name: "Morceaux longs", spec: { match: "all", rules: [{ field: "duration", op: "gt", value: 480 }], sort: "duration", limit: null } },
  { name: "Qualité CD (FLAC)", spec: { match: "all", rules: [{ field: "format", op: "is", value: "flac" }], sort: "artist", limit: null } },
];

const clone = (x) => JSON.parse(JSON.stringify(x));
const minutes = (sec) => Math.round((sec / 60) * 10) / 10;

function ruleText(r) {
  const k = KIND[r.field];
  const f = LABEL[r.field].replace(" (minutes)", "");
  if (k === "bool") return r.op === "is_true" ? "Likés" : "Pas likés";
  if (k === "date") {
    if (r.op === "never") return r.field === "played" ? "Jamais écoutés" : `${f} : jamais`;
    const d = r.value;
    const span = d % 365 === 0 && d >= 365 ? plural(d / 365, "an", "ans") : d % 30 === 0 && d >= 30 ? `${d / 30} mois` : plural(d, "jour", "jours");
    return `${f} ${r.op === "in_last" ? "dans les" : "pas dans les"} ${span}`;
  }
  const v = r.field === "duration"
    ? (Array.isArray(r.value) ? r.value.map(minutes).join(" et ") : minutes(r.value)) + " min"
    : Array.isArray(r.value) ? r.value.join(" et ") : `« ${r.value} »`;
  return `${f} ${OPLABEL[r.op]} ${v}`;
}
const SORT_PHRASE = {
  random: "ordre aléatoire", plays: "les plus écoutés d'abord", added: "les derniers ajoutés d'abord",
  played: "les derniers écoutés d'abord", title: "par titre", artist: "par artiste et album",
  year: "du plus récent au plus ancien", year_asc: "du plus ancien au plus récent", duration: "les plus longs d'abord",
};
export function specSummary(spec) {
  const rules = (spec.rules || []).map(ruleText);
  const head = rules.length ? rules.join(spec.match === "any" ? " ou " : " · ") : "Toute la bibliothèque";
  return `${head} — ${SORT_PHRASE[spec.sort] || ""}${spec.limit ? `, ${spec.limit} titres max` : ""}`;
}

export function smartCover(p, size) {
  return `<div class="smart-cover">${mosaic(p.mosaic, size, p.name)}<span class="smart-badge">${icon("sparkles")}</span></div>`;
}
export function smartCard(p) {
  return `<div class="card">
    <div class="cv">${smartCover(p, 384)}<button class="fab" data-play="smart:${p.id}" aria-label="Lire">${icon("play")}</button></div>
    <a class="card-link" href="#/smart/${p.id}"><div class="c1 ell">${esc(p.name)}</div></a>
    <div class="c2">Intelligente • ${plural(p.count, "titre", "titres")}</div>
  </div>`;
}
export function newSmartCard() {
  return `<div class="card">
    <div class="cv"><div class="ph" style="--h:200">${icon("sparkles")}</div></div>
    <button class="card-link" data-act="new-smart" style="text-align:left;width:100%"><div class="c1 ell">Playlist intelligente</div></button>
    <div class="c2">Se remplit toute seule</div>
  </div>`;
}

// ---------------------------------------------------------------- page
export async function smartView(el, [id]) {
  const p = await api.get(`/api/smart/${id}`);
  const key = `smart:${p.id}`;
  const ctx = { key, label: p.name, href: `#/smart/${p.id}`, kind: "la playlist" };
  document.title = `${p.name} · Onde`;
  page.setColor("#27406b");
  page.setTopTitle(p.name, key);
  el.innerHTML = `
    ${hero({
      kind: "Playlist intelligente", title: p.name, desc: specSummary(p.spec), titleAct: "edit-smart",
      coverHtml: smartCover(p, 384),
      meta: `<span>${plural(p.count, "titre", "titres")}</span>${p.dur ? `<span class="sep"></span><span class="muted">${fmtLong(p.dur)}</span>` : ""}<span class="sep"></span><span class="muted">mise à jour automatiquement</span>`,
    })}
    <div class="body">
      ${actionBar(key, `
        <button class="icon-btn sm" data-act="edit-smart" aria-label="Modifier les règles" title="Modifier les règles">${icon("sliders")}</button>
        <button class="icon-btn sm" data-act="more" aria-label="Plus d'options">${icon("more")}</button>`)}
      ${trackList(p.tracks, {
        ctx, added: true,
        empty: emptyState("sparkles", "Aucun titre ne correspond pour l'instant",
          "Modifie les règles, ou importe de la musique : cette playlist se remplit toute seule.",
          `<button class="btn white" data-act="edit-smart">Modifier les règles</button>`),
      })}
    </div>`;
  el.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    const act = btn?.dataset.act;
    if (act === "edit-smart") openSmartEditor(p);
    if (act === "more") {
      openMenu([
        { icon: "queue", label: "Ajouter à la file d'attente", disabled: !p.count, onClick: () => { page.player.addToQueue(p.tracks.map((t) => t.id)); toast("Ajouté à la file d'attente"); } },
        { icon: "sliders", label: "Modifier les règles", onClick: () => openSmartEditor(p) },
        { icon: "plus", label: "Dupliquer", onClick: () => openSmartEditor(null, { name: `${p.name} (copie)`, spec: p.spec }) },
        "hr",
        { icon: "trash", label: "Supprimer", danger: true, onClick: () => deleteSmart(p) },
      ], { anchor: btn });
    }
  });
}

async function deleteSmart(p) {
  const ok = await confirmDialog({ title: "Supprimer cette playlist intelligente ?", text: `« ${p.name} » sera supprimée. Les titres, eux, restent dans ta bibliothèque.` });
  if (!ok) return;
  try {
    await api.del(`/api/smart/${p.id}`);
    offline.remove(`smart:${p.id}`).catch(() => {}); // libère aussi l'appareil s'il était téléchargé
    emit("playlists");
    toast("Playlist supprimée");
    location.hash = "#/library/playlists";
  } catch (err) { toastError(err); }
}

// ---------------------------------------------------------------- éditeur
let genresP = null;
const genres = () => (genresP ||= api.get("/api/genres").then((g) => g.map((x) => x.name)).catch(() => []));

function defaultRule(field = "genre") {
  const kind = KIND[field];
  const op = OPS[kind][0][0];
  return { field, op, value: kind === "date" ? 30 : kind === "number" ? (field === "year" ? 2000 : field === "duration" ? 300 : 1) : kind === "enum" ? "flac" : "" };
}

function valueHtml(r) {
  const k = KIND[r.field];
  const num = (v, attrs = "") => `<input class="field sm-num" type="number" inputmode="decimal" data-k="value" value="${esc(v ?? "")}" ${attrs}>`;
  if (k === "text") return `<input class="field" data-k="value" value="${esc(r.value ?? "")}" placeholder="…" ${r.field === "genre" ? 'list="sm-genres"' : ""} autocomplete="off">`;
  if (k === "enum") return `<select class="select field-select" data-k="value">${FORMATS.map((f) => `<option value="${f}"${f === r.value ? " selected" : ""}>${f.toUpperCase()}</option>`).join("")}</select>`;
  if (k === "bool") return "";
  if (k === "date") return r.op === "never" ? "" : `${num(r.value, 'min="1" step="1"')}<span class="dim">jours</span>`;
  const conv = (v) => (r.field === "duration" ? minutes(v) : v);
  const unit = r.field === "duration" ? `<span class="dim">min</span>` : "";
  if (r.op === "between") {
    const [a, b] = Array.isArray(r.value) ? r.value : [r.value, r.value];
    return `${num(conv(a), 'data-part="0"')}<span class="dim">et</span>${num(conv(b), 'data-part="1"')}${unit}`;
  }
  return `${num(conv(Array.isArray(r.value) ? r.value[0] : r.value))}${unit}`;
}

function ruleHtml(r, i) {
  const kind = KIND[r.field];
  return `<div class="sm-rule" data-i="${i}">
    <select class="select field-select" data-k="field" aria-label="Critère">${FIELDS.map(([k, l]) => `<option value="${k}"${k === r.field ? " selected" : ""}>${l}</option>`).join("")}</select>
    <select class="select field-select" data-k="op" aria-label="Condition">${OPS[kind].map(([k, l]) => `<option value="${k}"${k === r.op ? " selected" : ""}>${l}</option>`).join("")}</select>
    <span class="sm-val">${valueHtml(r)}</span>
    <button class="icon-btn" data-rm aria-label="Retirer cette règle" title="Retirer">${icon("close", "sm")}</button>
  </div>`;
}

export function openSmartEditor(existing = null, preset = null) {
  const src = existing || preset;
  let spec = clone(src?.spec || { match: "all", rules: [defaultRule()], sort: "random", limit: 100 });
  const body = `
    ${existing ? "" : `<div class="sm-templates"><span class="dim">Idées :</span>${TEMPLATES.map((t, i) => `<button class="chip" data-tpl="${i}">${esc(t.name)}</button>`).join("")}</div>`}
    <form class="form" id="sm-form" autocomplete="off">
      <label>Nom<input class="field" id="sm-name" maxlength="120" value="${esc(src?.name || "")}" placeholder="Ma playlist intelligente"></label>
      <div class="sm-match">Les titres qui correspondent à
        <select class="select" id="sm-match"><option value="all">toutes les règles</option><option value="any">au moins une règle</option></select></div>
      <div id="sm-rules"></div>
      <div><button type="button" class="btn sm" id="sm-add">${icon("plus")} Ajouter une règle</button></div>
      <div class="sm-opts">
        <label class="sm-inline">Trier par <select class="select" id="sm-sort">${SORTS.map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select></label>
        <label class="check"><input type="checkbox" id="sm-lim-on"> Limiter à <input class="field sm-num" id="sm-limit" type="number" min="1" max="2000" inputmode="numeric"> titres</label>
      </div>
    </form>
    <datalist id="sm-genres"></datalist>
    <div class="sm-preview" id="sm-preview" aria-live="polite"></div>`;
  openModal({
    title: existing ? "Modifier la playlist intelligente" : "Nouvelle playlist intelligente",
    wide: true, body,
    foot: `<button class="btn ghost" data-close>Annuler</button><button class="btn primary" id="sm-save">${existing ? "Enregistrer" : "Créer la playlist"}</button>`,
    onMount: (m, close) => {
      const rulesEl = $("#sm-rules", m);
      const draw = () => {
        rulesEl.innerHTML = spec.rules.length ? spec.rules.map(ruleHtml).join("")
          : `<p class="dim" style="margin:0">Aucune règle : toute la bibliothèque (avec le tri et la limite ci-dessous).</p>`;
        $("#sm-match", m).value = spec.match;
        $("#sm-sort", m).value = spec.sort;
        $("#sm-lim-on", m).checked = !!spec.limit;
        $("#sm-limit", m).value = spec.limit || 100;
        $("#sm-limit", m).disabled = !spec.limit;
      };
      const preview = debounce(async () => {
        const box = $("#sm-preview", m);
        if (!box) return;
        const incomplete = spec.rules.some((r) => (KIND[r.field] === "text" && !String(r.value ?? "").trim())
          || ((KIND[r.field] === "number" || (KIND[r.field] === "date" && r.op !== "never")) && [].concat(r.value).some((v) => v === "" || v == null)));
        if (incomplete) {
          box.classList.remove("err");
          box.innerHTML = `<span class="dim">Complète la règle pour voir les titres qui correspondent.</span>`;
          return;
        }
        try {
          const r = await api.post("/api/smart/preview", { spec });
          box.classList.remove("err");
          box.innerHTML = `<div class="sm-count"><b>${plural(r.count, "titre correspond", "titres correspondent")}</b>${r.dur ? ` · ${fmtLong(r.dur)}` : ""}</div>
            ${r.tracks.length ? `<ol class="sm-list">${r.tracks.slice(0, 8).map((t) => `<li><span class="ell">${esc(t.title)}</span><span class="dim ell">${esc(t.artist)}</span></li>`).join("")}</ol>${r.count > 8 ? `<div class="dim" style="font-size:12px">… et ${r.count - 8} autres</div>` : ""}` : ""}`;
        } catch (err) {
          box.classList.add("err");
          box.textContent = err.message;
        }
      }, 250);
      const changed = () => { preview(); };
      genres().then((g) => { const dl = $("#sm-genres", m); if (dl) dl.innerHTML = g.map((x) => `<option value="${esc(x)}">`).join(""); });
      draw(); preview();

      rulesEl.addEventListener("change", (e) => {
        const row = e.target.closest(".sm-rule");
        if (!row) return;
        const r = spec.rules[+row.dataset.i];
        const k = e.target.dataset.k;
        if (k === "field") { spec.rules[+row.dataset.i] = defaultRule(e.target.value); draw(); }
        else if (k === "op") {
          r.op = e.target.value;
          if (KIND[r.field] === "number" && r.op === "between" && !Array.isArray(r.value)) r.value = [r.value, r.value];
          if (KIND[r.field] === "number" && r.op !== "between" && Array.isArray(r.value)) r.value = r.value[0];
          if (KIND[r.field] === "date" && r.op !== "never" && !(r.value > 0)) r.value = 30;
          draw();
        }
        changed();
      });
      rulesEl.addEventListener("input", (e) => {
        const row = e.target.closest(".sm-rule");
        if (!row || e.target.dataset.k !== "value") return;
        const r = spec.rules[+row.dataset.i];
        const kind = KIND[r.field];
        let v = e.target.value;
        if (kind === "number" || kind === "date") {
          v = v === "" ? "" : Number(v);
          if (r.field === "duration" && v !== "") v = Math.round(v * 60);
        }
        if (e.target.dataset.part != null) { r.value = Array.isArray(r.value) ? r.value : [r.value, r.value]; r.value[+e.target.dataset.part] = v; }
        else r.value = v;
        changed();
      });
      rulesEl.addEventListener("click", (e) => {
        const rm = e.target.closest("[data-rm]");
        if (!rm) return;
        spec.rules.splice(+rm.closest(".sm-rule").dataset.i, 1);
        draw(); changed();
      });
      $("#sm-add", m).addEventListener("click", () => { spec.rules.push(defaultRule(spec.rules.length ? "year" : "genre")); draw(); changed(); });
      $("#sm-match", m).addEventListener("change", (e) => { spec.match = e.target.value; changed(); });
      $("#sm-sort", m).addEventListener("change", (e) => { spec.sort = e.target.value; changed(); });
      $("#sm-lim-on", m).addEventListener("change", (e) => {
        spec.limit = e.target.checked ? (Number($("#sm-limit", m).value) || 100) : null;
        $("#sm-limit", m).disabled = !e.target.checked;
        changed();
      });
      $("#sm-limit", m).addEventListener("input", (e) => { if (spec.limit !== null) { spec.limit = Number(e.target.value) || 1; changed(); } });
      $$("[data-tpl]", m).forEach((b) => b.addEventListener("click", () => {
        const t = TEMPLATES[+b.dataset.tpl];
        spec = clone(t.spec);
        $("#sm-name", m).value = t.name;
        $$("[data-tpl]", m).forEach((x) => x.classList.toggle("on", x === b));
        draw(); changed();
      }));
      const save = async (e) => {
        e?.preventDefault();
        const name = $("#sm-name", m).value.trim() || "Playlist intelligente";
        const btn = $("#sm-save", m);
        btn.disabled = true;
        try {
          const out = existing
            ? await api.patch(`/api/smart/${existing.id}`, { name, spec })
            : await api.post("/api/smart", { name, spec });
          close();
          emit("playlists");
          if (existing) { toast("Playlist mise à jour"); page.refresh(); }
          else { toast(`« ${out.name} » créée : ${plural(out.count, "titre", "titres")}`); location.hash = `#/smart/${out.id}`; }
        } catch (err) { btn.disabled = false; toastError(err); }
      };
      $("#sm-save", m).addEventListener("click", save);
      $("#sm-form", m).addEventListener("submit", save);
    },
  });
}
