// Briques HTML réutilisables : listes de titres, cartes, étagères, en-têtes.
import { esc, enc, icon, fmtTime, fmtRel, cover, coverImg, mosaic, likedCover, placeholder } from "./util.js";
import { offline, DOWNLOADABLE } from "./offline.js";

// Registre des listes affichées : permet de lancer la lecture depuis n'importe quelle ligne.
export const lists = new Map();
let seq = 0;
export function registerList(tracks, ctx, allIds = null) {
  const id = `L${++seq}`;
  lists.set(id, { ids: allIds || tracks.map((t) => t.id), tracks, ctx });
  if (lists.size > 60) lists.delete(lists.keys().next().value);
  return id;
}

let currentId = () => null;
let isPaused = () => true;
export function bindPlayerState(getId, getPaused) { currentId = getId; isPaused = getPaused; }

const eq = () => `<span class="eq${isPaused() ? " paused" : ""}"><i></i><i></i><i></i></span>`;
// glisser des titres vers une playlist : seulement avec une souris
const DRAG = typeof matchMedia === "function" && matchMedia("(hover: hover) and (pointer: fine)").matches;

export function artistLinks(t) {
  return `<a href="#/artist/${enc(t.artist)}">${esc(t.artist)}</a>`;
}

const DLM = `<svg class="ic dlm" role="img" aria-label="Sur cet appareil"><use href="#i-dl-done"/></svg>`;

function row(t, i, o) {
  const playing = t.id === currentId();
  const num = o.numbered ? (t.no ?? i + 1) : i + 1;
  const hi = t.ext && ["flac", "wav"].includes(t.ext) ? `<span class="badge">${t.ext.toUpperCase()}</span>` : "";
  return `<div class="row${playing ? " playing" : ""}${offline.has(t.id) ? " is-dl" : ""}" data-i="${i}" data-tid="${t.id}"${t.entry ? ` data-entry="${t.entry}"` : ""} tabindex="0"${DRAG ? ' draggable="true"' : ""}>
  <div class="c-num"><span class="n">${num}</span>${eq()}<button class="rowplay" tabindex="-1" aria-label="Lire">${icon("play")}</button></div>
  <div class="c-title">${o.cover ? `<div class="rc">${cover(t.cover, 96, t.album, "note")}</div>` : ""}<div class="tt"><div class="t1">${esc(t.title)}</div><div class="t2">${DLM}${hi}${artistLinks(t)}${o.note ? `<span class="note-m">· ${esc(o.note(t))}</span>` : ""}</div></div></div>
  ${o.album ? `<div class="c-album">${o.note ? `<span class="note">${esc(o.note(t))}</span>` : `<a href="#/album/${t.albumId}">${esc(t.album)}</a>`}</div>` : ""}
  ${o.album && o.added ? `<div class="c-added">${fmtRel(t.padded || t.added)}</div>` : ""}
  <div class="c-like"><button class="icon-btn like${t.liked ? " on" : ""}" data-like="${t.id}" aria-label="${t.liked ? "Retirer des titres likés" : "Liker"}">${icon(t.liked ? "heart-fill" : "heart")}</button></div>
  <div class="c-dur">${fmtTime(t.dur)}</div>
  <div class="c-more"><button class="icon-btn more" data-more aria-label="Plus d'options">${icon("more")}</button></div>
  ${o.sortable ? `<div class="grip" aria-label="Déplacer">${icon("grip", "sm")}</div>` : ""}
</div>`;
}

/**
 * options : ctx {key,label,href,kind}, album, added, cover, numbered, sortable, discs, head, allIds, playlistId
 */
export function trackList(tracks, o = {}) {
  o = { album: true, added: false, cover: true, numbered: false, sortable: false, discs: false, head: true, ...o };
  if (!tracks.length) return o.empty || "";
  const listId = registerList(tracks, o.ctx || null, o.allIds);
  const cls = ["tl", tracks.length > 300 ? "long" : "", o.album ? "" : "no-album", o.album && !o.added ? "no-added" : "", o.numbered ? "numbered" : "", o.sortable ? "sortable" : ""]
    .filter(Boolean).join(" ");
  const head = o.head ? `<div class="tl-head"><div class="c-num">#</div><div>Titre</div>${o.album ? "<div>Album</div>" : ""}${o.album && o.added ? '<div class="c-added">Ajouté</div>' : ""}<div></div><div class="c-dur">${icon("clock", "sm")}</div><div></div>${o.sortable ? "<div></div>" : ""}</div>` : "";
  const multiDisc = o.discs && new Set(tracks.map((t) => t.disc || 1)).size > 1;
  // longues listes : les 150 premières lignes tout de suite, le reste juste après (flushLists)
  const first = tracks.length > 300 && !multiDisc && !o.sortable ? 150 : tracks.length;
  const html = rowsHtml(tracks, 0, first, o, multiDisc);
  if (first < tracks.length) lists.get(listId).pending = { from: first, o };
  return `<div class="${cls}" data-list="${listId}"${o.playlistId ? ` data-playlist="${o.playlistId}"` : ""}>${head}${html}</div>`;
}

function rowsHtml(tracks, from, to, o, multiDisc = false) {
  let html = "";
  let disc = null;
  for (let i = from; i < to; i++) {
    const t = tracks[i];
    if (multiDisc && (t.disc || 1) !== disc) {
      disc = t.disc || 1;
      html += `<div class="disc-head">${icon("disc", "sm")} Disque ${disc}</div>`;
    }
    html += row(t, i, o);
  }
  return html;
}

/**
 * Longues listes : les lignes suivantes ne sont dessinées qu'à l'approche du bas de l'écran (par
 * paquets), au lieu de toutes d'un coup. « Tous les titres » sur 20 000 morceaux reste ainsi fluide
 * sur un téléphone : seules les lignes réellement parcourues existent dans la page.
 */
export function flushLists(root) {
  root.querySelectorAll("[data-list]").forEach((el) => {
    const list = lists.get(el.dataset.list);
    if (!list || !list.pending || list.pending.watching) return;
    list.pending.watching = true;
    const { o } = list.pending;
    const scroller = el.closest("#main, .modal-body, .np-side") || null;
    const sentinel = document.createElement("div");
    sentinel.className = "tl-more";
    sentinel.setAttribute("aria-hidden", "true");
    el.append(sentinel);
    const MARGIN = 1600;
    const near = () => {
      const r = sentinel.getBoundingClientRect();
      const bottom = scroller ? scroller.getBoundingClientRect().bottom : innerHeight;
      return r.top - bottom < MARGIN;
    };
    let io = null;
    const done = () => { io?.disconnect(); sentinel.remove(); };
    const load = () => {
      if (!el.isConnected || !list.pending) { done(); return; }
      const from = list.pending.from;
      const to = Math.min(from + 250, list.tracks.length);
      sentinel.insertAdjacentHTML("beforebegin", rowsHtml(list.tracks, from, to, o));
      list.pending.from = to;
      const added = [];
      for (let n = sentinel.previousElementSibling; n && added.length < to - from; n = n.previousElementSibling) {
        if (n.classList.contains("row")) added.push(n);
      }
      el.dispatchEvent(new CustomEvent("rows-added", { bubbles: true, detail: { rows: added } }));
      if (to >= list.tracks.length) { list.pending = null; done(); el.dispatchEvent(new Event("rows-complete")); return; }
      // encore proche du bas (grand écran) : un paquet de plus à l'image suivante
      requestAnimationFrame(() => { if (list.pending && near()) load(); });
    };
    if (typeof IntersectionObserver === "function") {
      io = new IntersectionObserver((entries) => { if (entries.some((e) => e.isIntersecting)) load(); },
        { root: scroller, rootMargin: `0px 0px ${MARGIN}px 0px` });
      io.observe(sentinel);
    } else {
      const step = () => { load(); if (list.pending) setTimeout(step, 16); }; // très vieux navigateurs
      setTimeout(step, 0);
    }
  });
}

const fab = (key, label = "Lire") => `<button class="fab" data-play="${esc(key)}" aria-label="${esc(label)}">${icon("play")}</button>`;

export function albumCard(a, sub = null) {
  return `<div class="card">
    <div class="cv">${cover(a.cover, 384, a.name, "disc", a.name)}${fab(`album:${a.id}`)}</div>
    <a class="card-link" href="#/album/${a.id}"><div class="c1 ell">${esc(a.name)}</div></a>
    <div class="c2">${sub ?? `${a.year ? `${a.year} • ` : ""}${esc(a.artist)}`}</div>
  </div>`;
}
export function artistCard(a) {
  return `<div class="card round">
    <div class="cv">${cover(a.cover, 384, a.name, "user", a.name)}${fab(`artist:${a.name}`)}</div>
    <a class="card-link" href="#/artist/${enc(a.name)}"><div class="c1 ell">${esc(a.name)}</div></a>
    <div class="c2">Artiste</div>
  </div>`;
}
export function playlistCover(p, size) {
  return p.cover ? coverImg(p.cover, size, p.name) : mosaic(p.mosaic, size, p.name);
}
export function playlistCard(p) {
  return `<div class="card">
    <div class="cv">${playlistCover(p, 384)}${fab(`playlist:${p.id}`)}</div>
    <a class="card-link" href="#/playlist/${p.id}"><div class="c1 ell">${esc(p.name)}</div></a>
    <div class="c2">${p.description ? esc(p.description) : `${p.count} titre${p.count > 1 ? "s" : ""}`}</div>
  </div>`;
}
export function mixCard(m) {
  const art = m.covers && m.covers.length >= 4
    ? `<div class="mosaic">${m.covers.slice(0, 4).map((c) => coverImg(c, 192)).join("")}</div>`
    : m.covers && m.covers[0] ? coverImg(m.covers[0], 384, m.name) : placeholder(m.name, "radio");
  return `<div class="card mix" style="--mc:${esc(m.color || "#5b45c9")}">
    <div class="cv">${art}<span class="mix-band"><span class="mix-name">${esc(m.name)}</span></span>${fab(m.key)}</div>
    <button class="card-link" data-play-open="${esc(m.key)}" style="text-align:left;width:100%"><div class="c1 ell">${esc(m.name)}</div></button>
    <div class="c2">${esc(m.desc)}</div>
  </div>`;
}

export function likedCard(count) {
  return `<div class="card">
    <div class="cv">${likedCover()}${fab("liked")}</div>
    <a class="card-link" href="#/liked"><div class="c1 ell">Titres likés</div></a>
    <div class="c2">${count} titre${count > 1 ? "s" : ""}</div>
  </div>`;
}
export function newPlaylistCard() {
  return `<div class="card">
    <div class="cv"><div class="ph" style="--h:250">${icon("plus")}</div></div>
    <button class="card-link" data-act="new-playlist" style="text-align:left;width:100%"><div class="c1 ell">Nouvelle playlist</div></button>
    <div class="c2">Crée ta sélection</div>
  </div>`;
}

export function shelf(title, cards, href = null) {
  if (!cards.length) return "";
  return `<section class="section">
    <div class="sec-head"><h2>${href ? `<a href="${href}">${esc(title)}</a>` : esc(title)}</h2>${href ? `<a class="see-all" href="${href}">Tout afficher</a>` : ""}</div>
    <div class="shelf">${cards.join("")}</div>
  </section>`;
}
export function grid(cards) {
  return `<div class="grid">${cards.join("")}</div>`;
}
/**
 * Grande grille (1 800 albums…) : les cartes sont ajoutées par paquets à l'approche du bas de l'écran,
 * comme les longues listes de titres. Remplace le contenu de `box`.
 */
export function mountGrid(box, cards, first = 72, step = 96) {
  if (cards.length <= first + step) { box.innerHTML = grid(cards); return; }
  box.innerHTML = `<div class="grid">${cards.slice(0, first).join("")}</div>`;
  const g = box.firstElementChild;
  let at = first;
  const scroller = box.closest("#main") || null;
  const sentinel = document.createElement("div");
  sentinel.className = "tl-more";
  sentinel.setAttribute("aria-hidden", "true");
  box.append(sentinel);
  const near = () => sentinel.getBoundingClientRect().top - (scroller ? scroller.getBoundingClientRect().bottom : innerHeight) < 1400;
  let io = null;
  const more = () => {
    if (!g.isConnected) { io?.disconnect(); return; }
    g.insertAdjacentHTML("beforeend", cards.slice(at, at + step).join(""));
    at += step;
    if (at >= cards.length) { io?.disconnect(); sentinel.remove(); return; }
    requestAnimationFrame(() => { if (near()) more(); });
  };
  if (typeof IntersectionObserver !== "function") { box.innerHTML = grid(cards); return; }
  io = new IntersectionObserver((e) => { if (e.some((x) => x.isIntersecting)) more(); }, { root: scroller, rootMargin: "0px 0px 1400px 0px" });
  io.observe(sentinel);
}

export function hero({ kind, title, coverHtml, round = false, meta = "", desc = "", editable = false, titleAct = "" }) {
  const len = (title || "").length;
  const size = len > 42 ? "xlong" : len > 20 ? "long" : "";
  return `<header class="hero">
    <div class="hero-cover${round ? " round" : ""}${editable ? " editable" : ""}"${editable ? ' data-act="edit-cover" role="button" tabindex="0" aria-label="Changer la pochette"' : ""}>
      ${coverHtml}${editable ? `<div class="edit-hint">${icon("image")}<span>Changer la pochette</span></div>` : ""}
    </div>
    <div class="hero-info">
      <div class="hero-kind">${esc(kind)}</div>
      <h1 class="hero-title ${size}${titleAct ? " click" : ""}"${titleAct ? ` data-act="${titleAct}"` : ""}>${esc(title)}</h1>
      ${desc ? `<div class="hero-desc">${esc(desc)}</div>` : ""}
      <div class="hero-meta">${meta}</div>
    </div>
  </header>`;
}

export function actionBar(key, extra = "") {
  return `<div class="actions">
    <button class="play-btn accent xl" data-play="${esc(key)}" aria-label="Lecture">${icon("play")}</button>
    <button class="icon-btn p-shuffle" data-act="shuffle" aria-label="Lecture aléatoire" title="Lecture aléatoire">${icon("shuffle")}</button>
    ${offlineButton(key)}
    ${extra}
  </div>`;
}

// ---------------------------------------------------------------- écoute hors connexion
/** Bouton « télécharger sur cet appareil » (vide si ce n'est pas téléchargeable ici). */
export function offlineButton(key) {
  if (!offline.supported || !DOWNLOADABLE.test(key)) return "";
  const st = offline.progress(key).state;
  const label = esc(offlineLabel(key));
  return `<button class="icon-btn dl-btn${st === "done" ? " on" : st === "busy" ? " busy" : ""}" data-offline="${esc(key)}" title="${label}" aria-label="${label}">${offlineInner(key)}</button>`;
}
export function offlineInner(key) {
  const p = offline.progress(key);
  if (p.state === "done") return `${icon("dl-done")}<span class="sr">Retirer de cet appareil</span>`;
  if (p.state === "busy") {
    const pct = p.total ? p.done / p.total : 0;
    const c = 2 * Math.PI * 9;
    return `<svg class="ic dl-ring" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><circle class="arc" cx="12" cy="12" r="9" stroke-dasharray="${(c * pct).toFixed(1)} ${c.toFixed(1)}"/><path d="M12 8.5v6M9.6 12.3l2.4 2.4 2.4-2.4"/></svg><span class="sr">Téléchargement : ${p.done} sur ${p.total}</span>`;
  }
  return `${icon("download-circle")}<span class="sr">Télécharger sur cet appareil</span>`;
}
export function offlineLabel(key) {
  const p = offline.progress(key);
  return p.state === "done" ? "Sur cet appareil — toucher pour retirer"
    : p.state === "busy" ? `Téléchargement… ${p.done}/${p.total}` : "Télécharger sur cet appareil";
}
/** Met à jour boutons et marques « téléchargé » déjà affichés (sans tout redessiner). */
export function refreshOffline(root = document) {
  root.querySelectorAll("[data-offline]").forEach((b) => {
    const key = b.dataset.offline;
    const state = offline.progress(key).state;
    b.classList.toggle("on", state === "done");
    b.classList.toggle("busy", state === "busy");
    b.title = offlineLabel(key);
    b.setAttribute("aria-label", offlineLabel(key));
    b.innerHTML = offlineInner(key);
  });
  root.querySelectorAll(".row[data-tid]").forEach((r) => r.classList.toggle("is-dl", offline.has(+r.dataset.tid)));
}

export const emptyState = (ic, title, text, action = "") =>
  `<div class="empty">${ic ? `<svg class="empty-ic"><use href="#i-${ic}"/></svg>` : ""}<h3>${esc(title)}</h3><p>${text}</p>${action}</div>`;

export { placeholder, likedCover };
