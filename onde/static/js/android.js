// Application Android (Onde.apk) : la page transmet l'état de lecture à l'appli (notification, écran verrouillé,
// casque, voiture) et reçoit ses commandes. Sans l'appli (window.OndeApp absent), ce module ne fait rien.
import { api } from "./api.js";
import { artworkUrl, esc } from "./util.js";
import { toast } from "./ui.js";

export function bindAndroid(player) {
  const app = window.OndeApp;
  if (!app) return;
  document.documentElement.classList.add("in-app");
  window.OndeControl = {
    play: () => player.play(),
    pause: () => player.pause(),
    toggle: () => player.toggle(),
    next: () => player.next(),
    prev: () => player.prev(),
    seek: (s) => player.seek(s),
  };
  let last = "";
  let lastAt = 0;
  const push = (force = false) => {
    const t = player.current;
    const playing = !!t && !player.paused;
    const key = `${playing}|${t ? t.id : ""}`;
    const now = Date.now();
    if (!force && key === last && now - lastAt < 10_000) return; // la position se rafraîchit toutes les 10 s
    last = key;
    lastAt = now;
    let art = "";
    try { if (t && t.cover) art = new URL(artworkUrl(t.cover, 384), location.href).href; } catch { /* ignore */ }
    try {
      app.update(JSON.stringify({
        playing, title: t?.title || "", artist: t?.artist || "", album: t?.album || "", art,
        pos: Math.round((player.position || 0) * 1000), dur: Math.round((player.duration || 0) * 1000) || 0,
      }));
    } catch { /* appli plus ancienne : ignoré */ }
  };
  player.addEventListener("track", () => push(true));
  player.addEventListener("state", () => push(true));
  player.addEventListener("time", () => push());
  document.addEventListener("click", (e) => {
    if (e.target.closest("#app-change")) app.changeServer();
  });
  push(true);

  // minuterie de sommeil : notification quand elle arrive à son terme (pas quand on l'annule)
  let armed = 0;
  player.addEventListener("sleep", () => {
    const at = player.sleep && player.sleep.at;
    if (at) { armed = at; return; }
    if (armed && Date.now() >= armed - 1500) {
      try { app.notify("Minuterie terminée", "La musique s'est arrêtée. Bonne nuit !"); } catch { /* appli plus ancienne */ }
    }
    armed = 0;
  });

  // adresse à distance : si le serveur la connaît (./start.sh remote), l'appli la retient toute seule
  api.get("/api/remote").then((r) => {
    if (r && r.mode && r.mode !== "off" && r.url && typeof app.setRemote === "function") {
      let known = "";
      try { known = app.remote() || ""; } catch { /* ignore */ }
      if (known !== r.url) app.setRemote(r.url);
    }
  }).catch(() => {});
}

const NOTIF = [
  ["notif", "Notifications", "Paroles synchronisées, musique ajoutée, téléchargements terminés."],
  ["always", "Notifications même appli fermée", "Garde une petite icône discrète dans la barre d'état pour rester à l'écoute du serveur. Désactive l'optimisation de batterie pour Onde dans les réglages Android."],
  ["down", "Prévenir si Onde est injoignable", "Une notification si le serveur ne répond plus (et quand il revient)."],
];
const sw = (key, on) =>
  `<label class="switch"><input type="checkbox" data-app-pref="${key}"${on ? " checked" : ""}><span class="sw-track"><span class="sw-thumb"></span></span></label>`;

/** Bloc des Réglages (affiché seulement dans l'appli Android). */
export function androidSection() {
  const app = window.OndeApp;
  if (!app) return "";
  let server = "";
  let remote = "";
  let prefs = null;
  try { server = app.server(); } catch { /* ignore */ }
  try { remote = app.remote(); } catch { /* ignore */ }
  try { prefs = JSON.parse(app.prefs()); } catch { /* appli plus ancienne */ }
  let html = `<h2 class="set-title">Application Android</h2>
    <div class="settings-card">
      <div class="sc-text"><h3>Serveur Onde</h3><p>Connecté à <b>${esc(server)}</b>.${remote ? ` Adresse à distance : <b>${esc(remote)}</b>.` : ""} En voiture, passe sur le Wi-Fi « Onde-Music » du Pi, puis choisis « Changer de serveur » : l'appli le retrouve toute seule.</p></div>
      <button class="btn sm" id="app-change">Changer de serveur</button>
    </div>`;
  if (prefs) {
    html += NOTIF.map(([k, t, d]) => `<div class="settings-card">
      <div class="sc-text"><h3>${t}</h3><p>${d}${k === "notif" && prefs.canNotify === false ? `<br><b>Les notifications sont bloquées pour Onde dans Android</b> : autorise-les dans les réglages du téléphone.` : ""}</p></div>
      ${sw(k, prefs[k])}
    </div>`).join("");
  }
  return html;
}

export function bindAndroidSettings(el) {
  const app = window.OndeApp;
  if (!app) return;
  el.addEventListener("change", (e) => {
    const input = e.target.closest("[data-app-pref]");
    if (!input) return;
    try {
      app.setPref(input.dataset.appPref, input.checked);
      toast(`${NOTIF.find((n) => n[0] === input.dataset.appPref)[1]} : ${input.checked ? "activé" : "désactivé"}`);
    } catch { input.checked = !input.checked; toast("Mets à jour l'appli Onde pour changer ce réglage"); }
  });
}

/** Bloc des Réglages : accès à distance (Tailscale). */
export function remoteSection() {
  return `<h2 class="set-title">Accès à distance</h2><div id="remote-box"><div class="settings-card"><div class="sc-text"><p class="dim">Chargement…</p></div></div></div>`;
}

export function bindRemote(el) {
  const box = el.querySelector("#remote-box");
  if (!box) return;
  api.get("/api/remote").then((r) => {
    let body;
    let qr = "";
    if (!r.installed || !r.running || r.mode === "off") {
      body = `<p>Pour écouter ta musique hors de la maison (4G, autre Wi-Fi), lance sur le Raspberry Pi : <code>./start.sh remote</code> (privé, via l'appli Tailscale) ou <code>./start.sh remote public</code> (sans appli, mot de passe obligatoire).</p>`;
    } else {
      const pub = r.mode === "public";
      body = `<p>${pub ? "Accès <b>public</b> (tout Internet, protégé par le mot de passe)" : "Accès <b>privé</b> (seulement tes appareils où l'appli Tailscale est connectée)"} — en 4G/5G ou sur n'importe quel Wi-Fi :</p><div class="urls"><code>${esc(r.url)}</code></div>${r.warning ? `<p class="dim" style="margin-top:8px">${esc(r.warning)}</p>` : ""}
        <p class="dim" style="font-size:12px;margin-top:8px"><b>iPhone / iPad :</b> scanne le code avec l'appareil photo, ouvre dans Safari, puis <i>Partager → Sur l'écran d'accueil</i>.${pub ? "" : " Installe d'abord l'appli Tailscale (gratuite) et connecte-la au même compte."}<br><b>Android :</b> l'appli Onde retient cette adresse toute seule et bascule maison ⇄ distance sans rien faire.</p>`;
      qr = `<img class="qr" src="/api/qr.svg?text=${encodeURIComponent(r.url)}" alt="QR code vers ${esc(r.url)}" width="128" height="128">`;
    }
    box.innerHTML = `<div class="settings-card remote-card"><div class="sc-text"><h3>Écouter hors de chez toi</h3>${body}</div>${qr}</div>`;
  }).catch(() => { box.innerHTML = ""; });
}
