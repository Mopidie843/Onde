// Service worker d'Onde : garde l'appli elle-même (page, scripts, styles, icônes) sur l'appareil pour
// qu'elle s'ouvre même quand le Raspberry Pi n'est pas joignable. La musique téléchargée, elle, est
// dans IndexedDB (voir offline.js) : ce fichier ne touche jamais à l'API, aux flux audio ni à la
// connexion — seulement aux fichiers de l'appli, toujours pris en priorité sur le réseau (donc à
// jour), la copie locale ne servant que si le Pi ne répond pas.
const VERSION = "__VERSION__";
const CACHE = `onde-app-${VERSION}`;
const ASSETS = __ASSETS__;
const NETWORK_TIMEOUT = 4000;
// Pages principales (accueil, bibliothèque, playlists, albums, artistes…) : gardées sur l'appareil.
// Réseau d'abord, mais si le réseau traîne (VPN, 4G faible) on affiche tout de suite la dernière copie
// au lieu d'attendre — et le téléchargement continue en fond pour la mettre à jour.
const DATA = "onde-data-v1";
const DATA_RE = /^\/api\/(home|info|genres(\/[^/]+)?|playlists(\/\d+)?|smart|albums(\/\d+)?|artists(\/[^/]+)?|liked|tracks|me\/quota)$/;
const DATA_TIMEOUT = 1800;

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await cache.addAll(ASSETS.map((u) => new Request(u, { cache: "reload" })));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith("onde-app-") && key !== CACHE) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

async function networkFirst(request, cacheKey) {
  const cache = await caches.open(CACHE);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), NETWORK_TIMEOUT);
  try {
    const res = await fetch(request, { signal: ctrl.signal });
    clearTimeout(timer);
    if (res.ok && !res.redirected && res.type === "basic") cache.put(cacheKey || request, res.clone());
    return res;
  } catch {
    clearTimeout(timer);
    const hit = (await cache.match(cacheKey || request)) || (await cache.match(request, { ignoreSearch: true }));
    return hit || Response.error();
  }
}

async function dataFirst(event) {
  const request = event.request;
  const cache = await caches.open(DATA);
  const net = fetch(request).then((res) => {
    if (res.ok && res.type === "basic") cache.put(request, res.clone());
    return res;
  });
  event.waitUntil(net.catch(() => null)); // (la mise à jour continue même si la copie a déjà été montrée)
  const q = new URL(request.url).searchParams;
  const sortOnly = [...q.keys()].every((k) => k === "sort");
  const hit = (await cache.match(request)) || (sortOnly ? await cache.match(request, { ignoreSearch: true }) : undefined);
  if (!hit) return net.catch(() => Response.error());
  const late = new Promise((r) => setTimeout(() => r(null), DATA_TIMEOUT));
  try {
    const res = await Promise.race([net, late]);
    return res || hit;
  } catch {
    return hit;
  }
}

self.addEventListener("message", (event) => {
  if (event.data?.type === "forget-data") event.waitUntil(caches.delete(DATA));
});

// une modification (mot de passe, like, playlist…) : les copies des pages ne sont plus sûres
const FREQUENT = /\/(played|resume|sync|dev|jam|pi|asks|me\/quota-request)\b/;

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const path = url.pathname;
  if (req.method !== "GET") {
    if (path.startsWith("/api/") && !FREQUENT.test(path)) {
      event.waitUntil(caches.delete(DATA).catch(() => false)); // (la requête elle-même part normalement)
    }
    return;
  }
  if (req.mode === "navigate") {
    if (path === "/login" || path === "/logout") { event.waitUntil(caches.delete(DATA)); return; } // (autre profil)
    if (path === "/" || path === "/index.html") event.respondWith(networkFirst(req, "/"));
    return;
  }
  if (DATA_RE.test(path) && req.cache !== "no-store") { event.respondWith(dataFirst(event)); return; }
  if (path.startsWith("/static/") || path === "/manifest.webmanifest") event.respondWith(networkFirst(req));
});
