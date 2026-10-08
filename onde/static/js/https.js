// Réglages > Accès sécurisé (HTTPS) : interrupteur, redémarrage guidé, installation du certificat.
import { $, $$, esc, enc, icon } from "./util.js";
import { api } from "./api.js";
import { toast, toastError, openModal, confirmDialog } from "./ui.js";
import { offline } from "./offline.js";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function detectDevice() {
  const ua = navigator.userAgent || "";
  if (/iPhone|iPod/.test(ua)) return "iphone";
  if (/iPad/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)) return "iphone"; // iPadOS
  if (/Android/i.test(ua)) return "android";
  if (/Windows/i.test(ua)) return "windows";
  if (/Macintosh|Mac OS X/.test(ua)) return "mac";
  return "windows";
}
const isPhone = (d) => d === "iphone" || d === "android";
const iosNotSafari = () => /CriOS|FxiOS|EdgiOS|OPiOS|GSA\//.test(navigator.userAgent);

const TABS = [["iphone", "iPhone / iPad"], ["android", "Android"], ["windows", "Windows"], ["mac", "Mac"]];

function steps(dev) {
  const dl = "<b>Télécharger le certificat</b>";
  if (dev === "iphone") return [
    `Dans <b>Safari</b>, touche ${dl}, puis <b>Autoriser</b>.`,
    `Ouvre <b>Réglages</b> : « <b>Profil téléchargé</b> » apparaît tout en haut. Touche-le, puis <b>Installer</b> (trois fois, avec ton code).`,
    `Va dans <b>Réglages › Général › Informations</b>, tout en bas <b>Réglages de confiance des certificats</b>, et active <b>Onde</b>. C'est fini !`,
  ];
  if (dev === "android") return [
    `Touche ${dl}.`,
    `Ouvre <b>Paramètres</b> et tape « <b>certificat CA</b> » dans la recherche (sinon : Sécurité › Chiffrement et identifiants › Installer un certificat › <b>Certificat CA</b>).`,
    `Touche <b>Installer quand même</b>, puis choisis le fichier <b>onde.crt</b>. C'est fini !`,
  ];
  if (dev === "mac") return [
    `Clique sur ${dl}, puis ouvre le fichier <b>onde.crt</b> : il s'ajoute à <b>Trousseaux d'accès</b>.`,
    `Dans Trousseaux d'accès, double-clique sur <b>Onde</b> › <b>Se fier</b> › « Lors de l'utilisation de ce certificat » : <b>Toujours approuver</b>.`,
    `Ferme la fenêtre (mot de passe du Mac), puis recharge Onde. C'est fini !`,
  ];
  return [
    `Clique sur ${dl}, puis ouvre le fichier <b>onde.crt</b>.`,
    `<b>Installer le certificat…</b> › <b>Utilisateur actuel</b> › « Placer tous les certificats dans le magasin suivant » › <b>Parcourir</b> › <b>Autorités de certification racines de confiance</b>.`,
    `<b>Suivant</b> › <b>Terminer</b> › <b>Oui</b>, puis ferme et rouvre ton navigateur. C'est fini !`,
  ];
}

function stepsHtml(dev) {
  const note = dev === "iphone" && iosNotSafari() && detectDevice() === "iphone"
    ? `<p class="cert-note">Ouvre cette page dans <b>Safari</b> : les autres navigateurs de l'iPhone ne savent pas installer de certificat.</p>` : "";
  return `${note}<ol class="cert-steps">${steps(dev).map((s) => `<li>${s}</li>`).join("")}</ol>`;
}

export function httpsSection(info) {
  const dev = detectDevice();
  const onServer = info.https && location.protocol === "http:"; // localhost : le serveur lui-même
  const desc = info.https
    ? `Activé : la connexion entre tes appareils et Onde est chiffrée. Les anciens liens en <code>http://</code> redirigent tout seuls.${onServer
      ? `<br><span class="dim">Ici, sur le serveur lui-même, Onde reste en http:// : la connexion ne sort pas de la machine, il n'y a rien à installer.</span>` : ""}`
    : `Chiffre la connexion sur ton réseau et affiche le cadenas dans le navigateur. Onde redémarre tout seul, en quelques secondes${info.tlsAvailable
      ? "" : " (la première fois, il installe d'abord le module nécessaire : il faut Internet, environ une minute)"}.`;
  const qr = info.certUrl && !isPhone(dev)
    ? `<div class="cert-qr"><img src="/api/qr.svg?text=${enc(info.certUrl)}" alt="QR code du certificat" width="104" height="104">
        <p>Pour ton <b>téléphone</b> : scanne ce code avec l'appareil photo, puis suis les étapes « iPhone » ou « Android ».</p></div>` : "";
  const guide = info.https ? `
    <div class="settings-card cert-guide">
      <div class="sc-text">
        <h3>Ne plus voir d'avertissement</h3>
        <p>Installe le certificat d'Onde <b>une fois</b> sur chaque appareil. Il reste valable même si l'adresse du Pi change.</p>
        <div class="cert-tabs" role="tablist">${TABS.map(([k, label]) =>
          `<button class="chip${k === dev ? " on" : ""}" role="tab" aria-selected="${k === dev}" data-cert-dev="${k}">${label}</button>`).join("")}</div>
        <div id="cert-steps">${stepsHtml(dev)}</div>
        <div class="cert-actions">
          <a class="btn primary" id="cert-dl" href="/onde.crt"${dev === "iphone" ? "" : " download"}>${icon("download")} Télécharger le certificat</a>
          ${qr}
        </div>
      </div>
    </div>` : "";
  return `<h2 class="set-title">Accès sécurisé (HTTPS)</h2>
    <div class="settings-card">
      <div class="sc-text"><h3>Connexion chiffrée</h3><p>${desc}</p></div>
      <label class="switch" aria-label="HTTPS"><input type="checkbox" id="https-toggle"${info.https ? " checked" : ""}><span class="sw-track"><span class="sw-thumb"></span></span></label>
    </div>${guide}`;
}

// Attend qu'Onde soit revenu avec le nouveau réglage, puis renvoie l'adresse à ouvrir.
async function waitForRestart(enabled) {
  const path = `${location.pathname}#/settings`;
  const deadline = Date.now() + 120000;
  await sleep(800);
  while (Date.now() < deadline) {
    try {
      const res = await fetch("/api/info", { cache: "no-store", redirect: "manual" });
      // la porte d'entrée renvoie désormais cette adresse vers l'autre protocole : Onde est prêt
      if (res.type === "opaqueredirect") return `${enabled ? "https" : "http"}://${location.host}${path}`;
      if (res.ok && (await res.json()).https === enabled) return `${location.protocol}//${location.host}${path}`;
    } catch { /* Onde redémarre */ }
    await sleep(500);
  }
  throw new Error("Onde met du temps à redémarrer. Recharge la page dans un instant.");
}

async function toggleHttps(input, info) {
  const enabled = input.checked;
  // l'adresse change (http <-> https) : le navigateur range les données par adresse exacte
  if (offline.hasDownloads && location.hostname !== "localhost" && location.hostname !== "127.0.0.1") {
    const ok = await confirmDialog({
      title: enabled ? "Activer le HTTPS ?" : "Désactiver le HTTPS ?",
      text: `La musique téléchargée sur cet appareil (${offline.urls.size} titres) est liée à l'adresse actuelle : après le changement, il faudra la télécharger à nouveau. Ta musique reste bien sûr sur le Raspberry Pi.`,
      ok: enabled ? "Activer" : "Désactiver", danger: false,
    });
    if (!ok) { input.checked = !enabled; return; }
  }
  input.disabled = true;
  const say = (html) => { const p = $("#https-step"); if (p) p.innerHTML = html; };
  const dlg = openModal({
    title: enabled ? "Activation du HTTPS" : "Désactivation du HTTPS",
    body: `<div class="spinner" id="https-spin"></div><p class="muted" id="https-step" style="text-align:center;margin:0 0 14px">${
      enabled && !info.tlsAvailable ? "Installation du module HTTPS… (jusqu'à une minute)" : "Enregistrement…"}</p><div id="https-go" style="text-align:center"></div>`,
    onClose: () => { input.disabled = false; },
  });
  try {
    const r = await api.post("/api/settings/https", { enabled });
    if (!r.restarting) {
      dlg.close();
      toast("Réglage enregistré — relance Onde pour l'appliquer.");
      return;
    }
    say("Onde redémarre…");
    const target = await waitForRestart(enabled);
    if (!target.startsWith("https:") || !enabled) { location.replace(target); if (target.split("#")[0] === location.href.split("#")[0]) location.reload(); return; }
    $("#https-spin")?.remove();
    say(`C'est prêt ! La première fois, ton navigateur peut afficher « <b>connexion non privée</b> » : c'est normal. Choisis
      « Paramètres avancés › Continuer » (ou « Afficher les détails › visiter ce site web » sur iPhone).
      Ensuite, installe le certificat depuis cette page pour ne plus jamais le voir.`);
    const go = $("#https-go");
    go.innerHTML = `<a class="btn primary" href="${esc(target)}">${icon("lock")} Ouvrir Onde en HTTPS</a>`;
    go.querySelector("a").addEventListener("click", (e) => { e.preventDefault(); location.replace(target); });
  } catch (err) {
    dlg.close();
    input.checked = !enabled;
    input.disabled = false;
    toastError(err);
  }
}

export function bindHttps(el, info) {
  const input = $("#https-toggle", el);
  input?.addEventListener("change", () => toggleHttps(input, info));
  el.addEventListener("click", (e) => {
    const tab = e.target.closest("[data-cert-dev]");
    if (!tab) return;
    const dev = tab.dataset.certDev;
    $$("[data-cert-dev]", el).forEach((b) => { const on = b === tab; b.classList.toggle("on", on); b.setAttribute("aria-selected", on); });
    $("#cert-steps", el).innerHTML = stepsHtml(dev);
    const dl = $("#cert-dl", el);
    if (dl) dl.toggleAttribute("download", dev !== "iphone" || detectDevice() !== "iphone");
  });
}
