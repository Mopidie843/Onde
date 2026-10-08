// Sélection multiple de titres (Ctrl/⌘ + clic, Maj + clic, ou mode sélection sur mobile)
// et glisser-déposer des titres vers les playlists de la barre latérale.
import { api, emit, toggleLike, trackCache } from "./api.js";
import { $, $$, icon, isMobile } from "./util.js";
import { lists } from "./components.js";
import { openMenu, toast, toastError, confirmDialog } from "./ui.js";
import { playlistSubmenu, addToPlaylist } from "./dialogs.js";
import { isAdmin } from "./profiles.js";
import { batchEdit, batchFix } from "./batch.js";

let player = null;
let router = null;
const sel = { listId: null, set: new Set(), anchor: -1, mode: false };

export function bindSelection(p, r) {
  player = p;
  router = r;
  bindDrag();
  // lignes dessinées plus tard (longues listes) : on y reporte la sélection en cours
  document.addEventListener("rows-added", (e) => {
    if (!sel.set.size || e.target.dataset?.list !== sel.listId) return;
    for (const r of e.detail?.rows || []) r.classList.toggle("sel", sel.set.has(+r.dataset.i));
  });
}

export const selecting = () => sel.mode || sel.set.size > 0;

function listOf(row) {
  const el = row.closest("[data-list]");
  return el ? { el, id: el.dataset.list } : null;
}

/** Gère un clic sur une ligne ; renvoie true si le clic a servi à la sélection. */
export function handleRowClick(row, e) {
  const l = listOf(row);
  if (!l) return false;
  const i = +row.dataset.i;
  const mod = e.ctrlKey || e.metaKey;
  if (!mod && !e.shiftKey && !sel.mode) {
    if (sel.set.size) clear();
    return false;
  }
  if (sel.listId !== l.id) { clear(false); sel.listId = l.id; }
  if (e.shiftKey && sel.anchor >= 0) {
    const [a, b] = [Math.min(sel.anchor, i), Math.max(sel.anchor, i)];
    for (let k = a; k <= b; k++) sel.set.add(k);
  } else if (sel.set.has(i)) {
    sel.set.delete(i);
  } else {
    sel.set.add(i);
  }
  sel.anchor = i;
  paint();
  return true;
}

export function startSelection(row) {
  const l = listOf(row);
  if (!l) return;
  clear(false);
  sel.mode = true;
  sel.listId = l.id;
  sel.set.add(+row.dataset.i);
  sel.anchor = +row.dataset.i;
  paint();
}

export function selectAll() {
  const el = document.querySelector("#view [data-list]");
  if (!el) return;
  const list = lists.get(el.dataset.list);
  if (!list) return;
  sel.listId = el.dataset.list;
  sel.set = new Set(list.tracks.map((_, i) => i));
  paint();
}

export function clear(repaint = true) {
  sel.set.clear();
  sel.anchor = -1;
  sel.mode = false;
  sel.listId = null;
  if (repaint) paint();
}

export function isSelected(row) {
  const l = listOf(row);
  return !!l && l.id === sel.listId && sel.set.has(+row.dataset.i);
}

function selectedTracks() {
  const list = lists.get(sel.listId);
  if (!list) return [];
  return [...sel.set].sort((a, b) => a - b).map((i) => list.tracks[i]).filter(Boolean);
}

function paint() {
  const el = sel.listId ? document.querySelector(`[data-list="${sel.listId}"]`) : null;
  $$(".row.sel").forEach((r) => { if (!el || !el.contains(r)) r.classList.remove("sel"); });
  // une seule passe sur les lignes affichées (et non une recherche par titre sélectionné :
  // « tout sélectionner » sur 20 000 titres restait sinon bloqué de longues secondes)
  if (el) el.querySelectorAll(".row[data-i]").forEach((r) => r.classList.toggle("sel", sel.set.has(+r.dataset.i)));
  const bar = $("#selbar");
  const n = sel.set.size;
  document.body.classList.toggle("selecting", selecting());
  if (!selecting()) { bar.hidden = true; return; }
  const inPlaylist = document.querySelector(`[data-list="${sel.listId}"]`)?.dataset.playlist;
  bar.hidden = false;
  bar.innerHTML = `<div class="selbar-in">
    <button class="icon-btn" data-sel="close" aria-label="Terminer">${icon("close")}</button>
    <b>${n ? `${n} titre${n > 1 ? "s" : ""} sélectionné${n > 1 ? "s" : ""}` : "Touche des titres pour les sélectionner"}</b>
    <span class="spacer"></span>
    <button class="btn sm white" data-sel="play" ${n ? "" : "disabled"}>${icon("play")} Lire</button>
    <button class="btn sm" data-sel="queue" ${n ? "" : "disabled"}>${icon("queue")}<span class="hide-mobile">File d'attente</span></button>
    <button class="btn sm" data-sel="playlist" ${n ? "" : "disabled"}>${icon("plus")}<span class="hide-mobile">Playlist</span></button>
    <button class="icon-btn" data-sel="more" aria-label="Plus" ${n ? "" : "disabled"}>${icon("more")}</button>
  </div>`;
  bar.dataset.playlist = inPlaylist || "";
}

export function selectionMenuItems() {
  const tracks = selectedTracks();
  const ids = tracks.map((t) => t.id);
  const pid = +($("#selbar").dataset.playlist || 0);
  return [
    { icon: "play", label: "Lire la sélection", onClick: () => { player.playList(ids, 0, null); clear(); } },
    { icon: "right", label: "Lire ensuite", onClick: () => { player.playNext(ids); toast(`${ids.length} titre(s) lus ensuite`); clear(); } },
    { icon: "queue", label: "Ajouter à la file d'attente", onClick: () => { player.addToQueue(ids); toast(`${ids.length} titre(s) ajoutés à la file`); clear(); } },
    { icon: "plus", label: "Ajouter à une playlist", sub: () => playlistSubmenu(ids, pid || null) },
    { icon: "heart", label: "Ajouter aux titres likés", onClick: async () => {
      for (const t of tracks) if (!(trackCache.get(t.id) || t).liked) await toggleLike(t.id);
      toast("Ajoutés aux titres likés"); clear();
    } },
    ...(pid ? [{ icon: "trash", label: "Retirer de la playlist", onClick: async () => {
      for (const t of tracks) if (t.entry) await api.del(`/api/playlists/${pid}/entries/${t.entry}`);
      emit("playlists"); toast("Retirés de la playlist"); clear(); router.refresh();
    } }] : []),
    ...(isAdmin() ? ["hr",
      { icon: "edit", label: "Modifier les infos de la sélection", onClick: () => batchEdit(tracks, clear) },
      { icon: "sparkles", label: "Réparer les infos en ligne", onClick: () => batchFix(tracks, clear) }] : []),
    ...(isAdmin() ? ["hr",
    { icon: "trash", label: "Supprimer les fichiers", danger: true, onClick: async () => {
      if (!(await confirmDialog({ title: `Supprimer ${ids.length} titre(s) ?`, text: "Les fichiers seront définitivement supprimés du Raspberry Pi." }))) return;
      for (const id of ids) { try { await api.del(`/api/tracks/${id}`); trackCache.delete(id); } catch (e) { toastError(e); } }
      toast("Titres supprimés"); clear(); emit("library"); router.refresh();
    } }] : []),
  ];
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-sel]");
  if (!b) return;
  const tracks = selectedTracks();
  const ids = tracks.map((t) => t.id);
  switch (b.dataset.sel) {
    case "close": clear(); break;
    case "play": player.playList(ids, 0, null); clear(); break;
    case "queue": player.addToQueue(ids); toast(`${ids.length} titre(s) ajoutés à la file`); clear(); break;
    case "playlist": playlistSubmenu(ids).then((items) => openMenu([{ heading: "Ajouter à une playlist" }, ...items], { anchor: b })).catch(toastError); break;
    case "more": openMenu(selectionMenuItems(), { anchor: b }); break;
    default: break;
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && selecting() && !document.querySelector(".modal, .menu")) clear();
});
addEventListener("hashchange", () => clear());

// ---------------------------------------------------------------- glisser des titres vers une playlist
function bindDrag() {
  let grip = false;
  document.addEventListener("pointerdown", (e) => { grip = !!e.target.closest(".grip"); }, true);
  document.addEventListener("dragstart", (e) => {
    const row = e.target.closest?.(".row[data-tid]");
    if (!row || grip || isMobile()) return;
    let tracks;
    if (isSelected(row)) tracks = selectedTracks();
    else {
      const l = lists.get(row.closest("[data-list]")?.dataset.list);
      const t = l && l.tracks[+row.dataset.i];
      tracks = t ? [t] : [];
    }
    if (!tracks.length) { e.preventDefault(); return; }
    e.dataTransfer.effectAllowed = "copy";
    e.dataTransfer.setData("application/x-onde-tracks", JSON.stringify(tracks.map((t) => t.id)));
    e.dataTransfer.setData("text/plain", tracks.map((t) => `${t.artist} - ${t.title}`).join("\n"));
    const ghost = document.createElement("div");
    ghost.className = "drag-ghost";
    ghost.textContent = tracks.length > 1 ? `${tracks.length} titres` : `${tracks[0].title} · ${tracks[0].artist}`;
    document.body.appendChild(ghost);
    e.dataTransfer.setDragImage(ghost, 16, 16);
    setTimeout(() => ghost.remove(), 0);
    document.body.classList.add("dragging-tracks");
  });
  document.addEventListener("dragend", () => {
    document.body.classList.remove("dragging-tracks");
    $$(".drop-target").forEach((x) => x.classList.remove("drop-target"));
  });
  const target = (e) => e.target.closest?.("#lib-list .lib-item[href^='#/playlist/'], #lib-list .lib-item[href='#/liked'], #queue-panel");
  const ours = (e) => [...(e.dataTransfer?.types || [])].includes("application/x-onde-tracks");
  document.addEventListener("dragover", (e) => {
    if (!ours(e)) return;
    const t = target(e);
    $$(".drop-target").forEach((x) => { if (x !== t) x.classList.remove("drop-target"); });
    if (t) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; t.classList.add("drop-target"); }
  });
  document.addEventListener("drop", async (e) => {
    if (!ours(e)) return;
    const t = target(e);
    if (!t) return;
    e.preventDefault();
    t.classList.remove("drop-target");
    const ids = JSON.parse(e.dataTransfer.getData("application/x-onde-tracks") || "[]");
    try {
      if (t.id === "queue-panel") { player.addToQueue(ids); toast(`${ids.length} titre(s) ajoutés à la file`); }
      else if (t.getAttribute("href") === "#/liked") {
        for (const id of ids) if (!trackCache.get(id)?.liked) await toggleLike(id);
        toast("Ajouté aux titres likés");
      } else {
        const pid = +t.getAttribute("href").split("/").pop();
        await addToPlaylist(pid, ids, t.querySelector(".li-1")?.textContent || "la playlist");
      }
      clear();
    } catch (err) { toastError(err); }
  });
}

