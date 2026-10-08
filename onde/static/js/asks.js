// « Mon espace et mes demandes » (profils non administrateurs) : demander un titre ou plus d'espace.
import { api } from "./api.js";
import { $, esc, icon, fmtRel } from "./util.js";
import { toast, toastError } from "./ui.js";
import { currentUser, quotaText } from "./profiles.js";

const STATUS = {
  pending: ["En attente", "wait"], downloading: ["Accepté · téléchargement…", "ok"], done: ["Accepté", "ok"],
  refused: ["Refusé", "no"], failed: ["Échec", "no"],
};

export function statusChip(a) {
  const [label, cls] = STATUS[a.status] || [a.status, ""];
  return `<span class="ask-st ${cls}">${label}${a.decidedBy === "auto" ? " (mini IA)" : ""}</span>`;
}

export function countdown(a, now = Date.now() / 1000) {
  if (!a.autoAt || a.status !== "pending") return "";
  const left = Math.max(0, Math.round(a.autoAt - now));
  return left ? `la mini IA décide dans ${Math.floor(left / 60)} min ${String(left % 60).padStart(2, "0")}` : "la mini IA décide…";
}

/** Demande un titre (depuis la recherche ou cette page). */
export async function askTrack(query) {
  const a = await api.post("/api/asks", { query });
  toast("Demande envoyée à l'admin");
  return a;
}

export async function asksView(el) {
  document.title = "Mes demandes · Onde";
  if (currentUser().role === "admin") { location.hash = "#/dashboard"; return; }
  el.innerHTML = `<div class="page asks-page">
    <h1 class="page-title">Mon espace et mes demandes</h1>
    <section class="card-box">
      <div class="sec-head"><h2>${icon("upload")} Demander un titre</h2></div>
      <p class="muted" style="margin:0 0 12px">L'admin accepte ou refuse. S'il ne répond pas dans les 5 minutes, une mini IA décide : elle regarde le nom, les paroles, et si l'artiste est déjà dans la bibliothèque.</p>
      <form class="ask-form" id="ask-form"><input class="field" name="q" maxlength="200" required placeholder="Artiste - Titre (ex. Bigflo et Oli - Alors alors)" autocomplete="off">
        <button class="btn white" type="submit">Demander</button></form>
    </section>
    <section class="card-box">
      <div class="sec-head"><h2>${icon("disc")} Mon espace</h2></div>
      <div id="ask-quota" class="muted">…</div>
    </section>
    <section class="card-box"><div class="sec-head"><h2>Mes demandes</h2></div><div id="ask-list" class="prof-list">…</div></section>
  </div>`;
  const load = async () => {
    const [q, r] = await Promise.all([api.get("/api/me/quota"), api.get("/api/asks")]);
    const qbox = $("#ask-quota", el);
    if (qbox) {
      qbox.innerHTML = `<p style="margin:0 0 8px">${esc(quotaText(q))} ${q.tracks ? `(${q.tracks} titre${q.tracks > 1 ? "s" : ""} ajouté${q.tracks > 1 ? "s" : ""} par toi)` : ""}</p>
        ${q.limit != null ? `<div class="q-bar${q.full ? " full" : ""}"><i style="width:${q.pct || 0}%"></i></div>
        <div class="ask-form" style="margin-top:12px"><select class="select" id="ask-mb"><option value="1000">+1 Go</option><option value="2000" selected>+2 Go</option><option value="5000">+5 Go</option><option value="10000">+10 Go</option></select>
        <button class="btn" data-ask-quota>Demander plus d'espace</button></div>` : ""}`;
    }
    const list = $("#ask-list", el);
    if (!list) return;
    list.innerHTML = r.asks.length ? r.asks.map((a) => `<div class="prof-row" data-ask="${a.id}">
        <div class="pr-main"><b>${a.kind === "quota" ? `Plus d'espace : +${a.amountMb} Mo` : esc(a.query)}</b>
          <small class="dim">${fmtRel(a.created)}${a.reason && a.status !== "pending" ? ` · ${esc(a.reason)}` : ""} <span data-cd="${a.autoAt || ""}">${countdown(a)}</span></small></div>
        ${statusChip(a)}
        ${a.status === "pending" ? `<button class="icon-btn" data-ask-cancel="${a.id}" title="Annuler">${icon("close", "sm")}</button>` : ""}</div>`).join("")
      : `<p class="muted">Aucune demande pour l'instant.</p>`;
  };
  el.addEventListener("submit", async (e) => {
    if (e.target.id !== "ask-form") return;
    e.preventDefault();
    const input = e.target.q;
    try { await askTrack(input.value); input.value = ""; await load(); } catch (err) { toastError(err); }
  });
  el.addEventListener("click", async (e) => {
    try {
      if (e.target.closest("[data-ask-quota]")) {
        await api.post("/api/me/quota-request", { amountMb: +$("#ask-mb", el).value });
        toast("Demande d'espace envoyée"); await load();
      }
      const c = e.target.closest("[data-ask-cancel]");
      if (c) { await api.del(`/api/asks/${c.dataset.askCancel}`); await load(); }
    } catch (err) { toastError(err); }
  });
  await load();
  const timer = setInterval(() => {
    el.querySelectorAll("[data-cd]").forEach((s) => { const at = +s.dataset.cd; if (at) s.textContent = countdown({ autoAt: at, status: "pending" }); });
  }, 1000);
  const poll = setInterval(() => load().catch(() => {}), 15000);
  return () => { clearInterval(timer); clearInterval(poll); };
}
