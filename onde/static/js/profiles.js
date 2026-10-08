// Profils : « Mon profil » (tout le monde) et « Profils et invitations » (administrateur).
import { api } from "./api.js";
import { $, esc, enc, icon, fmtRel, plural } from "./util.js";
import { openModal, toast, toastError, confirmDialog } from "./ui.js";

let me = { id: 1, name: "", role: "admin" };
let roles = { admin: "Administrateur", member: "Écoute + import", listener: "Écoute seule" };

/** Profil connecté (renvoyé par /api/info) : classes sur <body> pour masquer ce qui n'est pas permis. */
export function setMe(info) {
  me = info?.me || me;
  roles = info?.roles || roles;
  document.body.classList.remove("role-admin", "role-member", "role-listener");
  document.body.classList.add(`role-${me.role}`);
  document.body.classList.toggle("accounts", !!info?.password);
}
export const currentUser = () => me;
export const isAdmin = () => me.role === "admin";
export const canImport = () => me.role === "admin" || me.role === "member";

const roleChip = (r) => `<span class="chip role-chip role-${r}" style="pointer-events:none">${esc(roles[r] || r)}</span>`;

export function profileSection(info) {
  const accounts = !!info.password;
  const admin = me.role === "admin";
  return `<h2 class="set-title">Mon profil</h2>
    <div class="settings-card">
      <div class="sc-text"><h3>${esc(me.name)} ${roleChip(me.role)}</h3><p>${accounts
        ? `${me.email ? `Mail : ${esc(me.email)} · ` : "Pas de mail (facultatif). "}Tes likes, playlists, historique et statistiques sont à toi.`
        : admin ? "<b>Accès libre :</b> tout appareil qui ouvre Onde peut tout faire. Donne-toi un mot de passe pour activer les profils : chacun se connectera avec son pseudo, et tu pourras inviter des potes avec des droits limités."
          : ""}</p></div>
      <div class="sc-btns">
        <button class="btn" data-prof="edit">${icon("user")} Pseudo / mail</button>
        <button class="btn${accounts ? "" : " white"}" data-prof="pwd">${icon("lock")} ${me.hasPassword ? "Changer le mot de passe" : "Choisir un mot de passe"}</button>
        ${accounts ? `<a class="btn ghost" href="/logout" data-prof="logout">${icon("close")} Se déconnecter</a>` : ""}
      </div>
    </div>
    ${admin ? `<a class="settings-card link" href="#/dashboard"><div class="sc-text"><h3>${icon("chart")} Tableau de bord</h3>
      <p>Qui est connecté, ce qui s'écoute, l'espace de chacun, les demandes de titres et les erreurs d'import.</p></div><span class="btn white">Ouvrir</span></a>`
      : `<a class="settings-card link" href="#/asks"><div class="sc-text"><h3>${icon("upload")} Mon espace et mes demandes</h3>
      <p><span data-myquota>…</span> Demande un titre à l'admin, ou plus d'espace.</p></div><span class="btn white">Ouvrir</span></a>`}`;
}

/** « 1,2 Go sur 5 Go » (ou « illimité »). */
export function quotaText(q) {
  if (!q) return "";
  const size = (n) => (n >= 1073741824 ? `${(n / 1073741824).toFixed(1).replace(".", ",")} Go` : `${Math.round(n / 1048576)} Mo`);
  return q.limit == null ? `${size(q.used)} importés (illimité).` : `Espace : ${size(q.used)} sur ${size(q.limit)}${q.full ? " — plein" : ""}.`;
}

export function adminSection(info) {
  if (me.role !== "admin") return "";
  return `<h2 class="set-title">Profils et invitations</h2>
    <div class="settings-card" style="flex-wrap:wrap">
      <div class="sc-text" style="flex-basis:100%"><h3>Qui a accès à Onde</h3>
        <p>Une seule bibliothèque pour tous. <b>Écoute + import</b> : écouter, liker, faire ses playlists, ajouter de la musique — sans rien supprimer ni toucher aux réglages. <b>Écoute seule</b> : sans import.</p>
        <div id="prof-list" class="prof-list">…</div>
        <div id="inv-list" class="prof-list"></div></div>
      <div class="sc-btns">
        <button class="btn white" data-prof="invite" ${info.password ? "" : "disabled title=\"Choisis d'abord ton mot de passe\""}>${icon("link")} Inviter un pote</button>
        <button class="btn" data-prof="add" ${info.password ? "" : "disabled"}>${icon("plus")} Créer un profil</button>
        <button class="btn ghost" data-prof="journal">${icon("list")} Journal d'activité</button>
      </div>
    </div>`;
}

function profRow(u) {
  const self = u.id === me.id;
  return `<div class="prof-row" data-uid="${u.id}">
    <div class="pr-main"><b>${esc(u.name)}</b>${self ? ' <span class="dim">(toi)</span>' : ""}
      <small class="dim">${u.email ? esc(u.email) + " · " : ""}${u.lastSeen ? `vu ${fmtRel(u.lastSeen)}` : "jamais connecté"} · ${plural(u.plays, "écoute", "écoutes")}</small></div>
    ${u.id === 1 ? roleChip("admin") : `<select class="select sm" data-role="${u.id}">${Object.entries(roles).map(([k, v]) => `<option value="${k}"${k === u.role ? " selected" : ""}>${esc(v)}</option>`).join("")}</select>`}
    ${self ? "" : `<button class="icon-btn" data-ppwd="${u.id}" title="Nouveau mot de passe">${icon("lock", "sm")}</button>
      <button class="icon-btn" data-prst="${u.id}" title="Lien « mot de passe oublié » à lui envoyer">${icon("link", "sm")}</button>
      <button class="icon-btn" data-pout="${u.id}" title="Déconnecter ses appareils">${icon("close", "sm")}</button>
      ${u.id === 1 ? "" : `<button class="icon-btn" data-pdel="${u.id}" title="Supprimer le profil">${icon("trash", "sm")}</button>`}`}
  </div>`;
}

// Adresse à donner au pote : l'adresse publique (Tailscale Funnel) si elle existe, sinon celle de la page
// (sauf « localhost », qui ne marche que sur ce PC : on prend alors l'adresse du réseau local).
let base = location.origin, publicOk = false;
/** Adresse à mettre dans un lien partagé (publique si l'accès public est actif). */
export async function publicBase() { await refreshBase(); return { base, publicOk }; }
async function refreshBase() {
  try {
    const r = await api.get("/api/remote");
    publicOk = r.mode === "public" && !!r.url;
    if (publicOk) { base = r.url.replace(/\/$/, ""); return; }
  } catch { /* ignore */ }
  if (/^(localhost|127\.|\[::1\])/.test(location.hostname)) {
    try { const i = await api.get("/api/info"); if (i.urls?.[0]) base = i.urls[0].replace(/\/$/, ""); } catch { /* ignore */ }
  }
}
function inviteUrl(token) { return `${base}/invite/${token}`; }

async function loadLists(el) {
  const box = $("#prof-list", el);
  if (!box) return;
  try {
    const r = await api.get("/api/users");
    box.innerHTML = r.users.map(profRow).join("");
    const active = r.invites.filter((i) => i.active);
    $("#inv-list", el).innerHTML = active.length ? `<div class="q-sec" style="padding:10px 0 4px">Invitations en cours</div>${active.map((i) => `
      <div class="prof-row"><div class="pr-main"><b>${esc(roles[i.role])}</b> <small class="dim">expire ${fmtRel(i.expires)} · ${i.maxUses - i.uses} utilisation${i.maxUses - i.uses > 1 ? "s" : ""} restante${i.maxUses - i.uses > 1 ? "s" : ""}${i.note ? " · " + esc(i.note) : ""}</small></div>
        ${i.token ? `<button class="btn sm" data-icopy="${esc(i.token)}">${icon("link", "sm")} Lien</button>` : ""}
        <button class="icon-btn" data-idel="${esc(i.id)}" title="Annuler l'invitation">${icon("close", "sm")}</button></div>`).join("")}` : "";
  } catch (err) { box.textContent = err.message; }
}

export async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Lien copié"); }
  catch { window.prompt("Copie ce lien :", text); }
}

/** Lien « mot de passe oublié » : l'admin l'envoie au pote, qui choisit lui-même son nouveau mot de passe. */
export async function showResetLink(name, path, expires) {
  await refreshBase();
  const url = `${base}${path}`;
  openModal({
    title: `Mot de passe de ${name}`,
    body: `<div style="display:grid;gap:14px;justify-items:center;text-align:center">
      <img class="qr" src="/api/qr.svg?text=${enc(url)}" alt="QR code" width="180" height="180" style="background:#fff;border-radius:12px;padding:8px">
      <p class="muted" style="margin:0">Envoie ce lien à <b>${esc(name)}</b> : il y choisit un nouveau mot de passe, et ses anciens appareils sont déconnectés. Le lien marche une seule fois et expire ${fmtRel(expires)}.</p>
      <input class="field" readonly value="${esc(url)}" style="width:100%" onclick="this.select()">
      ${publicOk ? "" : `<p class="dim" style="margin:0;font-size:12px">⚠ Ce lien ne marche que sur ton réseau (ou avec Tailscale), comme les invitations.</p>`}</div>`,
    foot: `${navigator.share ? `<button class="btn" data-share>${icon("link")} Partager</button>` : ""}<button class="btn white" data-copy>${icon("link")} Copier le lien</button>`,
    onMount: (m) => {
      $("[data-copy]", m).addEventListener("click", () => copy(url));
      $("[data-share]", m)?.addEventListener("click", () => navigator.share({ title: "Onde", text: "Choisis ton nouveau mot de passe Onde :", url }).catch(() => {}));
    },
  });
}

async function showInvite(token) {
  await refreshBase();
  const url = inviteUrl(token);
  openModal({
    title: "Invitation prête",
    body: `<div style="display:grid;gap:14px;justify-items:center;text-align:center">
      <img class="qr" src="/api/qr.svg?text=${enc(url)}" alt="QR code" width="180" height="180" style="background:#fff;border-radius:12px;padding:8px">
      <p class="muted" style="margin:0">Envoie ce lien à ton pote (ou fais-lui scanner le code). Il choisit son pseudo et son mot de passe, sans rien installer — et sans ton mail ni ton compte Tailscale.</p>
      <input class="field" readonly value="${esc(url)}" style="width:100%" onclick="this.select()">
      ${publicOk ? "" : `<p class="dim" style="margin:0;font-size:12px">⚠ Ce lien ne marche que sur ton réseau (ou avec Tailscale). Pour que ton pote se connecte de chez lui sans Tailscale, active l'<b>accès public</b> (Réglages → Accès à distance), puis recrée le lien.</p>`}</div>`,
    foot: `${navigator.share ? `<button class="btn" data-share>${icon("link")} Partager</button>` : ""}<button class="btn white" data-copy>${icon("link")} Copier le lien</button>`,
    onMount: (m) => {
      $("[data-copy]", m).addEventListener("click", () => copy(url));
      $("[data-share]", m)?.addEventListener("click", () => navigator.share({ title: "Onde", text: "Rejoins mon Onde :", url }).catch(() => {}));
    },
  });
}

function inviteDialog(el) {
  openModal({
    title: "Inviter un pote",
    body: `<form class="form" id="inv-form">
      <label>Droits<select class="select" name="role"><option value="member">Écoute + import (pas de suppression)</option><option value="listener">Écoute seule</option></select></label>
      <label>Le lien marche pendant<select class="select" name="days"><option value="1">24 heures</option><option value="7" selected>7 jours</option><option value="30">30 jours</option></select></label>
      <label>Nombre de personnes<select class="select" name="uses"><option value="1" selected>1 personne</option><option value="5">5 personnes</option><option value="20">20 personnes</option></select></label>
      <label>Note (pour toi)<input class="field" name="note" maxlength="80" placeholder="ex. Lucas"></label></form>`,
    foot: `<button class="btn ghost" data-close>Annuler</button><button class="btn white" data-save>Créer le lien</button>`,
    onMount: (m, close) => {
      $("[data-save]", m).addEventListener("click", async () => {
        const f = new FormData($("#inv-form", m));
        try {
          const r = await api.post("/api/invites", { role: f.get("role"), days: +f.get("days"), uses: +f.get("uses"), note: f.get("note") });
          close(); showInvite(r.token); loadLists(el);
        } catch (err) { toastError(err); }
      });
    },
  });
}

function addDialog(el) {
  openModal({
    title: "Créer un profil",
    body: `<form class="form" id="add-form" autocomplete="off">
      <label>Pseudo<input class="field" name="name" required maxlength="32" autofocus></label>
      <label>Mot de passe (6 caractères ou plus)<input class="field" type="password" name="password" required minlength="6" autocomplete="new-password"></label>
      <label>Mail (facultatif)<input class="field" type="email" name="email"></label>
      <label>Droits<select class="select" name="role">${Object.entries(roles).map(([k, v]) => `<option value="${k}"${k === "member" ? " selected" : ""}>${esc(v)}</option>`).join("")}</select></label></form>`,
    foot: `<button class="btn ghost" data-close>Annuler</button><button class="btn white" data-save>Créer</button>`,
    onMount: (m, close) => {
      $("[data-save]", m).addEventListener("click", async () => {
        const f = Object.fromEntries(new FormData($("#add-form", m)));
        try { await api.post("/api/users", f); close(); toast(`Profil « ${f.name} » créé`); loadLists(el); } catch (err) { toastError(err); }
      });
    },
  });
}

function editDialog(refresh) {
  openModal({
    title: "Mon profil",
    body: `<form class="form" id="me-form">
      <label>Pseudo<input class="field" name="name" required maxlength="32" value="${esc(me.name)}"></label>
      <label>Mail (facultatif — pour recevoir un lien si tu oublies ton mot de passe)<input class="field" type="email" name="email" value="${esc(me.email || "")}"></label></form>`,
    foot: `<button class="btn ghost" data-close>Annuler</button><button class="btn white" data-save>Enregistrer</button>`,
    onMount: (m, close) => {
      $("[data-save]", m).addEventListener("click", async () => {
        const f = Object.fromEntries(new FormData($("#me-form", m)));
        try { await api.patch("/api/me", f); close(); toast("Profil enregistré"); refresh(); } catch (err) { toastError(err); }
      });
    },
  });
}

function passwordDialog(refresh, forUser = null) {
  const own = !forUser;
  openModal({
    title: own ? (me.hasPassword ? "Changer mon mot de passe" : "Choisir mon mot de passe") : `Nouveau mot de passe pour ${forUser.name}`,
    body: `<form class="form" id="pw-form" autocomplete="on">
      ${own && me.hasPassword ? `<label>Mot de passe actuel<input class="field" type="password" name="current" autocomplete="current-password" required autofocus></label>` : ""}
      <label>Nouveau mot de passe<input class="field" type="password" name="password" autocomplete="new-password" minlength="6" maxlength="128" required></label>
      <label>Confirme-le<input class="field" type="password" name="confirm" autocomplete="new-password" minlength="6" maxlength="128" required></label>
      <p class="dim" style="margin:0;font-size:12px">${own ? (me.hasPassword ? "Tes autres appareils devront se reconnecter ; celui-ci reste connecté." : `Les profils s'activent : chacun devra se connecter avec son pseudo (le tien : <b>${esc(me.name)}</b>). Cet appareil reste connecté.`) : "Ses appareils seront déconnectés."}</p></form>`,
    foot: `<button class="btn ghost" data-close>Annuler</button><button class="btn white" data-save>Enregistrer</button>`,
    onMount: (m, close) => {
      const save = async (e) => {
        e?.preventDefault();
        const f = new FormData($("#pw-form", m));
        if (f.get("password") !== f.get("confirm")) { toast("Les deux mots de passe ne correspondent pas", { error: true }); return; }
        try {
          if (own) await api.post("/api/settings/password", { password: f.get("password"), current: f.get("current") || "" });
          else await api.patch(`/api/users/${forUser.id}`, { password: f.get("password") });
          close(); toast("Mot de passe enregistré"); refresh();
        } catch (err) { toastError(err); }
      };
      $("[data-save]", m).addEventListener("click", save);
      $("#pw-form", m).addEventListener("submit", save);
    },
  });
}

async function journalDialog() {
  let users = [];
  try { users = (await api.get("/api/users")).users; } catch { /* ignore */ }
  openModal({
    title: "Journal d'activité",
    wide: true,
    body: `<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px">
        <select class="select" id="j-user"><option value="0">Tout le monde</option>${users.map((u) => `<option value="${u.id}">${esc(u.name)}</option>`).join("")}</select>
        <label class="dim" style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="j-plays" checked> écoutes</label></div>
      <div id="j-list" class="journal">…</div>`,
    onMount: (m) => {
      const load = async () => {
        const box = $("#j-list", m);
        try {
          const rows = await api.get(`/api/activity?limit=300&user=${$("#j-user", m).value}&plays=${$("#j-plays", m).checked ? 1 : 0}`);
          box.innerHTML = rows.length ? rows.map((r) => `<div class="j-row j-${esc(r.action)}"><span class="dim">${new Date(r.ts * 1000).toLocaleString("fr", { dateStyle: "short", timeStyle: "short" })}</span>
            <b>${esc(r.name || "?")}</b><span>${esc(r.label)}</span><span class="dim j-d">${esc(r.detail)}</span></div>`).join("") : `<p class="muted">Rien pour l'instant.</p>`;
        } catch (err) { box.textContent = err.message; }
      };
      $("#j-user", m).addEventListener("change", load);
      $("#j-plays", m).addEventListener("change", load);
      load();
    },
  });
}

export function bindProfiles(el, refresh) {
  loadLists(el);
  const mq = $("[data-myquota]", el);
  if (mq) api.get("/api/me/quota").then((q) => { mq.textContent = quotaText(q); }).catch(() => { mq.textContent = ""; });
  if (me.role === "admin") refreshBase();
  el.addEventListener("click", async (e) => {
    const p = e.target.closest("[data-prof]")?.dataset.prof;
    if (p === "logout" && window.OndeApp?.logout) { e.preventDefault(); window.OndeApp.logout(); return; } // appli : écran de compte natif
    if (p === "edit") editDialog(refresh);
    else if (p === "pwd") passwordDialog(refresh);
    else if (p === "invite") inviteDialog(el);
    else if (p === "add") addDialog(el);
    else if (p === "journal") journalDialog();
    const b = e.target.closest("[data-ppwd],[data-prst],[data-pout],[data-pdel],[data-icopy],[data-idel]");
    if (!b) return;
    const row = b.closest("[data-uid]");
    const name = row ? $("b", row).textContent : "";
    try {
      if (b.dataset.ppwd) passwordDialog(() => loadLists(el), { id: +b.dataset.ppwd, name });
      else if (b.dataset.prst) { const r = await api.post(`/api/users/${b.dataset.prst}/reset-link`); showResetLink(r.name || name, r.path, r.expires); }
      else if (b.dataset.pout) { await api.post(`/api/users/${b.dataset.pout}/logout`); toast(`${name} est déconnecté de ses appareils`); }
      else if (b.dataset.pdel) {
        if (!(await confirmDialog({ title: `Supprimer le profil « ${name} » ?`, text: "Ses likes et ses playlists seront supprimés. La musique de la bibliothèque reste.", ok: "Supprimer" }))) return;
        await api.del(`/api/users/${b.dataset.pdel}`); toast("Profil supprimé"); loadLists(el);
      } else if (b.dataset.icopy) copy(inviteUrl(b.dataset.icopy));
      else if (b.dataset.idel) { await api.del(`/api/invites/${b.dataset.idel}`); toast("Invitation annulée"); loadLists(el); }
    } catch (err) { toastError(err); }
  });
  el.addEventListener("change", async (e) => {
    const s = e.target.closest("[data-role]");
    if (!s) return;
    try { await api.patch(`/api/users/${s.dataset.role}`, { role: s.value }); toast(`Droits : ${roles[s.value]}`); }
    catch (err) { toastError(err); loadLists(el); }
  });
}

/** Conditions d'utilisation : à accepter une fois par profil (sinon : déconnexion). */
export async function ensureTerms(info) {
  if (!info?.me || info.me.terms !== false) return;
  let t;
  try { t = await api.get("/api/terms"); } catch { return; }
  openModal({
    title: t.title,
    locked: true,
    body: `<div class="terms-box">${t.html}</div>
      <label class="terms-chk"><input type="checkbox" data-terms-ok> J'ai lu et j'accepte ces conditions : usage privé, je ne partage rien à personne.</label>`,
    foot: `${info.password ? `<button class="btn ghost" data-terms-no>Refuser (se déconnecter)</button>` : ""}<button class="btn white" data-terms-yes disabled>J'accepte</button>`,
    onMount: (m, close) => {
      m.closest(".modal")?.classList.add("terms-modal");
      $("[data-close]", m)?.remove(); // (pas de croix : il faut répondre)
      const yes = $("[data-terms-yes]", m);
      $("[data-terms-ok]", m).addEventListener("change", (e) => { yes.disabled = !e.target.checked; });
      yes.addEventListener("click", async () => {
        try { await api.post("/api/me/terms"); me.terms = true; close(); toast("Merci !"); } catch (err) { toastError(err); }
      });
      $("[data-terms-no]", m)?.addEventListener("click", () => {
        if (window.OndeApp?.logout) window.OndeApp.logout(); else location.href = "/logout";
      });
    },
  });
}
