// « Ton année » : le bilan d'écoute en diapositives plein écran (touche pour avancer, flèches, Échap pour fermer).
import { api } from "./api.js";
import { $, $$, esc, enc, icon, plural, cover } from "./util.js";

const MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"];
const SHORT = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"];
const dayFmt = new Intl.DateTimeFormat("fr", { weekday: "long", day: "numeric", month: "long" });
const hours = (sec) => Math.round(sec / 3600);
const minutes = (sec) => Math.round(sec / 60);
const DAYPARTS = {
  matin: ["Lève-tôt", "Tu écoutes surtout le matin."],
  "après-midi": ["Rythme de l'après-midi", "C'est l'après-midi que la musique tourne le plus."],
  soir: ["Âme du soir", "Tu écoutes surtout une fois la journée finie."],
  nuit: ["Oiseau de nuit", "Tes meilleures écoutes ont lieu tard dans la nuit."],
};

function bars(months, best) {
  const max = Math.max(...months, 1);
  return `<div class="wr-bars" aria-hidden="true">${months.map((m, i) =>
    `<span class="${i === best ? "on" : ""}" style="--h:${Math.max(4, Math.round((m / max) * 100))}%"><i>${SHORT[i]}</i></span>`).join("")}</div>`;
}

/** Les diapositives du bilan (chaque entrée : html + classe de couleur). */
export function buildSlides(d) {
  const slides = [];
  slides.push({ tone: "t1", html: `<p class="wr-kicker">Onde</p><h2 class="wr-big">Ton année<br>${d.year}</h2>
    <p class="wr-sub">Voici ce que tu as écouté. Touche l'écran pour avancer.</p>` });
  const h = hours(d.seconds);
  slides.push({ tone: "t2", html: `<p class="wr-kicker">Tu as écouté</p>
    <h2 class="wr-huge">${(h >= 2 ? h : minutes(d.seconds)).toLocaleString("fr")}</h2>
    <p class="wr-title">${h >= 2 ? "heures de musique" : "minutes de musique"}</p>
    <p class="wr-sub">${plural(d.plays, "écoute", "écoutes")}, ${plural(d.tracks, "titre différent", "titres différents")} et ${plural(d.artists, "artiste", "artistes")}.</p>` });
  const a = d.topArtists[0];
  if (a) slides.push({ tone: "t3", html: `<p class="wr-kicker">Ton artiste de l'année</p>
    <a class="wr-hero round" href="#/artist/${enc(a.name)}">${cover(a.cover, 384, a.name, "user", a.name)}</a>
    <h2 class="wr-title">${esc(a.name)}</h2>
    <p class="wr-sub">${plural(a.count, "écoute", "écoutes")} · ${minutes(a.seconds).toLocaleString("fr")} min</p>
    ${d.topArtists.length > 1 ? `<ol class="wr-list">${d.topArtists.slice(1).map((x, i) => `<li><b>${i + 2}</b><a href="#/artist/${enc(x.name)}">${esc(x.name)}</a></li>`).join("")}</ol>` : ""}` });
  const t = d.topTracks[0];
  if (t) slides.push({ tone: "t4", html: `<p class="wr-kicker">Ton titre de l'année</p>
    <div class="wr-hero">${cover(t.cover, 384, t.title, "note", t.title)}</div>
    <h2 class="wr-title">${esc(t.title)}</h2>
    <p class="wr-sub">${esc(t.artist)} · ${plural(t.count, "écoute", "écoutes")}</p>
    ${d.topTracks.length > 1 ? `<ol class="wr-list">${d.topTracks.slice(1).map((x, i) => `<li><b>${i + 2}</b><span>${esc(x.title)} <em>${esc(x.artist)}</em></span></li>`).join("")}</ol>` : ""}` });
  if (d.topAlbum || d.topGenres.length) slides.push({ tone: "t5", html: `<p class="wr-kicker">Ton univers</p>
    ${d.topAlbum ? `<a class="wr-hero" href="#/album/${d.topAlbum.id}">${cover(d.topAlbum.cover, 384, d.topAlbum.name, "disc", d.topAlbum.name)}</a>
    <h2 class="wr-title">${esc(d.topAlbum.name)}</h2><p class="wr-sub">Ton album n°1 · ${plural(d.topAlbum.plays, "écoute", "écoutes")}</p>` : ""}
    ${d.topGenres.length ? `<div class="wr-tags">${d.topGenres.map((g) => `<a class="chip" href="#/genre/${enc(g.name)}">${esc(g.name)}</a>`).join("")}</div>` : ""}` });
  const bm = d.busiestMonth;
  const [persona, personaText] = DAYPARTS[d.daypart] || DAYPARTS.soir;
  slides.push({ tone: "t6", html: `<p class="wr-kicker">Ton rythme</p>
    <h2 class="wr-title">${esc(persona)}</h2><p class="wr-sub">${esc(personaText)}</p>
    ${bars(d.months, bm.month - 1)}
    <p class="wr-sub">Ton mois record : <b>${MONTHS[bm.month - 1]}</b> (${bm.minutes.toLocaleString("fr")} min).<br>
    Ton jour record : <b>${esc(dayFmt.format(new Date(`${d.busiestDay.date}T12:00:00`)))}</b> (${d.busiestDay.minutes.toLocaleString("fr")} min).</p>` });
  slides.push({ tone: "t7", html: `<p class="wr-kicker">Tes habitudes</p>
    <h2 class="wr-huge">${d.bestStreak}</h2><p class="wr-title">${d.bestStreak > 1 ? "jours d'affilée" : "jour d'affilée"}</p>
    <p class="wr-sub">${d.newArtists
      ? `Et tu as découvert <b>${plural(d.newArtists, "nouvel artiste", "nouveaux artistes")}</b> cette année.`
      : "Cette année, tu es resté fidèle à tes artistes habituels."}
    ${d.firstTrack ? `<br>Ta toute première écoute : « ${esc(d.firstTrack.title)} ».` : ""}</p>` });
  slides.push({ tone: "t1", html: `<p class="wr-kicker">Merci d'avoir écouté</p><h2 class="wr-big">À l'année<br>prochaine&nbsp;!</h2>
    <div class="wr-actions"><button class="btn primary" data-wr="play">${icon("play")} Rejouer mon top</button>
    <a class="btn" href="#/stats">${icon("chart")} Mes statistiques</a></div>` });
  return slides;
}

export async function wrappedView(el, params, page) {
  document.title = "Ton année · Onde";
  page.setColor("#2b2356");
  const wanted = parseInt(params?.[0], 10) || 0;
  const d = await api.get(`/api/wrapped${wanted ? `?year=${wanted}` : ""}`);
  if (!d.plays) {
    el.innerHTML = `<div class="page"><h1 class="page-title">Ton année</h1>
      <p class="muted">Pas encore d'écoutes ${d.years.length ? `en ${d.year}` : "enregistrées"} : ton bilan se remplit au fil de tes écoutes.</p>
      <a class="btn primary" href="#/">${icon("play")} Écouter de la musique</a></div>`;
    return;
  }
  const slides = buildSlides(d);
  let i = 0;
  const root = document.createElement("div");
  root.className = "wr";
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-label", `Ton année ${d.year}`);
  root.innerHTML = `<div class="wr-prog">${slides.map(() => "<i></i>").join("")}</div>
    <div class="wr-top"><span>${d.years.length > 1 ? `<select class="select wr-year" aria-label="Année">${d.years.map((y) => `<option${y === d.year ? " selected" : ""}>${y}</option>`).join("")}</select>` : `<b>${d.year}</b>`}</span>
      <button class="icon-btn wr-close" aria-label="Fermer">${icon("close")}</button></div>
    <div class="wr-stage"></div>
    <button class="wr-nav prev" aria-label="Précédent"></button><button class="wr-nav next" aria-label="Suivant"></button>`;
  const stage = $(".wr-stage", root);
  const show = (n) => {
    i = Math.max(0, Math.min(slides.length - 1, n));
    root.className = `wr ${slides[i].tone}`;
    stage.innerHTML = `<div class="wr-slide">${slides[i].html}</div>`;
    $$(".wr-prog i", root).forEach((b, k) => b.classList.toggle("done", k <= i));
  };
  const close = () => { location.hash = "#/stats"; };
  root.addEventListener("click", (e) => {
    if (e.target.closest(".wr-close")) return close();
    if (e.target.closest("[data-wr=play]")) {
      page.player.playList(d.topTracks.map((t) => t.id), 0, "wrapped");
      return;
    }
    if (e.target.closest("a, select, button:not(.wr-nav)")) return;
    if (e.target.closest(".prev")) show(i - 1);
    else show(i + 1);
  });
  $(".wr-year", root)?.addEventListener("change", (e) => { location.hash = `#/wrapped/${e.target.value}`; });
  const onKey = (e) => {
    if (e.key === "ArrowRight" || e.key === " ") { e.preventDefault(); show(i + 1); }
    else if (e.key === "ArrowLeft") show(i - 1);
    else if (e.key === "Escape") close();
  };
  document.addEventListener("keydown", onKey);
  // sur <body> : dans la page, le bandeau du haut et le lecteur resteraient par-dessus
  document.body.appendChild(root);
  show(0);
  return () => { document.removeEventListener("keydown", onKey); root.remove(); };
}
