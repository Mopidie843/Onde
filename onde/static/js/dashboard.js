// Tableau de bord de l'administrateur : connectés, en écoute, espace, demandes, erreurs d'import.
import { api } from "./api.js";
import { $, esc, icon, fmtRel, fmtSize, cover, plural } from "./util.js";
import { toast, toastError, promptDialog } from "./ui.js";
import { isAdmin, showResetLink } from "./profiles.js";
import { statusChip, countdown } from "./asks.js";

let page = null;
export function bindDashboard(ctx) { page = ctx; }

/** « Mozilla/5.0 (Linux; Android 14…) Chrome/… » → « Android · Chrome ». */
export function deviceName(ua) {
  if (!ua) return "appareil";
  if (/OndeApp|; wv\)/.test(ua)) return "Appli Android";
  const os = /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iPhone" : /Windows/.test(ua) ? "Windows"
    : /Mac OS X/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : "";
  const br = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /OPR\//.test(ua) ? "Opera"
    : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : /python|httpx/i.test(ua) ? "script" : "";
  return [os, br].filter(Boolean).join(" · ") || ua.slice(0, 30);
}

const pct = (a, b) => (b ? Math.min(100, Math.round((a * 100) / b)) : 0);

export async function dashboardView(el) {
  document.title = "Tableau de bord · Onde";
  page?.setColor?.("#1f2a44");
  if (!isAdmin()) { location.hash = "#/asks"; return; }
  el.innerHTML = `<div class="page dash-page"><h1 class="page-title">Tableau de bord</h1><div id="dash"><div class="spinner"></div></div></div>`;
  const box = $("#dash", el);
  let skew = 0; // horloge du serveur - horloge de cet appareil (pour le compte à rebours)
  const render = (d) => {
    skew = d.now - Date.now() / 1000;
    const disk = d.disk;
    const pending = d.asks.filter((a) => a.status === "pending");
    const others = d.asks.filter((a) => a.status !== "pending").slice(0, 15);
    box.innerHTML = `
      <div class="kpis">
        <div class="kpi"><span>Connectés</span><b>${new Set(d.online.map((o) => o.userId)).size}</b></div>
        <div class="kpi"><span>En train d'écouter</span><b>${d.playing.length}</b></div>
        <div class="kpi"><span>Écoutes aujourd'hui</span><b>${d.playsToday.toLocaleString("fr")}</b></div>
        <div class="kpi"><span>Demandes en attente</span><b${d.pendingAsks ? ' class="hot"' : ""}>${d.pendingAsks}</b></div>
      </div>

      ${d.pwreqs?.length ? `<section class="card-box"><div class="sec-head"><h2>${icon("lock")} Mots de passe oubliés</h2>
        <span class="muted">Crée un lien et envoie-le à ton pote : il choisit lui-même son nouveau mot de passe</span></div>
        <div class="prof-list">${d.pwreqs.map((r) => `<div class="prof-row">
          <div class="pr-main"><b>${esc(r.name)}</b><small class="dim">a oublié son mot de passe · ${fmtRel(r.created)}</small></div>
          <button class="btn sm white" data-pwlink="${r.userId}" data-name="${esc(r.name)}">${icon("link", "sm")} Créer le lien</button>
          <button class="btn sm" data-pwno="${r.id}">Ignorer</button></div>`).join("")}</div></section>` : ""}

      <section class="card-box"><div class="sec-head"><h2>${icon("upload")} Demandes</h2>
        <span class="muted">Sans réponse au bout de ${Math.round(d.autoDelay / 60)} min, la mini IA décide</span></div>
        ${pending.length ? `<div class="prof-list">${pending.map((a) => `<div class="prof-row" data-ask="${a.id}">
          <div class="pr-main"><b>${a.kind === "quota" ? `Plus d'espace : +${a.amountMb} Mo` : esc(a.query)}</b>
            <small class="dim">${esc(a.name)} · ${fmtRel(a.created)} <span data-cd="${a.autoAt || ""}">${a.autoAt ? countdown(a, Date.now() / 1000 + skew) : ""}</span></small>
            <small class="dim" data-ai-out></small></div>
          ${a.kind === "track" ? `<button class="btn sm ghost" data-ai="${a.id}" title="Ce que la mini IA déciderait">${icon("sparkles", "sm")} Avis IA</button>` : ""}
          <button class="btn sm white" data-ok="${a.id}">Accepter</button>
          <button class="btn sm" data-no="${a.id}">Refuser</button></div>`).join("")}</div>`
          : `<p class="muted">Aucune demande en attente.</p>`}
        ${others.length ? `<div class="q-sec" style="padding:12px 0 4px">Dernières réponses</div><div class="prof-list">${others.map((a) => `<div class="prof-row">
          <div class="pr-main"><b>${a.kind === "quota" ? `Espace +${a.amountMb} Mo` : esc(a.query)}</b><small class="dim">${esc(a.name)} · ${fmtRel(a.decided || a.created)}${a.reason ? ` · ${esc(a.reason)}` : ""}</small></div>
          ${statusChip(a)}</div>`).join("")}</div>` : ""}
      </section>

      <div class="two-col">
        <section class="card-box"><div class="sec-head"><h2>${icon("user")} Connectés</h2><span class="muted">10 dernières minutes</span></div>
          ${d.online.length ? `<div class="prof-list">${d.online.map((o) => `<div class="prof-row"><span class="dot-on"></span><div class="pr-main"><b>${esc(o.name)}</b>
            <small class="dim">${esc(deviceName(o.device))} ${o.ip ? `· ${esc(o.ip)}` : ""} · ${fmtRel(o.last)}</small></div></div>`).join("")}</div>`
            : `<p class="muted">Personne en ce moment.</p>`}
        </section>
        <section class="card-box"><div class="sec-head"><h2>${icon("play")} Ce qui s'écoute</h2></div>
          ${d.playing.length ? `<div class="prof-list">${d.playing.map((p) => `<div class="prof-row dash-np"><div class="dash-cv">${cover(p.track.cover, 96, p.track.title)}</div>
            <div class="pr-main"><b class="ell">${esc(p.track.title)}</b><small class="dim">${esc(p.track.artist)} · <b>${esc(p.name)}</b>${p.device ? ` sur ${esc(p.device.length > 40 ? deviceName(p.device) : p.device)}` : ""}</small></div></div>`).join("")}</div>`
            : `<p class="muted">Rien ne joue en ce moment.</p>`}
        </section>
      </div>

      <section class="card-box"><div class="sec-head"><h2>${icon("disc")} Espace</h2>
        <span class="muted">${plural(d.library.tracks, "titre", "titres")} · ${fmtSize(d.library.size)} de musique · ${plural(d.addedWeek, "ajout", "ajouts")} cette semaine</span></div>
        ${disk ? `<div class="dash-disk"><div class="q-bar${pct(disk.used, disk.total) > 90 ? " full" : ""}"><i style="width:${pct(disk.used, disk.total)}%"></i></div>
          <p class="dim" style="margin:6px 0 14px">Disque : ${fmtSize(disk.free)} libres sur ${fmtSize(disk.total)}</p></div>` : ""}
        <div class="prof-list">${d.users.map((u) => `<div class="prof-row"><div class="pr-main"><b>${esc(u.name)}</b>
            <small class="dim">${fmtSize(u.used)}${u.limit != null ? ` sur ${fmtSize(u.limit)}` : " · illimité"} · ${plural(u.tracks, "titre importé", "titres importés")}</small>
            ${u.limit != null ? `<div class="q-bar sm${u.full ? " full" : ""}"><i style="width:${u.pct || 0}%"></i></div>` : ""}</div>
          ${u.limit != null ? `<button class="btn sm" data-quota="${u.id}" data-mb="${u.limitMb}" data-name="${esc(u.name)}">Changer la limite</button>` : ""}</div>`).join("")}</div>
      </section>

      <section class="card-box"><div class="sec-head"><h2>${icon("close")} Erreurs d'import</h2></div>
        ${d.errors.length ? `<div class="prof-list">${d.errors.map((e) => `<div class="prof-row"><div class="pr-main"><b class="ell">${esc(e.detail)}</b><small class="dim">${esc(e.name)} · ${fmtRel(e.ts)}</small></div></div>`).join("")}</div>`
          : `<p class="muted">Aucune erreur récente.</p>`}
      </section>`;
  };
  const load = async () => render(await api.get("/api/dashboard"));
  el.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-ok],[data-no],[data-ai],[data-quota],[data-pwlink],[data-pwno]");
    if (!b) return;
    try {
      if (b.dataset.pwlink) {
        const r = await api.post(`/api/users/${b.dataset.pwlink}/reset-link`);
        showResetLink(r.name || b.dataset.name, r.path, r.expires);
      } else if (b.dataset.pwno) { await api.del(`/api/pwreqs/${b.dataset.pwno}`); toast("Demande ignorée"); }
      else if (b.dataset.ok) { await api.post(`/api/asks/${b.dataset.ok}/approve`); toast("Accepté"); }
      else if (b.dataset.no) {
        const reason = await promptDialog({ title: "Refuser la demande", label: "Raison (facultatif, il la verra)", ok: "Refuser", placeholder: "ex. déjà une autre version" });
        if (reason === null) return;
        await api.post(`/api/asks/${b.dataset.no}/refuse`, { reason });
        toast("Refusé");
      } else if (b.dataset.ai) {
        b.disabled = true;
        const v = await api.post(`/api/asks/${b.dataset.ai}/ai`);
        const out = $("[data-ai-out]", b.closest(".prof-row"));
        if (out) out.textContent = `Mini IA : ${v.ok ? "oui" : "non"} — ${v.reasons.join(" · ")}`;
        b.disabled = false;
        return;
      } else if (b.dataset.quota) {
        const v = await promptDialog({ title: `Espace de ${b.dataset.name}`, label: "Limite en Mo (vide = valeur par défaut)", value: b.dataset.mb, ok: "Enregistrer" });
        if (v === null) return;
        await api.patch(`/api/users/${b.dataset.quota}`, { quotaMb: v.trim() === "" ? null : Math.max(0, parseInt(v, 10) || 0) });
        toast("Limite changée");
      }
      await load();
    } catch (err) { toastError(err); b.disabled = false; }
  });
  await load();
  const tick = setInterval(() => {
    el.querySelectorAll("[data-cd]").forEach((s) => { const at = +s.dataset.cd; if (at) s.textContent = countdown({ autoAt: at, status: "pending" }, Date.now() / 1000 + skew); });
  }, 1000);
  const poll = setInterval(() => { if (!document.querySelector(".prof-row [data-ai-out]:not(:empty)")) load().catch(() => {}); }, 10000);
  return () => { clearInterval(tick); clearInterval(poll); };
}

/** Pastille « demandes en attente » sur le lien Tableau de bord (administrateur). */
let badgeTimer = 0, lastN = null;
export function startAskBadge() {
  if (badgeTimer) return;
  const run = async () => {
    const b = document.getElementById("ask-badge");
    if (!b || !isAdmin() || document.hidden) return;
    try {
      const [asks, pw] = await Promise.all([api.get("/api/asks"), api.get("/api/pwreqs").catch(() => ({ requests: [] }))]);
      const n = asks.asks.filter((a) => a.status === "pending").length + pw.requests.length;
      b.hidden = !n; b.textContent = n;
      if (lastN != null && n > lastN) { // quelque chose de nouveau à traiter
        toast(pw.requests.length ? `${pw.requests[0].name} a oublié son mot de passe : ouvre le Tableau de bord` : "Nouvelle demande : ouvre le Tableau de bord");
      }
      lastN = n;
    } catch { /* ignore */ }
  };
  run();
  badgeTimer = setInterval(run, 30000);
}
