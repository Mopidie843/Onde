// Écoute hors connexion : la musique gardée sur cet appareil, et la bascule automatique quand le
// Raspberry Pi n'est pas joignable (loin de chez toi, Wi-Fi coupé…).
//
// Tout est rangé dans IndexedDB, qui marche partout (même en http://) :
//   audio        id  -> { id, blob }                         le fichier audio lui-même
//   tracks       id  -> { id, meta }                         les infos du titre (même JSON que l'API)
//   collections  key -> { key, path, name, meta, ids, at }   album, playlist, titres likés…
//   covers       k   -> { k, blob }                          pochettes (k = "id:taille")
//   queue        id  -> { id, at }                           titres encore à télécharger (reprise auto)
//   plays        ++  -> { id, at }                           écoutes faites hors connexion
//
// La lecture reste synchrone (indispensable pour enchaîner les titres écran verrouillé) : chaque
// fichier téléchargé reçoit une adresse locale (blob:) dès le démarrage.
import { api, trackCache, setOfflineHooks } from "./api.js";
import { AUDIO_MIME, canPlayExt, fmtSize, setCoverResolver, storage } from "./util.js";

const DB_NAME = "onde-offline";
const COVER_SIZES = [192, 768];
const CONCURRENCY = 2;
const PROBE_TIMEOUT = 7000;   // (4G qui capte mal : une réponse peut mettre plusieurs secondes)
const PROBE_STRIKES = 2;      // échecs d'affilée avant de passer hors connexion
const PROBE_EVERY = 8000;    // hors connexion : on revérifie souvent (retour rapide dès que le réseau revient)

export const DOWNLOADABLE = /^(album:\d+|playlist:\d+|smart:\d+|liked|recent)$/;

export function pathFor(key) {
  if (key === "liked") return "/api/liked";
  if (key === "recent") return "/api/recent";
  const [kind, id] = key.split(":");
  return { album: `/api/albums/${id}`, playlist: `/api/playlists/${id}`, smart: `/api/smart/${id}` }[kind] || null;
}
const tracksOf = (meta) => (Array.isArray(meta) ? meta : meta?.tracks || []);
function nameOf(key, meta) {
  if (key === "liked") return "Titres likés";
  if (key === "recent") return "Écoutés récemment";
  if (key.startsWith("album:") && meta.single) return `Singles · ${meta.artist}`;
  return meta.name || "Sans titre";
}
function kindOf(key) {
  return key === "liked" || key === "recent" ? "Playlist" : key.startsWith("album:") ? "Album"
    : key.startsWith("smart:") ? "Playlist intelligente" : "Playlist";
}
function coversOf(meta) {
  const ids = new Set();
  if (!Array.isArray(meta)) {
    if (meta.cover) ids.add(meta.cover);
    for (const c of meta.mosaic || []) if (c) ids.add(c);
  }
  for (const t of tracksOf(meta)) if (t.cover) ids.add(t.cover);
  return [...ids];
}

const wrap = (r) => new Promise((resolve, reject) => { r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error); });
const done = (tx) => new Promise((resolve, reject) => {
  tx.oncomplete = () => resolve();
  tx.onerror = tx.onabort = () => reject(tx.error || new DOMException("IndexedDB", "AbortError"));
});
const isQuota = (e) => e && (e.name === "QuotaExceededError" || /quota|space|disk/i.test(e.message || ""));

class Offline extends EventTarget {
  constructor() {
    super();
    this.supported = typeof indexedDB !== "undefined";
    this.ready = false;
    this.active = false;          // serveur injoignable : on vit sur les téléchargements
    this.collections = new Map(); // key -> { key, path, name, kind, meta, ids, at }
    this.metas = new Map();       // id -> JSON du titre (pour tous les titres des téléchargements)
    this.urls = new Map();        // id -> adresse blob: (titre complètement téléchargé)
    this.sizes = new Map();       // id -> octets
    this.coverUrls = new Map();   // "cid:taille" -> adresse blob:
    this.queue = [];              // ids à télécharger, dans l'ordre
    this.failed = new Map();      // id -> nombre d'échecs (cette session)
    this.full = false;            // plus de place sur l'appareil
    this._running = 0;
    this._aborts = new Map();
    this._probeT = 0;
    this._probing = null;
    this._emitT = 0;
    this._syncing = new Set();
  }

  // ------------------------------------------------------------------ démarrage
  async init() {
    if (!this.supported) return;
    try {
      this.db = await new Promise((resolve, reject) => {
        const r = indexedDB.open(DB_NAME, 1);
        r.onupgradeneeded = () => {
          const d = r.result;
          d.createObjectStore("audio", { keyPath: "id" });
          d.createObjectStore("tracks", { keyPath: "id" });
          d.createObjectStore("collections", { keyPath: "key" });
          d.createObjectStore("covers", { keyPath: "k" });
          d.createObjectStore("queue", { keyPath: "id" });
          d.createObjectStore("plays", { autoIncrement: true });
        };
        r.onsuccess = () => resolve(r.result);
        r.onerror = () => reject(r.error);
        r.onblocked = () => reject(new Error("IndexedDB bloquée"));
      });
    } catch {
      this.supported = false; // navigation privée, stockage désactivé…
      return;
    }
    this.db.onversionchange = () => this.db.close();
    const all = (store) => wrap(this.db.transaction(store).objectStore(store).getAll());
    let cols, tracks, audio, covers, queue;
    try {
      [cols, tracks, audio, covers, queue] = await Promise.all(["collections", "tracks", "audio", "covers", "queue"].map(all));
    } catch {
      this.supported = false; // stockage de l'appareil illisible (ex. Safari) : Onde marche sans
      return;
    }
    for (const c of cols) this.collections.set(c.key, c);
    for (const t of tracks) {
      this.metas.set(t.id, t.meta);
      if (!trackCache.has(t.id)) trackCache.set(t.id, { ...t.meta });
    }
    for (const a of audio) this._addUrl(a.id, a.blob);
    for (const c of covers) this.coverUrls.set(c.k, URL.createObjectURL(c.blob));
    this.queue = queue.sort((a, b) => a.at - b.at).map((q) => q.id).filter((id) => !this.urls.has(id));
    setCoverResolver((cid, size) => this.coverUrl(cid, size), () => this.active);
    setOfflineHooks({
      fallback: (path) => this._fallback(path),
      response: (path, data) => this._onResponse(path, data),
      failure: () => this._onFailure(),
      isOffline: () => this.active,
    });
    addEventListener("online", () => this.probe());
    addEventListener("offline", () => this.setActive(true));
    this.ready = true;
    this._emit();
  }

  /** Vérifie si le Pi répond. Renvoie true s'il est joignable (et repasse en ligne).
   *  Hors connexion seulement après PROBE_STRIKES échecs d'affilée (ou si l'appareil n'a plus aucun
   *  réseau) : un réseau faible qui rate une requête ne coupe plus la musique ni l'affichage.
   *  `quick` : une seule tentative (démarrage de l'appli). */
  probe(quick = false) {
    if (this._probing) return this._probing;
    this._probing = (async () => {
      let ok = await this._ping();
      for (let i = 1; !ok && !quick && !this.active && i < PROBE_STRIKES && navigator.onLine !== false; i++) {
        await new Promise((r) => setTimeout(r, 1500));
        ok = await this._ping();
      }
      this.setActive(!ok);
      return ok;
    })().finally(() => { this._probing = null; });
    return this._probing;
  }

  async _ping() {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), PROBE_TIMEOUT);
    try {
      const r = await fetch("/api/info", { cache: "no-store", signal: ctrl.signal });
      return r.ok || r.status === 401;
    } catch {
      return false;
    } finally {
      clearTimeout(t);
    }
  }

  setActive(on) {
    on = !!on && this.supported;
    clearTimeout(this._probeT);
    if (on) this._probeT = setTimeout(() => this.probe(), PROBE_EVERY);
    if (on === this.active) return;
    this.active = on;
    this.dispatchEvent(new Event("status"));
    if (!on) { this.flushPlays(); this.failed.clear(); this._pump(); this.syncAll(); }
  }

  get hasDownloads() { return this.collections.size > 0; }
  has(id) { return this.urls.has(id); }
  url(id) { return this.urls.get(id) || null; }
  isSaved(key) { return this.collections.has(key); }

  coverUrl(cid, size) {
    if (!cid || !this.coverUrls.size) return null;
    const want = size <= 192 ? [192, 768] : [768, 192];
    for (const s of want) { const u = this.coverUrls.get(`${cid}:${s}`); if (u) return u; }
    return null;
  }

  /** { done, total, bytes, state: "none" | "busy" | "done" } pour une collection. */
  progress(key) {
    const c = this.collections.get(key);
    if (!c) return { done: 0, total: 0, bytes: 0, state: "none" };
    let n = 0, bytes = 0;
    for (const id of c.ids) if (this.urls.has(id)) { n++; bytes += this.sizes.get(id) || 0; }
    return { done: n, total: c.ids.length, bytes, state: n >= c.ids.length ? "done" : "busy" };
  }
  get totalBytes() { let b = 0; for (const v of this.sizes.values()) b += v; return b; }
  get busy() { return this.queue.length > 0 || this._running > 0; }

  // ------------------------------------------------------------------ télécharger / retirer
  async add(key) {
    if (!this.supported) throw new Error("Ce navigateur ne permet pas de garder de la musique (navigation privée ?).");
    const path = pathFor(key);
    if (!path) throw new Error("Impossible de télécharger ceci.");
    const meta = await api.get(path);
    const tracks = tracksOf(meta);
    if (!tracks.length) throw new Error("Rien à télécharger ici pour l'instant.");
    const need = tracks.filter((t) => !this.urls.has(t.id)).reduce((s, t) => s + (t.size || 0), 0);
    try {
      const est = await navigator.storage?.estimate?.();
      if (est && est.quota && est.quota - est.usage < need * 1.05) {
        throw new Error(`Pas assez de place sur cet appareil : il faut ${fmtSize(need)}, il reste ${fmtSize(est.quota - est.usage)}.`);
      }
    } catch (e) { if (/place/.test(e.message)) throw e; }
    navigator.storage?.persist?.().catch(() => {}); // évite que le navigateur n'efface tout pour faire de la place
    this.full = false;
    await this._saveCollection(key, path, meta);
    this._emit(key);
    this._pump();
    this._fetchCovers(coversOf(meta));
  }

  async remove(key) {
    if (!this.collections.has(key)) return;
    const tx = this.db.transaction("collections", "readwrite");
    tx.objectStore("collections").delete(key);
    await done(tx);
    this.collections.delete(key);
    await this._collect();
    this._emit(key);
  }

  async removeAll() {
    for (const ctrl of this._aborts.values()) ctrl.abort();
    const stores = ["audio", "tracks", "collections", "covers", "queue"];
    const tx = this.db.transaction(stores, "readwrite");
    for (const s of stores) tx.objectStore(s).clear();
    await done(tx);
    for (const u of this.urls.values()) URL.revokeObjectURL(u);
    for (const u of this.coverUrls.values()) URL.revokeObjectURL(u);
    this.collections.clear(); this.metas.clear(); this.urls.clear(); this.sizes.clear();
    this.coverUrls.clear(); this.queue = []; this.full = false;
    this._emit();
  }

  /** Réessaie les téléchargements en attente (après une coupure, un manque de place…). */
  resume() { this.full = false; this.failed.clear(); this._pump(); }

  async _saveCollection(key, path, meta) {
    const tracks = tracksOf(meta);
    const rec = { key, path, name: nameOf(key, meta), kind: kindOf(key), meta, ids: [...new Set(tracks.map((t) => t.id))], at: this.collections.get(key)?.at || Date.now() };
    const queued = new Set(this.queue);
    const missing = rec.ids.filter((id) => !this.urls.has(id) && !queued.has(id));
    const tx = this.db.transaction(["collections", "tracks", "queue"], "readwrite");
    tx.objectStore("collections").put(rec);
    for (const t of tracks) tx.objectStore("tracks").put({ id: t.id, meta: t });
    const now = Date.now();
    missing.forEach((id, i) => tx.objectStore("queue").put({ id, at: now + i }));
    await done(tx);
    this.collections.set(key, rec);
    for (const t of tracks) this.metas.set(t.id, t);
    this.queue.push(...missing);
    return rec;
  }

  // Supprime ce qui n'appartient plus à aucun téléchargement (titres, pochettes).
  async _collect() {
    const keep = new Set();
    const keepCovers = new Set();
    for (const c of this.collections.values()) {
      c.ids.forEach((id) => keep.add(id));
      coversOf(c.meta).forEach((cid) => keepCovers.add(cid));
    }
    const dropIds = [...new Set([...this.metas.keys(), ...this.urls.keys(), ...this.queue])].filter((id) => !keep.has(id));
    const dropCovers = [...this.coverUrls.keys()].filter((k) => !keepCovers.has(k.split(":")[0]));
    if (!dropIds.length && !dropCovers.length) return;
    for (const id of dropIds) this._aborts.get(id)?.abort();
    const tx = this.db.transaction(["audio", "tracks", "queue", "covers"], "readwrite");
    for (const id of dropIds) { tx.objectStore("audio").delete(id); tx.objectStore("tracks").delete(id); tx.objectStore("queue").delete(id); }
    for (const k of dropCovers) tx.objectStore("covers").delete(k);
    await done(tx);
    for (const id of dropIds) {
      const u = this.urls.get(id);
      if (u) URL.revokeObjectURL(u);
      this.urls.delete(id); this.sizes.delete(id); this.metas.delete(id);
    }
    const dropSet = new Set(dropIds);
    this.queue = this.queue.filter((id) => !dropSet.has(id));
    for (const k of dropCovers) { URL.revokeObjectURL(this.coverUrls.get(k)); this.coverUrls.delete(k); }
  }

  _addUrl(id, blob) {
    const old = this.urls.get(id);
    if (old) URL.revokeObjectURL(old);
    this.urls.set(id, URL.createObjectURL(blob));
    this.sizes.set(id, blob.size);
  }

  // ------------------------------------------------------------------ file de téléchargement
  _pump() {
    if (!this.ready || this.active || this.full) return;
    while (this._running < CONCURRENCY) {
      const id = this.queue.find((x) => !this._aborts.has(x) && (this.failed.get(x) || 0) < 3);
      if (id == null) break;
      this._running++;
      this._downloadOne(id).finally(() => { this._running--; this._emit(); this._pump(); });
    }
  }

  async _downloadOne(id) {
    const meta = this.metas.get(id);
    const ctrl = new AbortController();
    this._aborts.set(id, ctrl);
    try {
      const url = `/api/tracks/${id}/stream${meta && meta.ext && !canPlayExt(meta.ext) ? "?t=1" : ""}`;
      const res = await fetch(url, { signal: ctrl.signal, cache: "no-store" });
      if (res.status === 404) { await this._forget(id); return; } // titre supprimé du serveur
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      let blob = await res.blob();
      if (!blob.size) throw new Error("fichier vide");
      if (!blob.type) blob = new Blob([blob], { type: AUDIO_MIME[meta?.ext] || "audio/mpeg" });
      const tx = this.db.transaction(["audio", "queue"], "readwrite");
      tx.objectStore("audio").put({ id, blob });
      tx.objectStore("queue").delete(id);
      await done(tx);
      blob = null;
      // on relit le fichier depuis IndexedDB : l'adresse locale pointe alors vers le disque, pas vers
      // une copie en mémoire (sinon un gros album occuperait toute la RAM du téléphone)
      this.queue = this.queue.filter((x) => x !== id);
      if (!this._wanted(id)) { // retiré pendant le téléchargement
        const del = this.db.transaction("audio", "readwrite");
        del.objectStore("audio").delete(id);
        await done(del);
        return;
      }
      const stored = await wrap(this.db.transaction("audio").objectStore("audio").get(id));
      if (stored) this._addUrl(id, stored.blob);
    } catch (e) {
      if (ctrl.signal.aborted) return;
      if (isQuota(e)) {
        this.full = true;
        this.dispatchEvent(new CustomEvent("problem", { detail: "Plus assez de place sur cet appareil : libère de l'espace, puis relance les téléchargements (Réglages › Musique téléchargée)." }));
        return;
      }
      this.failed.set(id, (this.failed.get(id) || 0) + 1);
      if (e instanceof TypeError) this._onFailure(); // réseau coupé
    } finally {
      this._aborts.delete(id);
    }
  }

  _wanted(id) {
    for (const c of this.collections.values()) if (c.ids.includes(id)) return true;
    return false;
  }

  async _forget(id) {
    const tx = this.db.transaction("queue", "readwrite");
    tx.objectStore("queue").delete(id);
    await done(tx);
    this.queue = this.queue.filter((x) => x !== id);
    for (const c of this.collections.values()) {
      if (c.ids.includes(id)) c.ids = c.ids.filter((x) => x !== id);
    }
  }

  async _fetchCovers(cids) {
    const todo = [];
    for (const cid of cids) for (const s of COVER_SIZES) if (!this.coverUrls.has(`${cid}:${s}`)) todo.push([cid, s]);
    const worker = async () => {
      while (todo.length && !this.active) {
        const [cid, s] = todo.shift();
        try {
          const r = await fetch(`/api/cover/${cid}/${s}`);
          if (!r.ok) continue;
          const blob = await r.blob();
          const tx = this.db.transaction("covers", "readwrite");
          tx.objectStore("covers").put({ k: `${cid}:${s}`, blob });
          await done(tx);
          if (!this.coverUrls.has(`${cid}:${s}`)) this.coverUrls.set(`${cid}:${s}`, URL.createObjectURL(blob));
        } catch (e) { if (isQuota(e)) return; }
      }
    };
    await Promise.all([worker(), worker(), worker()]);
  }

  // ------------------------------------------------------------------ synchronisation
  /** Met à jour les téléchargements (titres ajoutés/retirés d'une playlist, nouveaux likes…). */
  async syncAll() {
    if (this.active || !this.ready) return;
    for (const c of [...this.collections.values()]) {
      try { await api.get(c.path); } catch (e) {
        if (/introuvable|404/i.test(e.message)) await this.remove(c.key); // supprimé du serveur
      }
    }
  }

  async _onResponse(path, data) {
    if (!this.ready) return;
    try { await this._sync(path, data); } catch { /* on réessaiera à la prochaine visite */ }
  }

  async _sync(path, data) {
    for (const c of [...this.collections.values()]) {
      if (c.path !== path || this._syncing.has(c.key)) continue;
      const ids = [...new Set(tracksOf(data).map((t) => t.id))];
      const changed = ids.length !== c.ids.length || ids.some((id, i) => id !== c.ids[i])
        || JSON.stringify(data) !== JSON.stringify(c.meta);
      if (!changed) continue;
      this._syncing.add(c.key);
      try {
        await this._saveCollection(c.key, path, data);
        await this._collect();
        this._fetchCovers(coversOf(data));
        this._emit(c.key);
        this._pump();
      } finally { this._syncing.delete(c.key); }
    }
  }

  // ------------------------------------------------------------------ hors connexion
  _fallback(path) {
    if (!this.ready) return undefined;
    const clone = (x) => (typeof structuredClone === "function" ? structuredClone(x) : JSON.parse(JSON.stringify(x)));
    for (const c of this.collections.values()) if (c.path === path) return clone(c.meta);
    if (path.startsWith("/api/tracks/by-ids")) {
      const ids = (new URLSearchParams(path.split("?")[1] || "").get("ids") || "").split(",").map(Number);
      return ids.map((id) => this.metas.get(id)).filter(Boolean).map((t) => ({ ...t }));
    }
    return undefined;
  }

  _onFailure() {
    if (!this.active && this.ready) this.probe();
  }

  /** Une écoute faite hors connexion : envoyée au Pi dès qu'il est de nouveau joignable. */
  async queuePlay(id) {
    if (!this.ready) return;
    const tx = this.db.transaction("plays", "readwrite");
    tx.objectStore("plays").add({ id, at: Date.now() / 1000 });
    await done(tx).catch(() => {});
  }

  async flushPlays() {
    if (!this.ready || this._flushing) return;
    this._flushing = true;
    try {
      const store = () => this.db.transaction("plays", "readwrite").objectStore("plays");
      const keys = await wrap(store().getAllKeys());
      for (const k of keys) {
        const p = await wrap(store().get(k));
        if (p) {
          try { await api.post(`/api/tracks/${p.id}/played?at=${Math.round(p.at)}`); } catch (e) {
            if (!/introuvable|404/i.test(e.message)) break; // encore hors ligne : on garde pour plus tard
          }
        }
        await wrap(store().delete(k));
      }
    } catch { /* on réessaiera au prochain retour en ligne */ }
    this._flushing = false;
  }

  _emit(key = null) {
    // regroupe les notifications (une par image au plus)
    this._pendingKeys ||= new Set();
    if (key) this._pendingKeys.add(key);
    if (this._emitT) return;
    this._emitT = requestAnimationFrame(() => {
      this._emitT = 0;
      const keys = [...this._pendingKeys];
      this._pendingKeys.clear();
      this.dispatchEvent(new CustomEvent("change", { detail: { keys } }));
    });
  }
}

export const offline = new Offline();


// ---------------------------------------------------------------- hors connexion automatique
// Sur le Wi-Fi, l'appareil garde tout seul tes titres likés et tes dernières écoutes : en 4G qui capte mal
// (ou sans réseau du tout), ils se lisent depuis l'appareil.
const AUTO_KEY = "onde.autoOffline";
const isMobileUA = () => /android|iphone|ipad/i.test(navigator.userAgent);
export const autoOffline = {
  get on() { return storage.get(AUTO_KEY, isMobileUA()); },
  set(on) { storage.set(AUTO_KEY, !!on); if (on) this.check(); },
  /** Wi-Fi ? (Android le dit ; sinon : connecté au serveur par son adresse de la maison = Wi-Fi de la maison) */
  onWifi() {
    const c = navigator.connection;
    if (c && c.type) return c.type === "wifi" || c.type === "ethernet";
    return /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|localhost$|127\.)|\.local$/.test(location.hostname);
  },
  async check() {
    if (!this.on || !offline.ready || !offline.supported || offline.active || !this.onWifi()) return;
    for (const key of ["liked", "recent"]) {
      if (offline.collections.has(key)) continue;
      try { await offline.add(key); } catch { /* rien à garder pour l'instant, ou plus de place */ }
    }
  },
  start() {
    setTimeout(() => this.check(), 15000);
    setInterval(() => this.check(), 30 * 60 * 1000);
    navigator.connection?.addEventListener?.("change", () => this.check());
  },
};
