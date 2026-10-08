// « Onde a été mis à jour » : une carte moderne affichée une fois après chaque nouvelle version.
import { esc, icon, storage } from "./util.js";
import { openModal } from "./ui.js";

const NEWS = {
  "1.3.5": [
    ["sparkles", "Ton année", "Ton bilan d'écoute façon « Wrapped » : un bouton sur la page Statistiques ouvre tes tops, tes records et tes habitudes en diapositives."],
    ["refresh", "Mise à jour automatique plus fiable", "Onde retrouve le bon zip même si le dépôt publie aussi Onde.exe et Onde.apk."],
  ],
  "1.3.3": [
    ["lock", "Mot de passe oublié", "Le bouton de la page de connexion prévient l'admin par notification ; il crée un lien depuis le Tableau de bord."],
  ],
  "1.3.2": [
    ["link", "Mot de passe oublié", "Dans Profils, le bouton « lien » crée un lien (24 h, usage unique) à envoyer à un pote pour qu'il choisisse un nouveau mot de passe, sans mail."],
    ["speaker", "Le son ne coupe plus", "Si la lecture avance sans son, Onde le détecte et relance tout seul."],
  ],
  "1.3.1": [
    ["speaker", "Le son ne coupe plus", "Si la lecture avance sans son (changement de page), Onde le détecte et relance tout seul."],
  ],
  "1.3.0": [
    ["upload", "Demandes de titres", "Tes potes en écoute seule demandent un titre ; sans réponse en 5 min, une mini IA décide."],
    ["disc", "Espace par profil", "Chaque profil a sa limite d'espace, et peut demander plus."],
    ["link", "Partage de playlist", "Un lien d'écoute seule, sans compte, que tu peux couper quand tu veux."],
    ["chart", "Tableau de bord", "Qui est connecté, ce qui s'écoute, l'espace, les erreurs d'import."],
    ["sparkles", "Mix selon l'heure", "Énergique le matin, calme le soir."],
    ["refresh", "Plus fluide", "Pages gardées sur l'appareil et titre suivant préchargé : la musique ne coupe plus sur un réseau faible."],
  ],
};

export function showWhatsNew(info) {
  const v = info?.version;
  if (!v) return;
  const seen = storage.get("onde.version", null);
  storage.set("onde.version", v);
  if (!seen || seen === v) return; // (premier lancement sur cet appareil : rien à annoncer)
  const items = NEWS[v] || [];
  openModal({
    title: "Mise à jour installée",
    body: `<div class="wn-hero"><div class="wn-badge">${icon("sparkles")}</div><div><b>Onde ${esc(v)}</b><span>installé automatiquement · avant : ${esc(seen)}</span></div></div>
      ${items.length ? `<div class="wn-list">${items.map(([ic, t, d]) => `<div class="wn-item"><span class="wn-ic">${icon(ic)}</span><div><b>${esc(t)}</b><p>${esc(d)}</p></div></div>`).join("")}</div>` : ""}`,
    foot: `<button class="btn white" data-close>Super</button>`,
  });
}
