// Mode soirée côté hôte : QR code à montrer, et les titres ajoutés par les invités arrivent dans la file.
import { api, ensureTracks, trackCache } from "./api.js";
import { $, esc, enc, icon, storage } from "./util.js";
import { openModal, toast, toastError } from "./ui.js";

let player = null;
let last = 0;
let timer = 0;
let active = false;
const listeners = new Set();
export const jamActive = () => active;
export const onJam = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
const changed = () => listeners.forEach((fn) => fn(active));

// (seul l'appareil qui a lancé la soirée reçoit les titres des invités, pas tes autres appareils)
const HOST_KEY = "onde.jamHost";

async function poll() {
  clearTimeout(timer);
  const mine = storage.get(HOST_KEY, null);
  if (!mine) { if (active) { active = false; changed(); } return; }
  try {
    const r = await api.get(`/api/jam?after=${last}&now=${player.currentId || 0}`);
    if (!r.active || r.token !== mine) { storage.set(HOST_KEY, null); if (active) { active = false; changed(); } return; }
    if (!active) { active = true; changed(); }
    if (r.items.length) {
      const ids = r.items.map((i) => i.trackId);
      await ensureTracks(ids).catch(() => {});
      player.addToQueue(ids);
      last = Math.max(last, ...r.items.map((i) => i.n));
      const t = trackCache.get(ids[ids.length - 1]);
      toast(r.items.length === 1 ? `${r.items[0].by} a ajouté « ${t?.title || "un titre"} »` : `${r.items.length} titres ajoutés par les invités`);
    }
  } catch { /* réseau : on réessaie */ }
  timer = setTimeout(poll, active ? 4000 : 60000);
}

export function bindJam(p) {
  player = p;
  poll();
}

export async function openJam() {
  let r;
  try { r = await api.post("/api/jam"); } catch (err) { toastError(err); return; }
  storage.set(HOST_KEY, r.token);
  active = true; changed(); poll();
  let base = location.origin;
  try { const rem = await api.get("/api/remote"); if (rem.mode === "public" && rem.url) base = rem.url.replace(/\/$/, ""); } catch { /* ignore */ }
  if (/^(localhost|127\.)/.test(location.hostname)) {
    try { const i = await api.get("/api/info"); if (i.urls?.[0]) base = i.urls[0].replace(/\/$/, ""); } catch { /* ignore */ }
  }
  const url = `${base}${r.path}`;
  openModal({
    title: "Mode soirée",
    body: `<div style="display:grid;gap:14px;justify-items:center;text-align:center">
      <img src="/api/qr.svg?text=${enc(url)}" alt="QR code" width="220" height="220" style="background:#fff;border-radius:14px;padding:10px">
      <p class="muted" style="margin:0">Tes potes scannent ce code (même Wi-Fi) : ils cherchent dans ta bibliothèque et ajoutent des titres à la file, <b>sans compte ni appli</b>. Ils ne peuvent rien supprimer.</p>
      <input class="field" readonly value="${esc(url)}" style="width:100%" onclick="this.select()"></div>`,
    foot: `<button class="btn danger" data-jam-stop>${icon("close")} Arrêter la soirée</button><button class="btn white" data-close>OK</button>`,
    onMount: (m, close) => {
      $("[data-jam-stop]", m).addEventListener("click", async () => {
        await api.del("/api/jam").catch(() => {});
        storage.set(HOST_KEY, null);
        active = false; changed(); close(); toast("Soirée terminée : le lien ne marche plus");
      });
    },
  });
}
