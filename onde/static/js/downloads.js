// Page « Téléchargés » (ta musique gardée sur cet appareil) — c'est aussi l'accueil hors connexion.
import { $, esc, icon, fmtSize, plural, cover, likedCover, mosaic, debounce, storage } from "./util.js";
import { offline } from "./offline.js";
import { emptyState, playlistCover, trackList } from "./components.js";
import { confirmDialog, toast, toastError } from "./ui.js";

let page = null;
export function bindDownloads(ctx) { page = ctx; }

export function hrefOf(key) {
  if (key === "liked") return "#/liked";
  if (key === "recent") return "#/stats";
  const [kind, id] = key.split(":");
  return `#/${kind}/${id}`;
}

function artOf(c, size = 384) {
  const m = c.meta || {};
  if (c.key === "liked") return likedCover();
  if (c.key.startsWith("album:")) return cover(m.cover, size, c.name, "disc", c.name);
  if (m.cover) return playlistCover(m, size);
  if (m.mosaic) return mosaic(m.mosaic, size, c.name);
  return mosaic((m.tracks || []).map((t) => t.cover).filter(Boolean).slice(0, 4), size, c.name);
}

function statusOf(c) {
  const p = offline.progress(c.key);
  if (p.state === "busy") {
    const why = offline.full ? "plus de place" : offline.active ? "en pause (hors connexion)" : "téléchargement…";
    return `${p.done}/${p.total} · ${why}`;
  }
  return `${plural(p.total, "titre", "titres")} · ${fmtSize(p.bytes)}`;
}

function card(c) {
  const p = offline.progress(c.key);
  return `<div class="card">
    <div class="cv">${artOf(c)}<button class="fab" data-play="${esc(c.key)}" aria-label="Lire">${icon("play")}</button>
      ${p.state === "busy" ? `<span class="dl-badge">${Math.round((p.done / Math.max(1, p.total)) * 100)} %</span>` : ""}</div>
    <a class="card-link" href="${hrefOf(c.key)}"><div class="c1 ell">${esc(c.name)}</div></a>
    <div class="c2">${esc(c.kind)} · ${statusOf(c)}</div>
  </div>`;
}

/** Peut-on ouvrir Onde sans le Pi ? (appli mise en cache par le service worker) */
export function appShellTip() {
  if (!("serviceWorker" in navigator)) return "";
  if (navigator.serviceWorker.controller) {
    return `<p class="dl-tip ok">${icon("check")}<span>Onde s'ouvre même sans le Pi. Sur téléphone, ajoute-le à l'écran d'accueil pour le retrouver facilement.</span></p>`;
  }
  if (!window.isSecureContext) {
    return `<p class="dl-tip">${icon("lock")}<span>Pour ouvrir Onde loin de chez toi, active le HTTPS (<a href="#/settings">Réglages › Accès sécurisé</a>) puis installe le certificat. En attendant, ta musique téléchargée se lit déjà ici, plus vite et sans coupure.</span></p>`;
  }
  return `<p class="dl-tip">${icon("lock")}<span>Pour ouvrir Onde loin de chez toi, installe le certificat d'Onde sur cet appareil (<a href="#/settings">Réglages › Accès sécurisé</a>), puis recharge la page une fois à la maison.</span></p>`;
}

export async function downloadsView(el) {
  document.title = offline.active ? "Hors connexion · Onde" : "Téléchargés · Onde";
  page.setColor(offline.active ? "#3a3340" : "#1d3a31");
  const render = () => {
    const cols = [...offline.collections.values()].sort((a, b) => b.at - a.at);
    const n = offline.urls.size;
    el.innerHTML = `<div class="page">
      ${offline.active ? `<div class="off-hero">${icon("offline")}<div><h1 class="page-title" style="margin:0">Hors connexion</h1>
        <p class="muted" style="margin:4px 0 0">Le Raspberry Pi n'est pas joignable. Voici ta musique gardée sur cet appareil.</p></div></div>`
        : `<h1 class="page-title">Téléchargés</h1>`}
      ${cols.length ? `<p class="muted dl-sum">${plural(n, "titre", "titres")} · ${fmtSize(offline.totalBytes)} sur cet appareil${offline.busy && !offline.active ? " · téléchargement en cours…" : ""}</p>` : ""}
      ${!offline.full && !offline.active && !offline._running && [...offline.failed.values()].some((n) => n >= 3)
        ? `<p class="dl-tip warn">${icon("close")}<span>Certains titres n'ont pas pu être téléchargés. <button class="linklike" data-dl-resume>Réessayer</button></span></p>` : ""}
      ${offline.full ? `<p class="dl-tip warn">${icon("close")}<span>Plus assez de place sur cet appareil. Libère de l'espace, puis <button class="linklike" data-dl-resume>reprends les téléchargements</button>.</span></p>` : ""}
      ${cols.length ? `<label class="search-box dl-search">${icon("search")}<input id="dl-q" type="search" placeholder="Chercher dans ta musique téléchargée" autocomplete="off" enterkeyhint="search" value="${esc(storage.get("onde.dlq", ""))}"></label><div id="dl-res"></div>` : ""}
      <div id="dl-cols">${cols.length ? `<div class="grid">${cols.map(card).join("")}</div>` : emptyState("download-circle", "Rien sur cet appareil pour l'instant",
        "Ouvre un album, une playlist ou tes titres likés et touche le bouton de téléchargement : tu pourras les écouter même sans le Pi (voiture, métro, voyage…).",
        offline.active ? "" : `<a class="btn white" href="#/library">Voir ma bibliothèque</a>`)}</div>
      ${appShellTip()}
      ${cols.length && !offline.active ? `<div style="margin-top:22px"><button class="btn ghost" data-dl-clear>${icon("trash")} Tout retirer de cet appareil</button></div>` : ""}
    </div>`;
    const q = $("#dl-q", el);
    if (q) {
      q.addEventListener("input", debounce(() => { storage.set("onde.dlq", q.value); search(q.value); }, 120));
      if (q.value) search(q.value);
    }
  };
  const norm = (x) => String(x || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase();
  function search(text) {
    const res = $("#dl-res", el);
    const colsEl = $("#dl-cols", el);
    const words = norm(text).split(/\s+/).filter(Boolean);
    if (!words.length) { res.innerHTML = ""; colsEl.hidden = false; return; }
    const hits = [...offline.urls.keys()].map((id) => offline.metas.get(id)).filter(Boolean)
      .filter((t) => { const hay = norm(`${t.title} ${t.artist} ${t.album}`); return words.every((w) => hay.includes(w)); })
      .slice(0, 200);
    colsEl.hidden = true;
    res.innerHTML = hits.length
      ? trackList(hits, { ctx: { key: "dl-search", label: "Recherche (téléchargés)", href: "#/downloads", kind: "tes téléchargements" }, head: false })
      : `<p class="muted" style="padding:8px 4px">Aucun titre téléchargé ne correspond.</p>`;
  }
  render();
  let last = 0;
  const onChange = () => {
    if ($("#dl-q", el)?.value) return; // ne pas effacer une recherche en cours
    const now = performance.now();
    if (now - last < 400) { clearTimeout(onChange.t); onChange.t = setTimeout(onChange, 420); return; }
    last = now;
    render();
  };
  offline.addEventListener("change", onChange);
  el.addEventListener("click", async (e) => {
    if (e.target.closest("[data-dl-resume]")) { offline.resume(); toast("Téléchargements relancés"); }
    if (e.target.closest("[data-dl-clear]")) {
      const ok = await confirmDialog({ title: "Tout retirer de cet appareil ?", text: `${fmtSize(offline.totalBytes)} seront libérés. Ta musique reste bien sûr sur le Raspberry Pi.`, ok: "Tout retirer" });
      if (!ok) return;
      try { await offline.removeAll(); toast("Musique retirée de cet appareil"); render(); } catch (err) { toastError(err); }
    }
  });
  return () => { offline.removeEventListener("change", onChange); clearTimeout(onChange.t); };
}

/** Page affichée hors connexion pour ce qui a besoin du Pi. */
export function offlineNotice(el) {
  document.title = "Hors connexion · Onde";
  page.setColor("#3a3340");
  el.innerHTML = `<div class="page">${emptyState("offline", "Pas disponible hors connexion",
    "Le Raspberry Pi n'est pas joignable pour l'instant. Ta musique téléchargée, elle, reste disponible.",
    offline.hasDownloads ? `<a class="btn white" href="#/downloads">Ma musique téléchargée</a>` : "")}</div>`;
}
