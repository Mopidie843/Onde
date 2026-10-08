// Lecteur : file d'attente, lecture locale (<audio>) ou sur le Raspberry Pi (mpv).
//
// Lecture en arrière-plan / écran verrouillé :
//  - un seul élément <audio> réutilisé pour tous les titres (iOS/Android le
//    laissent jouer écran éteint une fois lancé par un geste de l'utilisateur) ;
//  - le passage au titre suivant se fait de façon synchrone dans l'événement
//    "ended" (aucun await avant play(), sinon le navigateur peut bloquer) ;
//  - les titres de la lecture automatique sont chargés dès le début du dernier
//    titre, pour être prêts quand il se termine (même écran éteint) ;
//  - Media Session : pochette, titre et boutons précédent/suivant sur l'écran
//    de verrouillage ;
//  - reprise automatique si le Wi-Fi décroche (erreur réseau -> rechargement
//    à la même position), et surveillance des blocages de flux.
//  - musique téléchargée (écoute hors connexion) : lue depuis l'appareil, sans réseau ; hors
//    connexion, les titres absents de l'appareil sont sautés.
import { api, trackCache, ensureTracks, emit as emitBus, OFFLINE_MSG } from "./api.js";
import { shuffleArr, storage, canPlayExt, markUnplayable, artworkUrl } from "./util.js";
import { offline } from "./offline.js";

const KEY = "onde.player.v1";
const TARGET_LUFS = -14;
// Égaliseur (5 bandes) et préréglages
export const EQ_BANDS = [60, 230, 910, 3600, 14000];
export const EQ_PRESETS = {
  flat: { label: "Neutre", g: [0, 0, 0, 0, 0] },
  bass: { label: "Basses", g: [6, 3, 0, 0, 1] },
  vocal: { label: "Voix", g: [-2, 0, 3, 3, 1] },
  rock: { label: "Rock", g: [4, 1, -1, 2, 4] },
  pop: { label: "Pop", g: [-1, 2, 3, 1, -1] },
  rap: { label: "Rap", g: [5, 3, 0, 1, 2] },
  soft: { label: "Doux (soirée)", g: [1, 0, -1, -2, -3] },
  car: { label: "Voiture", g: [4, 2, 0, 2, 3] },
};

// WAV silencieux de 0,1 s : « déverrouille » l'audio sur iOS au premier geste de l'utilisateur
export const SILENT_SRC = (() => {
  const n = 800, buf = new ArrayBuffer(44 + n), v = new DataView(buf);
  const w = (o, str) => [...str].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  w(0, "RIFF"); v.setUint32(4, 36 + n, true); w(8, "WAVE"); w(12, "fmt "); v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, 8000, true); v.setUint32(28, 8000, true);
  v.setUint16(32, 1, true); v.setUint16(34, 8, true); w(36, "data"); v.setUint32(40, n, true);
  for (let i = 0; i < n; i++) v.setUint8(44 + i, 128);
  return URL.createObjectURL(new Blob([buf], { type: "audio/wav" }));
})();
const isNetErr = (e) => e && (e.message === OFFLINE_MSG || String(e.message).startsWith("Serveur injoignable"));

const MEM_MAX = 3;
const MEM_BYTES = 45 * 1048576;

export class Player extends EventTarget {
  constructor(audio) {
    super();
    this.audio = audio;
    // Mémoire tampon : le titre suivant est téléchargé en entier pendant l'écoute. Il se lit ensuite
    // depuis la mémoire : changer de page (listes, pochettes) ne peut plus le faire couper quand le
    // réseau est faible (VPN, 4G).
    this._mem = new Map();      // id -> URL blob
    this._memBusy = new Set();
    this.queue = [];
    this.index = -1;
    this.original = null;      // ordre avant mélange
    this.manualEnd = -1;       // fin des titres ajoutés « à la file »
    this.autoFrom = -1;        // début des titres ajoutés par la lecture automatique
    this.shuffle = false;
    this.repeat = "off";       // off | all | one
    this.source = null;        // { key, label, href, kind }
    this.volume = 0.8;
    this.muted = false;
    this.gain = 1;             // volume égalisé du titre en cours
    this.normalize = true;
    this.autoplay = storage.get("onde.autoplay", true);
    this.sleep = { at: 0, end: false };
    this.mode = "local";       // local | pi
    this.pi = { available: false, state: null, at: 0, rev: -1 };
    this.counted = false;
    this.retries = 0;
    this.lastPos = 0;
    this.pendingSeek = null;
    this.wantPlay = false;     // l'utilisateur veut que ça joue (pour la reprise auto)
    this._saveT = 0;
    this._badLocal = new Set(); // copies locales illisibles : on repasse par le Pi
    this._warmed = null;
    this.crossfade = Math.max(0, Math.min(12, +storage.get("onde.crossfade", 0) || 0)); // fondu enchaîné (s, cet appareil)
    this.eq = storage.get("onde.eq", { on: false, preset: "flat", g: [0, 0, 0, 0, 0] });
    this._xf = null;           // fondu enchaîné en cours
    this._fadeIn = null;       // { at, dur } : montée du titre suivant
    this._graph = null;        // Web Audio (égaliseur), créé seulement s'il est activé
    // Garde-fou du son : la lecture avance mais plus rien ne sort (changement de page, carte son qui
    // se rendort…) → on le détecte (volume mesuré) et on relance proprement la lecture.
    this._g = { an: null, from: "", mctx: null, node: null, dirty: true, buf: null, since: 0, ok: 0, step: 0, id: null, pos: 0, at: 0 };
    this._bindAudio();
    this._bindMediaSession();
    this._restore();
  }

  // ------------------------------------------------------------ getters
  get currentId() { return this.queue[this.index] ?? null; }
  get current() { return trackCache.get(this.currentId) || null; }
  get hasTrack() { return this.currentId != null; }
  get paused() {
    if (this.mode === "pi") return this.pi.state ? this.pi.state.paused : true;
    return this.audio.paused;
  }
  get position() {
    if (this.mode === "pi") {
      const s = this.pi.state;
      if (!s) return 0;
      const extra = s.paused || !s.loaded ? 0 : (performance.now() - this.pi.at) / 1000;
      return Math.min(s.position + extra, s.duration || Infinity);
    }
    return this.pendingSeek ?? this.audio.currentTime ?? 0;
  }
  get duration() {
    if (this.mode === "pi") return this.pi.state?.duration || this.current?.dur || 0;
    const d = this.audio.duration;
    return Number.isFinite(d) && d > 0 ? d : this.current?.dur || 0;
  }
  /** Secondes avant l'arrêt programmé (null si aucune minuterie), ou "end" (fin du titre). */
  get sleepLeft() {
    if (this.mode === "pi") {
      const s = this.pi.state;
      if (!s) return null;
      if (s.sleepEnd) return "end";
      if (s.sleepIn == null) return null;
      return Math.max(0, s.sleepIn - (performance.now() - this.pi.at) / 1000);
    }
    if (this.sleep.end) return "end";
    return this.sleep.at ? Math.max(0, (this.sleep.at - Date.now()) / 1000) : null;
  }
  emit(type) { this.dispatchEvent(new Event(type)); }

  applySettings(s) {
    if (!s) return;
    this.normalize = !!s.normalize;
    if (storage.get("onde.autoplay", null) == null) this.autoplay = s.autoplay !== false;
    this._applyVolume();
  }

  // ------------------------------------------------------------ lecture
  playList(ids, start = 0, source = null) {
    ids = ids.filter((x) => x != null);
    if (!ids.length) return;
    start = Math.max(0, Math.min(start, ids.length - 1));
    this.source = source;
    this.autoFrom = -1;
    if (this.shuffle) {
      const first = ids[start];
      const rest = shuffleArr(ids.filter((_, i) => i !== start));
      this.original = ids.slice();
      this.queue = [first, ...rest];
      this.index = 0;
    } else {
      this.original = null;
      this.queue = ids.slice();
      this.index = start;
    }
    this.manualEnd = this.index;
    this._start(0, true);
  }

  _src(id, retry = false) {
    if (!this._badLocal.has(id) && !(retry && !offline.active)) {
      const local = offline.url(id); // téléchargé sur cet appareil : pas de réseau du tout
      if (local) return local;
    }
    if (!retry && this._mem.has(id)) return this._mem.get(id);
    return this._remoteSrc(id, retry);
  }
  /** Télécharge le titre `id` en entier, en tâche de fond peu prioritaire (45 Mo au plus). */
  async _memFetch(id) {
    if (id == null || this._mem.has(id) || this._memBusy.has(id) || offline.active || offline.url(id)) return;
    if (navigator.connection?.saveData) return;
    this._memBusy.add(id);
    const ctrl = new AbortController();
    try {
      const res = await fetch(this._remoteSrc(id), { signal: ctrl.signal, priority: "low" });
      const len = +res.headers.get("content-length") || 0;
      if (!res.ok || len > MEM_BYTES) { ctrl.abort(); return; }
      const blob = await res.blob();
      if (blob.size > MEM_BYTES || !blob.size) return;
      this._mem.set(id, URL.createObjectURL(blob));
      this._memTrim();
    } catch { /* réseau : on lira en direct, comme avant */ } finally { this._memBusy.delete(id); }
  }
  _memTrim() {
    const keep = new Set([this.currentId, this.queue[this.index + 1], this.queue[this.index - 1]]);
    for (const [id, url] of this._mem) {
      if (this._mem.size <= MEM_MAX) break;
      if (keep.has(id)) continue;
      // (le titre en cours peut encore lire ce blob : on ne libère jamais celui-là)
      if (this.audio.currentSrc === url) continue;
      URL.revokeObjectURL(url);
      this._mem.delete(id);
    }
  }
  _remoteSrc(id, retry = false) {
    const t = trackCache.get(id);
    let url = `/api/tracks/${id}/stream`;
    if (t && t.ext && !canPlayExt(t.ext)) url += "?t=1"; // format non lu par ce navigateur : converti par le Pi
    if (retry) url += `${url.includes("?") ? "&" : "?"}r=${Date.now()}`;
    return url;
  }
  /** Hors connexion : prochain titre disponible sur l'appareil à partir de `from` (ou -1). */
  _available(from, step = 1) {
    const n = this.queue.length;
    for (let k = 0; k < n; k++) {
      let i = from + k * step;
      if (i >= n || i < 0) {
        if (this.repeat !== "all") return -1;
        i = ((i % n) + n) % n;
      }
      if (offline.has(this.queue[i])) return i;
    }
    return -1;
  }

  // Démarre le titre courant. Volontairement SYNCHRONE en mode local.
  _start(position = 0, play = true) {
    if (!this.hasTrack) return;
    if (this.mode === "local" && offline.active && !offline.has(this.currentId)) {
      const i = this._available(this.index);
      if (i < 0) {
        this.wantPlay = false;
        this.audio.pause();
        this.dispatchEvent(new CustomEvent("problem", { detail: "Ce titre n'est pas téléchargé : il n'est pas disponible hors connexion." }));
        this.emit("state");
        return;
      }
      if (i !== this.index) position = 0;
      this.index = i;
    }
    if (this.mode === "pi") {
      this._piSync({ position, play });
    } else {
      const id = this.currentId;
      this.counted = false;
      this.retries = 0;
      this._warmed = null;
      this.lastPos = position;
      this.pendingSeek = position > 0.5 ? position : null;
      this._startedAt = performance.now();
      this._updateGain();
      this.audio.src = this._src(id);
      this.wantPlay = play;
      if (play) this._play();
      else this.audio.load();
      this._maybeAutoplay();
    }
    this.emit("track");
    this.emit("queue");
    this.emit("state");
    this._updateMediaSession();
    this._save(true);
    if (!this.current) ensureTracks([this.currentId]).then(() => { this._updateGain(); this.emit("track"); this._updateMediaSession(); });
  }

  _play() {
    this.wantPlay = true;
    if (this.eq.on && !this._graph) this._ensureGraph(), this._applyEq(), this._applyVolume();
    if (this._graph && this._graph.ctx.state !== "running") this._graph.ctx.resume?.().catch(() => {});
    const p = this.audio.play();
    if (p && p.catch) {
      p.catch((err) => {
        if (err && err.name === "NotAllowedError") { this.wantPlay = false; this.emit("state"); }
      });
    }
  }

  toggle() {
    if (this.mode === "pi") return this._pi(this.paused ? "play" : "pause", {}, { paused: !this.paused });
    if (!this.hasTrack) return;
    if (!this.audio.currentSrc || this.audio.currentSrc === SILENT_SRC) { this._start(this.lastPos, true); return; }
    if (this.audio.paused) { this._applyVolume(); this._play(); }
    else { this.wantPlay = false; this.audio.pause(); }
  }
  play() { if (this.paused) this.toggle(); }
  pause() { if (!this.paused) this.toggle(); }

  next(auto = false) {
    if (!this.queue.length) return;
    if (this.mode === "pi") return this._pi("next");
    if (auto && this.sleep.end) { // minuterie « fin du titre » : on s'arrête sur le suivant
      this.sleep = { at: 0, end: false };
      if (this.index + 1 < this.queue.length) this.index++;
      this._start(0, false);
      this.emit("sleep");
      return;
    }
    if (auto && this.repeat === "one") {
      this.audio.currentTime = 0;
      this.counted = false;
      this._play();
      return;
    }
    let play = true;
    if (this.index + 1 < this.queue.length) this.index++;
    else if (this.repeat === "all") this.index = 0;
    else if (auto) { this.wantPlay = false; this.emit("state"); this._save(true); return; }
    else { this.index = 0; play = !this.audio.paused; }
    if (this.index > this.manualEnd) this.manualEnd = this.index;
    this._start(0, play);
  }

  prev() {
    if (!this.queue.length) return;
    if (this.mode === "pi") return this._pi("prev");
    if (this.audio.currentTime > 3 || this.index === 0) { this.seek(0); return; }
    if (offline.active) { // hors connexion : titre précédent disponible sur l'appareil
      const i = this._available(this.index - 1, -1);
      if (i < 0 || i >= this.index) { this.seek(0); return; }
      this.index = i;
    } else this.index--;
    this._start(0, true);
  }

  jump(i) {
    if (i < 0 || i >= this.queue.length) return;
    if (this.mode === "pi") { this.index = i; return this._pi("jump", { index: i }); }
    this.index = i;
    this.manualEnd = Math.max(this.manualEnd, i);
    this._start(0, true);
  }

  seek(sec) {
    sec = Math.max(0, Math.min(sec, this.duration || sec));
    if (this.mode === "pi") {
      if (this.pi.state) { this.pi.state.position = sec; this.pi.at = performance.now(); }
      this._pi("seek", { position: sec });
    } else if (this.audio.readyState >= 1) {
      this.audio.currentTime = sec;
      this.pendingSeek = null;
    } else {
      this.pendingSeek = sec;
      this.lastPos = sec;
    }
    this.emit("time");
    this._positionState();
  }

  // ------------------------------------------------------------ volume (+ égalisation)
  _gainFor(t) {
    if (!this.normalize || !t || t.lufs == null) return 1;
    return Math.max(0.15, Math.min(1, 10 ** ((TARGET_LUFS - t.lufs) / 20)));
  }
  _updateGain() { this.gain = this._gainFor(this.current); this._applyVolume(); }
  _applyVolume(fade = 1) {
    if (this._fadeIn) {
      const k = (performance.now() - this._fadeIn.at) / (this._fadeIn.dur * 1000);
      if (k >= 1) this._fadeIn = null; else fade *= Math.max(0, k);
    }
    fade *= this._sleepFade();
    const v = Math.max(0, Math.min(1, this.volume * this.gain * fade));
    if (this._graph) { // égaliseur actif : le volume passe par le graphe Web Audio
      try { this.audio.volume = 1; } catch { /* ignore */ }
      this._graph.out.gain.value = v;
      return;
    }
    try { this.audio.volume = v; } catch { /* iOS : volume matériel */ }
  }

  // ------------------------------------------------------------ égaliseur (Web Audio, seulement s'il est activé)
  setEq(eq) {
    this.eq = { on: !!eq.on, preset: eq.preset || "custom", g: (eq.g || [0, 0, 0, 0, 0]).map((x) => Math.max(-12, Math.min(12, +x || 0))) };
    storage.set("onde.eq", this.eq);
    if (this.eq.on) this._ensureGraph();
    this._applyEq();
    this._applyVolume();
  }
  _ensureGraph() {
    if (this._graph) { this._graph.ctx.resume?.().catch(() => {}); return this._graph; }
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    try {
      const ctx = new AC();
      const src = ctx.createMediaElementSource(this.audio);
      const filters = EQ_BANDS.map((f, i) => {
        const b = ctx.createBiquadFilter();
        b.type = i === 0 ? "lowshelf" : i === EQ_BANDS.length - 1 ? "highshelf" : "peaking";
        b.frequency.value = f;
        b.Q.value = 1;
        return b;
      });
      const out = ctx.createGain();
      filters.reduce((prev, b) => { prev.connect(b); return b; }, src).connect(out);
      out.connect(ctx.destination);
      this._graph = { ctx, filters, out };
      ctx.resume?.().catch(() => {});
    } catch { this._graph = null; }
    return this._graph;
  }
  _applyEq() {
    if (!this._graph) return;
    this._graph.filters.forEach((b, i) => { b.gain.value = this.eq.on ? this.eq.g[i] || 0 : 0; });
  }

  // ------------------------------------------------------------ fondu enchaîné
  setCrossfade(sec) {
    this.crossfade = Math.max(0, Math.min(12, +sec || 0));
    storage.set("onde.crossfade", this.crossfade);
  }
  _hasNext() { return this.index + 1 < this.queue.length || (this.repeat === "all" && this.queue.length > 1); }
  // Quelques secondes avant la fin : la fin du titre continue dans un second lecteur qui baisse
  // doucement, pendant que le titre suivant démarre en montant. (Annulé sans bruit si ça ne marche pas.)
  _maybeCrossfade(t) {
    const xf = this.crossfade, d = this.duration;
    if (!xf || this._xf || this.mode !== "local" || this.repeat === "one" || this.sleep.end || !this._hasNext()
      || !d || d < xf * 3 || d - t > xf || this.audio.paused) return;
    const ghost = new Audio();
    ghost.preload = "auto";
    const job = this._xf = { ghost, done: false };
    const startVol = this.audio.volume * (this._graph ? this._graph.out.gain.value : 1);
    const abort = () => { if (job.done) return; job.done = true; try { ghost.pause(); ghost.removeAttribute("src"); ghost.load(); } catch { /* ignore */ } if (this._xf === job) this._xf = null; };
    const timer = setTimeout(abort, 2500);
    ghost.addEventListener("loadedmetadata", () => { try { ghost.currentTime = this.audio.currentTime + 0.15; } catch { /* ignore */ } }, { once: true });
    ghost.addEventListener("playing", () => {
      if (job.done) return;
      clearTimeout(timer);
      job.done = true;
      const left = Math.max(1, (this.duration || d) - this.audio.currentTime);
      const at = performance.now();
      this._fadeIn = { at, dur: Math.min(xf, left) };
      this.next(true); // titre suivant (sa montée est gérée par _applyVolume)
      const step = () => {
        const k = (performance.now() - at) / (left * 1000);
        if (k >= 1 || ghost.ended) { abort(); this._xf = null; return; }
        ghost.volume = Math.max(0, Math.min(1, startVol * (1 - k)));
        this._applyVolume();
        setTimeout(step, 60);
      };
      step();
    }, { once: true });
    ghost.addEventListener("error", abort, { once: true });
    ghost.volume = Math.max(0, Math.min(1, startVol));
    ghost.src = this.audio.currentSrc;
    const p = ghost.play();
    if (p && p.catch) p.catch(abort); // (lecture refusée par le navigateur : fin normale du titre)
  }

  // ------------------------------------------------------------ minuterie : baisse progressive
  _sleepFade() {
    if (this.mode !== "local") return 1;
    if (this.sleep.at) {
      const left = (this.sleep.at - Date.now()) / 1000;
      const span = Math.min(300, Math.max(12, (this.sleep.total || 600) / 3)); // jusqu'à 5 min de descente
      return left >= span ? 1 : Math.max(0, left / span) ** 1.6;
    }
    if (this.sleep.end && this.duration) {
      const left = this.duration - this.audio.currentTime;
      return left >= 25 ? 1 : Math.max(0, left / 25) ** 1.6;
    }
    return 1;
  }
  setVolume(v) {
    this._volAt = performance.now();
    this.volume = Math.max(0, Math.min(1, v));
    this.muted = this.volume === 0;
    this._applyVolume();
    this.audio.muted = false;
    if (this.mode === "pi") this._piVolume();
    this.emit("volume");
    this._save();
  }
  toggleMute() {
    if (this.mode === "pi") {
      if (this.volume > 0) { this._prevVol = this.volume; this.setVolume(0); } else this.setVolume(this._prevVol || 0.7);
      return;
    }
    this.muted = !this.muted;
    this.audio.muted = this.muted;
    this.emit("volume");
  }

  // ------------------------------------------------------------ minuterie de sommeil
  /** minutes : nombre, "end" (fin du titre) ou 0 (désactiver). */
  setSleep(minutes) {
    if (this.mode === "pi") {
      return this._pi("sleep", { minutes: minutes === "end" ? "end" : Number(minutes) || 0 }).then(() => this.emit("sleep"));
    }
    this.sleep = minutes === "end" ? { at: 0, end: true }
      : { at: Number(minutes) > 0 ? Date.now() + Number(minutes) * 60000 : 0, end: false, total: Number(minutes) * 60 };
    this._applyVolume();
    this.emit("sleep");
    return Promise.resolve();
  }
  _checkSleep() {
    if ((!this.sleep.at && !this.sleep.end) || this.audio.paused) return;
    const left = this.sleep.at ? (this.sleep.at - Date.now()) / 1000 : 1;
    if (left <= 0) {
      this.sleep = { at: 0, end: false };
      this.wantPlay = false;
      this.audio.pause();
      this._applyVolume();
      this.emit("sleep");
    } else {
      this._applyVolume(); // baisse progressive du volume (les dernières minutes)
    }
  }

  toggleShuffle() {
    this.shuffle = !this.shuffle;
    const cur = this.currentId;
    if (this.shuffle) {
      this.original = this.queue.slice();
      const upcoming = shuffleArr(this.queue.slice(this.index + 1));
      this.queue = [...this.queue.slice(0, this.index + 1), ...upcoming];
    } else if (this.original) {
      this.queue = this.original;
      this.index = Math.max(0, this.queue.indexOf(cur));
      this.original = null;
    }
    this.manualEnd = this.index;
    this.autoFrom = -1;
    this._queueChanged();
    this.emit("state");
  }

  cycleRepeat() {
    this.repeat = { off: "all", all: "one", one: "off" }[this.repeat];
    if (this.mode === "pi") this._pi("repeat", { mode: this.repeat });
    this.emit("state");
    this._save();
  }

  setAutoplay(on) {
    this.autoplay = !!on;
    storage.set("onde.autoplay", this.autoplay);
    if (on) this._maybeAutoplay();
    else if (this.autoFrom > this.index && this.mode === "local") { // retire les titres pas encore joués
      this.queue = this.queue.slice(0, this.autoFrom);
      this.autoFrom = -1;
      this._queueChanged();
    }
    this.emit("queue");
  }

  // Lecture automatique : titres similaires ajoutés avant la fin de la file.
  _maybeAutoplay() {
    if (!this.autoplay || this.repeat !== "off" || this.mode !== "local" || this._autoLoading || offline.active) return;
    if (this.index < this.queue.length - 2) return;
    this._autoLoading = true;
    const seeds = this.queue.slice(Math.max(0, this.index - 4), this.index + 1);
    const exclude = this.queue.slice(-300);
    api.get(`/api/radio?tracks=${seeds.join(",")}&exclude=${exclude.join(",")}&limit=25`)
      .then((r) => {
        const ids = (r.tracks || []).map((t) => t.id).filter((id) => !this.queue.includes(id));
        if (!ids.length || this.mode !== "local" || !this.autoplay) return;
        if (this.autoFrom < 0) this.autoFrom = this.queue.length;
        this.queue.push(...ids);
        if (this.original) this.original.push(...ids);
        this.emit("queue");
        this._save(true);
      })
      .catch(() => {})
      .finally(() => { this._autoLoading = false; });
  }

  // ------------------------------------------------------------ file d'attente
  playNext(ids) {
    ids = [].concat(ids);
    if (!this.hasTrack) return this.playList(ids, 0, null);
    const at = this.index + 1;
    this.queue.splice(at, 0, ...ids);
    this.manualEnd = Math.max(this.manualEnd, this.index) + ids.length;
    if (this.autoFrom >= at) this.autoFrom += ids.length;
    if (this.original) this.original.push(...ids);
    this._queueChanged();
  }
  addToQueue(ids) {
    ids = [].concat(ids);
    if (!this.hasTrack) return this.playList(ids, 0, null);
    const at = Math.max(this.index, this.manualEnd) + 1;
    this.queue.splice(at, 0, ...ids);
    this.manualEnd = at + ids.length - 1;
    if (this.autoFrom >= at) this.autoFrom += ids.length;
    if (this.original) this.original.push(...ids);
    this._queueChanged();
  }
  removeAt(i) {
    if (i === this.index || i < 0 || i >= this.queue.length) return;
    this.queue.splice(i, 1);
    if (i < this.index) this.index--;
    if (i <= this.manualEnd) this.manualEnd--;
    if (this.autoFrom > i) this.autoFrom--;
    if (this.autoFrom >= this.queue.length) this.autoFrom = -1;
    this._queueChanged();
  }
  move(from, to) {
    if (from === to || from === this.index) return;
    const cur = this.currentId;
    const [id] = this.queue.splice(from, 1);
    this.queue.splice(to, 0, id);
    this.index = this.queue.indexOf(cur);
    this._queueChanged();
  }
  clearUpcoming() {
    this.queue = this.queue.slice(0, this.index + 1);
    this.manualEnd = this.index;
    this.autoFrom = -1;
    this.original = null;
    this._queueChanged();
  }
  _queueChanged() {
    this._memAsked = null;
    if (this.mode === "pi") this._piSync({ keep: true });
    else this._maybeAutoplay();
    this.emit("queue");
    this._save(true);
  }

  // ------------------------------------------------------------ <audio>
  _bindAudio() {
    const a = this.audio;
    a.volume = this.volume;
    a.addEventListener("ended", () => this.next(true));
    for (const ev of ["play", "pause", "playing", "waiting"]) {
      a.addEventListener(ev, () => { this.emit("state"); this._positionState(); });
    }
    a.addEventListener("playing", () => { this.retries = 0; clearTimeout(this._stallT); });
    a.addEventListener("play", () => { this.wantPlay = true; });
    // pause venant du système (appel, casque débranché…) : on ne relancera pas tout seul
    a.addEventListener("pause", () => {
      if (!a.ended && performance.now() - (this._startedAt || 0) > 800) this.wantPlay = false;
    });
    a.addEventListener("loadedmetadata", () => {
      if (this.pendingSeek != null) {
        try { a.currentTime = this.pendingSeek; } catch { /* ignore */ }
        this.pendingSeek = null;
      }
      this.emit("time");
      this._positionState();
    });
    a.addEventListener("timeupdate", () => {
      if (this.mode !== "local") return;
      const t = a.currentTime;
      if (t > 0) this.lastPos = t;
      if (!this.counted && t >= Math.min(30, (this.duration || 60) * 0.5)) {
        this.counted = true;
        const id = this.currentId;
        api.post(`/api/tracks/${id}/played`).then(() => emitBus("played", { id }))
          .catch((e) => { if (isNetErr(e)) offline.queuePlay(id); }); // hors connexion : compté plus tard
      }
      if (this.duration && this.duration - t < 25) this._warmNext();
      if (t > 6 && this._memAsked !== this.index && !offline.active) {
        // le titre en cours a assez d'avance (20 s) : on prend le suivant en entier, sans le gêner
        const b = a.buffered;
        const ahead = b.length ? b.end(b.length - 1) - t : 0;
        if (ahead > 20 || (this.duration && b.length && b.end(b.length - 1) >= this.duration - 1) || a.currentSrc.startsWith("blob:")) {
          this._memAsked = this.index;
          this._memFetch(this.queue[this.index + 1] ?? (this.repeat === "all" ? this.queue[0] : null));
        }
      }
      this._maybeCrossfade(t);
      if (this._fadeIn) this._applyVolume();
      this._checkSleep();
      this._save();
    });
    a.addEventListener("error", () => this._onError());
    a.addEventListener("loadedmetadata", () => { this._g.dirty = true; });
    a.addEventListener("emptied", () => { this._g.dirty = true; });
    setInterval(() => { try { this._guardTick(); } catch { /* le garde-fou ne doit jamais gêner la lecture */ } }, 1000);
    for (const ev of ["hashchange", "visibilitychange", "focus", "pageshow"]) {
      addEventListener(ev, () => { try { this._guardWake(); } catch { /* ignore */ } });
    }
    // flux bloqué (Wi-Fi en veille…) : on relance après 10 s d'attente
    a.addEventListener("waiting", () => {
      clearTimeout(this._stallT);
      this._stallT = setTimeout(() => {
        if (this.mode === "local" && this.wantPlay && a.readyState < 3) this._reload();
      }, 10000);
    });
    // retour au premier plan : resynchronise l'interface
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) { this.emit("state"); this.emit("time"); this.emit("sleep"); }
    });
  }

  // ------------------------------------------------------------ garde-fou du son
  /** Au changement de page / retour dans l'appli : remet le volume et le moteur audio d'équerre. */
  _guardWake() {
    if (this.mode !== "local") return;
    const a = this.audio;
    if (a.muted && !this.muted) a.muted = false;
    if (this._graph && this._graph.ctx.state !== "running") this._graph.ctx.resume?.().catch(() => {});
    this._applyVolume();
  }
  /** Niveau sonore actuel (0 à 1) de ce qui sort du lecteur, ou null si on ne peut pas le mesurer. */
  _level() {
    const g = this._g, a = this.audio;
    const fromGraph = !!this._graph;
    if (g.an && ((fromGraph && g.from !== "graph") || (!fromGraph && g.from === "graph"))) g.dirty = true;
    if (g.dirty || !g.an) {
      g.dirty = false;
      try {
        if (g.node) { try { g.node.disconnect(); } catch { /* ignore */ } g.node = null; }
        g.an = null;
        if (fromGraph) {
          const an = this._graph.ctx.createAnalyser();
          an.fftSize = 512;
          this._graph.out.connect(an);
          g.an = an; g.from = "graph";
        } else if (a.captureStream) {
          const stream = a.captureStream();
          if (!stream.getAudioTracks().length) { g.dirty = true; return null; } // (pas encore de piste audio)
          g.mctx = g.mctx || new (window.AudioContext || window.webkitAudioContext)();
          if (g.mctx.state !== "running") g.mctx.resume?.().catch(() => {});
          g.node = g.mctx.createMediaStreamSource(stream);
          const an = g.mctx.createAnalyser();
          an.fftSize = 512;
          g.node.connect(an); // (rien n'est rebranché vers la sortie : on ne fait que mesurer)
          g.an = an; g.from = "stream";
        }
        g.buf = g.an ? new Float32Array(g.an.fftSize) : null;
      } catch { g.an = null; g.dirty = false; g.dead = (g.dead || 0) + 1; return null; }
    }
    if (!g.an) return null;
    g.an.getFloatTimeDomainData(g.buf);
    let sum = 0;
    for (let i = 0; i < g.buf.length; i++) sum += g.buf[i] * g.buf[i];
    return Math.sqrt(sum / g.buf.length);
  }
  _guardTick() {
    const g = this._g, a = this.audio;
    const idle = this.mode !== "local" || a.paused || a.ended || !this.wantPlay || a.readyState < 3
      || !a.currentSrc || a.currentSrc === SILENT_SRC || this._xf || this._fadeIn;
    const pos = a.currentTime, now = performance.now();
    if (this.currentId !== g.id) { g.id = this.currentId; g.step = 0; g.since = 0; g.ok = 0; }
    if (idle) { g.since = 0; g.ok = 0; g.pos = pos; return; }
    if (a.muted && !this.muted) a.muted = false;               // (jamais coupé sans que tu l'aies demandé)
    if (this._graph && this._graph.ctx.state !== "running") this._graph.ctx.resume?.().catch(() => {});
    if (g.dead > 3) return;
    const moving = pos - g.pos > 0.4;                           // la lecture avance
    g.pos = pos;
    const expected = this.volume * this.gain * this._sleepFade();
    if (!moving || expected < 0.04 || pos < 2) { g.since = 0; g.ok = 0; return; }
    const lvl = this._level();
    if (lvl == null) return;
    if (lvl > 0.0006) { g.since = 0; g.ok = Math.min(g.ok + 1, 5); if (g.ok >= 5) g.step = 0; return; }
    if (!g.since) { g.since = now; g.n = 0; }
    g.n++;
    if (now - g.since < 5500 || g.n < 3) return;
    // la lecture avance depuis plus de 5 secondes mais rien ne sort : on répare, par étapes
    g.since = 0; g.ok = 0;
    g.step++;
    console.info("[onde] son muet pendant la lecture → réparation", g.step);
    if (g.step === 1) {                       // 1) volume, moteur audio, et petit « réveil » de la sortie
      this._guardWake();
      try { a.currentTime = Math.max(0, a.currentTime - 0.05); } catch { /* ignore */ }
      this._play();
    } else if (g.step === 2) {                // 2) on recharge le titre en direct depuis le serveur (sans la copie en mémoire)
      const url = this._mem.get(this.currentId);
      if (url) { this._mem.delete(this.currentId); try { URL.revokeObjectURL(url); } catch { /* ignore */ } }
      this.lastPos = pos;
      this.retries = 0;
      this._reload();
    } else if (g.step === 3) {                // 3) dernier recours : on remet tout à zéro
      for (const u of this._mem.values()) { try { URL.revokeObjectURL(u); } catch { /* ignore */ } }
      this._mem.clear();
      this.lastPos = pos;
      this.retries = 0;
      this._reload();
    }
  }

  async _onError() {
    const src = this.audio.currentSrc || "";
    if (this.mode !== "local" || !src || src === SILENT_SRC) return;
    if (src.startsWith("blob:") && [...this._mem.values()].includes(src)) { // copie en mémoire abîmée
      URL.revokeObjectURL(src);
      this._mem.delete(this.currentId);
      this._reload();
      return;
    }
    if (src.startsWith("blob:")) { // copie locale abîmée : on repasse par le Pi (ou on saute, hors connexion)
      this._badLocal.add(this.currentId);
      if (offline.active) {
        this.dispatchEvent(new CustomEvent("problem", { detail: "Ce titre téléchargé est illisible — titre suivant." }));
        if (this.index + 1 < this.queue.length) this.next(false);
        return;
      }
      this._reload();
      return;
    }
    const code = this.audio.error ? this.audio.error.code : 0;
    if (code === 3 || code === 4) {
      // Erreur de « format » : soit le fichier est vraiment illisible, soit le Wi-Fi a coupé
      // pile au changement de titre. On vérifie que le serveur répond avant de passer au suivant.
      let reachable = false;
      try {
        await fetch(`/api/tracks/${this.currentId}/stream`, { headers: { Range: "bytes=0-1" }, cache: "no-store" });
        reachable = true;
      } catch { /* réseau indisponible */ }
      if (reachable) {
        const t = this.current;
        if (t && t.ext && canPlayExt(t.ext) && !src.includes("t=1")) {
          // le navigateur croyait savoir lire ce format : on réessaie avec la version convertie
          markUnplayable(t.ext);
          this._reload();
          return;
        }
        this.dispatchEvent(new CustomEvent("problem", { detail: "Ce titre n'est pas lisible — titre suivant." }));
        if (this.index + 1 < this.queue.length) this.next(false);
        return;
      }
    }
    this._reload();
  }

  // Le titre suivant démarre plus vite : le Pi prépare sa conversion (formats lus par conversion,
  // la plus lente) et relit le début du fichier pendant que le titre en cours se termine.
  _warmNext() {
    if (this._warmed === this.index || this.mode !== "local" || offline.active) return;
    this._warmed = this.index;
    const nextId = this.queue[this.index + 1] ?? (this.repeat === "all" ? this.queue[0] : null);
    if (nextId == null || offline.has(nextId)) return;
    fetch(this._remoteSrc(nextId), { headers: { Range: "bytes=0-1" }, cache: "no-store" }).catch(() => {});
  }

  _reload() {
    if (this.retries === 0) offline.probe(); // le Pi répond-il encore ? (sinon : hors connexion)
    if (offline.active && !offline.has(this.currentId)) {
      const i = this._available(this.index + 1);
      if (i >= 0) {
        this.dispatchEvent(new CustomEvent("problem", { detail: "Hors connexion : lecture de ta musique téléchargée." }));
        this.index = i;
        this._start(0, this.wantPlay);
        return;
      }
    }
    if (this.retries >= 10) {
      this.dispatchEvent(new CustomEvent("problem", { detail: "Connexion au Raspberry Pi perdue." }));
      return;
    }
    this.retries++;
    const pos = this.lastPos;
    const play = this.wantPlay;
    setTimeout(() => {
      if (this.mode !== "local") return;
      if (offline.active && !offline.has(this.currentId)) { this._reload(); return; } // hors ligne entre-temps
      this._startedAt = performance.now();
      this.pendingSeek = pos > 0.5 ? pos : null;
      this.audio.src = this._src(this.currentId, true);
      if (play) this._play(); else this.audio.load();
    }, Math.min(1000 * this.retries, 5000));
  }

  // ------------------------------------------------------------ Media Session
  _bindMediaSession() {
    const ms = navigator.mediaSession;
    if (!ms) return;
    const set = (action, fn) => { try { ms.setActionHandler(action, fn); } catch { /* non supporté */ } };
    set("play", () => this.play());
    set("pause", () => this.pause());
    set("stop", () => this.pause());
    set("previoustrack", () => this.prev());
    set("nexttrack", () => this.next());
    set("seekto", (d) => { if (d.seekTime != null) this.seek(d.seekTime); });
    // Pas de « ±10 s » : sur iPhone ils remplaceraient les boutons titre précédent / suivant.
    set("seekbackward", null);
    set("seekforward", null);
  }
  _updateMediaSession() {
    const ms = navigator.mediaSession;
    const t = this.current;
    if (!ms || !t) return;
    const abs = (p) => new URL(p, location.href).href;
    try {
      ms.metadata = new MediaMetadata({
        title: t.title, artist: t.artist, album: t.album,
        artwork: t.cover ? [192, 384, 768].map((s) => {
          const src = artworkUrl(t.cover, s);
          return { src: abs(src), sizes: `${s}x${s}`, type: src.startsWith("blob:") ? "image/webp" : "image/jpeg" };
        }) : [{ src: abs("/static/icons/icon-512.png"), sizes: "512x512", type: "image/png" }],
      });
    } catch { /* ignore */ }
  }
  _positionState() {
    const ms = navigator.mediaSession;
    if (!ms) return;
    try { ms.playbackState = this.hasTrack ? (this.paused ? "paused" : "playing") : "none"; } catch { /* ignore */ }
    if (!ms.setPositionState) return;
    const d = this.duration;
    if (!d || !Number.isFinite(d)) return;
    try { ms.setPositionState({ duration: d, position: Math.min(this.position, d), playbackRate: 1 }); } catch { /* ignore */ }
  }

  // ------------------------------------------------------------ persistance
  _save(now = false) {
    const write = () => storage.set(KEY, {
      queue: this.queue.slice(0, 3000), index: this.index, pos: this.lastPos, shuffle: this.shuffle,
      repeat: this.repeat, volume: this.volume, original: this.original ? this.original.slice(0, 3000) : null,
      manualEnd: this.manualEnd, autoFrom: this.autoFrom, source: this.source, mode: this.mode,
    });
    if (now) { write(); return; }
    const t = Date.now();
    if (t - this._saveT > 4000) { this._saveT = t; write(); }
  }
  _restore() {
    const s = storage.get(KEY);
    if (!s || !Array.isArray(s.queue)) return;
    this.queue = s.queue;
    this.index = Math.min(s.index ?? -1, this.queue.length - 1);
    this.lastPos = s.pos || 0;
    this.shuffle = !!s.shuffle;
    this.repeat = ["off", "all", "one"].includes(s.repeat) ? s.repeat : "off";
    this.volume = typeof s.volume === "number" ? s.volume : 0.8;
    this.audio.volume = this.volume;
    this.original = s.original || null;
    this.manualEnd = s.manualEnd ?? this.index;
    this.autoFrom = s.autoFrom ?? -1;
    this.source = s.source || null;
    this._wantedMode = s.mode;
  }
  async init() {
    if (this.queue.length) {
      try { await ensureTracks(this.queue); } catch { /* serveur absent */ }
      if (offline.active) {
        // hors connexion : on garde la file telle quelle (elle reviendra entière avec le Pi) ;
        // on se place seulement sur un titre présent sur l'appareil
        if (!offline.has(this.currentId)) {
          const i = this._available(Math.max(this.index, 0));
          if (i >= 0 && i !== this.index) { this.index = i; this.lastPos = 0; }
        }
      } else {
        const before = this.queue.length;
        const cur = this.currentId;
        this.queue = this.queue.filter((id) => trackCache.has(id));
        if (this.queue.length !== before) this.autoFrom = -1;
        const at = this.queue.indexOf(cur);
        this.index = at >= 0 ? at : Math.min(Math.max(this.index, 0), this.queue.length - 1);
      }
      if (this.hasTrack && (!offline.active || offline.has(this.currentId))) {
        this.pendingSeek = this.lastPos > 0.5 ? this.lastPos : null;
        this._updateGain();
        this.audio.preload = "metadata";
        this.audio.src = this._src(this.currentId);
        this.audio.preload = "auto";
      }
    }
    this.emit("track"); this.emit("queue"); this.emit("state"); this.emit("volume");
    this._updateMediaSession();
  }

  // ------------------------------------------------------------ mode enceinte
  connectPi(available) {
    this.pi.available = available;
    if (!available || this._es) return;
    const es = new EventSource("/api/pi/events");
    this._es = es;
    es.onmessage = (e) => { try { this._onPiState(JSON.parse(e.data)); } catch { /* ignore */ } };
    es.onerror = () => { /* EventSource se reconnecte tout seul */ };
    if (this._wantedMode === "pi") this.setMode("pi", { adopt: true }).catch(() => {});
  }

  // adopt = reprendre ce que joue déjà le Pi (au rechargement de la page)
  async setMode(mode, { adopt = false } = {}) {
    if (mode === this.mode) return;
    if (mode === "pi") {
      if (!this.pi.available) throw new Error("La lecture sur le Pi n'est pas disponible (installe mpv).");
      const pos = this.audio.currentTime || this.lastPos || 0;
      const playing = !this.audio.paused;
      this.wantPlay = false;
      this.audio.pause();
      this.mode = "pi";
      this.pi.rev = -1;
      const st = this.pi.state || await api.get("/api/pi/state").catch(() => null);
      if (!adopt && this.hasTrack) {
        await this._piSync({ position: pos, play: playing });
        this._piVolume();
        if (this.sleep.at || this.sleep.end) { // la minuterie suit la musique sur le Pi
          const left = this.sleep.end ? "end" : Math.max(0.1, (this.sleep.at - Date.now()) / 60000);
          this.sleep = { at: 0, end: false };
          this._pi("sleep", { minutes: left }).catch(() => {});
        }
      } else if (st) {
        if (st.active) this._onPiState(st);
        this.volume = (st.volume ?? 70) / 100;
        this.emit("volume");
      }
    } else {
      const st = this.pi.state;
      const pos = this.position;
      const wasPlaying = st && !st.paused;
      this.mode = "local";
      if (st && st.active) {
        this.queue = st.ids.slice();
        this.index = st.index;
        this.autoFrom = st.autoFrom ?? -1;
        await ensureTracks(this.queue);
        this._pi("pause").catch(() => {});
      }
      this._wantedMode = "local";
      if (this.hasTrack) this._start(pos, !!wasPlaying);
    }
    this._wantedMode = mode;
    this._save(true);
    this.emit("mode"); this.emit("state"); this.emit("queue"); this.emit("track"); this.emit("sleep");
  }

  async _pi(action, params = {}, optimistic = null) {
    if (optimistic && this.pi.state) { Object.assign(this.pi.state, optimistic); this.pi.at = performance.now(); this.emit("state"); }
    try {
      const st = await api.post("/api/pi/cmd", { action, ...params });
      this._onPiState(st);
      return st;
    } catch (e) {
      this.dispatchEvent(new CustomEvent("problem", { detail: e.message }));
      throw e;
    }
  }
  _piSync({ position = 0, play = true, keep = false } = {}) {
    return this._pi("set_queue", {
      ids: this.queue, index: this.index, position, play, keep, repeat: this.repeat, autoFrom: this.autoFrom,
    }).catch(() => {});
  }
  _piVolume() {
    clearTimeout(this._volT);
    this._volT = setTimeout(() => this._pi("volume", { value: Math.round(this.volume * 100) }).catch(() => {}), 150);
  }
  _onPiState(st) {
    const prev = this.pi.state;
    this.pi.state = st;
    this.pi.at = performance.now();
    if (this.mode !== "pi") { this.emit("pi"); return; }
    const trackChanged = st.trackId !== this.currentId;
    if (st.rev !== this.pi.rev || trackChanged) {
      this.pi.rev = st.rev;
      this.queue = st.ids.slice();
      this.index = st.index;
      this.autoFrom = st.autoFrom ?? -1;
      const missing = this.queue.filter((id) => !trackCache.has(id));
      const done = () => { this.emit("queue"); if (trackChanged) { this.emit("track"); } };
      if (missing.length) ensureTracks(missing).then(done); else done();
    }
    this.repeat = st.repeat;
    if (typeof st.volume === "number" && performance.now() - (this._volAt || 0) > 1500) {
      const v = st.volume / 100;
      if (Math.abs(v - this.volume) > 0.005) { this.volume = v; this.emit("volume"); }
    }
    if (!prev || prev.sleepIn !== st.sleepIn || prev.sleepEnd !== st.sleepEnd) this.emit("sleep");
    this.emit("state"); this.emit("time"); this.emit("pi");
  }
}
