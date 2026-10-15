// Lecture synchronisée entre appareils du même profil : chaque appareil qui joue de la musique
// diffuse en continu ce qu'il écoute (titre, position, pause) ; un autre appareil peut choisir de
// « suivre » et se calque alors automatiquement sur cet état. Sa propre file d'attente est mise de
// côté quand le suivi la remplace, et revient quand on arrête de suivre.
import { api, ensureTracks } from "./api.js";
import { storage } from "./util.js";
import { toast } from "./ui.js";

const SAVED_KEY = "onde.syncSaved"; // file d'attente de cet appareil, gardée pendant le suivi
const MAX_AGE = 600;                // s : état plus ancien (appareil suivi arrêté depuis) → ignoré

let player = null;
let es = null;
let following = storage.get("onde.following", false);
let lastSent = 0;
let sendTimer = 0;
let lastRemoteTrack = null;
const listeners = new Set();
export const onSync = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
const notify = () => listeners.forEach((fn) => fn());

function guessDeviceName() {
  const saved = storage.get("onde.deviceName");
  if (saved) return saved;
  const ua = navigator.userAgent || "";
  const kind = /iphone/i.test(ua) ? "iPhone" : /ipad/i.test(ua) ? "iPad" : /android/i.test(ua) ? "Android"
    : /mac/i.test(ua) ? "Mac" : /win/i.test(ua) ? "PC" : "Appareil";
  const name = `${kind} · ${Math.random().toString(36).slice(2, 5)}`;
  storage.set("onde.deviceName", name);
  return name;
}
export const deviceName = guessDeviceName();

export function bindSync(p) {
  player = p;
  player.addEventListener("state", broadcastSoon);
  player.addEventListener("track", broadcastSoon);
  player.addEventListener("time", broadcastThrottled);
  if (following) connect();
  else storage.set(SAVED_KEY, null); // (reste d'un suivi déjà arrêté : il ne doit pas revenir au prochain arrêt)
}

function broadcastSoon() { clearTimeout(sendTimer); sendTimer = setTimeout(broadcast, 200); }
function broadcastThrottled() { if (Date.now() - lastSent > 4000) broadcast(); }

function broadcast() {
  if (following || !player || !player.currentId) return;
  lastSent = Date.now();
  api.post("/api/sync/broadcast", {
    trackId: player.currentId, position: player.position, paused: player.paused, device: deviceName,
  }).catch(() => {});
}

export const isFollowing = () => following;
export const lastRemote = () => lastRemoteTrack;

export function setFollowing(on) {
  following = on;
  storage.set("onde.following", on);
  if (on) connect(); else { disconnect(); restoreQueue(); }
  notify();
}

/** Garde la file d'attente de cet appareil avant que le suivi ne la remplace (une seule fois par suivi). */
function saveQueue() {
  if (player.source?.key === "sync" || storage.get(SAVED_KEY) || !player.queue.length) return;
  storage.set(SAVED_KEY, {
    queue: player.queue.slice(0, 3000), index: Math.max(0, player.index), pos: player.position, source: player.source,
  });
}

/** Arrêt du suivi : rend la file gardée par saveQueue(), là où elle en était. */
async function restoreQueue() {
  const saved = storage.get(SAVED_KEY);
  if (!saved || !player) return;
  try {
    await ensureTracks(saved.queue || []);
  } catch { /* hors connexion : le lecteur fera avec les titres qu'il connaît */ }
  if (following) return; // suivi relancé pendant le chargement : la file reste de côté
  storage.set(SAVED_KEY, null);
  // (autre chose lancé sur cet appareil pendant le suivi : on n'y touche pas)
  if (!Array.isArray(saved.queue) || !saved.queue.length || player.source?.key !== "sync") return;
  try {
    const playing = !player.paused;
    player.playList(saved.queue, saved.index || 0, saved.source || null);
    if (saved.pos > 1) player.seek(saved.pos);
    if (!playing) player.pause();
    toast("Ta file d'attente est revenue");
  } catch { /* le lecteur garde le titre suivi : rien n'est coupé */ }
}

function connect() {
  if (es || !player) return;
  es = new EventSource("/api/sync/events");
  es.onmessage = async (e) => {
    let s;
    try { s = JSON.parse(e.data); } catch { return; }
    // (`age` est calculé par le serveur : pas de comparaison avec l'horloge, parfois décalée, de cet appareil)
    if (s.age > MAX_AGE) return; // vieil état : il ne doit pas remplacer la file d'attente
    lastRemoteTrack = s;
    notify();
    if (!following || s.trackId == null) return;
    try {
      if (player.currentId !== s.trackId) {
        await ensureTracks([s.trackId]);
        if (!following) return; // suivi arrêté pendant le chargement
        saveQueue();
        player.playList([s.trackId], 0, { key: "sync", label: `Suivi : ${s.device}` });
      }
      const drift = Math.abs(player.position - (s.position || 0));
      if (drift > 2) player.seek(s.position || 0);
      if (s.paused && !player.paused) player.pause();
      else if (!s.paused && player.paused) player.play();
    } catch { /* appareil suivi pas encore prêt : on retentera au prochain événement */ }
  };
  es.onerror = () => { /* le navigateur reconnecte tout seul */ };
}

function disconnect() {
  es?.close();
  es = null;
}
