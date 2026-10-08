// Onde — point d'entrée : routeur, lecteur, barre latérale, raccourcis.
import { dev } from "./devhooks.js"; // en premier : la console (F7) note tout dès le chargement
import { api, bus, trackCache, contextTracks, toggleLike } from "./api.js";
import { $, $$, esc, enc, icon, fmtTime, cover, likedCover, isMobile, storage } from "./util.js";
import { setMe, ensureTerms } from "./profiles.js";
import { bindResume, offerResume } from "./resume.js";
import { bindJam, jamActive, onJam, openJam } from "./jam.js";
import { Player, SILENT_SRC } from "./player.js";
import { bindPlayerState, lists, artistLinks, playlistCover, emptyState, flushLists, refreshOffline } from "./components.js";
import { bindDialogs, openTrackMenu, createPlaylist, cardMenuItems } from "./dialogs.js";
import { openMenu, closeMenu, toast, toastError, makeSortable, confirmDialog } from "./ui.js";
import { offline, autoOffline } from "./offline.js";
import { bindDownloads, downloadsView, offlineNotice, hrefOf } from "./downloads.js";
import { bindSmart, smartView, openSmartEditor, smartCover } from "./smart.js";
import { bindViews, home, search, library, album, artist, playlist, liked, tracks, genre, importView } from "./views.js";
import { bindViews2, stats, wrapped, settingsView } from "./views2.js";
import { asksView } from "./asks.js";
import { showWhatsNew } from "./whatsnew.js";
import { bindDashboard, dashboardView, startAskBadge } from "./dashboard.js";
import { bindLyrics, setVisible as setLyricsVisible } from "./lyrics.js";
import { bindSelection, handleRowClick, selecting, clear as clearSelection, startSelection, selectAll } from "./selection.js";
import { enqueue, filesFromDrop } from "./upload.js";
import { bindSync } from "./sync.js";
import { bindAndroid } from "./android.js";

const main = $("#main");
const view = $("#view");
const topbar = $("#topbar");
const playerEl = $("#player");
const np = $("#np");
const queuePanel = $("#queue-panel");
const player = new Player($("#audio"));
dev.ctx = { player, api };
let info = null;
// AirPlay (Safari : iPhone, iPad, Mac) et Chromecast (Chrome) : sélecteurs d'appareils du navigateur
const cast = { airplay: false, airplayOn: false, remote: false, remoteOn: false };

bindPlayerState(() => player.currentId, () => player.paused);

// ======================================================================== routeur
const routes = [
  [/^\/?$/, home, "home"],
  [/^\/search(?:\/(.*))?$/, search, "search"],
  [/^\/library(?:\/(\w+))?$/, library, "library"],
  [/^\/album\/(\d+)$/, album, "album"],
  [/^\/artist\/(.+)$/, artist, "artist"],
  [/^\/playlist\/(\d+)$/, playlist, "playlist"],
  [/^\/liked$/, liked, "liked"],
  [/^\/tracks$/, tracks, "tracks"],
  [/^\/genre\/(.+)$/, genre, "genre"],
  [/^\/import$/, importView, "import"],
  [/^\/stats$/, stats, "stats"],
  [/^\/wrapped(?:\/(\d{4}))?$/, wrapped, "wrapped"],
  [/^\/settings$/, settingsView, "settings"],
  [/^\/downloads$/, downloadsView, "downloads"],
  [/^\/smart\/(\d+)$/, smartView, "smart"],
  [/^\/asks$/, asksView, "asks"],
  [/^\/dashboard$/, dashboardView, "dashboard"],
];
// hors connexion : ce qui marche sans le Pi (les albums/playlists téléchargés se lisent depuis l'appareil)
const OFFLINE_OK = { home: downloadsView, library: downloadsView, downloads: downloadsView, search: downloadsView, album, playlist, liked, smart: smartView };
const navStack = [];
const scrolls = new Map();
let cleanup = null;
let currentRoute = "home";
let renderSeq = 0;
let pageTitle = "Onde";
const lastRender = { offline: false, failed: false }; // comment la page affichée a été faite
let topTitleKey = null;

const router = {
  async go({ keepScroll = false } = {}) {
    const hash = location.hash.replace(/^#/, "") || "/";
    const found = routes.find(([re]) => re.test(hash)) || routes[0];
    const [re, routeFn, name] = found;
    const fn = offline.active ? (OFFLINE_OK[name] || offlineNotice) : routeFn;
    const params = (hash.match(re) || []).slice(1).filter((x) => x !== undefined);
    const token = ++renderSeq;
    // navigation arrière ? on restaure le défilement
    let restore = keepScroll ? main.scrollTop : 0;
    if (!keepScroll) {
      if (navStack.length > 1 && navStack[navStack.length - 2] === hash) { navStack.pop(); restore = scrolls.get(hash) || 0; }
      else if (navStack[navStack.length - 1] !== hash) navStack.push(hash);
    }
    closeMenu();
    if (cleanup) { try { cleanup(); } catch { /* ignore */ } cleanup = null; }
    currentRoute = name;
    document.body.classList.toggle("deep", !["home", "search", "library", "import", "stats", "downloads", "asks", "dashboard"].includes(name));
    setTopTitle(null);
    $$("[data-nav]").forEach((a) => a.classList.toggle("on", a.dataset.nav === name));
    let el = document.createElement("div");
    let failed = false;
    try {
      let out;
      for (let attempt = 0; ; attempt++) {
        try { out = await fn(el, params); break; } catch (e) {
          // réseau faible : la page actuelle reste affichée, on réessaie avant d'afficher une erreur
          if (attempt >= 1 || offline.active || token !== renderSeq || !/injoignable|Failed to fetch|NetworkError|Load failed/i.test(e.message)) throw e;
          await new Promise((r) => setTimeout(r, 2000));
          if (token !== renderSeq) return;
          el = document.createElement("div");
        }
      }
      if (token !== renderSeq) return;
      if (typeof out === "function") cleanup = out;
    } catch (e) {
      if (token !== renderSeq) return;
      failed = true;
      if (offline.active) offlineNotice(el);
      else el.innerHTML = `<div class="page">${emptyState("close", "Oups…", esc(e.message), `<button class="btn white" onclick="location.reload()">Réessayer</button>`)}</div>`;
    }
    view.replaceChildren(el);
    lastRender.offline = offline.active;
    lastRender.failed = failed;
    flushLists(el);
    const af = el.querySelector("[data-autofocus]");
    if (af && matchMedia("(hover: hover)").matches) af.focus({ preventScroll: true });
    pageTitle = document.title;
    if (!player.paused && player.current) document.title = `${player.current.title} • ${player.current.artist}`;
    markPlaying();
    renderState();
    main.scrollTop = restore;
    onScroll();
  },
  refresh() { return this.go({ keepScroll: true }); },
};
addEventListener("hashchange", () => {
  scrolls.set(navStack[navStack.length - 1], main.scrollTop);
  if (isMobile() && !queuePanel.hidden) toggleQueue();
  if (!np.hidden) closeNP();
  router.go();
});

function setColor(c) { main.style.setProperty("--c", c); }
function setTopTitle(title, key = null) {
  topTitleKey = title ? key : null;
  $("#top-title").innerHTML = title
    ? `${key ? `<button class="play-btn accent" data-play="${esc(key)}" aria-label="Lecture">${icon("play")}</button>` : ""}<b class="ell">${esc(title)}</b>`
    : "";
}
let ticking = false;
function onScroll() {
  if (ticking) return;
  ticking = true;
  requestAnimationFrame(() => {
    const y = main.scrollTop;
    topbar.classList.toggle("solid", y > 40);
    topbar.classList.toggle("titled", !!topTitleKey && y > 260);
    ticking = false;
  });
}
main.addEventListener("scroll", onScroll, { passive: true });

bindDialogs(player, router, {
  playKey, openLyrics, startSelection,
  offlineItem: (key) => (offline.supported && /^(album:\d+|playlist:\d+|smart:\d+|liked)$/.test(key) ? (offline.isSaved(key)
    ? { icon: "dl-done", label: "Retirer de cet appareil", onClick: () => toggleOffline(key) }
    : { icon: "download-circle", label: "Télécharger sur cet appareil", onClick: () => toggleOffline(key) }) : null),
});
bindViews({ setColor, setTopTitle, player, get info() { return info; }, refresh: () => router.refresh() });
bindViews2({ setColor, setTopTitle, player, get info() { return info; }, refresh: () => router.refresh() });
bindDownloads({ setColor, setTopTitle, player, refresh: () => router.refresh() });
bindSmart({ setColor, setTopTitle, player, refresh: () => router.refresh() });
bindDashboard({ setColor });
bindSelection(player, router);
bindLyrics(player, $("#np-lyrics"));
bindSync(player);
bindAndroid(player);

// ======================================================================== lecture depuis l'interface
const SILENT = SILENT_SRC; // « déverrouille » l'audio sur iOS au premier geste
let unlocked = false;
function unlockAudio() {
  if (unlocked || player.mode !== "local") return;
  unlocked = true;
  const a = player.audio;
  if (!a.paused) return;
  const prev = a.currentSrc;
  if (prev) return; // un titre est déjà chargé : sa lecture servira de déverrouillage
  a.src = SILENT;
  const p = a.play();
  if (p && p.then) p.then(() => { if (a.src === SILENT) a.pause(); }).catch(() => {});
}

async function playKey(key) {
  if (player.source?.key === key && player.hasTrack) { player.toggle(); return; }
  unlockAudio();
  let found = null;
  for (const l of lists.values()) if (l.ctx?.key === key && l.ids.length) found = l;
  try {
    if (found) { player.playList(found.ids, 0, found.ctx); return; }
    const r = await contextTracks(key);
    const ids = r.tracks.map((t) => t.id);
    if (!ids.length) return toast("Rien à lire ici pour l'instant");
    player.playList(ids, 0, { key, label: r.label, href: r.href, kind: r.kind });
  } catch (e) { toastError(e); }
}

function rowInfo(rowEl) {
  const listEl = rowEl.closest("[data-list]");
  const list = listEl && lists.get(listEl.dataset.list);
  if (!list) return null;
  const i = +rowEl.dataset.i;
  const t = trackCache.get(+rowEl.dataset.tid) || list.tracks.find((x) => x.id === +rowEl.dataset.tid);
  return { list, i, t, playlistId: listEl.dataset.playlist ? +listEl.dataset.playlist : null, entry: rowEl.dataset.entry ? +rowEl.dataset.entry : null };
}
function playRow(rowEl) {
  const r = rowInfo(rowEl);
  if (!r) return;
  if (player.currentId === r.t?.id && player.source?.key === r.list.ctx?.key) { player.toggle(); return; }
  player.playList(r.list.ids, r.i, r.list.ctx);
}
function rowMenu(rowEl, pos) {
  const r = rowInfo(rowEl);
  if (!r || !r.t) return;
  rowEl.classList.add("menu-open");
  openTrackMenu(r.t, pos, {
    playlistId: r.playlistId, entry: r.entry,
    onRemoved: () => router.refresh(),
    onClose: () => rowEl.classList.remove("menu-open"),
  });
}

// ======================================================================== clics (délégation)
document.addEventListener("click", async (e) => {
  const t = e.target;
  const play = t.closest("[data-play]");
  if (play) { e.preventDefault(); e.stopPropagation(); playKey(play.dataset.play); return; }
  const dl = t.closest("[data-offline]");
  if (dl) { e.preventDefault(); e.stopPropagation(); toggleOffline(dl.dataset.offline, dl); return; }
  const like = t.closest("[data-like]");
  if (like) {
    e.stopPropagation();
    try {
      const on = await toggleLike(+like.dataset.like);
      toast(on ? "Ajouté aux titres likés" : "Retiré des titres likés");
    } catch (err) { toastError(err); }
    return;
  }
  const more = t.closest("[data-more]");
  if (more) {
    e.stopPropagation();
    const row = more.closest(".row");
    if (row) rowMenu(row, { anchor: more });
    return;
  }
  const qi = t.closest("[data-qrm]");
  if (qi) { e.stopPropagation(); player.removeAt(+qi.dataset.qrm); return; }
  const qrow = t.closest("[data-qi]");
  if (qrow && !t.closest(".grip, a")) { player.jump(+qrow.dataset.qi); return; }
  const row = t.closest(".row[data-tid]");
  if (row && !t.closest("a, button, .grip, input")) {
    if (handleRowClick(row, e)) return;
    playRow(row);
    return;
  }
  const rowPlay = t.closest(".rowplay");
  if (rowPlay) { playRow(rowPlay.closest(".row")); return; }
  const act = t.closest("[data-act]");
  if (act) {
    switch (act.dataset.act) {
      case "new-playlist":
        if (act.closest(".lib-head")) { newMenu(act); break; } // le « + » de la bibliothèque : playlist ou intelligente
        try { const p = await createPlaylist(); location.hash = `#/playlist/${p.id}`; } catch (err) { toastError(err); }
        break;
      case "new-smart": openSmartEditor(); break;
      case "back": if (navStack.length > 1) history.back(); else location.hash = "#/"; break;
      case "forward": history.forward(); break;
      case "shuffle": player.toggleShuffle(); break;
      default: break;
    }
  }
  const libf = t.closest("[data-libf]");
  if (libf) { libFilter = libf.dataset.libf; storage.set("onde.libf", libFilter); renderSidebar(); }
});
function cardKeyOf(el) {
  const card = el.closest(".card, .qt");
  if (!card) return null;
  const fab = card.querySelector("[data-play]");
  return fab ? fab.dataset.play : null;
}
function openCardMenu(key, pos) {
  if (!key) return;
  cardMenuItems(key).then((items) => openMenu(items, pos)).catch(toastError);
}
document.addEventListener("contextmenu", (e) => {
  const row = e.target.closest(".row[data-tid]");
  if (row && !e.target.closest("input, textarea")) { e.preventDefault(); rowMenu(row, { x: e.clientX, y: e.clientY }); return; }
  const cardEl = e.target.closest(".card, .qt");
  if (cardEl) {
    const key = cardKeyOf(cardEl);
    if (key) { e.preventDefault(); openCardMenu(key, { x: e.clientX, y: e.clientY }); }
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.matches?.(".row[data-tid]")) { e.preventDefault(); playRow(e.target); }
  if (e.key === "Enter" && e.target.matches?.("[data-act='edit-cover']")) e.target.click();
});

// appui long sur mobile = menu contextuel
let pressT = 0;
document.addEventListener("touchstart", (e) => {
  const row = e.target.closest(".row[data-tid]");
  if (!row || e.target.closest(".grip")) return;
  clearTimeout(pressT);
  pressT = setTimeout(() => { navigator.vibrate?.(10); rowMenu(row, {}); row.dataset.pressed = "1"; }, 520);
}, { passive: true });
["touchend", "touchmove", "touchcancel"].forEach((ev) => document.addEventListener(ev, () => clearTimeout(pressT), { passive: true }));
document.addEventListener("click", (e) => {
  const row = e.target.closest(".row[data-pressed]");
  if (row) { e.stopImmediatePropagation(); e.preventDefault(); delete row.dataset.pressed; }
}, true);

// ======================================================================== barre de lecture
const seekEls = $$(".p-seek");
const volEl = $(".p-vol");
let seeking = false;

function renderTrack() {
  const t = player.current;
  playerEl.classList.toggle("is-empty", !t);
  $$(".p-title").forEach((el) => {
    el.textContent = t ? t.title : "";
    if (el.tagName === "A") el.href = t ? `#/album/${t.albumId}` : "#/";
  });
  $$(".p-artist").forEach((el) => { el.innerHTML = t ? artistLinks(t) : ""; });
  $$(".p-cover").forEach((el) => {
    const big = el.closest(".np-art");
    const blur = el.classList.contains("np-blur");
    el.innerHTML = t ? cover(t.cover, big ? 768 : blur ? 96 : 96, t.album, "note") : "";
  });
  const c = t?.color || "#3b3355";
  np.style.setProperty("--c", c);
  playerEl.style.setProperty("--pc", c);
  const src = player.source;
  const srcA = $(".p-source");
  if (srcA) {
    srcA.textContent = src?.label || t?.album || "";
    srcA.href = src?.href || (t ? `#/album/${t.albumId}` : "#/");
    srcA.previousElementSibling.textContent = src?.kind ? `Lecture depuis ${src.kind}` : "Lecture en cours";
  }
  renderLike();
  markPlaying();
  if (t && !player.paused) document.title = `${t.title} • ${t.artist}`;
  if (!np.hidden) renderQueueInto($("#np-queue"), true);
  renderNext();
}
// « À suivre : … » sous les commandes du lecteur plein écran (téléphone, tablette)
function renderNext() {
  const box = $(".np-next");
  if (!box) return;
  const nextId = player.queue[player.index + 1] ?? (player.repeat === "all" && player.queue.length > 1 ? player.queue[0] : null);
  const t = nextId != null ? trackCache.get(nextId) : null;
  box.hidden = !t;
  if (t) $(".np-next-t", box).textContent = `${t.title} · ${t.artist}`;
}
$(".np-next")?.addEventListener("click", (e) => {
  e.stopPropagation();
  if (phoneNP()) scrollNP("#np-queue"); else if (queuePanel.hidden) toggleQueue();
});
function renderLike() {
  const t = player.current;
  $$(".p-like").forEach((b) => {
    b.classList.toggle("on", !!t?.liked);
    b.innerHTML = icon(t?.liked ? "heart-fill" : "heart");
    b.setAttribute("aria-label", t?.liked ? "Retirer des titres likés" : "Liker");
  });
}
function markPlaying() {
  const id = player.currentId;
  $$(".row.playing").forEach((r) => { if (+r.dataset.tid !== id) r.classList.remove("playing"); });
  if (id != null) $$(`.row[data-tid="${id}"]`).forEach((r) => r.classList.add("playing"));
  $$(".eq").forEach((e) => e.classList.toggle("paused", player.paused));
}
function renderState() {
  const paused = player.paused;
  $$(".p-play").forEach((b) => {
    b.innerHTML = icon(paused ? "play" : "pause");
    b.setAttribute("aria-label", paused ? "Lecture" : "Pause");
  });
  const key = player.source?.key;
  $$("[data-play]").forEach((b) => {
    const on = !!key && b.dataset.play === key && !paused;
    b.classList.toggle("playing", on);
    const want = on ? "pause" : "play";
    if (b.dataset.icon !== want) { b.innerHTML = icon(want); b.dataset.icon = want; }
  });
  $$(".p-shuffle").forEach((b) => b.classList.toggle("on", player.shuffle));
  $$(".p-repeat").forEach((b) => {
    b.classList.toggle("on", player.repeat !== "off");
    b.innerHTML = icon(player.repeat === "one" ? "repeat-one" : "repeat");
    b.title = { off: "Répéter", all: "Répéter tout", one: "Répéter le titre" }[player.repeat];
  });
  const remote = player.mode === "pi";
  const wireless = !remote && (cast.airplayOn || cast.remoteOn);
  playerEl.classList.toggle("remote", remote);
  $$(".p-device").forEach((b) => b.classList.toggle("on", remote || wireless));
  $$(".p-device-name").forEach((s) => { s.textContent = remote ? "Raspberry Pi" : cast.airplayOn ? "AirPlay" : cast.remoteOn ? "Chromecast" : "Ce navigateur"; });
  $$(".eq").forEach((e) => e.classList.toggle("paused", paused));
  const t = player.current;
  document.title = t && !paused ? `${t.title} • ${t.artist}` : pageTitle;
  $$(".p-prev, .p-next").forEach((b) => { b.disabled = !player.hasTrack; });
}
function renderTime() {
  const pos = player.position;
  const dur = player.duration;
  const pct = dur ? Math.min(100, (pos / dur) * 100) : 0;
  if (!seeking) {
    seekEls.forEach((s) => { s.value = Math.round(pct * 10); s.style.setProperty("--p", `${pct}%`); });
    $$(".p-cur").forEach((el) => { el.textContent = fmtTime(pos); });
  }
  $$(".p-dur").forEach((el) => { el.textContent = fmtTime(dur); });
  const bar = $(".p-bar");
  if (bar) bar.style.width = `${pct}%`;
}
function renderVolume() {
  const v = player.muted ? 0 : player.volume;
  volEl.value = Math.round(v * 100);
  volEl.style.setProperty("--p", `${v * 100}%`);
  $(".p-mute").innerHTML = icon(v === 0 ? "mute" : v < 0.5 ? "volume-low" : "volume");
}

player.addEventListener("track", renderTrack);
player.addEventListener("state", () => { renderState(); renderTime(); });
player.addEventListener("time", renderTime);
player.addEventListener("volume", renderVolume);
player.addEventListener("queue", () => { if (!queuePanel.hidden) renderQueueInto(queuePanel); if (!np.hidden) renderQueueInto($("#np-queue"), true); renderNext(); });
player.addEventListener("problem", (e) => toast(e.detail, { error: true }));
setInterval(() => { if (!player.paused && !document.hidden) renderTime(); }, 250);

seekEls.forEach((s) => {
  s.addEventListener("pointerdown", () => { seeking = true; s.classList.add("drag"); });
  s.addEventListener("input", () => {
    seeking = true;
    const pct = s.value / 10;
    s.style.setProperty("--p", `${pct}%`);
    $$(".p-cur").forEach((el) => { el.textContent = fmtTime((pct / 100) * player.duration); });
  });
  s.addEventListener("change", () => {
    player.seek((s.value / 1000) * player.duration);
    seeking = false;
    s.classList.remove("drag");
  });
  s.addEventListener("pointerup", () => s.classList.remove("drag"));
});
volEl.addEventListener("input", () => player.setVolume(volEl.value / 100));
volEl.addEventListener("wheel", (e) => { e.preventDefault(); player.setVolume(player.volume + (e.deltaY < 0 ? 0.05 : -0.05)); }, { passive: false });
$(".p-mute").addEventListener("click", () => player.toggleMute());
$$(".p-play").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); unlocked = true; player.toggle(); }));
$$(".p-next").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); player.next(); }));
$$(".p-prev").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); player.prev(); }));
$$(".p-shuffle").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); player.toggleShuffle(); }));
$$(".p-repeat").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); player.cycleRepeat(); }));
$$(".p-like").forEach((b) => b.addEventListener("click", async (e) => {
  e.stopPropagation();
  if (!player.current) return;
  try { const on = await toggleLike(player.currentId); toast(on ? "Ajouté aux titres likés" : "Retiré des titres likés"); } catch (err) { toastError(err); }
}));
$$(".p-queue, .np-queue-btn").forEach((b) => b.addEventListener("click", (e) => {
  e.stopPropagation();
  if (b.classList.contains("np-queue-btn") && phoneNP()) { scrollNP("#np-queue"); return; }
  toggleQueue();
}));
$$(".p-device").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); deviceMenu(b); }));
$$(".p-open").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); openNP(); }));
playerEl.addEventListener("click", (e) => {
  if (isMobile() && !e.target.closest("button, a, input")) openNP();
});

// ======================================================================== appareils
function deviceMenu(anchor) {
  const st = player.pi.state;
  const piBusy = st && st.active && !st.paused && player.mode !== "pi";
  const onPi = player.mode === "pi";
  const a = player.audio;
  const local = player.mode === "local" && !cast.airplayOn && !cast.remoteOn;
  openMenu([
    { heading: "Appareil de lecture" },
    { icon: "device", label: "Ce navigateur", on: local, right: local ? "Actif" : "", onClick: () => player.setMode("local") },
    ...(cast.airplay ? [{
      icon: "airplay", label: "AirPlay…", on: cast.airplayOn, disabled: onPi,
      right: onPi ? "d'abord « Ce navigateur »" : cast.airplayOn ? "Actif" : "",
      onClick: () => { try { a.webkitShowPlaybackTargetPicker(); } catch (err) { toastError(err); } },
    }] : []),
    ...(cast.remote ? [{
      icon: "cast", label: "Chromecast…", on: cast.remoteOn, disabled: onPi,
      right: onPi ? "d'abord « Ce navigateur »" : cast.remoteOn ? "Actif" : "",
      onClick: () => a.remote.prompt().catch((err) => {
        if (err.name === "NotAllowedError" || err.name === "AbortError") return; // fenêtre fermée
        toast(a.currentSrc.startsWith("blob:") ? "Un titre téléchargé sur l'appareil ne peut pas être envoyé au Chromecast."
          : location.protocol === "https:" ? "Le Chromecast ne peut pas lire depuis Onde en HTTPS (certificat privé) : désactive le HTTPS pour l'utiliser."
          : "Aucun appareil Chromecast compatible trouvé sur le Wi-Fi.", { error: true });
      }),
    }] : []),
    {
      icon: "speaker", label: "Raspberry Pi (haut-parleurs)", on: onPi, disabled: !player.pi.available,
      right: !player.pi.available ? "mpv absent" : onPi ? "Actif" : piBusy ? "En lecture" : "",
      onClick: () => player.setMode("pi").then(() => toast("Lecture sur le Raspberry Pi")),
    },
  ], { anchor });
}
(function bindCast() {
  const a = player.audio;
  if (window.WebKitPlaybackTargetAvailabilityEvent) {
    a.addEventListener("webkitplaybacktargetavailabilitychanged", (e) => { cast.airplay = e.availability === "available"; });
    a.addEventListener("webkitcurrentplaybacktargetiswirelesschanged", () => { cast.airplayOn = !!a.webkitCurrentPlaybackTargetIsWireless; renderState(); });
  }
  if (a.remote && typeof a.remote.watchAvailability === "function") {
    a.remote.watchAvailability((available) => { cast.remote = available; }).catch(() => { cast.remote = false; });
    const upd = () => { cast.remoteOn = a.remote.state === "connected"; renderState(); };
    a.remote.addEventListener?.("connect", upd);
    a.remote.addEventListener?.("disconnect", upd);
  }
})();

// ======================================================================== paroles
let lyricsOpen = false;
function setLyricsOpen(on) {
  lyricsOpen = on;
  np.classList.toggle("show-lyrics", on);
  setLyricsVisible(on);
  $$(".p-lyrics").forEach((b) => b.classList.toggle("on", on));
}
// Téléphone : tout sur un seul écran qui défile — le lecteur, puis les paroles, puis « À suivre ».
// Les boutons paroles / file d'attente y amènent directement (rien à cocher).
const phoneNP = () => isMobile();
function scrollNP(sel) {
  const el = $(sel);
  if (el) requestAnimationFrame(() => el.scrollIntoView({ behavior: "smooth", block: "start" }));
}
function openLyrics() {
  openNP();
  if (phoneNP()) { scrollNP("#np-lyrics"); return; }
  setLyricsOpen(true);
}
$$(".p-lyrics").forEach((b) => b.addEventListener("click", (e) => {
  e.stopPropagation();
  if (np.hidden || phoneNP()) { openLyrics(); return; }
  setLyricsOpen(!lyricsOpen);
}));

// ======================================================================== minuterie de sommeil
function sleepLabel(left) {
  if (left == null) return "";
  if (left === "end") return "Fin du titre";
  const m = Math.ceil(left / 60);
  return `${m} min`;
}
function renderSleep() {
  const left = player.sleepLeft;
  $$(".p-sleep").forEach((b) => {
    b.classList.toggle("on", left != null);
    const badge = $(".sleep-badge", b);
    if (badge) badge.textContent = left != null ? sleepLabel(left) : "";
  });
}
function sleepMenu(anchor) {
  const left = player.sleepLeft;
  const opt = (minutes, label) => ({ label, onClick: () => player.setSleep(minutes).then(() => toast(minutes ? `Arrêt dans ${label.toLowerCase()}` : "Minuterie désactivée")) });
  openMenu([
    { heading: "Minuterie de sommeil" },
    ...(left != null ? [{ icon: "close", label: "Désactiver", onClick: () => player.setSleep(0).then(() => toast("Minuterie désactivée")) }, "hr"] : []),
    opt(10, "10 minutes"), opt(20, "20 minutes"), opt(30, "30 minutes"), opt(45, "45 minutes"), opt(60, "1 heure"),
    "hr",
    opt("end", "Fin du titre en cours"),
  ], { anchor });
}
$$(".p-sleep").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); sleepMenu(b); }));
player.addEventListener("sleep", renderSleep);
setInterval(() => { if (player.sleepLeft != null) renderSleep(); }, 1000);

// ======================================================================== file d'attente
function queueRow(id, qi, cur = false) {
  const t = trackCache.get(id);
  if (!t) return "";
  return `<div class="row${cur ? " playing" : ""}" data-qi="${qi}">
    <div class="c-title"><div class="rc">${cover(t.cover, 96, t.album)}</div><div class="tt"><div class="t1">${esc(t.title)}</div><div class="t2">${artistLinks(t)}</div></div></div>
    ${cur ? "<div></div><div></div>" : `<button class="icon-btn c-rm" data-qrm="${qi}" aria-label="Retirer">${icon("close", "sm")}</button><div class="grip">${icon("grip", "sm")}</div>`}
  </div>`;
}
function renderQueueInto(target, compact = false) {
  if (!target) return;
  const i = player.index;
  const upcoming = player.queue.slice(i + 1, i + 101);
  const manual = Math.max(0, player.manualEnd - i);
  target.innerHTML = `
    <div class="q-head"><h3>File d'attente</h3><span style="display:flex;gap:6px;align-items:center">
      <button class="btn sm${jamActive() ? " white" : ""}" data-jam title="Tes potes ajoutent des titres depuis leur téléphone">${icon("qr", "sm")} ${jamActive() ? "Soirée en cours" : "Mode soirée"}</button>
      ${compact ? "" : `<button class="icon-btn" data-qclose aria-label="Fermer">${icon("close")}</button>`}</span></div>
    ${player.hasTrack ? `<div class="q-sec">En cours de lecture</div><div class="q-list">${queueRow(player.currentId, i, true)}</div>` : `<p class="muted">Rien en lecture.</p>`}
    ${upcoming.length ? `<div class="q-sec"><span>${manual ? "À suivre dans la file" : `À suivre${player.source?.label ? ` · ${esc(player.source.label)}` : ""}`}</span><button data-qclear>Effacer</button></div>
      <div class="q-list" data-qlist>${upcoming.map((id, k) => queueRow(id, i + 1 + k)).join("")}</div>` : ""}
    ${player.queue.length - i - 1 > 100 ? `<p class="dim" style="font-size:12px;padding:8px">+ ${player.queue.length - i - 101} autres titres</p>` : ""}`;
  $("[data-qclose]", target)?.addEventListener("click", toggleQueue);
  $("[data-jam]", target)?.addEventListener("click", (e) => { e.stopPropagation(); openJam(); });
  $("[data-qclear]", target)?.addEventListener("click", () => player.clearUpcoming());
  const ql = $("[data-qlist]", target);
  if (ql) makeSortable(ql, { onEnd: (from, to) => player.move(i + 1 + from, i + 1 + to) });
}
function toggleQueue() {
  queuePanel.hidden = !queuePanel.hidden;
  $$(".p-queue, .np-queue-btn").forEach((b) => b.classList.toggle("on", !queuePanel.hidden));
  if (!queuePanel.hidden) renderQueueInto(queuePanel);
  storage.set("onde.queue", !queuePanel.hidden && !isMobile());
}

// ======================================================================== lecteur plein écran
function openNP() {
  if (!player.hasTrack) return;
  const wasHidden = np.hidden;
  np.hidden = false;
  np.classList.remove("closing");
  np.classList.toggle("all-in-one", phoneNP());
  if (phoneNP()) {
    setLyricsVisible(true); // les paroles suivent la chanson sous le lecteur
    if (wasHidden) $(".np-inner").scrollTop = 0;
  }
  renderTrack(); renderState(); renderTime();
}
function closeNP() {
  np.classList.add("closing");
  if (lyricsOpen) setLyricsOpen(false);
  else if (np.classList.contains("all-in-one")) setLyricsVisible(false);
  setTimeout(() => { np.hidden = true; np.classList.remove("closing"); }, 200);
}
$(".np-close").addEventListener("click", closeNP);
$(".np-more").addEventListener("click", (e) => { if (player.current) openTrackMenu(player.current, { anchor: e.currentTarget }); });
np.addEventListener("click", (e) => { if (e.target.closest("a[href]")) closeNP(); });
// glisser vers le bas pour fermer (mobile)
let swipeY = null;
np.addEventListener("touchstart", (e) => {
  // (glisser vers le bas ferme seulement quand on est tout en haut : sinon c'est un défilement)
  if (!e.target.closest("input, .np-side, .np-lyrics") && $(".np-inner").scrollTop <= 0) swipeY = e.touches[0].clientY;
}, { passive: true });
np.addEventListener("touchmove", (e) => {
  if (swipeY == null) return;
  const dy = e.touches[0].clientY - swipeY;
  if (dy > 0) $(".np-inner").style.transform = `translateY(${dy * 0.6}px)`;
}, { passive: true });
np.addEventListener("touchend", (e) => {
  if (swipeY == null) return;
  const dy = e.changedTouches[0].clientY - swipeY;
  $(".np-inner").style.transform = "";
  swipeY = null;
  if (dy > 110) closeNP();
});

// ======================================================================== barre latérale
let libFilter = storage.get("onde.libf", "playlists");
let sideSeq = 0;
async function renderSidebar() {
  const box = $("#lib-list");
  $$("[data-libf]").forEach((c) => c.classList.toggle("on", c.dataset.libf === libFilter));
  const token = ++sideSeq;
  try {
    let html = "";
    const chip = $('[data-libf="downloads"]');
    if (chip) chip.hidden = !offline.supported || (!offline.hasDownloads && !offline.active);
    if (offline.active || libFilter === "downloads") {
      const cols = [...offline.collections.values()].sort((a, b) => b.at - a.at);
      html = cols.map((c) => {
        const p = offline.progress(c.key);
        const m = c.meta || {};
        const art = c.key === "liked" ? likedCover() : c.key.startsWith("album:") ? cover(m.cover, 96, c.name, "disc") : playlistCover(m.mosaic || m.cover ? m : { mosaic: (m.tracks || []).map((x) => x.cover).filter(Boolean) }, 96);
        const sub = p.state === "busy" ? `Téléchargement ${p.done}/${p.total}` : `${c.kind} • ${p.total} titre${p.total > 1 ? "s" : ""}`;
        return `<a class="lib-item" href="${hrefOf(c.key)}"><div class="li-cover">${art}</div><div class="li-text"><div class="li-1 ell">${esc(c.name)}</div><div class="li-2 ell"><svg class="ic dlm on"><use href="#i-dl-done"/></svg>${esc(sub)}</div></div></a>`;
      }).join("");
      if (!cols.length) html = `<div class="lib-empty"><b>Rien sur cet appareil</b><p>Télécharge un album ou une playlist pour l'écouter sans le Pi.</p></div>`;
      if (token !== sideSeq) return;
      box.innerHTML = html;
      highlightSidebar();
      return;
    }
    if (libFilter === "albums") {
      const albums = await api.get("/api/albums?sort=played");
      html = albums.slice(0, 300).map((a) => `<a class="lib-item" href="#/album/${a.id}"><div class="li-cover">${cover(a.cover, 96, a.name, "disc")}</div><div class="li-text"><div class="li-1 ell">${esc(a.single ? "Singles" : a.name)}</div><div class="li-2 ell">Album • ${esc(a.artist)}</div></div></a>`).join("");
    } else if (libFilter === "artists") {
      const artists = await api.get("/api/artists?sort=plays");
      html = artists.slice(0, 300).map((a) => `<a class="lib-item round" href="#/artist/${enc(a.name)}"><div class="li-cover">${cover(a.cover, 96, a.name, "user")}</div><div class="li-text"><div class="li-1 ell">${esc(a.name)}</div><div class="li-2">Artiste</div></div></a>`).join("");
    } else {
      const [pls, inf, smartPls] = await Promise.all([api.get("/api/playlists"), api.get("/api/info"), api.get("/api/smart").catch(() => [])]);
      info = inf;
      html = `<a class="lib-item" href="#/liked"><div class="li-cover">${likedCover()}</div><div class="li-text"><div class="li-1 ell">Titres likés</div><div class="li-2 ell">Playlist • ${inf.stats.liked} titre${inf.stats.liked > 1 ? "s" : ""}</div></div></a>`
        + pls.map((p) => `<a class="lib-item" href="#/playlist/${p.id}"><div class="li-cover">${playlistCover(p, 96)}</div><div class="li-text"><div class="li-1 ell">${esc(p.name)}</div><div class="li-2 ell">Playlist • ${p.count} titre${p.count > 1 ? "s" : ""}</div></div></a>`).join("")
        + smartPls.map((p) => `<a class="lib-item" href="#/smart/${p.id}"><div class="li-cover">${smartCover(p, 96)}</div><div class="li-text"><div class="li-1 ell">${esc(p.name)}</div><div class="li-2 ell">Intelligente • ${p.count} titre${p.count > 1 ? "s" : ""}</div></div></a>`).join("");
      if (!pls.length && !smartPls.length) html += `<div class="lib-empty"><b>Crée ta première playlist</b><p>C'est simple, on va t'aider.</p><button class="pill-btn" data-act="new-playlist">Créer une playlist</button></div>`;
    }
    if (token !== sideSeq) return;
    box.innerHTML = html || `<div class="lib-empty"><b>Rien pour l'instant</b><p>Importe de la musique pour commencer.</p><a class="pill-btn" href="#/import">Importer</a></div>`;
    highlightSidebar();
  } catch { /* hors ligne */ }
}
function highlightSidebar() {
  const h = location.hash || "#/";
  $$(".lib-item").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === h));
}
addEventListener("hashchange", highlightSidebar);

// ======================================================================== événements globaux
bus.addEventListener("liked", (e) => {
  const { id, liked: on } = e.detail;
  $$(`[data-like="${id}"]`).forEach((b) => {
    b.classList.toggle("on", on);
    b.innerHTML = icon(on ? "heart-fill" : "heart");
    b.setAttribute("aria-label", on ? "Retirer des titres likés" : "Liker");
  });
  if (player.currentId === id) renderLike();
  if (libFilter === "playlists") renderSidebar();
  if (currentRoute === "liked" && !on) router.refresh();
});
bus.addEventListener("playlists", () => renderSidebar());
bus.addEventListener("library", async () => {
  renderSidebar();
  if (player.hasTrack) {
    // rafraîchit le titre en cours et la suite de la file (pochettes, infos modifiées)
    const ids = player.queue.slice(Math.max(0, player.index - 1), player.index + 120);
    try {
      await api.get(`/api/tracks/by-ids?ids=${ids.join(",")}`);
      renderTrack(); player._updateMediaSession();
      if (!queuePanel.hidden) renderQueueInto(queuePanel);
    } catch { /* hors ligne */ }
  }
  if (["home", "library", "tracks"].includes(currentRoute)) router.refresh();
});

function newMenu(anchor) {
  openMenu([
    { heading: "Créer" },
    { icon: "plus", label: "Nouvelle playlist", onClick: async () => { try { const p = await createPlaylist(); location.hash = `#/playlist/${p.id}`; } catch (err) { toastError(err); } } },
    { icon: "sparkles", label: "Playlist intelligente…", right: "se remplit toute seule", onClick: () => openSmartEditor() },
  ], { anchor });
}

// ======================================================================== écoute hors connexion
async function toggleOffline(key, btn) {
  if (offline.isSaved(key)) {
    const c = offline.collections.get(key);
    const ok = await confirmDialog({ title: "Retirer de cet appareil ?", text: `« ${c.name} » ne sera plus disponible hors connexion. Rien n'est supprimé du Raspberry Pi.`, ok: "Retirer" });
    if (!ok) return;
    try { await offline.remove(key); toast("Retiré de cet appareil"); } catch (err) { toastError(err); }
    return;
  }
  if (offline.active) { toast("Reconnecte-toi au Raspberry Pi pour télécharger", { error: true }); return; }
  btn?.classList.add("busy");
  try {
    await offline.add(key);
    const c = offline.collections.get(key);
    toast(`Téléchargement de « ${c?.name || "la sélection"} » sur cet appareil…`);
  } catch (err) { btn?.classList.remove("busy"); toastError(err); }
}
let offlineRefreshT = 0;
offline.addEventListener("change", () => {
  refreshOffline(document);
  const chip = $('[data-libf="downloads"]');
  const wantChip = offline.supported && (offline.hasDownloads || offline.active);
  if (chip) chip.hidden = !wantChip;
  if (!wantChip && libFilter === "downloads") { libFilter = "playlists"; storage.set("onde.libf", libFilter); renderSidebar(); return; }
  // la liste latérale ne dépend des téléchargements que quand elle les affiche
  if (libFilter === "downloads" || offline.active) { clearTimeout(offlineRefreshT); offlineRefreshT = setTimeout(renderSidebar, 300); }
});
offline.addEventListener("problem", (e) => toast(e.detail, { error: true }));
let appStarted = false; // avant la fin du démarrage, start() s'occupe lui-même de l'affichage
offline.addEventListener("status", async () => {
  document.body.classList.toggle("offline", offline.active);
  $("#offline-bar").hidden = !offline.active;
  if (!appStarted) return;
  if (offline.active) {
    toast("Hors connexion : ta musique téléchargée reste disponible");
  } else {
    toast("De nouveau connecté au Raspberry Pi");
    try { info = await api.get("/api/info"); setMe(info); player.connectPi(info.pi); } catch { /* ignore */ }
  }
  renderSidebar();
  // La page affichée reste à l'écran tant qu'elle est bonne : passer hors connexion ne la remplace
  // plus par une page vide (seulement si elle n'a pas pu se charger) ; au retour du réseau, on
  // recharge celles affichées hors connexion ou en erreur.
  if (offline.active ? lastRender.failed : (lastRender.offline || lastRender.failed)) router.refresh();
});

// ======================================================================== glisser-déposer global
let dragDepth = 0;
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
addEventListener("dragenter", (e) => {
  if (!hasFiles(e) || document.querySelector(".modal")) return;
  dragDepth++;
  $("#drop").hidden = false;
});
addEventListener("dragleave", (e) => {
  if (!hasFiles(e)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $("#drop").hidden = true;
});
addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
addEventListener("drop", async (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth = 0;
  $("#drop").hidden = true;
  if (document.querySelector(".modal")) return;
  enqueue(await filesFromDrop(e.dataTransfer));
});

// ======================================================================== raccourcis clavier
document.addEventListener("keydown", (e) => {
  if (e.defaultPrevented || e.altKey || document.querySelector(".modal, .menu")) return;
  if (e.target.closest?.("input, textarea, select, [contenteditable]")) {
    if (e.key === "Escape") e.target.blur();
    return;
  }
  const mod = e.ctrlKey || e.metaKey;
  const k = e.key;
  if (k === " " || k === "k") { if (e.target.closest?.("button, a") && k === " ") return; e.preventDefault(); player.toggle(); }
  else if (k === "ArrowRight") { e.preventDefault(); mod ? player.next() : player.seek(player.position + 10); }
  else if (k === "ArrowLeft") { e.preventDefault(); mod ? player.prev() : player.seek(player.position - 10); }
  else if (k === "ArrowUp" && mod) { e.preventDefault(); player.setVolume(player.volume + 0.05); }
  else if (k === "ArrowDown" && mod) { e.preventDefault(); player.setVolume(player.volume - 0.05); }
  else if ((k === "a" || k === "A") && mod) { e.preventDefault(); selectAll(); }
  else if (mod) return;
  else if (k === "m") player.toggleMute();
  else if (k === "s") player.toggleShuffle();
  else if (k === "r") player.cycleRepeat();
  else if (k === "l" && player.current) toggleLike(player.currentId).catch(toastError);
  else if (k === "q") toggleQueue();
  else if (k === "f") (np.hidden ? openNP() : closeNP());
  else if ((k === "p" || k === "P") && player.current) { if (np.hidden) openLyrics(); else setLyricsOpen(!lyricsOpen); }
  else if (k === "/") { e.preventDefault(); if (currentRoute === "search") $("#q")?.focus(); else location.hash = "#/search"; }
  else if (k === "Escape" && !np.hidden) closeNP();
  else if (k === "Escape" && selecting()) clearSelection();
});

// ======================================================================== démarrage
(async function start() {
  await offline.init().catch(() => { offline.supported = false; });
  const reachable = offline.ready ? await offline.probe(true) : true;
  if (reachable) {
    try { info = await api.get("/api/info"); setMe(info); } catch { info = null; }
    if (info?.me?.role === "admin") startAskBadge();
    ensureTerms(info);
    if (info) showWhatsNew(info);
  }
  if (!info && !offline.hasDownloads) {
    view.innerHTML = `<div class="page">${emptyState("speaker", "Serveur injoignable", "Vérifie que le Raspberry Pi est allumé et que tu es sur le même Wi-Fi. Onde se reconnecte tout seul dès qu'il répond.", `<button class="btn white" onclick="location.reload()">Réessayer</button>`)}</div>`;
    offline.addEventListener("status", () => { if (!offline.active) location.reload(); });
    return;
  }
  if (!info) offline.setActive(true);
  document.body.classList.toggle("offline", offline.active);
  $("#offline-bar").hidden = !offline.active;
  renderVolume();
  await player.init();
  if (info) player.connectPi(info.pi);
  renderTrack(); renderState(); renderTime(); renderSleep();
  if (storage.get("onde.queue", false) && !isMobile()) toggleQueue();
  renderSidebar();
  router.go();
  appStarted = true;
  bindResume(player);
  bindJam(player);
  onJam(() => { if (!queuePanel.hidden) renderQueueInto(queuePanel); if (!np.hidden) renderQueueInto($("#np-queue"), true); });
  autoOffline.start();
  if (!offline.active) setTimeout(() => offerResume().catch(() => {}), 1200);
  if (!offline.active) { offline.syncAll(); offline.flushPlays(); offline.resume(); }
  // l'appli elle-même est gardée sur l'appareil (s'ouvre sans le Pi) : seulement en contexte sécurisé
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/sw.js").catch(() => { /* certificat non approuvé, navigation privée… */ });
  }
  if (!offline.active) setTimeout(() => warmPages().catch(() => {}), 9000);
})();

// Pages principales gardées sur l'appareil (accueil, bibliothèque, playlists…) : chargées une à une,
// en tâche de fond, quand le réseau est libre. Sur un réseau faible (VPN, 4G), changer de page les
// affiche alors tout de suite, sans faire attendre — ni couper — la musique.
async function warmPages() {
  if (navigator.connection?.saveData) return;
  const urls = ["/api/home", "/api/playlists", "/api/smart", "/api/liked", "/api/albums", "/api/artists", "/api/genres", "/api/info"];
  try {
    const pls = await api.get("/api/playlists");
    for (const p of (pls || []).slice(0, 25)) urls.push(`/api/playlists/${p.id}`);
  } catch { return; }
  for (const u of urls) {
    if (offline.active || document.hidden) return;
    // pas pendant qu'un titre se charge : la musique passe d'abord
    for (let i = 0; i < 20 && !player.paused && player.audio.readyState < 3; i++) await new Promise((r) => setTimeout(r, 1000));
    try { await api.get(u); } catch { return; }
    await new Promise((r) => setTimeout(r, 400));
  }
}
