// Pages de l'application.
import { api, emit } from "./api.js";
import {
  $, $$, esc, enc, icon, fmtTime, fmtLong, plural, cover, coverImg, likedCover, greeting, hue, debounce, isTouch, storage,
} from "./util.js";
import {
  trackList, albumCard, artistCard, playlistCard, likedCard, newPlaylistCard, shelf, grid, hero, actionBar, emptyState,
  playlistCover, lists, flushLists, mixCard, mountGrid,
} from "./components.js";
import { editAlbum, coverDialog, editPlaylist, deletePlaylist, download, playlistSubmenu, addToPlaylist, createPlaylist } from "./dialogs.js";
import { openMenu, makeSortable, toast, toastError, openModal } from "./ui.js";
import { renderImport } from "./upload.js";
import { askTrack } from "./asks.js";
import { publicBase, copy } from "./profiles.js";
import { canImport, isAdmin } from "./profiles.js";
import { offline } from "./offline.js";
import { smartCard, newSmartCard } from "./smart.js";

let page = null; // { setColor, setTopTitle, player, info }
export function bindViews(ctx) { page = ctx; }

const DEFAULT_C = "#3a2f66";

// ---------------------------------------------------------------- accueil
export async function home(el) {
  const d = await api.get("/api/home");
  const s = d.stats;
  document.title = "Onde";
  if (!s.tracks) {
    page.setColor(DEFAULT_C);
    el.innerHTML = `<div class="page">${emptyState("logo", "Bienvenue sur Onde",
      "Ta bibliothèque est vide. Importe tes MP3 depuis ce téléphone ou cet ordinateur : glisse-les dans la fenêtre ou choisis des fichiers.",
      `<a class="btn primary" href="#/import">${icon("upload")} Importer de la musique</a>
       <p class="dim" style="margin-top:22px;font-size:13px">Tu peux aussi copier tes dossiers dans <code>${esc(page.info?.musicDir || "~/Musique")}</code> sur le Raspberry Pi.</p>`)}</div>`;
    return;
  }
  const quick = [];
  const seen = new Set();
  if (s.liked) quick.push({ href: "#/liked", name: "Titres likés", cover: likedCover(), key: "liked", color: "#4b2fd8" });
  for (const p of d.playlists.slice(0, 3)) quick.push({ href: `#/playlist/${p.id}`, name: p.name, cover: playlistCover(p, 96), key: `playlist:${p.id}`, color: p.color });
  for (const a of [...d.recentlyPlayed, ...d.recentAlbums]) {
    if (quick.length >= 8) break;
    if (seen.has(a.id)) continue;
    seen.add(a.id);
    quick.push({ href: `#/album/${a.id}`, name: a.single ? `Singles · ${a.artist}` : a.name, cover: cover(a.cover, 96, a.name, "disc"), key: `album:${a.id}`, color: a.color });
  }
  const baseColor = quick.find((q) => q.color)?.color || DEFAULT_C;
  page.setColor(baseColor);
  const qt = (q) => `<div class="qt" data-color="${esc(q.color || "")}"><div class="qt-cover">${q.cover}</div><a class="card-link qt-name ell" href="${q.href}">${esc(q.name)}</a><button class="fab" data-play="${esc(q.key)}" aria-label="Lire">${icon("play")}</button></div>`;
  el.innerHTML = `<div class="page">
    <div class="greet-row"><h1 class="greet">${greeting()}</h1><a class="chip" href="#/stats">${icon("chart", "sm")} Tes stats</a></div>
    <div class="quick">${quick.map(qt).join("")}</div>
    ${shelf("Tes mixes", (d.mixes || []).map(mixCard))}
    ${shelf("Écoutés récemment", d.recentlyPlayed.map((a) => albumCard(a)))}
    ${d.topTracks.length ? `<section class="section"><div class="sec-head"><h2>Tes titres du moment</h2></div>${trackList(d.topTracks.slice(0, 5), { ctx: { key: "top", label: "Tes titres du moment", href: "#/", kind: "tes favoris" }, head: false, added: false })}</section>` : ""}
    ${shelf("Ajoutés récemment", d.recentAlbums.map((a) => albumCard(a)), "#/library/albums")}
    ${shelf("Tes artistes", d.artists.map(artistCard), "#/library/artists")}
    ${shelf("Tes playlists", d.playlists.map(playlistCard), "#/library/playlists")}
    ${shelf("À redécouvrir", d.randomAlbums.map((a) => albumCard(a)))}
  </div>`;
  if (!isTouch()) {
    $$(".qt", el).forEach((q) => {
      q.addEventListener("pointerenter", () => q.dataset.color && page.setColor(q.dataset.color));
      q.addEventListener("pointerleave", () => page.setColor(baseColor));
    });
  }
}

// ---------------------------------------------------------------- recherche
let searchSeq = 0;
export async function search(el, [raw = ""]) {
  const q0 = decodeURIComponent(raw);
  document.title = "Rechercher · Onde";
  page.setColor("#26232f");
  el.innerHTML = `<div class="page">
    <div class="search-top"><label class="search-box">${icon("search")}<input id="q" type="search" placeholder="Que veux-tu écouter ?" value="${esc(q0)}" data-autofocus autocomplete="off" enterkeyhint="search" spellcheck="false"></label></div>
    <div id="sr"></div></div>`;
  const input = $("#q", el);
  const out = $("#sr", el);
  const run = async (text) => {
    const token = ++searchSeq;
    const url = text.trim() ? `#/search/${enc(text)}` : "#/search";
    if (location.hash !== url) history.replaceState(null, "", url);
    if (!text.trim()) {
      const genres = await api.get("/api/genres");
      if (token !== searchSeq) return;
      out.innerHTML = browseHtml(genres);
      return;
    }
    const r = await api.get(`/api/search?q=${enc(text)}`);
    if (token !== searchSeq) return;
    out.innerHTML = (r.shortened ? `<p class="suggest">Recherche raccourcie : « <b>${esc(r.shortened)}</b> »</p>` : "") + (r.suggestion ? `<p class="suggest">Résultats pour « <b>${esc(r.suggestion)}</b> » — aucun résultat exact pour « ${esc(text)} ».</p>` : "") + resultsHtml(r, r.suggestion || text)
      + `<div class="ask-cta listener-only"><div><b>Pas le bon titre ?</b><p class="muted">Demande « ${esc(text.trim())} » à l'admin : il l'ajoute à la bibliothèque (ou la mini IA décide au bout de 5 min).</p></div>
        <button class="btn white" data-ask-q="${esc(text.trim())}">${icon("upload")} Demander ce titre</button></div>`;
    flushLists(out);
  };
  out.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-ask-q]");
    if (!b) return;
    b.disabled = true;
    try { await askTrack(b.dataset.askQ); b.innerHTML = `${icon("check")} Demandé`; } catch (err) { toastError(err); b.disabled = false; }
  });
  input.addEventListener("input", debounce(() => run(input.value).catch(toastError), 150));
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); });
  await run(q0);
}

function browseHtml(genres) {
  const tiles = [
    { name: "Titres likés", href: "#/liked", h: 255 },
    { name: "Tous les titres", href: "#/tracks", h: 200 },
    { name: "Albums", href: "#/library/albums", h: 330 },
    { name: "Artistes", href: "#/library/artists", h: 25 },
    ...genres.map((g) => ({ name: g.name, href: `#/genre/${enc(g.name)}`, h: hue(g.name), count: g.count, cover: g.cover })),
  ];
  return `<div class="sec-head"><h2>Parcourir tout</h2></div>
    <div class="genres">${tiles.map((t) => `<a class="genre" href="${t.href}" style="--h:${t.h}">${esc(t.name)}${t.count ? `<small>${plural(t.count, "titre", "titres")}</small>` : ""}${t.cover ? `<div class="g-cover">${coverImg(t.cover, 192)}</div>` : ""}</a>`).join("")}</div>`;
}

function resultsHtml(r, q) {
  const fold = (x) => x.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const nq = fold(q.trim());
  const startsWord = (name) => fold(name).split(/[\s\-_.,()']+/).some((w) => w.startsWith(nq)) || fold(name).startsWith(nq);
  if (!r.tracks.length && !r.albums.length && !r.artists.length && !r.playlists.length) {
    return emptyState("search", `Aucun résultat pour « ${q} »`, "Vérifie l'orthographe ou essaie d'autres mots-clés.");
  }
  let top = null;
  const artist = r.artists.find((a) => startsWord(a.name)) || null;
  const album = r.albums.find((a) => startsWord(a.name)) || null;
  if (artist) top = { kind: "Artiste", name: artist.name, href: `#/artist/${enc(artist.name)}`, cover: cover(artist.cover, 192, artist.name, "user"), key: `artist:${artist.name}`, round: true };
  else if (album) top = { kind: "Album", name: album.name, sub: album.artist, href: `#/album/${album.id}`, cover: cover(album.cover, 192, album.name, "disc"), key: `album:${album.id}` };
  else if (r.tracks[0]) { const t = r.tracks[0]; top = { kind: "Titre", name: t.title, sub: t.artist, href: `#/album/${t.albumId}`, cover: cover(t.cover, 192, t.album), key: `album:${t.albumId}` }; }
  else if (r.artists[0]) { const a = r.artists[0]; top = { kind: "Artiste", name: a.name, href: `#/artist/${enc(a.name)}`, cover: cover(a.cover, 192, a.name, "user"), key: `artist:${a.name}`, round: true }; }
  else if (r.albums[0]) { const a = r.albums[0]; top = { kind: "Album", name: a.name, sub: a.artist, href: `#/album/${a.id}`, cover: cover(a.cover, 192, a.name, "disc"), key: `album:${a.id}` }; }
  const ctx = { key: `search:${q}`, label: `Recherche « ${q} »`, href: `#/search/${enc(q)}`, kind: "la recherche" };
  const topHtml = top ? `<div class="top-result${top.round ? " round" : ""}"><div class="tr-cover">${top.cover}</div>
      <a class="card-link" href="${top.href}"><div class="tr-name ell">${esc(top.name)}</div></a>
      <div class="muted">${top.sub ? `${esc(top.sub)} ` : ""}<span class="tr-kind">${top.kind}</span></div>
      <button class="fab" data-play="${esc(top.key)}" aria-label="Lire">${icon("play")}</button></div>` : "";
  return `
    ${r.tracks.length || top ? `<div class="two-col section">
      <div><div class="sec-head"><h2>Meilleur résultat</h2></div>${topHtml}</div>
      <div><div class="sec-head"><h2>Titres</h2></div>${trackList(r.tracks.slice(0, 4), { ctx, head: false, album: false, allIds: r.tracks.map((t) => t.id) })}</div>
    </div>` : ""}
    ${shelf("Artistes", r.artists.map(artistCard))}
    ${shelf("Albums", r.albums.map((a) => albumCard(a)))}
    ${shelf("Playlists", r.playlists.map(playlistCard))}
    ${r.tracks.length > 4 ? `<section class="section"><div class="sec-head"><h2>Tous les titres</h2></div>${trackList(r.tracks, { ctx })}</section>` : ""}`;
}

// ---------------------------------------------------------------- bibliothèque
const LIB_SORTS = {
  playlists: [["updated", "Modifiées récemment"], ["name", "Alphabétique"]],
  albums: [["added", "Ajoutés récemment"], ["name", "Alphabétique"], ["artist", "Artiste"], ["year", "Année de sortie"], ["played", "Écoutés récemment"], ["plays", "Les plus écoutés"]],
  artists: [["name", "Alphabétique"], ["plays", "Les plus écoutés"], ["played", "Écoutés récemment"]],
};
export async function library(el, [tab = "playlists"]) {
  if (!LIB_SORTS[tab]) tab = "playlists";
  document.title = "Bibliothèque · Onde";
  page.setColor("#2a2540");
  const sortKey = `onde.sort.${tab}`;
  let sort = storage.get(sortKey, LIB_SORTS[tab][0][0]);
  const tabs = [["playlists", "Playlists"], ["albums", "Albums"], ["artists", "Artistes"]];
  el.innerHTML = `<div class="page">
    <h1 class="page-title">Bibliothèque</h1>
    <div class="toolbar">
      ${tabs.map(([k, l]) => `<a class="chip${k === tab ? " on" : ""}" href="#/library/${k}">${l}</a>`).join("")}
      <a class="chip" href="#/tracks">Titres</a>
      ${offline.supported && offline.hasDownloads ? `<a class="chip" href="#/downloads">${icon("dl-done", "sm")} Téléchargés</a>` : ""}
      <span class="spacer"></span>
      ${tab === "playlists" ? `<button class="chip" data-m3u-import title="Importer une playlist .m3u / .m3u8">${icon("upload", "sm")} Importer .m3u</button>` : ""}
      <label class="filter">${icon("search")}<input id="lib-filter" placeholder="Filtrer" autocomplete="off"></label>
      <select class="select" id="lib-sort">${LIB_SORTS[tab].map(([k, l]) => `<option value="${k}"${k === sort ? " selected" : ""}>${l}</option>`).join("")}</select>
    </div>
    <div id="lib-body"><div class="spinner"></div></div></div>`;
  const body = $("#lib-body", el);
  let items = [];
  let extra = { liked: 0, smart: [] };
  const load = async () => {
    if (tab === "albums") items = await api.get(`/api/albums?sort=${sort}`);
    else if (tab === "artists") items = await api.get(`/api/artists?sort=${sort}`);
    else {
      const [pls, smartPls, inf] = await Promise.all([api.get("/api/playlists"), api.get("/api/smart"), api.get("/api/info")]);
      items = pls;
      extra.smart = smartPls;
      if (sort === "name") { items.sort((a, b) => a.name.localeCompare(b.name, "fr")); extra.smart.sort((a, b) => a.name.localeCompare(b.name, "fr")); }
      extra.liked = inf.stats.liked;
    }
    draw();
  };
  const draw = () => {
    const f = $("#lib-filter", el).value.trim().toLowerCase();
    const match = (s) => !f || s.toLowerCase().includes(f);
    let cards;
    if (tab === "albums") cards = items.filter((a) => match(`${a.name} ${a.artist}`)).map((a) => albumCard(a, a.single ? `Singles • ${esc(a.artist)}` : null));
    else if (tab === "artists") cards = items.filter((a) => match(a.name)).map(artistCard);
    else cards = [...(f ? [] : [likedCard(extra.liked), newPlaylistCard(), newSmartCard()]), ...items.filter((p) => match(p.name)).map(playlistCard), ...extra.smart.filter((p) => match(p.name)).map(smartCard)];
    if (cards.length) mountGrid(body, cards);
    else body.innerHTML = emptyState("", "Rien ici pour l'instant", tab === "playlists" ? "Crée ta première playlist." : "Importe de la musique pour remplir ta bibliothèque.");
  };
  $("#lib-sort", el).addEventListener("change", (e) => { sort = e.target.value; storage.set(sortKey, sort); load().catch(toastError); });
  $("#lib-filter", el).addEventListener("input", debounce(draw, 80));
  $("[data-m3u-import]", el)?.addEventListener("click", () => importM3U());
  await load();
}

export function importM3U() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".m3u,.m3u8,audio/x-mpegurl,audio/mpegurl";
  input.addEventListener("change", async () => {
    const f = input.files[0];
    if (!f) return;
    try {
      const name = f.name.replace(/\.m3u8?$/i, "");
      const r = await api.post(`/api/playlists/import?name=${enc(name)}`, new Blob([await f.arrayBuffer()], { type: "text/plain" }));
      toast(`« ${r.playlist.name} » : ${r.matched} titre${r.matched > 1 ? "s" : ""} sur ${r.total} retrouvé${r.matched > 1 ? "s" : ""}`,
        { action: { label: "Ouvrir", href: `#/playlist/${r.playlist.id}` }, timeout: 6000 });
      emit("playlists");
      page.refresh();
    } catch (e) { toastError(e); }
  });
  input.click();
}

// ---------------------------------------------------------------- album
export async function album(el, [id]) {
  const a = await api.get(`/api/albums/${id}`);
  const key = `album:${a.id}`;
  const ctx = { key, label: a.single ? `Singles · ${a.artist}` : a.name, href: `#/album/${a.id}`, kind: "l'album" };
  document.title = `${a.name} · ${a.artist} · Onde`;
  page.setColor(a.color || DEFAULT_C);
  page.setTopTitle(a.name, key);
  const kind = a.single ? "Singles" : a.count <= 3 ? "Single" : a.count <= 6 && a.dur < 1800 ? "EP" : "Album";
  const query = a.single ? a.artist : `${a.artist} ${a.name}`;
  el.innerHTML = `
    ${hero({
      kind, title: a.single ? `Singles` : a.name, editable: !a.single,
      coverHtml: cover(a.cover, 384, a.name, "disc", a.name),
      meta: `<a href="#/artist/${enc(a.artist)}">${esc(a.artist)}</a>${a.year ? `<span class="sep"></span><span>${a.year}</span>` : ""}<span class="sep"></span><span>${plural(a.count, "titre", "titres")}, <span class="muted">${fmtLong(a.dur)}</span></span>`,
    })}
    <div class="body">
      ${actionBar(key, `
        ${offline.supported ? "" : `<button class="icon-btn sm" data-act="dl" aria-label="Télécharger l'album" title="Télécharger l'album (.zip)">${icon("download-circle")}</button>`}
        <button class="icon-btn sm" data-act="more" aria-label="Plus d'options">${icon("more")}</button>`)}
      ${trackList(a.tracks, { ctx, album: false, cover: a.single, numbered: !a.single, discs: true })}
      ${a.genres.length ? `<p class="dim" style="margin:20px 14px 0;font-size:13px">${a.genres.map(esc).join(" · ")}</p>` : ""}
      <div style="height:24px"></div>
      ${shelf(`Plus de ${a.artist}`, (a.more || []).filter((x) => !offline.active || offline.isSaved(`album:${x.id}`)).map((x) => albumCard(x, `${x.year || ""}${x.year ? " • " : ""}${x.single ? "Singles" : "Album"}`)), offline.active ? null : `#/artist/${enc(a.artist)}`)}
    </div>`;
  const openCover = () => coverDialog({ kind: "album", id: a.id, query, title: "Pochette de l'album" });
  el.addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "edit-cover") openCover();
    if (act === "dl") download(`/api/albums/${a.id}/download`);
    if (act === "more") {
      const ids = a.tracks.map((t) => t.id);
      openMenu([
        { icon: "queue", label: "Ajouter à la file d'attente", onClick: () => { page.player.addToQueue(ids); toast("Ajouté à la file d'attente"); } },
        { icon: "radio", label: "Radio de l'album", onClick: () => page.playKey(`radio:album:${a.id}`) },
        { icon: "plus", label: "Ajouter à une playlist", sub: () => playlistSubmenu(ids) },
        "hr",
        ...(a.single || !isAdmin() ? [] : [{ icon: "edit", label: "Modifier l'album", onClick: () => editAlbum(a) }, { icon: "image", label: "Changer la pochette", onClick: openCover }]),
        { icon: "download", label: "Exporter en .zip", onClick: () => download(`/api/albums/${a.id}/download`) },
      ], { anchor: e.target.closest("[data-act]") });
    }
  });
}

// ---------------------------------------------------------------- artiste
export async function artist(el, [raw]) {
  const name = decodeURIComponent(raw);
  const d = await api.get(`/api/artists/${enc(name)}`);
  const key = `artist:${d.name}`;
  const ctx = { key, label: d.name, href: `#/artist/${enc(d.name)}`, kind: "l'artiste" };
  document.title = `${d.name} · Onde`;
  page.setColor(d.color || DEFAULT_C);
  page.setTopTitle(d.name, key);
  let expanded = false;
  const allIds = d.tracks.map((t) => t.id);
  el.innerHTML = `
    ${hero({
      kind: "Artiste", title: d.name, round: true, editable: true,
      coverHtml: cover(d.cover, 384, d.name, "user", d.name),
      meta: `<span>${plural(d.count, "titre", "titres")}</span>${d.plays ? `<span class="sep"></span><span>${plural(d.plays, "écoute", "écoutes")}</span>` : ""}`,
    })}
    <div class="body">
      ${actionBar(key, `
        <button class="icon-btn sm" data-act="radio" aria-label="Radio de l'artiste" title="Radio de l'artiste">${icon("radio")}</button>
        ${canImport() ? `<button class="btn sm${d.following ? " on" : ""}" data-act="follow" title="Ses nouveaux albums et singles sont téléchargés tout seuls">${d.following ? `${icon("check", "sm")} Suivi` : `${icon("plus", "sm")} Suivre`}</button>` : ""}
        <button class="icon-btn sm" data-act="more" aria-label="Plus d'options">${icon("more")}</button>`)}
      <section class="section"><div class="sec-head"><h2>Populaires</h2></div><div id="pop"></div>
        ${d.tracks.length > 5 ? `<button class="btn ghost sm" id="more-pop" style="margin:8px 0 0 2px">Voir plus</button>` : ""}</section>
      ${shelf("Discographie", d.albums.map((a) => albumCard(a, `${a.year || ""}${a.year ? " • " : ""}${a.single ? "Singles" : a.count <= 3 ? "Single" : "Album"}`)))}
      ${shelf("Apparaît sur", d.appears.map((a) => albumCard(a)))}
    </div>`;
  const pop = $("#pop", el);
  const drawPop = () => {
    pop.innerHTML = trackList(d.tracks.slice(0, expanded ? 30 : 5), { ctx, head: false, numbered: false, allIds, album: true });
  };
  drawPop();
  $("#more-pop", el)?.addEventListener("click", (e) => {
    expanded = !expanded;
    e.target.textContent = expanded ? "Voir moins" : "Voir plus";
    drawPop();
    flushLists(pop);
  });
  const openPhoto = () => coverDialog({ kind: "artist", id: d.name, query: d.name, title: `Photo de ${d.name}` });
  el.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    const act = btn?.dataset.act;
    if (act === "radio") page.playKey(`radio:artist:${d.name}`);
    if (act === "follow") {
      try {
        d.following = !d.following;
        await (d.following ? api.put(`/api/follows/${enc(d.name)}`) : api.del(`/api/follows/${enc(d.name)}`));
        btn.classList.toggle("on", d.following);
        btn.innerHTML = d.following ? `${icon("check", "sm")} Suivi` : `${icon("plus", "sm")} Suivre`;
        toast(d.following ? `Tu suis ${d.name} : ses nouveautés arriveront toutes seules` : `Tu ne suis plus ${d.name}`);
      } catch (err) { d.following = !d.following; toastError(err); }
    }
    if (act === "edit-cover") {
      if (d.photo) openMenu([
        { icon: "image", label: "Choisir une autre photo", onClick: openPhoto },
        { icon: "disc", label: "Utiliser la pochette d'un album", onClick: async () => { await api.del(`/api/artists/${enc(d.name)}/image`); page.refresh(); } },
      ], { x: e.clientX, y: e.clientY });
      else openPhoto();
    }
    if (act === "more") {
      openMenu([
        { icon: "queue", label: "Ajouter tous les titres à la file", onClick: () => { page.player.addToQueue(allIds); toast("Ajouté à la file d'attente"); } },
        { icon: "radio", label: "Radio de l'artiste", onClick: () => page.playKey(`radio:artist:${d.name}`) },
        { icon: "plus", label: "Créer une playlist avec ses titres", onClick: () => createPlaylist(allIds, `Le meilleur de ${d.name}`) },
        "hr",
        { icon: "image", label: d.photo ? "Changer la photo" : "Ajouter une photo", onClick: openPhoto },
      ], { anchor: btn });
    }
  });
}

// ---------------------------------------------------------------- playlist
export async function playlist(el, [id]) {
  const p = await api.get(`/api/playlists/${id}`);
  const key = `playlist:${p.id}`;
  const ctx = { key, label: p.name, href: `#/playlist/${p.id}`, kind: "la playlist" };
  document.title = `${p.name} · Onde`;
  page.setColor(p.color || "#4a3b8a");
  page.setTopTitle(p.name, key);
  el.innerHTML = `
    ${hero({
      kind: "Playlist", title: p.name, desc: p.description, editable: true, titleAct: "edit-pl",
      coverHtml: playlistCover(p, 384),
      meta: `<span>${plural(p.count, "titre", "titres")}</span>${p.dur ? `<span class="sep"></span><span class="muted">${fmtLong(p.dur)}</span>` : ""}`,
    })}
    <div class="body">
      ${actionBar(key, `
        ${p.count && !offline.supported ? `<button class="icon-btn sm" data-act="dl" aria-label="Télécharger" title="Télécharger la playlist (.zip)">${icon("download-circle")}</button>` : ""}
        <button class="icon-btn sm" data-act="more" aria-label="Plus d'options">${icon("more")}</button>`)}
      <div id="pl-tracks">${trackList(p.tracks, { ctx, added: true, sortable: true, playlistId: p.id })}</div>
      <section class="section" style="margin-top:32px">
        <div class="sec-head"><h2>${p.count ? "Ajouter des titres" : "Trouvons quelque chose pour ta playlist"}</h2></div>
        <label class="search-box" style="margin-bottom:12px">${icon("search")}<input id="pl-add" placeholder="Chercher des titres ou des albums" autocomplete="off"></label>
        <div id="pl-add-res"></div>
      </section>
    </div>`;
  const tl = $("#pl-tracks .tl", el);
  if (tl) {
    makeSortable(tl, {
      onEnd: async () => {
        const rows = $$(".row", tl);
        const entries = rows.map((r) => +r.dataset.entry);
        const list = lists.get(tl.dataset.list);
        if (list) list.ids = rows.map((r) => +r.dataset.tid);
        rows.forEach((r, i) => { r.dataset.i = i; const n = $(".c-num .n", r); if (n) n.textContent = i + 1; });
        try { await api.put(`/api/playlists/${p.id}/order`, { entries }); } catch (e) { toastError(e); }
      },
    });
  }
  const openCover = () => coverDialog({ kind: "playlist", id: p.id, query: p.name, title: "Image de la playlist" });
  el.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    const act = btn?.dataset.act;
    if (act === "edit-pl") editPlaylist(p);
    if (act === "edit-cover") {
      if (p.cover) openMenu([
        { icon: "image", label: "Choisir une autre image", onClick: openCover },
        { icon: "grid", label: "Revenir à la mosaïque", onClick: async () => { await api.del(`/api/playlists/${p.id}/cover`); page.refresh(); } },
      ], { x: e.clientX, y: e.clientY });
      else openCover();
    }
    if (act === "dl") download(`/api/playlists/${p.id}/download`);
    if (act === "more") {
      openMenu([
        { icon: "queue", label: "Ajouter à la file d'attente", disabled: !p.count, onClick: () => { page.player.addToQueue(p.tracks.map((t) => t.id)); toast("Ajouté à la file d'attente"); } },
        { icon: "edit", label: "Modifier les infos", onClick: () => editPlaylist(p) },
        { icon: "image", label: "Changer l'image", onClick: openCover },
        { icon: "download", label: "Exporter en .zip", disabled: !p.count, onClick: () => download(`/api/playlists/${p.id}/download`) },
        { icon: "list", label: "Exporter (.m3u8)", disabled: !p.count, onClick: () => download(`/api/playlists/${p.id}/export`) },
        { icon: "radio", label: "Radio de la playlist", disabled: !p.count, onClick: () => page.playKey(`radio:tracks:${p.tracks.slice(0, 20).map((t) => t.id).join(",")}`) },
        { icon: "link", label: "Partager (lien d'écoute)", onClick: () => shareDialog(p) },
        "hr",
        { icon: "trash", label: "Supprimer la playlist", danger: true, onClick: () => deletePlaylist(p) },
      ], { anchor: btn });
    }
    const add = e.target.closest("[data-add]");
    if (add) {
      e.stopPropagation();
      const ids = add.dataset.add.split(",").map(Number);
      await addToPlaylist(p.id, ids, p.name);
      page.refresh();
    }
  });
  // ajout rapide
  const res = $("#pl-add-res", el);
  let seqAdd = 0;
  $("#pl-add", el).addEventListener("input", debounce(async (e) => {
    const q = e.target.value.trim();
    const tok = ++seqAdd;
    if (!q) { res.innerHTML = ""; return; }
    const r = await api.get(`/api/search?q=${enc(q)}`);
    if (tok !== seqAdd) return;
    const present = new Set(p.tracks.map((t) => t.id));
    const rows = [
      ...r.albums.slice(0, 3).map((a) => `<div class="row" style="--cols:minmax(0,1fr) auto;grid-template-columns:minmax(0,1fr) auto">
        <div class="c-title"><div class="rc">${cover(a.cover, 96, a.name, "disc")}</div><div class="tt"><div class="t1">${esc(a.name)}</div><div class="t2">Album · ${esc(a.artist)}</div></div></div>
        <button class="btn sm" data-add-album="${a.id}">Ajouter l'album</button></div>`),
      ...r.tracks.slice(0, 12).map((t) => `<div class="row" style="grid-template-columns:minmax(0,1fr) auto">
        <div class="c-title"><div class="rc">${cover(t.cover, 96, t.album)}</div><div class="tt"><div class="t1">${esc(t.title)}</div><div class="t2">${esc(t.artist)} · ${esc(t.album)}</div></div></div>
        ${present.has(t.id) ? `<span class="dim" style="font-size:13px;padding:0 14px">Déjà ajouté</span>` : `<button class="btn sm" data-add="${t.id}">Ajouter</button>`}</div>`),
    ];
    res.innerHTML = rows.length ? rows.join("") : `<p class="muted">Aucun résultat.</p>`;
  }, 180));
  res.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-add-album]");
    if (!b) return;
    e.stopPropagation();
    const a = await api.get(`/api/albums/${b.dataset.addAlbum}`);
    await addToPlaylist(p.id, a.tracks.map((t) => t.id), p.name);
    page.refresh();
  });
}

// ---------------------------------------------------------------- titres likés
export async function liked(el) {
  const tracks = await api.get("/api/liked");
  const ctx = { key: "liked", label: "Titres likés", href: "#/liked", kind: "la playlist" };
  document.title = "Titres likés · Onde";
  page.setColor("#4b2fd8");
  page.setTopTitle("Titres likés", "liked");
  const dur = tracks.reduce((s, t) => s + (t.dur || 0), 0);
  el.innerHTML = `
    ${hero({ kind: "Playlist", title: "Titres likés", coverHtml: likedCover(), meta: `<span>${plural(tracks.length, "titre", "titres")}</span>${dur ? `<span class="sep"></span><span class="muted">${fmtLong(dur)}</span>` : ""}` })}
    <div class="body">
      ${tracks.length ? actionBar("liked") : ""}
      ${trackList(tracks, { ctx, added: false, empty: emptyState("heart", "Les titres que tu aimes s'affichent ici", "Clique sur le cœur à côté d'un titre pour l'ajouter.", `<a class="btn white" href="#/search">Trouver des titres</a>`) })}
    </div>`;
}

// ---------------------------------------------------------------- tous les titres
const TRACK_SORTS = [["added", "Ajoutés récemment"], ["title", "Titre"], ["artist", "Artiste"], ["album", "Album"], ["plays", "Les plus écoutés"], ["duration", "Durée"]];
export async function tracks(el) {
  document.title = "Tous les titres · Onde";
  page.setColor("#23313f");
  let sort = storage.get("onde.sort.tracks", "added");
  el.innerHTML = `<div class="page">
    <h1 class="page-title">Tous les titres</h1>
    <div class="toolbar">
      <button class="play-btn accent xl" data-play="tracks" aria-label="Tout lire">${icon("play")}</button>
      <button class="icon-btn p-shuffle" data-act="shuffle" style="width:40px;height:40px" aria-label="Aléatoire">${icon("shuffle")}</button>
      <span class="spacer"></span>
      <label class="filter">${icon("search")}<input id="tr-filter" placeholder="Filtrer" autocomplete="off"></label>
      <select class="select" id="tr-sort">${TRACK_SORTS.map(([k, l]) => `<option value="${k}"${k === sort ? " selected" : ""}>${l}</option>`).join("")}</select>
    </div>
    <div id="tr-body"><div class="spinner"></div></div></div>`;
  let all = [];
  const ctx = { key: "tracks", label: "Tous les titres", href: "#/tracks", kind: "la bibliothèque" };
  const draw = () => {
    const f = $("#tr-filter", el).value.trim().toLowerCase();
    const shown = f ? all.filter((t) => `${t.title} ${t.artist} ${t.album}`.toLowerCase().includes(f)) : all;
    $("#tr-body", el).innerHTML = trackList(shown, { ctx, added: true, empty: emptyState("note", "Aucun titre", "Importe de la musique pour commencer.", `<a class="btn primary" href="#/import">Importer</a>`) });
    $("#tr-body", el).insertAdjacentHTML("afterbegin", `<p class="muted" style="margin:0 0 12px 2px;font-size:13px">${plural(shown.length, "titre", "titres")} · ${fmtLong(shown.reduce((s, t) => s + (t.dur || 0), 0))}</p>`);
    flushLists($("#tr-body", el));
  };
  const load = async () => { all = await api.get(`/api/tracks?sort=${sort}`); draw(); };
  $("#tr-sort", el).addEventListener("change", (e) => { sort = e.target.value; storage.set("onde.sort.tracks", sort); load().catch(toastError); });
  $("#tr-filter", el).addEventListener("input", debounce(draw, 120));
  await load();
}

// ---------------------------------------------------------------- genre
export async function genre(el, [raw]) {
  const name = decodeURIComponent(raw);
  const g = await api.get(`/api/genres/${enc(name)}`);
  const key = `genre:${g.name}`;
  document.title = `${g.name} · Onde`;
  page.setColor(`hsl(${hue(g.name)} 50% 38%)`);
  page.setTopTitle(g.name, key);
  const dur = g.tracks.reduce((s, t) => s + (t.dur || 0), 0);
  el.innerHTML = `
    ${hero({ kind: "Genre", title: g.name, coverHtml: `<div class="ph" style="--h:${hue(g.name)}">${icon("note")}</div>`, meta: `<span>${plural(g.tracks.length, "titre", "titres")}</span><span class="sep"></span><span class="muted">${fmtLong(dur)}</span>` })}
    <div class="body">${actionBar(key)}${trackList(g.tracks, { ctx: { key, label: g.name, href: `#/genre/${enc(g.name)}`, kind: "le genre" } })}</div>`;
}

// ---------------------------------------------------------------- import
export async function importView(el) {
  document.title = "Importer · Onde";
  page.setColor("#2d2552");
  return renderImport(el, page.info);
}


// ---------------------------------------------------------------- partage d'une playlist (lecture seule)
async function shareDialog(p) {
  let cur = (await api.get(`/api/playlists/${p.id}/share`)).share;
  const { base, publicOk } = await publicBase();
  const draw = (m) => {
    const url = cur ? `${base}${cur.path}` : "";
    $("[data-body]", m).innerHTML = cur
      ? `<div style="display:grid;gap:14px;justify-items:center;text-align:center">
          <img class="qr" src="/api/qr.svg?text=${enc(url)}" alt="QR code" width="170" height="170" style="background:#fff;border-radius:12px;padding:8px">
          <p class="muted" style="margin:0">Avec ce lien, on peut <b>écouter</b> « ${esc(p.name)} » dans un navigateur, sans compte. Rien d'autre : pas de recherche, pas de téléchargement, pas de modification. ${cur.views ? `Ouvert ${plural(cur.views, "fois", "fois")}.` : ""}</p>
          <input class="field" readonly value="${esc(url)}" style="width:100%" onclick="this.select()">
          ${publicOk ? "" : `<p class="dim" style="margin:0;font-size:12px">⚠ Le lien ne marche que sur ton réseau (ou avec Tailscale), sauf si l'accès public est activé.</p>`}</div>`
      : `<p class="muted">Crée un lien pour faire écouter cette playlist à quelqu'un, en lecture seule. Tu pourras le couper quand tu veux.</p>`;
    $("[data-foot]", m).innerHTML = cur
      ? `<button class="btn ghost" data-stop>Couper le lien</button>${navigator.share ? `<button class="btn" data-sh>Partager</button>` : ""}<button class="btn white" data-cp>Copier le lien</button>`
      : `<button class="btn ghost" data-close>Annuler</button><button class="btn white" data-mk>Créer le lien</button>`;
  };
  openModal({
    title: "Partager la playlist",
    body: `<div data-body></div><div class="modal-foot" data-foot style="margin-top:16px;display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap"></div>`,
    onMount: (m, close) => {
      draw(m);
      m.addEventListener("click", async (e) => {
        const url = cur ? `${base}${cur.path}` : "";
        try {
          if (e.target.closest("[data-foot] [data-close]")) close();
          else if (e.target.closest("[data-mk]")) { cur = (await api.post(`/api/playlists/${p.id}/share`)).share; draw(m); }
          else if (e.target.closest("[data-cp]")) copy(url);
          else if (e.target.closest("[data-sh]")) navigator.share({ title: p.name, text: `Écoute « ${p.name} » :`, url }).catch(() => {});
          else if (e.target.closest("[data-stop]")) { await api.del(`/api/playlists/${p.id}/share`); cur = null; toast("Lien coupé"); close(); }
        } catch (err) { toastError(err); }
      });
    },
  });
}
