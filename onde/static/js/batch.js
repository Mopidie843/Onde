// Infos de plusieurs titres d'un coup : modification commune, ou réparation automatique (Apple Music / Deezer).
import { api, emit, trackCache } from "./api.js";
import { $, esc, plural } from "./util.js";
import { openModal, toast, toastError } from "./ui.js";

const FIELDS = [["artist", "Artiste"], ["album", "Album"], ["album_artist", "Artiste de l'album"], ["year", "Année"], ["genre", "Genre"]];

function common(tracks, k) {
  const key = { album_artist: "albumArtist" }[k] || k;
  const vals = new Set(tracks.map((t) => (t[key] ?? "").toString()));
  return vals.size === 1 ? [...vals][0] : "";
}

export function batchEdit(tracks, done) {
  openModal({
    title: `Modifier ${plural(tracks.length, "titre", "titres")}`,
    body: `<form class="form" id="be-form">
      <p class="dim" style="margin:0;font-size:12px">Laisse un champ vide pour ne pas le changer. Les tags sont écrits dans les fichiers.</p>
      ${FIELDS.map(([k, label]) => `<label>${label}<input class="field" name="${k}" value="${esc(common(tracks, k))}" placeholder="(inchangé)"></label>`).join("")}</form>`,
    foot: `<button class="btn ghost" data-close>Annuler</button><button class="btn white" data-save>Enregistrer</button>`,
    onMount: (m, close) => {
      $("[data-save]", m).addEventListener("click", async () => {
        const fields = Object.fromEntries([...new FormData($("#be-form", m))].filter(([, v]) => String(v).trim()));
        if (!Object.keys(fields).length) { close(); return; }
        try {
          const r = await api.post("/api/tracks/batch-edit", { ids: tracks.map((t) => t.id), fields });
          tracks.forEach((t) => trackCache.delete(t.id));
          close(); toast(`${plural(r.done, "titre modifié", "titres modifiés")}${r.errors.length ? ` (${r.errors.length} en erreur)` : ""}`);
          emit("library"); done?.();
        } catch (err) { toastError(err); }
      });
    },
  });
}

export function batchFix(tracks, done) {
  let stop = false;
  const { el } = openModal({
    title: "Réparer les infos en ligne",
    body: `<p class="muted" style="margin:0 0 10px;font-size:13px">Pour chaque titre, Onde cherche sur Apple Music et Deezer le bon titre, l'artiste, l'album, l'année, le genre et la pochette — et ne change rien si le résultat ne correspond pas vraiment.</p>
      <div class="progress"><i style="width:0"></i></div><p class="dim" id="bf-txt" style="margin:8px 0 0">0 / ${tracks.length}</p>`,
    foot: `<button class="btn ghost" data-close>Fermer</button>`,
    onClose: () => { stop = true; },
  });
  (async () => {
    let fixed = 0, skipped = 0;
    for (let i = 0; i < tracks.length && !stop; i += 5) {
      const ids = tracks.slice(i, i + 5).map((t) => t.id);
      try {
        const r = await api.post("/api/tracks/batch-fix", { ids });
        fixed += r.fixed.length; skipped += r.skipped.length;
        ids.forEach((id) => trackCache.delete(id));
      } catch (err) { toastError(err); break; }
      const n = Math.min(tracks.length, i + 5);
      const bar = $(".progress i", el);
      if (bar) bar.style.width = `${Math.round((n / tracks.length) * 100)}%`;
      const txt = $("#bf-txt", el);
      if (txt) txt.textContent = `${n} / ${tracks.length} · ${plural(fixed, "réparé", "réparés")} · ${skipped} sans résultat sûr`;
    }
    if (fixed) emit("library");
    toast(`${plural(fixed, "titre réparé", "titres réparés")}`);
    done?.();
  })();
}
