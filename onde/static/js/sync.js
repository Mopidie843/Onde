// Lecture synchronisée entre appareils du même réseau : chaque appareil qui joue de la musique
// diffuse en continu ce qu'il écoute (titre, position, pause) ; un autre appareil peut choisir de
// « suivre » et se calque alors automatiquement sur cet état, sans toucher à sa propre file d'attente.
import { api, ensureTracks } from "./api.js";
import { storage } from "./util.js";
import { toast } from "./ui.js";

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
  if (on) connect(); else disconnect();
  notify();
}

function connect() {
  if (es || !player) return;
  es = new EventSource("/api/sync/events");
  es.onmessage = async (e) => {
    let s;
    try { s = JSON.parse(e.data); } catch { return; }
    lastRemoteTrack = s;
    notify();
    if (!following || s.trackId == null) return;
    try {
      if (player.currentId !== s.trackId) {
        await ensureTracks([s.trackId]);
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
