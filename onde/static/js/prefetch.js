// Préchargement des pages en arrière-plan : accueil, bibliothèque, playlists, albums récents… Chaque réponse
// est gardée par api.js (mémoire + IndexedDB) : les onglets s'affichent tout de suite, et l'appli reste
// utilisable sans réseau avec les dernières données vues. Discret : un seul appel à la fois, jamais sur un
// réseau économe ou lent, jamais hors connexion.
import { api } from "./api.js";
import { offline } from "./offline.js";
import { slowNet } from "./util.js";

const BASE = ["/api/home", "/api/info", "/api/playlists", "/api/smart", "/api/liked", "/api/genres",
  "/api/albums?sort=added", "/api/artists?sort=name"];
const MAX_DETAILS = 60;         // pages de détail préchargées par passage (playlists, albums récents…)
const GAP = 150;                // pause entre deux appels (ms)
const EVERY = 30 * 60 * 1000;
let running = false;
let timer = 0;

const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const canRun = () => !offline.active && !document.hidden && !slowNet() && navigator.onLine !== false;

async function warm(path) {
  if (!canRun()) throw new Error("stop");
  try { return await api.get(path); } catch (e) { if (offline.active) throw e; return null; }
}

export async function warmPages() {
  if (running || !canRun()) return;
  running = true;
  try {
    const got = {};
    for (const path of BASE) { got[path] = await warm(path); await pause(GAP); }
    const details = [];
    for (const p of got["/api/playlists"] || []) details.push(`/api/playlists/${p.id}`);
    for (const p of got["/api/smart"] || []) details.push(`/api/smart/${p.id}`);
    const home = got["/api/home"] || {};
    const albumIds = new Set();
    for (const a of [...(home.recentAlbums || []), ...(home.recentlyPlayed || [])]) if (a && a.id) albumIds.add(a.id);
    for (const a of (got["/api/albums?sort=added"] || []).slice(0, 30)) albumIds.add(a.id);
    for (const id of albumIds) details.push(`/api/albums/${id}`);
    for (const path of details.slice(0, MAX_DETAILS)) { await warm(path); await pause(GAP); }
  } catch { /* interrompu (hors connexion, page masquée…) : au prochain passage */ }
  running = false;
}

export function startPrefetch() {
  const later = (ms) => { clearTimeout(timer); timer = setTimeout(async () => { await warmPages(); later(EVERY); }, ms); };
  later(6000);  // (laisse d'abord la page s'afficher)
  addEventListener("online", () => later(3000));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) later(4000); });
}
