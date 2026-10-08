// Reprise sur un autre appareil : chaque appareil dit régulièrement au serveur où il en est ;
// en ouvrant Onde ailleurs, on propose de reprendre au même endroit.
import { api } from "./api.js";
import { storage, fmtTime } from "./util.js";
import { toast } from "./ui.js";
import { deviceName } from "./sync.js";

const deviceId = (() => {
  let id = storage.get("onde.deviceId");
  if (!id) { id = Math.random().toString(36).slice(2, 12); storage.set("onde.deviceId", id); }
  return id;
})();
let player = null;
let lastSent = 0;
let lastKey = "";

function payload() {
  const i = player.index;
  const from = Math.max(0, i - 50);
  return {
    trackId: player.currentId, position: Math.round(player.position), queue: player.queue.slice(from, from + 300),
    index: i - from, device: deviceName, deviceId, source: player.source, playing: !player.paused,
  };
}

function send(force = false) {
  if (!player || player.mode !== "local" || !player.hasTrack) return;
  const now = Date.now();
  const key = `${player.currentId}:${player.paused}`;
  if (!force && key === lastKey && now - lastSent < 20000) return;
  lastSent = now;
  lastKey = key;
  storage.set("onde.resumeAt", now / 1000);
  api.post("/api/resume", payload()).catch(() => {});
}

export function bindResume(p) {
  player = p;
  player.addEventListener("track", () => send(true));
  player.addEventListener("state", () => send());
  player.addEventListener("time", () => { if (!player.paused) send(); });
  addEventListener("pagehide", () => {
    if (!player.hasTrack || player.mode !== "local") return;
    try { navigator.sendBeacon?.("/api/resume", new Blob([JSON.stringify(payload())], { type: "application/json" })); } catch { /* ignore */ }
  });
}

/** Au démarrage : un autre appareil jouait plus récemment ? On propose d'y reprendre. */
export async function offerResume() {
  if (!player || !player.paused) return;
  let r;
  try { r = (await api.get("/api/resume")).state; } catch { return; }
  if (!r || !r.track || r.deviceId === deviceId) return;
  const mine = storage.get("onde.resumeAt", 0);
  if (r.at <= mine || Date.now() / 1000 - r.at > 7 * 86400) return;
  if (player.currentId === r.trackId && Math.abs(player.position - r.position) < 15) return;
  toast(`Reprendre « ${r.track.title} » à ${fmtTime(r.position)} (écouté sur ${r.device || "un autre appareil"}) ?`, {
    timeout: 15000,
    action: {
      label: "Reprendre",
      onClick: () => {
        const queue = r.queue && r.queue.length ? r.queue : [r.trackId];
        player.playList(queue, r.index || 0, r.source || null);
        setTimeout(() => player.seek(r.position || 0), 400);
      },
    },
  });
}
