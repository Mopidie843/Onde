// Fenêtres : modifier les infos, pochettes, playlists, menu d'un titre.
import { api, trackCache, emit, toggleLike, contextTracks } from "./api.js";
import { $, $$, esc, enc, icon, coverImg, cover, storage } from "./util.js";
import { openModal, openMenu, toast, toastError, confirmDialog, promptDialog } from "./ui.js";
import { offline } from "./offline.js";
import { isAdmin } from "./profiles.js";

let player = null;
let router = null;
let hooks = {}; // { playKey, openLyrics, startSelection }
export function bindDialogs(p, r, h = {}) { player = p; router = r; hooks = h; }

// ---------------------------------------------------------------- playlists
export async function createPlaylist(trackIds = [], name = "") {
  const p = await api.post("/api/playlists", { name, trackIds });
  emit("playlists");
  toast(trackIds.length ? `Ajouté à « ${p.name} »` : `Playlist « ${p.name} » créée`,
    { action: { label: "Ouvrir", href: `#/playlist/${p.id}` } });
  return p;
}

export async function addToPlaylist(pid, trackIds, name, position = null) {
  const r = await api.post(`/api/playlists/${pid}/tracks`, position == null ? { trackIds } : { trackIds, position, skipDuplicates: false });
  if (position != null) { emit("playlists"); return r; }
  emit("playlists");
  if (!r.added) toast(`Déjà dans « ${name} »`);
  else toast(`${r.added > 1 ? `${r.added} titres ajoutés` : "Ajouté"} à « ${name} »`);
}

export async function playlistSubmenu(trackIds, excludeId = null) {
  const pls = await api.get("/api/playlists");
  return [
    { icon: "plus", label: "Nouvelle playlist", onClick: () => createPlaylist(trackIds) },
    ...(pls.length ? ["hr"] : []),
    ...pls.filter((p) => p.id !== excludeId).map((p) => ({ icon: "note", label: p.name, right: `${p.count}`, onClick: () => addToPlaylist(p.id, trackIds, p.name) })),
  ];
}

export function editPlaylist(p) {
  openModal({
    title: "Modifier les infos",
    body: `<form class="form" id="pl-form">
      <label>Nom<input class="field" name="name" value="${esc(p.name)}" maxlength="120" autofocus required></label>
      <label>Description<textarea class="field" name="description" maxlength="500" placeholder="Ajoute une description (facultatif)">${esc(p.description || "")}</textarea></label>
    </form>`,
    foot: `<button class="btn ghost" data-close>Annuler</button><button class="btn white" data-save>Enregistrer</button>`,
    onMount: (m, close) => {
      const save = async (e) => {
        e?.preventDefault();
        const f = new FormData($("#pl-form", m));
        try {
          await api.patch(`/api/playlists/${p.id}`, { name: f.get("name"), description: f.get("description") });
          close(); emit("playlists"); router.refresh();
        } catch (err) { toastError(err); }
      };
      $("[data-save]", m).addEventListener("click", save);
      $("#pl-form", m).addEventListener("submit", save);
    },
  });
}

export async function deletePlaylist(p) {
  if (!(await confirmDialog({ title: "Supprimer la playlist ?", text: `« ${p.name} » sera supprimée. Les fichiers audio ne sont pas touchés.` }))) return;
  await api.del(`/api/playlists/${p.id}`);
  offline.remove(`playlist:${p.id}`).catch(() => {}); // libère aussi l'appareil si elle était téléchargée
  emit("playlists");
  toast("Playlist supprimée");
  if (location.hash.startsWith(`#/playlist/${p.id}`)) location.hash = "#/library/playlists";
}

// ---------------------------------------------------------------- infos d'un titre / album
export function editTrack(t) {
  const f = (name, label, value, attrs = "") => `<label>${label}<input class="field" name="${name}" value="${esc(value ?? "")}" ${attrs}></label>`;
  openModal({
    title: "Modifier les infos",
    body: `<form class="form" id="tr-form">
      <div style="display:flex;gap:16px;align-items:center;margin-bottom:4px">
        <div style="width:72px;height:72px;border-radius:6px;overflow:hidden;flex:none">${cover(t.cover, 192, t.album)}</div>
        <div class="muted" style="font-size:13px">Les modifications sont écrites directement dans le fichier (${esc((t.ext || "").toUpperCase())}).</div>
      </div>
      ${f("title", "Titre", t.title, "autofocus required")}
      <div class="form-row">${f("artist", "Artiste", t.artist)}${f("album_artist", "Artiste de l'album", t.albumArtist)}</div>
      ${f("album", "Album", t.album === "Singles" ? "" : t.album, 'placeholder="(aucun — rangé dans Singles)"')}
      <div class="form-row">${f("track_no", "N° de piste", t.no, 'inputmode="numeric"')}${f("disc_no", "N° de disque", t.disc, 'inputmode="numeric"')}</div>
      <div class="form-row">${f("year", "Année", t.year, 'inputmode="numeric" maxlength="4"')}${f("genre", "Genre", t.genre)}</div>
    </form>
    <details class="meta-online" id="meta-online">
      <summary>${icon("sparkles", "sm")} Compléter automatiquement avec les infos en ligne</summary>
      <form class="search-box" id="meta-q" style="max-width:none;margin-top:12px">${icon("search")}<input name="q" value="${esc(`${t.artist === "Artiste inconnu" ? "" : t.artist} ${t.title}`.trim())}" autocomplete="off"><button class="btn sm white" type="submit">Chercher</button></form>
      <div class="meta-res" id="meta-res"></div>
    </details>`,
    wide: true,
    foot: `<label class="check left" id="meta-cover-wrap" hidden><input type="checkbox" id="meta-cover" checked> Utiliser aussi la pochette</label>
      <button class="btn ghost" data-close>Annuler</button><button class="btn white" data-save>Enregistrer</button>`,
    onMount: (m, close) => {
      let chosenCover = null;
      const form = $("#tr-form", m);
      const res = $("#meta-res", m);
      const search = async (q) => {
        res.innerHTML = `<div class="spinner"></div>`;
        try {
          const r = await api.get(`/api/meta/search?q=${enc(q)}`);
          if (!r.results.length) {
            res.innerHTML = `<p class="muted">${r.errors.length ? "Recherche impossible : le Raspberry Pi a-t-il accès à Internet ?" : "Aucun résultat."}</p>`;
            return;
          }
          res.innerHTML = r.results.map((c, i) => `<button type="button" class="meta-opt" data-i="${i}">
            <img src="${esc(c.thumb)}" alt="" loading="lazy" referrerpolicy="no-referrer">
            <span class="mo-t"><b class="ell">${esc(c.title)}</b><span class="ell">${esc(c.artist)} · ${esc(c.album)}${c.year ? ` · ${c.year}` : ""}</span><small>${esc(c.source)}${c.genre ? ` · ${esc(c.genre)}` : ""}</small></span></button>`).join("");
          $$(".meta-opt", res).forEach((b) => b.addEventListener("click", () => {
            const c = r.results[+b.dataset.i];
            const set = (n, v) => { if (v != null && v !== "") form.elements[n].value = v; };
            set("title", c.title); set("artist", c.artist); set("album", c.album); set("album_artist", c.albumArtist);
            set("year", c.year); set("genre", c.genre); set("track_no", c.trackNo); set("disc_no", c.discNo);
            chosenCover = c.cover || null;
            $("#meta-cover-wrap", m).hidden = !chosenCover;
            $$(".meta-opt", res).forEach((x) => x.classList.toggle("sel", x === b));
            toast("Infos remplies — vérifie puis enregistre");
          }));
        } catch (e) { res.innerHTML = `<p class="muted">${esc(e.message)}</p>`; }
      };
      $("#meta-q", m).addEventListener("submit", (e) => { e.preventDefault(); search(e.target.q.value); });
      $("#meta-online", m).addEventListener("toggle", (e) => { if (e.target.open && !res.innerHTML) search($("#meta-q", m).q.value); });
      const save = async (e) => {
        e?.preventDefault();
        const data = Object.fromEntries(new FormData(form).entries());
        const btn = $("[data-save]", m);
        btn.disabled = true;
        try {
          const updated = await api.patch(`/api/tracks/${t.id}`, data);
          if (chosenCover && $("#meta-cover", m).checked) {
            const single = updated.album === "Singles";
            await api.post(`/api/${single ? "tracks" : "albums"}/${single ? t.id : updated.albumId}/cover?embed=${storage.get("onde.embed", true) ? 1 : 0}`, { url: chosenCover });
          }
          close(); toast("Infos enregistrées"); emit("library"); router.refresh();
        } catch (err) { toastError(err); btn.disabled = false; }
      };
      $("[data-save]", m).addEventListener("click", save);
      $("#tr-form", m).addEventListener("submit", save);
    },
  });
}

export function editAlbum(a) {
  openModal({
    title: "Modifier l'album",
    body: `<form class="form" id="al-form">
      <label>Nom de l'album<input class="field" name="name" value="${esc(a.name)}" autofocus required></label>
      <label>Artiste de l'album<input class="field" name="artist" value="${esc(a.artist)}"></label>
      <div class="form-row"><label>Année<input class="field" name="year" value="${esc(a.year ?? "")}" inputmode="numeric" maxlength="4"></label>
      <label>Genre (tous les titres)<input class="field" name="genre" value="${esc((a.genres || [])[0] || "")}" placeholder="inchangé"></label></div>
      <p class="muted" style="margin:0;font-size:13px">Appliqué aux ${a.count} titre${a.count > 1 ? "s" : ""} de l'album (tags écrits dans les fichiers).</p>
    </form>`,
    foot: `<button class="btn ghost" data-close>Annuler</button><button class="btn white" data-save>Enregistrer</button>`,
    onMount: (m, close) => {
      const save = async (e) => {
        e?.preventDefault();
        const data = Object.fromEntries(new FormData($("#al-form", m)).entries());
        if (!data.genre) delete data.genre;
        const btn = $("[data-save]", m);
        btn.disabled = true;
        btn.textContent = "Enregistrement…";
        try {
          const r = await api.patch(`/api/albums/${a.id}`, data);
          close(); toast(r.errors ? `Enregistré (${r.errors} fichier(s) non modifiable(s))` : "Album mis à jour");
          emit("library");
          if (r.id !== a.id) location.hash = `#/album/${r.id}`; else router.refresh();
        } catch (err) { toastError(err); btn.disabled = false; btn.textContent = "Enregistrer"; }
      };
      $("[data-save]", m).addEventListener("click", save);
      $("#al-form", m).addEventListener("submit", save);
    },
  });
}

// ---------------------------------------------------------------- pochettes
/**
 * target : { kind: "album" | "track" | "playlist", id, query, title }
 */
export function coverDialog(target) {
  const endpoint = target.kind === "artist" ? `/api/artists/${enc(target.id)}/image`
    : `/api/${target.kind === "playlist" ? "playlists" : target.kind === "track" ? "tracks" : "albums"}/${target.id}/cover`;
  const embedPref = storage.get("onde.embed", true);
  let choice = null; // { url } ou { blob }
  let tab = "search";
  let cleanup = () => {};
  openModal({
    title: target.title || "Changer la pochette",
    wide: true,
    body: `
      <div class="tabs">
        <button class="chip on" data-tab="search">${"Rechercher en ligne"}</button>
        <button class="chip" data-tab="file">Depuis l'appareil</button>
        <button class="chip" data-tab="url">Lien d'image</button>
      </div>
      <div data-pane="search">
        <form class="search-box" id="cv-q" style="max-width:none">${icon("search")}<input name="q" value="${esc(target.query || "")}" placeholder="${target.kind === "artist" ? "Nom de l'artiste" : "Artiste, album…"}" autocomplete="off"><button class="btn sm white" type="submit">Chercher</button></form>
        <div class="cover-grid" id="cv-res"></div>
      </div>
      <div data-pane="file" hidden>
        <label class="preview-drop" id="cv-drop">${icon("image")}<b>Choisis ou dépose une image</b><span>JPG, PNG, WebP — tu peux aussi coller (Ctrl+V)</span>
          <input type="file" accept="image/*" hidden id="cv-file"></label>
      </div>
      <div data-pane="url" hidden>
        <input class="field" id="cv-url" placeholder="https://…/pochette.jpg" style="width:100%">
        <div class="preview-drop" id="cv-url-prev" style="margin-top:12px;cursor:default">${icon("link")}<span>L'aperçu s'affiche ici</span></div>
      </div>`,
    foot: `${!["playlist", "artist"].includes(target.kind) ? `<label class="check left"><input type="checkbox" id="cv-embed" ${embedPref ? "checked" : ""}> Écrire la pochette dans les fichiers audio</label>` : ""}
      <button class="btn ghost" data-close>Annuler</button><button class="btn primary" data-apply disabled>Appliquer</button>`,
    onMount: (m, close) => {
      const apply = $("[data-apply]", m);
      const setChoice = (c) => { choice = c; apply.disabled = !c; };
      $$("[data-tab]", m).forEach((b) => b.addEventListener("click", () => {
        tab = b.dataset.tab;
        $$("[data-tab]", m).forEach((x) => x.classList.toggle("on", x === b));
        $$("[data-pane]", m).forEach((p) => { p.hidden = p.dataset.pane !== tab; });
        setChoice(null);
        $$(".cover-opt.sel", m).forEach((x) => x.classList.remove("sel"));
      }));

      // --- recherche en ligne
      const res = $("#cv-res", m);
      const search = async (q) => {
        if (!q.trim()) return;
        res.innerHTML = `<div class="spinner" style="grid-column:1/-1"></div>`;
        try {
          const kind = target.kind === "track" ? "song" : target.kind === "artist" ? "artist" : "album";
          const r = await api.get(`/api/covers/search?kind=${kind}&q=${enc(q)}`);
          if (!r.results.length) {
            res.innerHTML = `<p class="muted" style="grid-column:1/-1">${r.errors.length ? "Recherche en ligne impossible : le Raspberry Pi a-t-il accès à Internet ?" : "Aucune pochette trouvée. Essaie d'autres mots-clés."}</p>`;
            return;
          }
          res.innerHTML = r.results.map((c, i) => `<button class="cover-opt" data-i="${i}" type="button">
            <img src="${esc(c.thumb)}" alt="" loading="lazy" referrerpolicy="no-referrer">
            <div class="co-1 ell">${esc(c.album || c.title)}</div><div class="co-2 ell">${esc(c.artist)}${c.year ? ` · ${c.year}` : ""} · ${esc(c.source)}</div></button>`).join("");
          $$(".cover-opt", res).forEach((b) => {
            b.addEventListener("click", () => {
              $$(".cover-opt", res).forEach((x) => x.classList.toggle("sel", x === b));
              setChoice({ url: r.results[+b.dataset.i].url });
            });
            b.addEventListener("dblclick", () => apply.click());
          });
        } catch (e) { res.innerHTML = `<p class="muted" style="grid-column:1/-1">${esc(e.message)}</p>`; }
      };
      $("#cv-q", m).addEventListener("submit", (e) => { e.preventDefault(); search(e.target.q.value); });
      if (target.query) search(target.query);

      // --- fichier local / coller
      const drop = $("#cv-drop", m), file = $("#cv-file", m);
      const showFile = (blob) => {
        if (!blob || !blob.type.startsWith("image/")) return toast("Ce fichier n'est pas une image", { error: true });
        const url = URL.createObjectURL(blob);
        drop.innerHTML = `<img src="${url}" alt=""><span>${esc(blob.name || "Image collée")}</span>`;
        drop.appendChild(file);
        setChoice({ blob });
      };
      file.addEventListener("change", () => showFile(file.files[0]));
      drop.addEventListener("dragover", (e) => { e.preventDefault(); e.stopPropagation(); drop.classList.add("over"); });
      drop.addEventListener("dragleave", () => drop.classList.remove("over"));
      drop.addEventListener("drop", (e) => { e.preventDefault(); e.stopPropagation(); drop.classList.remove("over"); showFile(e.dataTransfer.files[0]); });
      const onPaste = (e) => {
        const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
        if (!item) return;
        $$("[data-tab]", m).find((b) => b.dataset.tab === "file").click();
        showFile(item.getAsFile());
      };
      document.addEventListener("paste", onPaste);

      // --- URL
      const urlIn = $("#cv-url", m), prev = $("#cv-url-prev", m);
      urlIn.addEventListener("input", () => {
        const u = urlIn.value.trim();
        if (/^https?:\/\/.+/i.test(u)) {
          prev.innerHTML = `<img src="${esc(u)}" alt="" referrerpolicy="no-referrer" onerror="this.replaceWith(Object.assign(document.createElement('span'),{textContent:'Aperçu indisponible (le serveur essaiera quand même)'}))">`;
          setChoice({ url: u });
        } else setChoice(null);
      });

      apply.addEventListener("click", async () => {
        if (!choice) return;
        const embedEl = $("#cv-embed", m);
        const embed = embedEl ? embedEl.checked : false;
        if (embedEl) storage.set("onde.embed", embed);
        apply.disabled = true;
        apply.textContent = "Application…";
        try {
          const q = `?embed=${embed ? 1 : 0}`;
          if (choice.blob) await api.post(endpoint + q, choice.blob);
          else await api.post(endpoint + q, { url: choice.url });
          close();
          toast(embed ? "Pochette mise à jour (et écrite dans les fichiers)" : "Pochette mise à jour");
          emit("library"); emit("playlists"); router.refresh();
        } catch (e) { toastError(e); apply.disabled = false; apply.textContent = "Appliquer"; }
      });
      cleanup = () => document.removeEventListener("paste", onPaste);
    },
    onClose: () => cleanup(),
  });
}

// ---------------------------------------------------------------- menu d'un titre
export function trackMenuItems(t, { playlistId = null, entry = null, onRemoved = null, row = null } = {}) {
  const albumQuery = `${t.albumArtist || t.artist} ${t.album === "Singles" ? t.title : t.album}`;
  return [
    { icon: "queue", label: "Ajouter à la file d'attente", onClick: () => { player.addToQueue([t.id]); toast("Ajouté à la file d'attente"); } },
    { icon: "right", label: "Lire ensuite", onClick: () => { player.playNext([t.id]); toast("Sera lu ensuite"); } },
    { icon: "radio", label: "Lancer la radio du titre", onClick: () => hooks.playKey?.(`radio:track:${t.id}`) },
    { icon: "plus", label: "Ajouter à une playlist", sub: () => playlistSubmenu([t.id], playlistId) },
    ...(playlistId && entry ? [{
      icon: "trash", label: "Retirer de cette playlist", onClick: async () => {
        const r = await api.del(`/api/playlists/${playlistId}/entries/${entry}`);
        emit("playlists"); onRemoved?.();
        toast("Retiré de la playlist", {
          timeout: 6000,
          action: {
            label: "Annuler", onClick: async () => {
              try { await addToPlaylist(playlistId, [t.id], "", r.index ?? 0); router.refresh(); } catch (e) { toastError(e); }
            },
          },
        });
      },
    }] : []),
    { icon: t.liked ? "heart-fill" : "heart", on: t.liked, label: t.liked ? "Retirer des titres likés" : "Ajouter aux titres likés", onClick: () => toggleLike(t.id) },
    "hr",
    { icon: "disc", label: "Aller à l'album", onClick: () => { location.hash = `#/album/${t.albumId}`; } },
    { icon: "user", label: "Aller à l'artiste", onClick: () => { location.hash = `#/artist/${enc(t.artist)}`; } },
    ...(hooks.openLyrics ? [{ icon: "lyrics", label: "Paroles", onClick: () => hooks.openLyrics(t) }] : []),
    ...(row && hooks.startSelection ? [{ icon: "select", label: "Sélectionner plusieurs titres", onClick: () => hooks.startSelection(row) }] : []),
    "hr",
    ...(!isAdmin() ? [{ icon: "download", label: "Télécharger le fichier", onClick: () => download(`/api/tracks/${t.id}/download`) }] : [
    { icon: "edit", label: "Modifier les infos", onClick: () => editTrack(t) },
    {
      icon: "image", label: "Changer la pochette", onClick: () => coverDialog(
        t.album === "Singles"
          ? { kind: "track", id: t.id, query: `${t.artist} ${t.title}`, title: "Pochette du titre" }
          : { kind: "album", id: t.albumId, query: albumQuery, title: "Pochette de l'album" }),
    },
    { icon: "download", label: "Télécharger le fichier", onClick: () => download(`/api/tracks/${t.id}/download`) },
    "hr",
    {
      icon: "trash", label: "Supprimer le fichier", danger: true, onClick: async () => {
        if (!(await confirmDialog({ title: "Supprimer ce titre ?", text: `« ${t.title} » de ${t.artist} sera définitivement supprimé du Raspberry Pi.` }))) return;
        await api.del(`/api/tracks/${t.id}`);
        trackCache.delete(t.id);
        toast("Titre supprimé");
        emit("library"); emit("playlists"); router.refresh();
      },
    }]),
  ];
}

/** Menu d'une carte (album, artiste, playlist, mix, titres likés). */
export async function cardMenuItems(key, label = "") {
  const [kind, ...rest] = key.split(":");
  const arg = rest.join(":");
  const ids = async () => (await contextTracks(key)).tracks.map((t) => t.id);
  const items = [
    { icon: "play", label: "Lire", onClick: () => hooks.playKey?.(key) },
    { icon: "right", label: "Lire ensuite", onClick: async () => { player.playNext(await ids()); toast("Sera lu ensuite"); } },
    { icon: "queue", label: "Ajouter à la file d'attente", onClick: async () => { player.addToQueue(await ids()); toast("Ajouté à la file d'attente"); } },
    { icon: "plus", label: "Ajouter à une playlist", sub: async () => playlistSubmenu(await ids(), kind === "playlist" ? +arg : null) },
  ];
  if (hooks.offlineItem) { const it = hooks.offlineItem(key); if (it) items.push(it); }
  if (kind === "album") {
    items.push({ icon: "radio", label: "Radio de l'album", onClick: () => hooks.playKey?.(`radio:album:${arg}`) },
      "hr", { icon: "disc", label: "Ouvrir l'album", onClick: () => { location.hash = `#/album/${arg}`; } },
      { icon: "download", label: "Exporter en .zip", onClick: () => download(`/api/albums/${arg}/download`) });
  } else if (kind === "artist") {
    items.push({ icon: "radio", label: "Radio de l'artiste", onClick: () => hooks.playKey?.(`radio:artist:${arg}`) },
      "hr", { icon: "user", label: "Ouvrir la page de l'artiste", onClick: () => { location.hash = `#/artist/${enc(arg)}`; } });
  } else if (kind === "playlist") {
    items.push("hr", { icon: "download", label: "Exporter en .zip", onClick: () => download(`/api/playlists/${arg}/download`) },
      { icon: "list", label: "Exporter (.m3u8)", onClick: () => download(`/api/playlists/${arg}/export`) },
      { icon: "trash", label: "Supprimer la playlist", danger: true, onClick: () => deletePlaylist({ id: +arg, name: label || "cette playlist" }) });
  } else if (kind === "genre") {
    items.push({ icon: "radio", label: "Radio du genre", onClick: () => hooks.playKey?.(`radio:genre:${arg}`) });
  } else if (kind === "smart") {
    items.push("hr", { icon: "sparkles", label: "Ouvrir la playlist", onClick: () => { location.hash = `#/smart/${arg}`; } });
  }
  return items;
}

export function menuHead(t) {
  return `<div class="menu-head"><div class="mh-cover">${cover(t.cover, 96, t.album)}</div><div style="min-width:0"><div class="mh-1 ell">${esc(t.title)}</div><div class="mh-2 ell">${esc(t.artist)}</div></div></div>`;
}

export function openTrackMenu(t, pos, opts = {}) {
  return openMenu(trackMenuItems(t, opts), { ...pos, head: menuHead(t), onClose: opts.onClose });
}

export function download(url) {
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export { coverImg, promptDialog };
