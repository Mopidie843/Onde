// Import de musique : glisser-déposer, fichiers, dossiers, liens directs.
import { api, emit } from "./api.js";
import { $, esc, icon, fmtSize, cover, AUDIO_EXT } from "./util.js";
import { toast, toastError } from "./ui.js";

const CONCURRENCY = 3;
export const jobs = [];
let active = 0;
let seq = 0;
const listeners = new Set();
export const onJobs = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
let raf = 0;
function notify() {
  if (raf) return;
  raf = requestAnimationFrame(() => { raf = 0; listeners.forEach((fn) => fn()); updateBadge(); });
}
function updateBadge() {
  const b = $("#upload-badge");
  if (!b) return;
  const pending = jobs.filter((j) => j.status === "queued" || j.status === "uploading").length;
  b.hidden = !pending;
  b.textContent = pending;
}

let batch = { added: 0, duplicate: 0, error: 0 };

export function enqueue(files) {
  const audio = [...files].filter((f) => AUDIO_EXT.test(f.name) || /\.zip$/i.test(f.name));
  const skipped = files.length - audio.length;
  if (!audio.length) {
    toast(skipped ? "Aucun fichier audio reconnu (MP3, M4A, FLAC, OGG, WAV, WMA, ZIP…)" : "Aucun fichier", { error: true });
    return 0;
  }
  for (const f of audio) {
    jobs.unshift({ id: ++seq, file: f, name: f.webkitRelativePath || f.name, size: f.size, loaded: 0, status: "queued" });
  }
  toast(`${audio.length} fichier${audio.length > 1 ? "s" : ""} en cours d'import${skipped ? ` (${skipped} ignoré${skipped > 1 ? "s" : ""})` : ""}`,
    location.hash.startsWith("#/import") ? {} : { action: { label: "Voir", href: "#/import" } });
  pump();
  notify();
  return audio.length;
}

function pump() {
  while (active < CONCURRENCY) {
    const job = [...jobs].reverse().find((j) => j.status === "queued");
    if (!job) break;
    start(job);
  }
  if (!active && jobs.length && !jobs.some((j) => j.status === "queued")) finishBatch();
}

function start(job) {
  active++;
  job.status = "uploading";
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.setRequestHeader("X-Filename", encodeURIComponent(job.file.name));
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.onprogress = (e) => { job.loaded = e.loaded; notify(); };
  xhr.onload = () => {
    let res = {};
    try { res = JSON.parse(xhr.responseText); } catch { /* ignore */ }
    if (xhr.status === 401) { location.href = "/login"; return; }
    job.status = res.status || (xhr.status < 300 ? "added" : "error");
    job.track = res.track || null;
    job.error = res.error || res.detail || (xhr.status >= 300 ? `Erreur ${xhr.status}` : "");
    if (res.added != null) { // archive .zip : plusieurs titres
      job.zip = { added: res.added, duplicates: res.duplicates, errors: res.errors };
      batch.added += res.added;
      batch.duplicate += res.duplicates || 0;
      batch.error += res.errors || 0;
    } else {
      batch[job.status in batch ? job.status : "error"]++;
    }
    done();
  };
  xhr.onerror = () => { job.status = "error"; job.error = "Connexion interrompue"; batch.error++; done(); };
  xhr.send(job.file);
  function done() {
    active--;
    job.file = null; // libère la mémoire
    job.loaded = job.size;
    notify();
    pump();
  }
}

function finishBatch() {
  const { added, duplicate, error } = batch;
  if (!added && !duplicate && !error) return;
  const parts = [];
  if (added) parts.push(`${added} titre${added > 1 ? "s" : ""} ajouté${added > 1 ? "s" : ""}`);
  if (duplicate) parts.push(`${duplicate} déjà présent${duplicate > 1 ? "s" : ""}`);
  if (error) parts.push(`${error} erreur${error > 1 ? "s" : ""}`);
  toast(parts.join(" · "), { error: !added && !!error, timeout: 4500 });
  batch = { added: 0, duplicate: 0, error: 0 };
  if (added) emit("library");
}

// ---------------------------------------------------------------- import d'une liste (plusieurs titres, un par ligne)
let batchQueue = [];
let batchRunning = false;

/** Découpe un texte collé (une musique par ligne, numérotée ou non) en requêtes propres. */
export function parseBatchLines(text) {
  return String(text || "")
    .split(/\r?\n/)
    .map((l) => l.replace(/^\s*(?:\d+[.)\-:]?|[-*•])\s*/, "").trim())
    .filter(Boolean);
}

const BATCH_CONCURRENCY = 3;   // titres téléchargés en parallèle (l'affichage reste dans l'ordre de la liste)
const AUTO_RETRIES = 2;        // relances automatiques en cas de coupure réseau (pas sur une vraie erreur "introuvable")
const RETRY_DELAY_MS = 1800;

/** Lien de playlist (YouTube, SoundCloud, Deezer, Spotify) plutôt que d'un seul titre ? */
export function isPlaylistUrl(url) {
  return /youtube\.com\/playlist\?/i.test(url)
    || /deezer\.com\/(\w\w\/)?playlist\//i.test(url)
    || /open\.spotify\.com\/(intl-[\w-]+\/)?(embed\/)?playlist\//i.test(url)
    || /^https?:\/\/(www\.|m\.)?soundcloud\.com\/(?!discover|search|charts|upload|you|stream|pages|terms)[\w-]+(\/(sets\/[\w-]+|likes|tracks|albums|reposts|popular-tracks|toptracks))?\/?([?#].*)?$/i.test(url);
}

export function importBatch(text) {
  let lines = parseBatchLines(text);
  if (!lines.length) { toast("Colle au moins un titre, un par ligne", { error: true }); return 0; }
  const MAX = 200;
  const truncated = lines.length > MAX;
  if (truncated) lines = lines.slice(0, MAX);
  const created = lines.map((q) => ({
    id: ++seq, name: q, query: q, size: 0, loaded: 0, status: "queued", isUrl: true, isSearch: true, isBatch: true, retries: 0,
  }));
  jobs.unshift(...created);
  batchQueue.push(...created);
  toast(`${lines.length} titre${lines.length > 1 ? "s" : ""} en file, téléchargement dans l'ordre…${truncated ? ` (limité à ${MAX})` : ""}`,
    location.hash.startsWith("#/import") ? {} : { action: { label: "Voir", href: "#/import" } });
  notify();
  runBatchQueue();
  return lines.length;
}

export function cancelBatch() {
  if (!batchQueue.length) return;
  for (const j of batchQueue) { j.status = "cancelled"; j.error = "Annulé"; }
  batchQueue = [];
  notify();
}

export const batchPending = () => batchQueue.length;

/** Relance manuelle d'un titre en échec (bouton « Réessayer »). */
export function retryJob(id) {
  const job = jobs.find((j) => j.id === id);
  if (!job || job.status === "uploading" || job.status === "queued") return;
  job.status = "queued";
  job.error = null;
  job.retries = 0;
  notify();
  if (job.isBatch) {
    batchQueue.push(job);
    runBatchQueue();
  } else {
    runSingle(job);
  }
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/** Télécharge un job (utilisé pour la liste comme pour un import isolé), avec relance auto sur coupure réseau. */
async function runJob(job) {
  job.status = "uploading";
  notify();
  try {
    const r = await api.post("/api/import-url", { url: job.query ?? job.name });
    job.status = r.status;
    job.track = r.track;
    job.error = r.error;
    if (r.name) job.name = r.name;
    batch[job.status in batch ? job.status : "error"]++;
  } catch (e) {
    // ne retente automatiquement que sur une vraie coupure réseau (le serveur n'a pas répondu du tout) —
    // pas sur une erreur propre du serveur (ex. « aucun résultat trouvé »), où réessayer ne changerait rien.
    const isNetworkError = e.message === "Serveur injoignable — vérifie que le Raspberry Pi est allumé.";
    if (isNetworkError && job.retries < AUTO_RETRIES && job.status !== "cancelled") {
      job.retries++;
      job.status = "retry";
      job.error = `Connexion interrompue, nouvel essai (${job.retries}/${AUTO_RETRIES})…`;
      notify();
      await wait(RETRY_DELAY_MS * job.retries);
      if (job.status === "cancelled") return;
      return runJob(job);
    }
    job.status = "error";
    job.error = e.message || "Connexion interrompue";
    batch.error++;
  }
  notify();
}

async function batchWorker() {
  while (batchQueue.length) {
    const job = batchQueue.shift();
    if (!job || job.status === "cancelled") continue;
    await runJob(job);
  }
}

async function runBatchQueue() {
  if (batchRunning) return;
  batchRunning = true;
  notify();
  await Promise.all(Array.from({ length: BATCH_CONCURRENCY }, batchWorker));
  batchRunning = false;
  finishBatch();
  notify();
}

async function runSingle(job) {
  await runJob(job);
}

export async function importUrl(url) {
  const isLink = /^https?:\/\//i.test(url.trim());
  const job = { id: ++seq, name: url, query: url, size: 0, loaded: 0, status: "uploading", isUrl: true, isSearch: !isLink, retries: 0 };
  jobs.unshift(job);
  toast(isLink ? "Téléchargement du lien…" : `Recherche de « ${url} »…`);
  notify();
  try {
    const r = await api.post("/api/import-url", { url });
    job.status = r.status;
    job.track = r.track;
    job.error = r.error;
    job.name = r.name || url;
    if (r.status === "added") { toast(isLink ? "Titre importé depuis le lien" : `« ${r.track ? r.track.title : url} » téléchargé`); emit("library"); }
    else if (r.status === "duplicate") toast("Ce titre est déjà dans ta bibliothèque");
    else toast(r.error || "Import impossible", { error: true });
  } catch (e) {
    job.status = "error";
    job.error = e.message;
    toast(e.message, { error: true });
  }
  notify();
}

// ---------------------------------------------------------------- dossiers déposés
async function readEntry(entry, out) {
  if (entry.isFile) {
    await new Promise((res) => entry.file((f) => { out.push(f); res(); }, res));
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    for (;;) {
      const batchEntries = await new Promise((res) => reader.readEntries(res, () => res([])));
      if (!batchEntries.length) break;
      for (const e of batchEntries) await readEntry(e, out);
    }
  }
}
export async function filesFromDrop(dt) {
  const items = [...(dt.items || [])];
  const entries = items.map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (!entries.length) return [...(dt.files || [])];
  const out = [];
  for (const e of entries) await readEntry(e, out);
  return out;
}

export function pickFiles({ folder = false } = {}) {
  const input = document.createElement("input");
  input.type = "file";
  input.multiple = true;
  if (folder) input.webkitdirectory = true;
  else input.accept = "audio/*,.mp3,.m4a,.aac,.flac,.ogg,.oga,.opus,.wav,.aif,.aiff,.wma,.ape,.wv,.mpc,.zip,application/zip";
  input.addEventListener("change", () => enqueue([...input.files]));
  input.click();
}

// ---------------------------------------------------------------- vue « Importer »
function jobHtml(j) {
  const pct = j.size ? Math.round((j.loaded / j.size) * 100) : j.status === "uploading" ? 30 : 100;
  const t = j.track;
  const state = {
    queued: "En attente", uploading: j.isUrl ? "Téléchargement…" : `${pct} %`, added: "Ajouté",
    duplicate: "Déjà présent", error: "Erreur", cancelled: "Annulé", retry: "Nouvel essai…",
  }[j.status] || j.status;
  const zipInfo = j.zip ? `Album .zip : ${j.zip.added} titre${j.zip.added > 1 ? "s" : ""} ajouté${j.zip.added > 1 ? "s" : ""}${j.zip.duplicates ? `, ${j.zip.duplicates} déjà présent${j.zip.duplicates > 1 ? "s" : ""}` : ""}${j.zip.errors ? `, ${j.zip.errors} illisible${j.zip.errors > 1 ? "s" : ""}` : ""}` : "";
  const sub = zipInfo ? esc(zipInfo) : t ? `${esc(t.artist)} — ${esc(t.album)}` : j.error ? esc(j.error) : j.size ? fmtSize(j.size) : "";
  return `<div class="job ${j.status}">
    <div class="j-ic">${t ? cover(t.cover, 96, t.album) : icon(j.isSearch ? "search" : j.isUrl ? "link" : "note")}</div>
    <div style="min-width:0">
      <div class="j-name ell">${esc(j.zip ? j.name : t ? t.title : j.name)}</div>
      <div class="j-sub ell">${sub}</div>
      ${j.status === "uploading" || j.status === "queued" ? `<div class="j-bar"><i style="width:${pct}%"></i></div>` : ""}
    </div>
    ${j.status === "error" && j.query ? `<button class="btn ghost sm" data-retry="${j.id}">${icon("refresh", "sm")} Réessayer</button>` : `<div class="j-state">${state}</div>`}
  </div>`;
}

export function renderImport(el, info) {
  el.innerHTML = `<div class="page">
    <h1 class="page-title">Importer de la musique</h1>
    <div class="dropzone" id="dz">
      <svg class="dz-ic"><use href="#i-upload"/></svg>
      <h3>Glisse tes fichiers ou dossiers ici</h3>
      <p>MP3, M4A, FLAC, OGG, OPUS, WAV, WMA, AIFF… et albums entiers en .zip · rangés automatiquement par artiste et album, pochettes comprises.</p>
      <div class="dz-actions">
        <button class="btn primary" data-pick="files">${icon("note")} Choisir des fichiers</button>
        <button class="btn" data-pick="folder">${icon("folder")} Choisir un dossier</button>
      </div>
    </div>
    <form class="url-form" id="url-form">
      <input class="field" name="url" type="text" placeholder="Tape le nom d'une musique (« Artiste - Titre ») ou colle un lien (YouTube, SoundCloud…)" required>
      <button class="btn white" type="submit">${icon("download")} Télécharger</button>
    </form>
    <details class="batch-import" id="batch-import">
      <summary>${icon("list")} Importer une liste de musiques (playlist collée, une par ligne)</summary>
      <form class="url-form" id="batch-form" style="margin-top:12px">
        <textarea class="field" name="list" rows="6" placeholder="1. Manau – La Tribu de Dana&#10;2. Mylène Farmer – Désenchantée&#10;3. IAM – Je danse le Mia&#10;…" required></textarea>
        <div style="display:flex;gap:10px;width:100%;flex-wrap:wrap">
          <button class="btn white" type="submit">${icon("download")} Tout télécharger, dans l'ordre</button>
          <button class="btn ghost sm" type="button" id="batch-cancel" hidden>${icon("close")} Annuler le reste</button>
        </div>
      </form>
      <p class="dim" style="font-size:12px;margin:8px 0 0">Une musique par ligne, numérotée ou non (« Artiste – Titre » ou juste le titre). Onde les télécharge une par une, dans l'ordre de la liste, et les ajoute au fur et à mesure ci-dessous.</p>
      <form class="url-form" id="playlist-form" style="margin-top:14px">
        <input class="field" name="purl" type="text" placeholder="…ou colle un lien de playlist YouTube, SoundCloud, Deezer ou Spotify">
        <button class="btn" type="submit" id="playlist-go">${icon("link")} Charger la liste</button>
      </form>
      <p class="dim" style="font-size:12px;margin:8px 0 0">Onde récupère l'ordre et les titres de la playlist, puis les télécharge un par un comme ci-dessus. Spotify n'a pas d'API publique officielle pour ça : ça marche la plupart du temps, mais peut casser si Spotify change son site — colle plutôt les titres à la main si ça échoue.</p>
    </details>
    <div class="jobs" id="jobs"></div>
    <div class="tips">
      <div class="tip" id="tip-dl">${icon("search")}<b>Télécharger par nom</b><p>Tape le nom d'un titre (ex. « Daft Punk - Get Lucky ») : Onde regarde d'abord ses vraies infos (titre, artiste, durée) puis compare plusieurs résultats YouTube — et SoundCloud en repli — pour choisir le meilleur (audio officiel, bonne durée, sans live ni remix), le télécharge en MP3 et écrit tags + pochette automatiquement.</p></div>
      <div class="tip">${icon("image")}<b>Pochettes automatiques</b><p>La pochette intégrée au MP3 est utilisée. Sinon, Onde la cherche en ligne${info && !info.autoCovers ? " (désactivé dans la config)" : ""}. Tu peux la changer à tout moment depuis l'album.</p></div>
      <div class="tip">${icon("folder")}<b>Copie directe sur le Pi</b><p>Tu peux aussi copier des dossiers dans <code>${esc(info ? info.musicDir : "~/Musique")}</code> (clé USB, partage réseau…) : ils sont détectés automatiquement.</p></div>
      <div class="tip">${icon("lock")}<b>Écran verrouillé</b><p>Sur téléphone, la musique continue écran éteint, avec les commandes sur l'écran de verrouillage. Ajoute Onde à l'écran d'accueil pour l'ouvrir comme une app.</p></div>
    </div>
  </div>`;
  const dz = $("#dz", el);
  dz.addEventListener("click", (e) => {
    const b = e.target.closest("[data-pick]");
    if (b) pickFiles({ folder: b.dataset.pick === "folder" });
  });
  $("#url-form", el).addEventListener("submit", (e) => {
    e.preventDefault();
    const url = e.target.url.value.trim();
    if (!url) return;
    e.target.reset();
    if (isPlaylistUrl(url)) loadPlaylist(url); // une playlist collée ici marche aussi
    else importUrl(url);
  });
  $("#batch-form", el).addEventListener("submit", (e) => {
    e.preventDefault();
    const list = e.target.list.value;
    if (importBatch(list)) e.target.reset();
  });
  const loadPlaylist = async (purl, form) => {
    const btn = $("#playlist-go", el);
    if (btn) btn.disabled = true;
    toast("Lecture de la playlist…");
    try {
      const r = await api.post("/api/import-playlist", { url: purl });
      if (!r.queries || !r.queries.length) { toast("Playlist vide ou illisible", { error: true }); return; }
      importBatch(r.queries.join("\n"));
      form?.reset();
      toast(`« ${r.name} » : ${r.queries.length} titre${r.queries.length > 1 ? "s" : ""} en file`);
    } catch (err) {
      toastError(err);
    } finally {
      if (btn) btn.disabled = false;
    }
  };
  $("#playlist-form", el).addEventListener("submit", (e) => {
    e.preventDefault();
    const purl = e.target.purl.value.trim();
    if (purl) loadPlaylist(purl, e.target);
  });
  const cancelBtn = $("#batch-cancel", el);
  cancelBtn.addEventListener("click", () => cancelBatch());
  const updateCancel = () => { cancelBtn.hidden = !batchPending(); };
  updateCancel();
  api.get("/api/download-available").then((r) => {
    if (r.available) return;
    const tip = $("#tip-dl", el);
    if (tip) tip.innerHTML = `${icon("close")}<b>Téléchargement par nom indisponible</b><p>Le serveur n'a pas <code>yt-dlp</code> installé. Dans le dossier d'Onde, sur le Pi : <code>.venv/bin/pip install -U yt-dlp</code> (sous Windows : <code>.venv\\Scripts\\pip install -U yt-dlp</code>), puis redémarre Onde. En attendant, colle un lien direct.</p>`;
  }).catch(() => {});
  const list = $("#jobs", el);
  list.addEventListener("click", (e) => {
    const b = e.target.closest("[data-retry]");
    if (b) retryJob(+b.dataset.retry);
  });
  const draw = () => { list.innerHTML = jobs.slice(0, 200).map(jobHtml).join(""); updateCancel(); };
  draw();
  return onJobs(draw);
}
