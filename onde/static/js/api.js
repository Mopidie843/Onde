// Accès à l'API + cache des titres (id -> objet titre).
export const trackCache = new Map();
export const bus = new EventTarget(); // événements globaux : library, liked, playlists

export const emit = (type, detail) => bus.dispatchEvent(new CustomEvent(type, { detail }));

function remember(data, depth = 0) {
  if (!data || depth > 2) return;
  if (Array.isArray(data)) {
    for (const item of data) {
      if (item && typeof item === "object" && "albumId" in item && "title" in item) {
        const prev = trackCache.get(item.id);
        trackCache.set(item.id, prev ? Object.assign(prev, item) : item);
      } else if (depth < 2) remember(item, depth + 1);
    }
  } else if (typeof data === "object") {
    for (const key of ["tracks", "topTracks", "track"]) {
      const v = data[key];
      if (Array.isArray(v)) remember(v, depth + 1);
      else if (v && typeof v === "object" && "albumId" in v) remember([v], depth + 1);
    }
  }
}

// Écoute hors connexion : offline.js branche ici ses crochets (évite un import circulaire).
//  fallback(path)       -> réponse gardée sur l'appareil pour ce GET, ou undefined
//  response(path, data) -> une réponse fraîche est arrivée (mise à jour des téléchargements)
//  failure()            -> le réseau vient d'échouer (vérifier si on est hors connexion)
//  isOffline()          -> vrai quand le serveur est injoignable : on ne tente même plus le réseau
const hooks = { fallback: null, response: null, failure: null, isOffline: () => false };
export function setOfflineHooks(h) { Object.assign(hooks, h); }
export const OFFLINE_MSG = "Tu es hors connexion : seule ta musique téléchargée est disponible.";

async function local(method, path) {
  if (method !== "GET" || !hooks.fallback) return undefined;
  const data = await hooks.fallback(path);
  if (data !== undefined) remember(data);
  return data;
}

// Réseau faible (4G qui capte mal, Wi-Fi en limite) : une lecture (GET) ratée est retentée deux fois
// avant d'abandonner — une seule coupure d'une seconde ne doit pas faire croire que le serveur est perdu.
const RETRY_DELAYS = [700, 1800];
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
async function fetchRetry(method, path, init) {
  for (let i = 0; ; i++) {
    try {
      return await fetch(path, init);
    } catch (err) {
      if (method !== "GET" || i >= RETRY_DELAYS.length || init.signal?.aborted || navigator.onLine === false) throw err;
      await pause(RETRY_DELAYS[i]);
    }
  }
}

// Pages principales gardées en mémoire : sur un réseau qui traîne (VPN, 4G), la page s'affiche avec la
// dernière copie au bout de 1,8 s au lieu d'attendre (la vraie réponse remplace la copie pour la suite).
const PAGE_RE = /^\/api\/(home|info|genres(\/[^/]+)?|playlists(\/\d+)?|smart|albums(\/\d+)?|artists(\/[^/]+)?|liked|tracks|me\/quota)$/;
const PAGE_WAIT = 1800;
const pageCache = new Map();
export function forgetPages() { pageCache.clear(); }
const clone = (d) => { try { return structuredClone(d); } catch { return JSON.parse(JSON.stringify(d)); } };

function req(method, path, body, headers) {
  const bare = path.split("?")[0];
  if (method !== "GET" || !PAGE_RE.test(bare) || hooks.isOffline()) {
    // une modification (like, playlist…) : les copies ne sont plus sûres
    if (method !== "GET" && !/\/(played|resume|sync|dev|jam|pi|asks|me\/quota-request)\b/.test(bare)) pageCache.clear();
    return reqNet(method, path, body, headers);
  }
  const fresh = reqNet(method, path, body, headers).then((d) => { pageCache.set(path, clone(d)); return d; });
  const q = path.includes("?") ? new URLSearchParams(path.split("?")[1]) : null;
  const hit = pageCache.get(path) ?? (q && [...q.keys()].every((k) => k === "sort") ? pageCache.get(bare) : undefined);
  if (hit === undefined) return fresh;
  fresh.catch(() => {});
  return Promise.race([fresh.catch(() => clone(hit)), pause(PAGE_WAIT).then(() => clone(hit))]);
}

async function reqNet(method, path, body, headers = {}) {
  if (hooks.isOffline()) {
    const data = await local(method, path);
    if (data !== undefined) return data;
    throw new Error(OFFLINE_MSG);
  }
  const init = { method, headers: { ...headers } };
  if (body instanceof Blob) {
    init.body = body;
    init.headers["Content-Type"] = body.type || "application/octet-stream";
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers["Content-Type"] = "application/json";
  }
  let res;
  try {
    res = await fetchRetry(method, path, init);
  } catch {
    hooks.failure?.();
    const data = await local(method, path);
    if (data !== undefined) return data;
    throw new Error("Serveur injoignable — vérifie que le Raspberry Pi est allumé.");
  }
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("Connexion requise");
  }
  const isJson = (res.headers.get("content-type") || "").includes("json");
  const data = isJson ? await res.json() : await res.text();
  if (!res.ok) {
    const msg = (isJson && (data.detail || data.error)) || `Erreur ${res.status}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  remember(data);
  if (method === "GET") hooks.response?.(path, data);
  return data;
}

export const api = {
  get: (p) => req("GET", p),
  post: (p, b, h) => req("POST", p, b ?? {}, h),
  put: (p, b) => req("PUT", p, b ?? {}),
  patch: (p, b) => req("PATCH", p, b),
  del: (p) => req("DELETE", p),
};

export async function ensureTracks(ids) {
  const missing = [...new Set(ids)].filter((id) => id != null && !trackCache.has(id));
  for (let i = 0; i < missing.length; i += 400) {
    await api.get(`/api/tracks/by-ids?ids=${missing.slice(i, i + 400).join(",")}`);
  }
}

export async function toggleLike(id) {
  const t = trackCache.get(id);
  const liked = !(t && t.liked);
  if (t) t.liked = liked;
  emit("liked", { id, liked });
  try {
    await (liked ? api.put(`/api/liked/${id}`) : api.del(`/api/liked/${id}`));
  } catch (e) {
    if (t) t.liked = !liked;
    emit("liked", { id, liked: !liked });
    throw e;
  }
  return liked;
}

// Contextes lisibles (album:3, playlist:2, artist:Nom, liked, genre:Rock, tracks)
export async function contextTracks(key) {
  const [kind, ...rest] = key.split(":");
  const arg = rest.join(":");
  switch (kind) {
    case "album": { const a = await api.get(`/api/albums/${arg}`); return { tracks: a.tracks, label: a.name, href: `#/album/${arg}`, kind: "l'album" }; }
    case "playlist": { const p = await api.get(`/api/playlists/${arg}`); return { tracks: p.tracks, label: p.name, href: `#/playlist/${arg}`, kind: "la playlist" }; }
    case "artist": { const a = await api.get(`/api/artists/${encodeURIComponent(arg)}`); return { tracks: a.tracks, label: a.name, href: `#/artist/${encodeURIComponent(arg)}`, kind: "l'artiste" }; }
    case "genre": { const g = await api.get(`/api/genres/${encodeURIComponent(arg)}`); return { tracks: g.tracks, label: g.name, href: `#/genre/${encodeURIComponent(arg)}`, kind: "le genre" }; }
    case "liked": return { tracks: await api.get("/api/liked"), label: "Titres likés", href: "#/liked", kind: "la playlist" };
    case "smart": { const p = await api.get(`/api/smart/${arg}`); return { tracks: p.tracks, label: p.name, href: `#/smart/${arg}`, kind: "la playlist" }; }
    case "mix": {
      const [mkind, ...margs] = arg.split(":");
      const m = await api.get(`/api/mix/${encodeURIComponent(mkind)}?arg=${encodeURIComponent(margs.join(":"))}`);
      return { tracks: m.tracks, label: m.name, href: "#/", kind: "le mix" };
    }
    case "radio": {
      const [rkind, ...rargs] = arg.split(":");
      const val = rargs.join(":");
      const param = rkind === "track" ? `track=${val}` : rkind === "album" ? `album=${val}` : rkind === "genre" ? `genre=${encodeURIComponent(val)}` : `artist=${encodeURIComponent(val)}`;
      const r = await api.get(`/api/radio?${param}&limit=50`);
      let tracks = r.tracks;
      if (rkind === "track") { await ensureTracks([+val]); const seed = trackCache.get(+val); if (seed) tracks = [seed, ...tracks]; }
      const label = rkind === "track" ? `Radio · ${trackCache.get(+val)?.title || "titre"}` : rkind === "album" ? "Radio de l'album" : `Radio · ${val}`;
      return { tracks, label, href: "#/", kind: "la radio" };
    }
    default: return { tracks: await api.get("/api/tracks?sort=added"), label: "Tous les titres", href: "#/tracks", kind: "la bibliothèque" };
  }
}
