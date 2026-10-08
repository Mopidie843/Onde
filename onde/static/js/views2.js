// Pages « Statistiques » et « Réglages ».
import { api, emit } from "./api.js";
import { $, $$, esc, enc, icon, fmtSize, fmtTime, fmtRel, plural, cover, storage } from "./util.js";
import { trackList, albumCard, emptyState, shelf, flushLists } from "./components.js";
import { toast, toastError, openModal, confirmDialog } from "./ui.js";
import { download } from "./dialogs.js";
import { isFollowing, setFollowing, lastRemote, onSync } from "./sync.js";
import { httpsSection, bindHttps } from "./https.js";
import { androidSection, bindAndroidSettings, remoteSection, bindRemote } from "./android.js";
import { alignSection, bindAlign } from "./align.js";
import { offline, autoOffline } from "./offline.js";
import { appShellTip } from "./downloads.js";
import { wrappedView } from "./wrapped.js";

function deviceSection() {
  if (!offline.supported) return "";
  const n = offline.urls.size;
  const cols = offline.collections.size;
  return `<h2 class="set-title">Musique sur cet appareil</h2>
    <div class="settings-card">
      <div class="sc-text"><h3>Écoute hors connexion</h3><p>${cols
        ? `${plural(n, "titre", "titres")} (${plural(cols, "album ou playlist", "albums ou playlists")}) · ${fmtSize(offline.totalBytes)} gardés sur cet appareil${offline.busy ? " · téléchargement en cours" : ""}.`
        : "Touche le bouton de téléchargement d'un album, d'une playlist ou de tes titres likés pour les écouter même sans le Pi (voiture, métro, voyage…)."}
        <br><span class="dim">Chaque appareil a ses propres téléchargements, liés à l'adresse utilisée pour ouvrir Onde.</span></p></div>
      <a class="btn" href="#/downloads">${icon("download-circle")} ${cols ? "Voir" : "Ouvrir"}</a>
    </div>
    <div class="settings-card">
      <div class="sc-text"><h3>Garder automatiquement</h3><p>Sur le Wi-Fi, cet appareil garde tout seul tes <b>titres likés</b> et tes <b>dernières écoutes</b> : en 4G qui capte mal ou sans réseau, ils se lisent quand même.</p></div>
      <label class="switch"><input type="checkbox" id="auto-off"${autoOffline.on ? " checked" : ""}><span class="sw-track"><span class="sw-thumb"></span></span></label>
    </div>
    ${cols ? appShellTip().replace('class="dl-tip', 'style="margin:-4px 0 14px" class="dl-tip') : ""}`;
}

import { adminSection, bindProfiles, isAdmin, profileSection, setMe } from "./profiles.js";
import { bindSound, soundSection } from "./sound.js";

let page = null; // { setColor, setTopTitle, player, info, refresh, playKey }
export function bindViews2(ctx) { page = ctx; }

// ---------------------------------------------------------------- utilitaires graphiques
function niceMax(v) {
  if (v <= 0) return 10;
  const exp = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * exp >= v) return m * exp;
  return 10 * exp;
}
function fmtMinutes(m) {
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60), r = m % 60;
  return r ? `${h} h ${String(r).padStart(2, "0")}` : `${h} h`;
}
function fmtListening(sec) {
  const m = Math.round(sec / 60);
  return fmtMinutes(m);
}

/**
 * Histogramme en colonnes (HTML/CSS, responsive). data : [{ label, value, tip }]
 * - une seule série : pas de légende (le titre la nomme) ;
 * - barres ≤ 24 px, bout arrondi 4 px, espace de 2 px, grille fine et discrète ;
 * - survol/focus : infobulle ; tableau des données disponible juste en dessous.
 */
function columnChart(data, { unit = "min", xLabel = (d) => d.label, every = 1, ariaLabel = "" } = {}) {
  const max = niceMax(Math.max(...data.map((d) => d.value), 0));
  const ticks = [max, max / 2, 0];
  const fmt = (v) => unit === "min" ? fmtMinutes(Math.round(v)) : Math.round(v).toLocaleString("fr");
  return `<div class="viz" role="img" aria-label="${esc(ariaLabel)}">
    <div class="viz-y">${ticks.map((t) => `<span style="bottom:${(t / max) * 100}%">${fmt(t)}</span>`).join("")}</div>
    <div class="viz-plot">
      ${ticks.map((t) => `<i class="viz-grid" style="bottom:${(t / max) * 100}%"></i>`).join("")}
      <div class="viz-cols">${data.map((d, i) => `<div class="viz-col" tabindex="0" data-tip="${esc(d.tip || `${fmt(d.value)} · ${d.label}`)}">
        <span class="viz-bar${d.value ? "" : " zero"}" style="height:${d.value ? Math.max(1.5, (d.value / max) * 100) : 0}%"></span></div>`).join("")}</div>
      <div class="viz-x">${data.map((d, i) => `<span>${i % every === 0 ? esc(xLabel(d, i)) : ""}</span>`).join("")}</div>
      <div class="viz-tip" hidden></div>
    </div>
  </div>
  <details class="viz-table"><summary>Voir les données</summary>
    <table><thead><tr><th>Période</th><th>${unit === "min" ? "Durée" : "Écoutes"}</th></tr></thead>
    <tbody>${data.map((d) => `<tr><td>${esc(d.label)}</td><td>${fmt(d.value)}</td></tr>`).join("")}</tbody></table>
  </details>`;
}

function bindCharts(root) {
  root.querySelectorAll(".viz-plot").forEach((plot) => {
    const tip = plot.querySelector(".viz-tip");
    const show = (col) => {
      const [value, ...rest] = col.dataset.tip.split(" · ");
      tip.innerHTML = "";
      const b = document.createElement("b");
      b.textContent = value;
      const sp = document.createElement("span");
      sp.textContent = rest.join(" · ");
      tip.append(b, sp);
      tip.hidden = false;
      const pr = plot.getBoundingClientRect(), cr = col.getBoundingClientRect();
      const x = cr.left - pr.left + cr.width / 2;
      tip.style.left = `${Math.min(Math.max(x, 60), pr.width - 60)}px`;
      plot.querySelectorAll(".viz-col.on").forEach((c) => c.classList.remove("on"));
      col.classList.add("on");
    };
    const hide = () => { tip.hidden = true; plot.querySelectorAll(".viz-col.on").forEach((c) => c.classList.remove("on")); };
    plot.addEventListener("pointerover", (e) => { const c = e.target.closest(".viz-col"); if (c) show(c); });
    plot.addEventListener("pointerleave", hide);
    plot.addEventListener("focusin", (e) => { const c = e.target.closest(".viz-col"); if (c) show(c); });
    plot.addEventListener("focusout", hide);
  });
}

const dayFmt = new Intl.DateTimeFormat("fr", { day: "numeric", month: "short" });
const monthFmt = new Intl.DateTimeFormat("fr", { month: "short", year: "2-digit" });

// ---------------------------------------------------------------- statistiques
const RANGES = [[7, "7 jours"], [30, "30 jours"], [365, "12 mois"], [0, "Depuis le début"]];

export const wrapped = (el, params) => wrappedView(el, params, page);

export async function stats(el) {
  document.title = "Statistiques · Onde";
  page.setColor("#2b2356");
  let days = storage.get("onde.stats.days", 30);
  el.innerHTML = `<div class="page stats-page">
    <div class="greet-row"><h1 class="page-title">Tes statistiques</h1><a class="chip" href="#/wrapped">${icon("sparkles", "sm")} Ton année</a></div>
    <div class="toolbar" role="tablist">${RANGES.map(([d, l]) => `<button class="chip${d === days ? " on" : ""}" data-days="${d}">${l}</button>`).join("")}</div>
    <div id="st-body"><div class="spinner"></div></div>
  </div>`;
  const body = $("#st-body", el);
  const load = async () => {
    body.classList.add("refetch");
    const [d, hist] = await Promise.all([api.get(`/api/stats?days=${days}`), api.get("/api/history?limit=30")]);
    render(d, hist);
    body.classList.remove("refetch");
  };
  const render = (d, hist) => {
    if (!d.plays) {
      body.innerHTML = emptyState("chart", "Pas encore d'écoutes sur cette période",
        "Tes statistiques se remplissent au fil de tes écoutes (un titre compte après 30 secondes).",
        `<a class="btn primary" href="#/">${icon("play")} Écouter de la musique</a>`);
      return;
    }
    const series = d.series.map((s) => {
      const date = d.unit === "day" ? new Date(`${s.label}T12:00:00`) : new Date(`${s.label}-15T12:00:00`);
      const label = d.unit === "day" ? dayFmt.format(date) : monthFmt.format(date);
      return { label, value: s.minutes, tip: `${fmtMinutes(s.minutes)} · ${label}` };
    });
    const every = series.length > 20 ? 7 : series.length > 12 ? 3 : 1;
    const hours = d.hours.map((n, h) => ({ label: `${h} h`, value: n, tip: `${plural(n, "écoute", "écoutes")} · ${h} h – ${h + 1} h` }));
    const busiest = d.hours.indexOf(Math.max(...d.hours));
    const totalGenres = d.topGenres.reduce((s, g) => s + g.count, 0) || 1;
    body.innerHTML = `
      <div class="kpis">
        <div class="kpi"><span>Temps d'écoute</span><b>${fmtListening(d.seconds)}</b></div>
        <div class="kpi"><span>Écoutes</span><b>${d.plays.toLocaleString("fr")}</b></div>
        <div class="kpi"><span>Titres différents</span><b>${d.tracks.toLocaleString("fr")}</b></div>
        <div class="kpi"><span>Artistes différents</span><b>${d.artists.toLocaleString("fr")}</b></div>
      </div>
      <section class="card-box">
        <div class="sec-head"><h2>Minutes d'écoute par ${d.unit === "day" ? "jour" : "mois"}</h2></div>
        ${columnChart(series, { unit: "min", every, ariaLabel: `Minutes d'écoute par ${d.unit === "day" ? "jour" : "mois"}` })}
      </section>
      <div class="two-col stats-2">
        <section class="card-box">
          <div class="sec-head"><h2>Quand tu écoutes</h2><span class="muted">Surtout vers ${busiest} h</span></div>
          ${columnChart(hours, { unit: "n", every: 6, ariaLabel: "Écoutes selon l'heure de la journée" })}
        </section>
        <section class="card-box">
          <div class="sec-head"><h2>Tes genres</h2></div>
          ${d.topGenres.length ? `<div class="meters">${d.topGenres.map((g) => {
            const pct = Math.round((g.count / totalGenres) * 100);
            return `<a class="meter" href="#/genre/${enc(g.name)}"><span class="m-l">${esc(g.name)}</span><span class="m-track"><i style="width:${pct}%"></i></span><span class="m-v">${pct} %</span></a>`;
          }).join("")}</div>` : `<p class="muted">Ajoute des genres à tes titres pour voir la répartition.</p>`}
        </section>
      </div>
      ${d.topArtists.length ? `<section class="section"><div class="sec-head"><h2>Tes artistes les plus écoutés</h2></div>
        <div class="shelf">${d.topArtists.map((a, i) => `<div class="card round">
          <div class="cv">${cover(a.cover, 384, a.name, "user", a.name)}<span class="rank">${i + 1}</span></div>
          <a class="card-link" href="#/artist/${enc(a.name)}"><div class="c1 ell">${esc(a.name)}</div></a>
          <div class="c2">${plural(a.count, "écoute", "écoutes")} · ${fmtListening(a.seconds)}</div></div>`).join("")}</div></section>` : ""}
      ${d.topTracks.length ? `<section class="section"><div class="sec-head"><h2>Tes titres les plus écoutés</h2></div>
        ${trackList(d.topTracks, { ctx: { key: `top:${days}`, label: "Tes titres les plus écoutés", href: "#/stats", kind: "tes stats" }, head: false, numbered: false, note: (t) => plural(t.count, "écoute", "écoutes") })}</section>` : ""}
      ${shelf("Tes albums les plus écoutés", d.topAlbums.map((a) => albumCard(a, plural(a.plays, "écoute", "écoutes"))))}
      ${hist.length ? `<section class="section"><div class="sec-head"><h2>Historique</h2></div>
        ${trackList(hist, { ctx: { key: "history", label: "Historique", href: "#/stats", kind: "l'historique" }, head: false, note: (t) => `${fmtRel(t.playedAt)}${t.source === "pi" ? " · sur le Pi" : t.source === "offline" ? " · hors connexion" : ""}` })}</section>` : ""}
      <p class="dim" style="font-size:12px;margin-top:24px">Une écoute compte après 30 secondes (ou la moitié d'un titre court). ${d.first ? `Première écoute enregistrée ${fmtRel(d.first)}.` : ""}</p>`;
    bindCharts(body);
    flushLists(body);
  };
  el.addEventListener("click", (e) => {
    const b = e.target.closest("[data-days]");
    if (!b) return;
    days = +b.dataset.days;
    storage.set("onde.stats.days", days);
    $$("[data-days]", el).forEach((x) => x.classList.toggle("on", x === b));
    load().catch(toastError);
  });
  await load();
}

// ---------------------------------------------------------------- doublons potentiels
const qual = (t) => `${(t.ext || "").replace(".", "").toUpperCase()}${t.bitrate ? ` ${Math.round(t.bitrate / 1000)} kbit/s` : ""}`;

async function showDuplicates() {
  toast("Recherche des doublons…");
  let groups;
  try {
    groups = (await api.get("/api/library/duplicates")).groups;
  } catch (e) { toastError(e); return; }
  const body = () => !groups.length
    ? `<p class="muted" style="margin:0">Aucun doublon potentiel trouvé — ta bibliothèque est propre !</p>`
    : `<p class="muted" style="margin:0 0 14px;font-size:13px">${plural(groups.length, "groupe", "groupes")} de titres qui se ressemblent. Le premier de chaque groupe est la <b>meilleure version</b> (qualité, puis la plus complète). « Garder » met les autres à la corbeille (récupérables 30 jours) ; likes, playlists et historique passent sur celui gardé.</p>
       <div class="dupe-groups">${groups.map((g, gi) => `
        <div class="dupe-group">
          <b>${esc(g.artist)} — ${esc(g.title)}</b>
          <div class="dupe-tracks">${g.tracks.map((t, i) => `
            <div class="dupe-track">
              <span class="ell">${esc(t.title)}</span>
              <span class="dim">${fmtTime(t.duration)} · ${fmtSize(t.size)} · ${esc(qual(t))}</span>
              <button class="btn ${i === 0 ? "white" : "ghost"} sm" data-keep="${t.id}" data-group="${gi}">${icon("check", "sm")} Garder${i === 0 ? " (conseillé)" : " celui-ci"}</button>
            </div>`).join("")}</div>
        </div>`).join("")}</div>`;
  const { el } = openModal({ title: "Doublons potentiels", wide: true, body: body(),
    foot: groups.length ? `<button class="btn white" data-keep-all>${icon("check")} Garder la meilleure version partout</button>` : "" });
  el.addEventListener("click", async (e) => {
    if (e.target.closest("[data-keep-all]")) {
      if (!(await confirmDialog({ title: "Tout fusionner ?", text: `Dans chacun des ${groups.length} groupes, la meilleure version est gardée et les autres vont à la corbeille (récupérables 30 jours).`, ok: "Fusionner", danger: false }))) return;
      try { const r = await api.post("/api/library/duplicates/merge", {}); groups = []; $(".modal-body", el).innerHTML = body(); toast(`${plural(r.removed, "doublon", "doublons")} mis à la corbeille`); emit("library"); } catch (err) { toastError(err); }
      return;
    }
    const b = e.target.closest("[data-keep]");
    if (!b) return;
    const gi = +b.dataset.group;
    const g = groups[gi];
    const keep = +b.dataset.keep;
    try {
      const r = await api.post("/api/library/duplicates/merge", { keep, remove: g.tracks.filter((t) => t.id !== keep).map((t) => t.id) });
      groups.splice(gi, 1);
      $(".modal-body", el).innerHTML = body();
      toast(`${plural(r.removed, "doublon", "doublons")} à la corbeille`);
      emit("library");
    } catch (err) { toastError(err); }
  });
}

async function showQuality() {
  let list;
  try { list = (await api.get("/api/library/quality")).tracks; } catch (e) { toastError(e); return; }
  const row = (t) => `<div class="dupe-track" data-q="${t.id}"><span class="ell">${esc(t.artist)} — ${esc(t.title)}</span>
    <span class="dim">${esc(t.ext.replace(".", "").toUpperCase())} ${t.kbps} kbit/s</span>
    <button class="btn ghost sm" data-qrep="${t.id}">${icon("refresh", "sm")} Remplacer</button></div>`;
  const { el } = openModal({ title: "Qualité audio", wide: true,
    body: !list.length ? `<p class="muted" style="margin:0">Aucun titre à 128 kbit/s ou moins : tout est en bonne qualité.</p>`
      : `<p class="muted" style="margin:0 0 12px;font-size:13px">${plural(list.length, "titre compressé", "titres compressés")} à 128 kbit/s ou moins. « Remplacer » cherche une meilleure version en ligne (YouTube, SoundCloud) : si elle est meilleure, elle prend sa place (likes et playlists gardés, l'ancienne va à la corbeille).</p>
        <div class="dupe-tracks">${list.map(row).join("")}</div>`,
    foot: list.length ? `<button class="btn white" data-qall>${icon("refresh")} Tout remplacer (un par un)</button>` : "" });
  const replace = async (id) => {
    const line = $(`[data-q="${id}"]`, el);
    const btn = line && $("button", line);
    if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spinner sm"></span> En cours…`; }
    try {
      const r = await api.post(`/api/library/quality/${id}/replace`);
      if (line) line.querySelector("button").outerHTML = `<span class="chip ${r.replaced ? "on" : ""}" style="pointer-events:none">${r.replaced ? "Remplacé" : esc(r.reason || "Gardé")}</span>`;
      if (r.replaced) emit("library");
    } catch (err) { if (btn) { btn.disabled = false; btn.textContent = "Réessayer"; } toastError(err); }
  };
  el.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-qrep]");
    if (b) { replace(+b.dataset.qrep); return; }
    if (e.target.closest("[data-qall]")) {
      e.target.closest("[data-qall]").disabled = true;
      for (const t of list) { if (!el.isConnected) break; await replace(t.id); }
      toast("Remplacement terminé");
    }
  });
}

// ---------------------------------------------------------------- réglages
const PREFS = [
  ["autoplay", "Lecture automatique", "Quand la file d'attente se termine, Onde continue avec des titres similaires."],
  ["normalize", "Volume égalisé", "Tous les titres au même volume (norme EBU R128), sans les faire saturer. Sur iPhone, seul le mode enceinte en profite (iOS verrouille le volume des pages web)."],
  ["online_lyrics", "Paroles en ligne", "Cherche tout seul, en arrière-plan, les paroles synchronisées de tes titres sur LRCLIB (base libre et gratuite), et réessaie régulièrement tant qu'elles ne sont pas trouvées. Les paroles non synchronisées peuvent ensuite être calées sur la voix par l'IA (ci-dessous)."],
  ["starter_pack", "Playlist de départ", "Au premier démarrage, Onde télécharge tout seul la playlist de départ (Mastu, JOYCA, Dominic Fike, Black M…). Les titres déjà présents sont sautés."],
  ["shorten_titles", "Titres raccourcis", "Retire des noms les émojis, « [AMV] », « 4K », « Sub. Español », « || »… (« MOONLIGHT🌙~ Kali Uchis || [AMV] 4K » devient « MOONLIGHT — Kali Uchis »). Fait au démarrage et à chaque ajout ; tes fichiers ne sont pas modifiés."],
  ["remove_unwanted", "Retirer « Slowed » et « Reverb »", "Les morceaux dont le titre ou le nom de fichier contient un de ces mots sont mis automatiquement à la corbeille ci-dessous (récupérables 30 jours)."],
  ["artist_images", "Photos d'artistes", "Récupère la photo de chaque artiste depuis Deezer."],
  ["auto_covers", "Pochettes automatiques", "Cherche en ligne (Apple Music, Deezer) les pochettes des albums qui n'en ont pas."],
  ["embed_covers", "Écrire les pochettes dans les fichiers", "Les pochettes ajoutées sont enregistrées dans les MP3 : elles s'afficheront aussi dans tes autres lecteurs."],
  ["notify_lyrics", "Notification : paroles synchronisées", "L'appli Android te prévient quand l'IA a fini de caler les paroles d'un titre."],
  ["notify_import", "Notification : musique ajoutée", "L'appli Android te prévient quand de nouveaux titres entrent dans la bibliothèque."],
  ["notify_download", "Notification : téléchargement terminé", "L'appli Android te prévient quand un titre demandé par nom ou par lien est prêt."],
  ["analyze_loudness", "Analyse du volume en arrière-plan", "Mesure le volume de chaque titre (ffmpeg, basse priorité) pour le volume égalisé."],
];

const sw = (key, on, disabled = false) =>
  `<label class="switch"><input type="checkbox" data-pref="${key}"${on ? " checked" : ""}${disabled ? " disabled" : ""}><span class="sw-track"><span class="sw-thumb"></span></span></label>`;

export async function settingsView(el) {
  document.title = "Réglages · Onde";
  page.setColor("#23232f");
  const info = await api.get("/api/info");
  setMe(info);
  const s = info.stats;
  const set = info.settings;
  const hours = Math.round(s.duration / 3600);
  const url = info.urls[0];
  el.innerHTML = `<div class="page">
    <h1 class="page-title">Réglages</h1>
    <section class="qr-card">
      <img class="qr" src="/api/qr.svg?text=${enc(url)}" alt="QR code vers ${esc(url)}" width="148" height="148">
      <div>
        <h3>Ouvre Onde sur ton téléphone</h3>
        <p class="muted">Scanne ce code avec l'appareil photo, ou tape l'adresse dans le navigateur (même Wi-Fi) :</p>
        <div class="urls">${info.urls.map((u) => `<code>${esc(u)}</code>`).join("")}</div>
        <p class="dim" style="font-size:12px;margin:10px 0 0">Ajoute ensuite la page à l'écran d'accueil pour l'ouvrir comme une app.</p>
      </div>
    </section>
    <div class="stats">
      <div class="stat"><b>${s.tracks.toLocaleString("fr")}</b><span>titres</span></div>
      <div class="stat"><b>${s.albums.toLocaleString("fr")}</b><span>albums</span></div>
      <div class="stat"><b>${s.artists.toLocaleString("fr")}</b><span>artistes</span></div>
      <div class="stat"><b>${hours ? `${hours} h` : `${Math.max(1, Math.round(s.duration / 60))} min`}</b><span>de musique</span></div>
      <div class="stat"><b>${fmtSize(s.size)}</b><span>sur le disque</span></div>
      <a class="stat link" href="#/stats"><b>${icon("chart")}</b><span>Tes statistiques d'écoute</span></a>
    </div>

    ${profileSection(info)}
    ${adminSection(info)}

    <div class="admin-only">
    <h2 class="set-title">Préférences</h2>
    <div class="prefs">
      ${PREFS.map(([k, t, d]) => `<div class="pref">
        <div class="pref-t"><b>${t}</b><p>${d}${k === "normalize" || k === "analyze_loudness" ? `<br><span class="dim" data-loudness></span>` : ""}</p></div>
        ${sw(k, set[k], k === "analyze_loudness" && !info.ffmpeg)}
      </div>`).join("")}
    </div>

    <h2 class="set-title">Corbeille automatique</h2>
    <div class="settings-card" style="flex-wrap:wrap"><div class="sc-text"><h3>Mots retirés de la bibliothèque</h3>
      <p>Séparés par des virgules (sans accents ni majuscules, mot entier).</p>
      <input id="unw-words" type="text" value="${esc(set.unwanted_words || "")}" spellcheck="false" style="width:100%;margin-top:8px">
      <div id="trash-list" class="dim" style="margin-top:10px;font-size:13px">…</div></div>
      <div style="display:flex;gap:8px;flex-direction:column"><button class="btn sm" data-tidy-run>Ranger maintenant</button><button class="btn sm" data-tidy-empty>Vider la corbeille</button></div>
    </div>

    <h2 class="set-title">Recherche des paroles</h2>
    <div class="settings-card"><div class="sc-text"><h3>Paroles trouvées toutes seules</h3>
      <p>Onde regarde d'abord le fichier (.lrc, tags, .txt), puis LRCLIB, pour toute ta bibliothèque, en commençant par le titre que tu lances. Les paroles non synchronisées sont remplacées par une version synchronisée dès qu'elle existe, ou calées par l'IA ci-dessous.</p>
      <p class="dim" style="margin-top:6px" data-lyr-status>…</p></div>
      <button class="btn sm" data-lyr-retry-all>Tout rechercher</button>
    </div>

    ${alignSection()}
    </div>

    ${soundSection(page.player)}

    <div class="import-only">
    <h2 class="set-title">Artistes suivis</h2>
    <div class="settings-card" style="flex-wrap:wrap"><div class="sc-text" style="flex-basis:100%"><h3>Nouveautés automatiques</h3>
      <p>Touche « Suivre » sur la page d'un artiste : ses <b>nouveaux</b> albums et singles sont téléchargés tout seuls (vérification deux fois par jour, sur Deezer).</p>
      <div id="follow-list" class="prof-list">…</div></div>
      <button class="btn admin-only" data-follow-check>${icon("refresh")} Vérifier maintenant</button>
    </div>
    </div>

    ${deviceSection()}

    <div class="admin-only">${remoteSection()}</div>

    ${androidSection()}

    <div class="admin-only">
    <h2 class="set-title">Console développeur</h2>
    <div class="settings-card"><div class="sc-text"><h3>Tout ce qui se passe, en direct</h3>
      <p>Journal du serveur et de cette page, requêtes, paroles, IA, système et lecteur, avec une ligne de commande. Sur ordinateur : touche <kbd>F7</kbd>. Marche aussi à distance (4G/5G) et dans l'appli Android.</p></div>
      <button class="btn sm" data-devconsole>Ouvrir</button>
    </div>

    ${httpsSection(info)}
    </div>

    <div class="admin-only">
    <h2 class="set-title">Lecture sur le Raspberry Pi</h2>
    <div class="settings-card">
      <div class="sc-text"><h3>Mode enceinte</h3><p>${info.pi
        ? "Choisis « Raspberry Pi » avec le bouton appareil du lecteur : la musique sort des haut-parleurs du Pi (HDMI, USB, Bluetooth) et continue même téléphone éteint."
        : "Indisponible : installe mpv sur le Pi (<code>sudo apt install mpv</code>) puis relance Onde."}</p></div>
      <span class="chip ${info.pi ? "on" : ""}" style="pointer-events:none">${info.pi ? "Prêt" : "mpv absent"}</span>
    </div>
    ${info.pi ? `<div class="settings-card">
      <div class="sc-text"><h3>Sortie audio du Pi</h3><p>Où le son du mode enceinte est envoyé.</p></div>
      <select class="select" id="pi-dev"><option value="">Chargement…</option></select>
    </div>` : ""}
    </div>

    <h2 class="set-title">Plusieurs appareils</h2>
    <div class="settings-card">
      <div class="sc-text"><h3>Suivre un autre appareil</h3><p>Quand c'est activé, cet appareil se calque automatiquement sur ce qui joue ailleurs sur le réseau (même titre, même position, pause comprise) au lieu de jouer sa propre file. Pratique pour écouter la même chose dans deux pièces.</p><p id="sync-status" class="dim" style="margin-top:6px"></p></div>
      <label class="switch"><input type="checkbox" id="sync-follow"${isFollowing() ? " checked" : ""}><span class="sw-track"><span class="sw-thumb"></span></span></label>
    </div>

    <div class="admin-only">
    <h2 class="set-title">Bibliothèque</h2>
    <div class="settings-card">
      <div class="sc-text"><h3>Rescanner la bibliothèque</h3><p>Dossier : <code>${esc(info.musicDir)}</code>${info.extraDirs.length ? ` + ${info.extraDirs.map((d) => `<code>${esc(d)}</code>`).join(", ")}` : ""}.
        ${info.watching ? "Les fichiers copiés dans le dossier sont détectés instantanément." : "Rescan automatique toutes les 5 minutes."}</p>
        <div class="progress" id="scan-prog" hidden><i style="width:0"></i></div><p id="scan-txt" style="margin-top:6px"></p></div>
      <button class="btn" data-scan="0">${icon("refresh")} Rescanner</button>
      <button class="btn ghost" data-scan="1" title="Relit tous les fichiers">Scan complet</button>
    </div>
    <div class="settings-card">
      <div class="sc-text"><h3>Pochettes et photos manquantes</h3><p>Cherche maintenant les pochettes d'albums et les photos d'artistes qui manquent.</p><p id="cov-txt" style="margin-top:6px"></p></div>
      <button class="btn" data-covers>${icon("sparkles")} Pochettes</button>
      <button class="btn" data-photos>${icon("user")} Photos d'artistes</button>
    </div>
    <div class="settings-card">
      <div class="sc-text"><h3>Playlists</h3><p>Importe une playlist au format .m3u / .m3u8 (VLC, Winamp, iTunes…). Pour exporter, utilise le menu ⋯ d'une playlist.</p></div>
      <button class="btn" data-m3u>${icon("upload")} Importer une playlist</button>
    </div>
    <div class="settings-card">
      <div class="sc-text"><h3>Sauvegarde complète</h3><p>Télécharge tous tes morceaux (tags et pochettes déjà dedans, rangés par artiste puis album) dans un seul .zip, avec tes playlists et titres likés en prime — de quoi tout retrouver si la carte du Pi lâche. <span id="backup-size" class="dim"></span></p></div>
      <a class="btn" id="backup-dl" href="/api/library/export" download>${icon("download")} Télécharger la sauvegarde</a>
    </div>
    <div class="settings-card" style="flex-wrap:wrap">
      <div class="sc-text" style="flex-basis:100%"><h3>Sauvegarde automatique</h3><p>Chaque jour, une copie de la base (profils, likes, playlists, historique, réglages, paroles) — les 14 dernières sont gardées. Mets-la sur un autre disque ou un dossier synchronisé (Google Drive, OneDrive…), et ajoute une phrase secrète pour la <b>chiffrer</b>.</p>
        <div class="form" style="margin-top:10px;gap:10px">
          <label>Dossier<input class="field" id="bk-dir" placeholder="(dans les données d'Onde)" spellcheck="false"></label>
          <label>Phrase secrète (chiffrement)<input class="field" id="bk-pass" type="password" autocomplete="new-password" placeholder="8 caractères ou plus — garde-la bien"></label>
          <label class="dim" style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="bk-auto"> Sauvegarde automatique chaque jour</label>
        </div>
        <p class="dim" id="bk-status" style="margin-top:8px"></p>
        <div id="bk-files" class="prof-list"></div></div>
      <div class="sc-btns"><button class="btn" data-bk-save>Enregistrer</button><button class="btn white" data-bk-run>${icon("download")} Sauvegarder maintenant</button></div>
    </div>
    <div class="settings-card">
      <div class="sc-text"><h3>Qualité audio</h3><p>Repère les titres compressés à faible débit (128 kbit/s ou moins) et propose de les remplacer par une meilleure version.</p></div>
      <button class="btn" data-quality>${icon("sparkles")} Vérifier la qualité</button>
    </div>
    <div class="settings-card">
      <div class="sc-text"><h3>Doublons potentiels</h3><p>Repère les morceaux importés plusieurs fois sous un titre légèrement différent (remaster, radio edit…) et garde la meilleure version.</p></div>
      <button class="btn" data-dupes>${icon("search")} Chercher les doublons</button>
    </div>
    </div>

    <div class="admin-only">
    <h2 class="set-title">Mise à jour</h2>
    <div class="settings-card"><div class="sc-text"><h3>Onde ${esc(info.version)}</h3>
      <p id="upd-txt">Choisis le nouveau zip d'Onde (onde-tout.zip) : le code est remplacé, tes réglages, ta musique et tes données sont gardés, puis Onde redémarre. L'ancienne version est gardée au cas où.</p></div>
      <label class="btn white" id="upd-btn">${icon("upload")} Installer une mise à jour<input type="file" accept=".zip,application/zip" id="upd-file" hidden></label>
    </div>
    <div class="settings-card" style="flex-wrap:wrap"><div class="sc-text" style="flex-basis:100%"><h3>Mise à jour automatique</h3>
      <p>À chaque démarrage (puis toutes les 12 h), en arrière-plan : si une nouvelle version existe, elle s'installe toute seule et Onde redémarre. Les téléphones avec l'appli reçoivent ensuite la nouvelle appli.</p>
      <div class="ask-form" style="margin-top:10px"><input class="field" id="upd-src" placeholder="pseudo/depot (GitHub) ou https://…/onde.json" spellcheck="false">
        <button class="btn" data-upd-save>Enregistrer</button><button class="btn" data-upd-check>${icon("refresh")} Vérifier maintenant</button></div>
      <p class="dim" id="upd-status" style="margin-top:8px;font-size:13px"></p></div>
      <label class="switch" title="Mise à jour automatique"><input type="checkbox" id="upd-auto"><span class="sw-track"><span class="sw-thumb"></span></span></label>
    </div>
    </div>

    <section class="section" style="margin-top:28px"><div class="sec-head"><h2>Raccourcis clavier</h2></div>
      <div class="kbd-list">
        <div><span>Lecture / pause</span><kbd>Espace</kbd></div><div><span>Titre suivant / précédent</span><span><kbd>Ctrl</kbd> <kbd>→</kbd> / <kbd>←</kbd></span></div>
        <div><span>Avancer / reculer 10 s</span><span><kbd>→</kbd> / <kbd>←</kbd></span></div><div><span>Volume</span><span><kbd>Ctrl</kbd> <kbd>↑</kbd> / <kbd>↓</kbd></span></div>
        <div><span>Couper le son</span><kbd>M</kbd></div><div><span>Aléatoire / répéter</span><span><kbd>S</kbd> / <kbd>R</kbd></span></div>
        <div><span>Liker le titre</span><kbd>L</kbd></div><div><span>Paroles</span><kbd>P</kbd></div>
        <div><span>File d'attente</span><kbd>Q</kbd></div><div><span>Plein écran</span><kbd>F</kbd></div>
        <div><span>Rechercher</span><kbd>/</kbd></div><div><span>Tout sélectionner</span><span><kbd>Ctrl</kbd> <kbd>A</kbd></span></div>
      </div></section>
    <p class="dim" style="font-size:12px;margin-top:28px">Onde ${esc(info.version)} · ${info.password ? "profils activés (connexion obligatoire)" : "accès libre sur le réseau local"} · ${info.ffmpeg ? "ffmpeg présent (conversion et analyse du volume)" : "ffmpeg absent"} · formats : ${info.formats.join(" ")}</p>
  </div>`;

  // préférences
  el.addEventListener("change", async (e) => {
    const input = e.target.closest("[data-pref]");
    if (input) {
      const key = input.dataset.pref;
      try {
        const res = await api.put("/api/settings", { [key]: input.checked });
        page.player.applySettings(res);
        if (key === "autoplay") { storage.set("onde.autoplay", null); page.player.setAutoplay(res.autoplay); }
        toast(`${PREFS.find((p) => p[0] === key)[1]} : ${input.checked ? "activé" : "désactivé"}`);
      } catch (err) { input.checked = !input.checked; toastError(err); }
    }
    if (e.target.id === "pi-dev") {
      try { await api.put("/api/settings", { pi_audio_device: e.target.value }); toast("Sortie audio du Pi modifiée"); } catch (err) { toastError(err); }
    }
  });
  bindHttps(el, info);
  bindAndroidSettings(el);
  const lyrStatus = () => api.get("/api/lyrics/status").then((s) => {
    const box = $("[data-lyr-status]", el);
    if (!box) return;
    const c = s.counts;
    box.textContent = !s.enabled ? "Recherche en ligne désactivée (Préférences → Paroles en ligne)."
      : `${c.synced} synchronisées · ${c.plain} non synchronisées · ${c.instrumental} instrumentaux · ${c.missing} introuvables pour l'instant (réessai automatique)${s.pending ? ` · ${s.pending} à chercher` : ""}${s.current ? ` · en cours : ${s.current.title}` : ""}${s.offlineSince ? " · LRCLIB injoignable, reprise automatique" : ""}`;
  }).catch(() => {});
  lyrStatus();
  el.addEventListener("click", async (e) => {
    if (!e.target.closest("[data-lyr-retry-all]")) return;
    try { const r = await api.post("/api/lyrics/retry"); toast(r.count ? `${plural(r.count, "titre relancé", "titres relancés")}` : "Rien à relancer : tout est déjà cherché"); lyrStatus(); } catch (err) { toastError(err); }
  });
  const trashList = () => !isAdmin() ? Promise.resolve() : api.get("/api/tidy").then((t) => {
    const box = $("#trash-list", el);
    if (!box) return;
    box.innerHTML = t.items.length
      ? t.items.map((i) => `<div style="display:flex;justify-content:space-between;gap:8px;padding:3px 0"><span>${esc(i.title)} — ${esc(i.artist)} <i>(${esc(i.word)})</i></span><button class="btn sm" data-tidy-restore="${esc(i.id)}">Récupérer</button></div>`).join("")
      : "La corbeille est vide.";
  }).catch(() => {});
  trashList();
  const words = $("#unw-words", el);
  words?.addEventListener("change", async () => {
    try { await api.put("/api/settings", { unwanted_words: words.value }); toast("Mots enregistrés"); } catch (err) { toastError(err); }
  });
  el.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-tidy-run],[data-tidy-empty],[data-tidy-restore]");
    if (!b) return;
    try {
      if (b.dataset.tidyRun !== undefined) { const r = await api.post("/api/tidy/run"); toast(`${plural(r.shortened, "titre raccourci", "titres raccourcis")} · ${plural(r.removed, "morceau retiré", "morceaux retirés")}`); }
      else if (b.dataset.tidyEmpty !== undefined) { const r = await api.del("/api/tidy"); toast(`${plural(r.deleted, "fichier supprimé", "fichiers supprimés")} définitivement`); }
      else { await api.post(`/api/tidy/restore/${b.dataset.tidyRestore}`); toast("Morceau récupéré"); }
      trashList();
    } catch (err) { toastError(err); }
  });
  bindRemote(el);
  const offAlign = bindAlign(el);
  bindProfiles(el, () => page.refresh());
  bindSound(el, page.player);
  const updLoad = () => api.get("/api/update").then((u) => {
    const src = $("#upd-src", el); if (src && document.activeElement !== src) src.value = u.source || "";
    const auto = $("#upd-auto", el); if (auto) auto.checked = !!u.auto;
    const st = u.status || {};
    const txt = $("#upd-status", el);
    if (txt) txt.textContent = !u.source ? "Pas de source : choisis un dépôt GitHub (ex. mopidie/onde) ou une adresse."
      : st.checking ? "Vérification…"
      : st.error ? `Dernière vérification : ${st.error}`
      : st.last ? `Vérifié ${fmtRel(st.last)} · dernière version publiée : ${st.latest || "?"}${st.latest && st.latest === u.version ? " (tu es à jour)" : ""}` : "Pas encore vérifié.";
    if (u.apk) { const t = $("#upd-txt", el); if (t && !t.dataset.apk) { t.dataset.apk = 1; t.insertAdjacentHTML("beforeend", ` <span class="dim">Appli Android fournie : ${esc(u.apk.versionName || "?")}.</span>`); } }
    return u;
  });
  el.addEventListener("click", async (e) => {
    try {
      if (e.target.closest("[data-upd-save]")) { await api.put("/api/settings", { update_source: $("#upd-src", el).value.trim() }); toast("Source enregistrée"); updLoad(); }
      if (e.target.closest("[data-upd-check]")) {
        await api.put("/api/settings", { update_source: $("#upd-src", el).value.trim() });
        await api.post("/api/update/check"); toast("Vérification en cours…");
        setTimeout(() => updLoad().catch(() => {}), 4000);
      }
    } catch (err) { toastError(err); }
  });
  $("#upd-auto", el)?.addEventListener("change", (e) => api.put("/api/settings", { auto_update: e.target.checked }).catch(toastError));
  if (isAdmin()) updLoad().then((u) => {
    if (!u.available) { const b = $("#upd-btn", el); if (b) { b.classList.add("disabled"); b.style.pointerEvents = "none"; b.style.opacity = ".5"; } const t = $("#upd-txt", el); if (t) t.textContent = u.reason; }
  }).catch(() => {});
  $("#upd-file", el)?.addEventListener("change", async (e) => {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    if (!(await confirmDialog({ title: "Installer cette mise à jour ?", text: `« ${f.name} » (${fmtSize(f.size)}) remplace le code d'Onde, puis Onde redémarre.`, ok: "Installer", danger: false }))) return;
    toast("Installation de la mise à jour…", { timeout: 8000 });
    try {
      const r = await api.post("/api/update", f);
      toast(`Onde ${r.from} → ${r.to} : ${r.restarting ? "redémarrage…" : "redémarre Onde pour terminer"}`, { timeout: 10000 });
      if (r.restarting) setTimeout(() => location.reload(), 7000);
    } catch (err) { toastError(err); }
  });
  const bkLoad = () => !isAdmin() ? null : api.get("/api/backup").then((b) => {
    const dir = $("#bk-dir", el);
    if (!dir) return;
    if (b.custom) dir.value = b.dir; else dir.placeholder = b.dir;
    $("#bk-auto", el).checked = b.auto;
    $("#bk-pass", el).placeholder = b.encrypted ? "•••••••• (chiffrement activé — vide = garder)" : b.canEncrypt ? "8 caractères ou plus — garde-la bien" : "chiffrement indisponible (paquet cryptography)";
    $("#bk-status", el).textContent = `${b.encrypted ? "Chiffrées" : "Non chiffrées"} · ${b.last ? `dernière ${fmtRel(b.last)}` : "pas encore de sauvegarde"}${b.error ? ` · ${b.error}` : ""}`;
    $("#bk-files", el).innerHTML = b.files.slice(0, 5).map((f) => `<div class="prof-row"><div class="pr-main"><b>${esc(f.name)}</b><small class="dim">${fmtRel(f.at)} · ${fmtSize(f.size)}${f.encrypted ? " · chiffrée" : ""}</small></div>
      <button class="btn sm ghost" data-bk-restore="${esc(f.name)}" data-enc="${f.encrypted ? 1 : 0}">Restaurer</button></div>`).join("");
  }).catch(() => {});
  bkLoad();
  el.addEventListener("click", async (e) => {
    try {
      if (e.target.closest("[data-bk-save]")) {
        const body = { dir: $("#bk-dir", el).value, auto: $("#bk-auto", el).checked };
        const pw = $("#bk-pass", el).value;
        if (pw) body.passphrase = pw;
        await api.put("/api/backup", body); $("#bk-pass", el).value = ""; toast("Sauvegarde enregistrée"); bkLoad();
      }
      if (e.target.closest("[data-bk-run]")) { const r = await api.post("/api/backup/run"); toast(`Sauvegarde écrite : ${r.file}`); bkLoad(); }
      const rb = e.target.closest("[data-bk-restore]");
      if (rb) {
        if (!(await confirmDialog({ title: "Restaurer cette sauvegarde ?", text: `La base actuelle est remplacée par « ${rb.dataset.bkRestore} » (elle est gardée à côté, au cas où). Onde redémarre.`, ok: "Restaurer" }))) return;
        const r = await api.post("/api/backup/restore", { name: rb.dataset.bkRestore });
        toast(r.restarting ? "Restauration : Onde redémarre…" : "Restauration prête : redémarre Onde pour l'appliquer", { timeout: 8000 });
        if (r.restarting) setTimeout(() => location.reload(), 6000);
      }
    } catch (err) { toastError(err); }
  });
  api.get("/api/follows").then((r) => {
    const box = $("#follow-list", el);
    if (!box) return;
    box.innerHTML = r.artists.length ? r.artists.map((a) => `<div class="prof-row"><div class="pr-main"><a href="#/artist/${enc(a.name)}"><b>${esc(a.name)}</b></a>
      <small class="dim">${a.releases.length ? a.releases.map((x) => `${esc(x.title)} (${esc(x.date || "")}${x.downloaded ? ` · ${x.downloaded} titres ajoutés` : ""})`).join(" · ") : `suivi ${fmtRel(a.since)} · pas de nouveauté pour l'instant`}</small></div>
      <button class="icon-btn" data-unfollow="${esc(a.name)}" title="Ne plus suivre">${icon("close", "sm")}</button></div>`).join("")
      : `<p class="dim" style="margin:0">Aucun artiste suivi.</p>`;
    if (r.status?.last) box.insertAdjacentHTML("beforeend", `<p class="dim" style="font-size:12px;margin:6px 0 0">Dernière vérification ${fmtRel(r.status.last)}${r.status.error ? ` · ${esc(r.status.error)}` : ""}</p>`);
  }).catch(() => {});
  el.addEventListener("click", async (e) => {
    const u = e.target.closest("[data-unfollow]");
    if (u) { try { await api.del(`/api/follows/${enc(u.dataset.unfollow)}`); u.closest(".prof-row").remove(); toast("Artiste retiré"); } catch (err) { toastError(err); } }
    if (e.target.closest("[data-follow-check]")) { try { await api.post("/api/follow-check"); toast("Vérification des nouveautés lancée"); } catch (err) { toastError(err); } }
  });
  $("#auto-off", el)?.addEventListener("change", (e) => { autoOffline.set(e.target.checked); toast(e.target.checked ? "Likes et dernières écoutes gardés sur cet appareil (en Wi-Fi)" : "Garde automatique désactivée"); });
  api.get("/api/library/export/info").then((r) => {
    const sz = $("#backup-size", el);
    if (sz) sz.textContent = `(${plural(r.tracks, "titre", "titres")}, ${fmtSize(r.size)} — peut prendre du temps selon ta connexion)`;
  }).catch(() => {});
  if (info.pi) {
    api.get("/api/pi/devices").then((r) => {
      const sel = $("#pi-dev", el);
      if (!sel) return;
      const devices = r.devices.length ? r.devices : [{ name: "", description: "Par défaut" }];
      sel.innerHTML = devices.map((d) => `<option value="${esc(d.name === "auto" ? "" : d.name)}"${(r.current || "") === (d.name === "auto" ? "" : d.name) ? " selected" : ""}>${esc(d.description || d.name)}</option>`).join("");
    }).catch(() => { const sel = $("#pi-dev", el); if (sel) sel.innerHTML = `<option>mpv ne répond pas</option>`; });
  }

  const syncStatus = () => {
    const t = $("#sync-status", el);
    if (!t) return;
    const r = lastRemote();
    if (!isFollowing()) { t.textContent = r ? `Dernier appareil vu : ${r.device}` : ""; return; }
    t.textContent = r ? `Suit « ${r.device} » ${r.paused ? "(en pause)" : "— en lecture"}` : "En attente d'un appareil qui joue de la musique…";
  };
  syncStatus();
  const offSync = onSync(syncStatus);
  $("#sync-follow", el).addEventListener("change", (e) => { setFollowing(e.target.checked); syncStatus(); });

  let timer = 0;
  const poll = async () => {
    const st2 = await api.get("/api/library/status");
    const prog = $("#scan-prog", el), txt = $("#scan-txt", el), cov = $("#cov-txt", el);
    if (!prog) return;
    if (st2.scanning) {
      prog.hidden = false;
      $("i", prog).style.width = `${st2.scan_total ? Math.round((st2.scan_done / st2.scan_total) * 100) : 5}%`;
      txt.textContent = `Analyse… ${st2.scan_done} / ${st2.scan_total}`;
    } else {
      prog.hidden = true;
      txt.textContent = st2.last_scan ? `Dernier scan ${fmtRel(st2.last_scan)} : +${st2.added} ajoutés, ${st2.updated} mis à jour, ${st2.removed} retirés${st2.errors ? `, ${st2.errors} illisibles` : ""}.` : "";
    }
    const parts = [];
    if (st2.covers_running) parts.push(`pochettes : ${st2.covers_running} en cours`);
    if (st2.covers_found) parts.push(`${st2.covers_found} pochette${st2.covers_found > 1 ? "s" : ""} trouvée${st2.covers_found > 1 ? "s" : ""}`);
    if (st2.artists_running) parts.push("photos d'artistes en cours…");
    parts.push(`${st2.artistPhotos} photo${st2.artistPhotos > 1 ? "s" : ""} d'artistes`);
    cov.textContent = parts.join(" · ");
    const L = st2.loudness;
    $$("[data-loudness]", el).forEach((x) => {
      x.textContent = !L.ffmpeg ? "ffmpeg absent : seuls les titres avec ReplayGain sont égalisés."
        : L.pending ? `Analyse : ${L.done} / ${L.total} titres${st2.loudness_running ? " (en cours)" : ""}` : `${L.done} / ${L.total} titres analysés`;
    });
    timer = setTimeout(() => poll().catch(() => {}), st2.scanning || st2.covers_running || st2.loudness_running || st2.artists_running ? 1000 : 5000);
  };
  el.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-scan]");
    if (b) { await api.post("/api/library/scan", { full: b.dataset.scan === "1" }); toast("Scan lancé"); clearTimeout(timer); setTimeout(poll, 300); }
    if (e.target.closest("[data-covers]")) {
      const r = await api.post("/api/library/covers");
      toast(r.queued ? `Recherche de ${r.queued} pochette${r.queued > 1 ? "s" : ""}…` : "Toutes les pochettes sont déjà là");
      clearTimeout(timer); setTimeout(poll, 300);
    }
    if (e.target.closest("[data-photos]")) { await api.post("/api/library/artists"); toast("Recherche des photos d'artistes…"); clearTimeout(timer); setTimeout(poll, 600); }
    if (e.target.closest("[data-m3u]")) (await import("./views.js")).importM3U();
    if (e.target.closest("[data-dupes]")) showDuplicates();
    if (e.target.closest("[data-quality]")) showQuality();
  });
  poll().catch(() => {});
  return () => { clearTimeout(timer); offSync(); offAlign(); };
}
