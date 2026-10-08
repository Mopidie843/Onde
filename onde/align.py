"""Paroles calées sur la voix : un texte brut devient un fichier .lrc synchronisé, ligne par ligne.

Moteur : stable-whisper (`stable-ts`), sur le processeur, dans un processus séparé
(`onde.align_worker`) pour ne jamais bloquer ni faire tomber Onde. Le son est décodé par ffmpeg :
celui du système, ou celui fourni par `imageio-ffmpeg` (rien à installer à côté).

Tout seul, en arrière-plan : dès qu'Onde a des paroles non synchronisées pour un titre (trouvées
sur LRCLIB, intégrées au fichier, ou collées à la main), il les cale sur la voix, un titre à la
fois, les plus écoutés d'abord. Le résultat est enregistré dans la base et écrit en .lrc à côté du
morceau (sans jamais écraser un .lrc synchronisé existant).

En ligne de commande, pour n'importe quel fichier :
    python -m onde.align chanson.mp3 paroles.txt [-o chanson.lrc] [--langue fr] [--modele base]
"""
from __future__ import annotations

import argparse
import collections
import importlib
import importlib.util
import json
import logging
import math
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, db, lyrics, notify, settings

log = logging.getLogger("onde.align")

# modèles proposés → mémoire libre nécessaire (Go, marge comprise) ; « base » : bon compromis sur Pi 5
MODELS = {"tiny": 0.6, "base": 0.9, "small": 1.9, "medium": 4.8}
DEFAULT_MODEL = "base"
LANGUAGES = ("fr", "en", "es", "de", "it", "pt")
MAX_DURATION = 20 * 60      # au-delà (mix, livre audio…), on ne cale pas automatiquement
MIN_LINES = 2
GAP_BLANK = 8.0             # silence de plus de 8 s après une ligne : ligne vide (« ♪ ») dans le .lrc
MIN_PROB = 0.12             # confiance moyenne minimale de l'alignement (sinon : mauvaises paroles)
MAX_ZERO = 0.6              # part maximale de mots sans durée (idem)
RETRY_DELAY = 86400.0       # échec passager (mémoire, moteur) : nouvel essai le lendemain
OFFLINE_DELAY = 3600.0      # modèle pas encore téléchargé et pas d'Internet : dans 1 h
MAX_TRIES = 3               # échecs passagers avant d'abandonner (le bouton « Caler » reste possible)
LRC_TAG = "[re:Onde · calage IA (stable-whisper)]"
SOURCE_SUFFIX = " · calées sur la voix (IA)"

AUTO_START_DELAY = 90.0     # laisse Onde démarrer, scanner et chercher les paroles d'abord
AUTO_PAUSE = 3.0
AUTO_IDLE = 900.0

MESSAGES = {
    "audio": "Fichier audio illisible ou abîmé",
    "memory": "Mémoire insuffisante",
    "model": "Modèle d'IA indisponible",
    "missing": "Module d'IA non installé",
    "mismatch": "Les paroles ne correspondent pas à ce qui est chanté (mauvaises paroles ou autre version ?)",
    "engine": "Le calage a échoué",
    "timeout": "Le calage a pris trop de temps",
    "skip": "Pas de calage automatique",
    "cancelled": "Calage interrompu",
    "install": "Installation impossible",
}


class AlignError(Exception):
    def __init__(self, kind: str, message: str = ""):
        self.kind = kind
        self.detail = message
        base = MESSAGES.get(kind, MESSAGES["engine"])
        super().__init__(f"{base} : {message}" if message and message != base else base)


# ------------------------------------------------------------------ texte
_ts = re.compile(r"\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]")
_tag = re.compile(r"^\[[a-z]{2,8}:.*\]$", re.I)              # [ar:…] [ti:…] (en-têtes LRC)
_section = re.compile(r"^[\[\({].*[\]\)}]$")                    # [Refrain], (Couplet 2), {Pont}
_label = re.compile(r"^(refrain|chorus|couplet|verse|pont|bridge|intro|outro|pre-?chorus|pr[ée]-?refrain"
                    r"|hook|interlude|instrumental)\b[^a-z]*\d*\s*:?\s*(x\s*\d+)?$", re.I)


def prepare_lines(text: str) -> list[str]:
    """Les lignes à caler : vides retirées, ainsi que les repères qui ne se chantent pas
    ([Refrain], « Couplet 2 : », en-têtes et horodatages LRC)."""
    out = []
    for raw in (text or "").splitlines():
        line = _ts.sub("", raw).strip()
        line = re.sub(r"\s+", " ", line)
        if not line or _tag.match(line) or _section.match(line) or _label.match(line):
            continue
        out.append(line)
    return out


_STOPWORDS = {
    "fr": "je tu il elle nous vous ils le la les un une des du de et est pas que qui dans pour sur avec "
          "mon ma mes ton ta tes moi toi ne plus tout ce cette mais au aux comme on suis c'est j'ai",
    "en": "i you he she we they the a an and is are not that what in for on with my your me it's "
          "don't i'm all be love oh baby just can no so this when",
    "es": "yo tú el ella nosotros los las un una y es no que en por con mi tu me te se lo como pero "
          "más del al mí quiero amor",
    "de": "ich du er sie wir ihr der die das und ist nicht ein eine zu mit mich dich mein dein auf es "
          "wie so was auch nur noch",
    "it": "io tu lui lei noi voi il lo la gli le un una e è non che di per con mi ti si mio tuo come "
          "ma più sono amore",
    "pt": "eu você ele ela nós o a os as um uma e é não que de em por com meu minha me te se como mas "
          "mais do da amor",
}
_STOP = {k: set(v.split()) for k, v in _STOPWORDS.items()}


def guess_language(lines: list[str]) -> str | None:
    """Langue des paroles d'après leurs petits mots (le, the, el…). None si ce n'est pas net :
    Whisper la devine alors à l'écoute du morceau."""
    words = re.findall(r"[\w']+", " ".join(lines).lower())
    if len(words) < 12:
        return None
    scores = {lang: sum(1 for w in words if w in stop) for lang, stop in _STOP.items()}
    best = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (lang, top), (_, second) = best[0], best[1]
    if top >= max(4, len(words) * 0.08) and top >= second * 1.4:
        return lang
    return None


def fmt_ts(seconds: float) -> str:
    """12.345 → « 00:12.35 » (format LRC [mm:ss.xx])."""
    cs = max(0, int(math.floor(float(seconds) * 100 + 0.5 + 1e-6)))  # 12.345 → 12.35 (pas 12.34)
    m, cs = divmod(cs, 6000)
    return f"{m:02d}:{cs // 100:02d}.{cs % 100:02d}"


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", "", db.norm(s or ""))


def match_segments(lines: list[str], segments: list[dict]) -> list[dict]:
    """Associe chaque ligne à son segment. Avec original_split=True il y en a autant, dans l'ordre ;
    sinon on recolle les segments ligne par ligne d'après le texte. AlignError si impossible."""
    if len(segments) == len(lines):
        return [{**s, "text": line} for s, line in zip(segments, lines)]
    out, i = [], 0
    for line in lines:
        target, acc, start, probs, words, zero = _norm(line), "", None, [], 0, 0
        while i < len(segments) and len(acc) < len(target):
            seg = segments[i]
            start = seg["start"] if start is None else start
            acc += _norm(seg["text"])
            probs.append(seg.get("prob", 0) * max(1, seg.get("words", 1)))
            words += max(1, seg.get("words", 1))
            zero += seg.get("zero", 0)
            end = seg["end"]
            i += 1
        if start is None or acc != target:
            raise AlignError("mismatch", "découpage des lignes non respecté")
        out.append({"start": start, "end": end, "text": line, "prob": sum(probs) / max(1, words),
                    "words": words, "zero": zero})
    return out


def assess(segments: list[dict]) -> None:
    """Refuse un alignement douteux : mieux vaut pas de synchro qu'une synchro fausse."""
    words = sum(max(0, s.get("words", 0)) for s in segments)
    if not words:
        raise AlignError("mismatch", "aucun mot reconnu")
    zero = sum(s.get("zero", 0) for s in segments) / words
    prob = sum(s.get("prob", 0) * s.get("words", 0) for s in segments) / words
    if zero > MAX_ZERO or prob < MIN_PROB:
        raise AlignError("mismatch", f"confiance {prob:.0%}, {zero:.0%} de mots non placés")


def build_lrc(segments: list[dict], duration: float = 0, title: str = "", artist: str = "",
              album: str = "") -> str:
    """Segments (une ligne chacun) → texte LRC « [mm:ss.xx] ligne », heures toujours croissantes."""
    head = []
    for key, value in (("ti", title), ("ar", artist), ("al", album)):
        value = re.sub(r"\s+", " ", re.sub(r"[\[\]\r\n]", " ", value or "")).strip()
        if value:
            head.append(f"[{key}:{value}]")
    if duration:
        head.append(f"[length:{fmt_ts(duration)[:5]}]")
    head.append(LRC_TAG)
    body, prev = [], -1.0
    for n, seg in enumerate(segments):
        start = max(float(seg["start"]), prev + 0.01)
        if duration:
            start = min(start, max(0.0, duration - 0.01))
        body.append(f"[{fmt_ts(start)}] {seg['text']}")
        prev = start
        end = float(seg.get("end") or start)
        nxt = float(segments[n + 1]["start"]) if n + 1 < len(segments) else None
        # long passage sans chant (solo, fin du morceau) : la dernière ligne ne reste pas allumée
        if end > start and ((nxt is not None and nxt - end > GAP_BLANK)
                            or (nxt is None and duration and duration - end > GAP_BLANK)):
            blank = end + 0.5
            if nxt is None or blank < nxt:
                body.append(f"[{fmt_ts(blank)}]")
                prev = blank
    return "\n".join(head + body) + "\n"


# ------------------------------------------------------------------ moteur
def unavailable_reason() -> str | None:
    """None si le calage est possible, sinon pourquoi."""
    if config.FROZEN:
        return "non disponible dans Onde.exe (lance Onde avec start.bat ou start.sh pour en profiter)"
    for mod in ("torch", "stable_whisper"):
        try:
            if importlib.util.find_spec(mod) is None:
                return "module d'IA non installé"
        except (ImportError, ValueError):
            return "module d'IA non installé"
    if not ffmpeg_exe():
        return "ffmpeg introuvable"
    return None


def available() -> bool:
    return unavailable_reason() is None


def ffmpeg_exe() -> str | None:
    """ffmpeg du système, sinon celui fourni par imageio-ffmpeg."""
    found = config.FFMPEG or shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def mem_available() -> float | None:
    """Mémoire disponible en Go (Linux), None si inconnue."""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def model_name() -> str:
    name = (settings.get("align_model") or "").strip() or config.ALIGN_MODEL or DEFAULT_MODEL
    return name if name in MODELS or os.path.isfile(name) else DEFAULT_MODEL


def models_dir() -> Path:
    return config.DATA_DIR / "whisper"


def check_memory(model: str) -> None:
    need, free = MODELS.get(model, MODELS[DEFAULT_MODEL]), mem_available()
    if free is not None and free < need:
        smaller = [m for m, n in MODELS.items() if n <= free]
        hint = f" Choisis le modèle « {smaller[-1]} » dans les Réglages." if smaller else \
            " Ferme d'autres programmes sur le serveur."
        raise AlignError("memory", f"{free:.1f} Go libres, il en faut {need:.1f} pour « {model} ».{hint}")


WORKER_CMD: list[str] | None = None  # (tests : remplace le travailleur)


def _threads() -> int:
    n = os.cpu_count() or 2
    return max(1, min(4, n - 1))  # un cœur reste libre pour Onde et la musique


def run_job(job: dict, on_message=None, timeout: float | None = None,
            cancel: threading.Event | None = None) -> dict:
    """Lance le travailleur pour une tâche, attend son résultat. Lève AlignError."""
    job = {"threads": _threads(), "download_root": str(models_dir()), "ffmpeg": ffmpeg_exe() or "",
           "nice": 10, **job}
    models_dir().mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
               PYTHONPATH=os.pathsep.join(p for p in (str(config.ROOT), os.environ.get("PYTHONPATH")) if p))
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    try:
        proc = subprocess.Popen(WORKER_CMD or [sys.executable, "-m", "onde.align_worker"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", env=env, cwd=str(config.ROOT),
                                creationflags=flags)
    except OSError as exc:
        raise AlignError("engine", f"impossible de lancer le calage : {exc}")
    messages: queue.Queue = queue.Queue()
    errors: collections.deque[str] = collections.deque(maxlen=40)

    def read_out():
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("{"):
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    pass
        messages.put(None)

    def read_err():
        for line in proc.stderr:
            if line.strip():
                errors.append(line.rstrip())

    threading.Thread(target=read_out, daemon=True, name="onde-align-out").start()
    threading.Thread(target=read_err, daemon=True, name="onde-align-err").start()
    try:
        proc.stdin.write(json.dumps(job))
        proc.stdin.close()
    except OSError:
        pass
    deadline = time.monotonic() + timeout if timeout else None
    result, why = None, None
    while True:
        try:
            msg = messages.get(timeout=0.5)
        except queue.Empty:
            msg = ...
        if msg is None:
            break
        if isinstance(msg, dict):
            if "ok" in msg:
                result = msg
            elif on_message:
                try:
                    on_message(msg)
                except Exception:
                    pass
        if cancel is not None and cancel.is_set():
            why = "cancelled"
        elif deadline and time.monotonic() > deadline:
            why = "timeout"
        if why:
            proc.kill()
            break
    try:
        rc = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = proc.wait()
    if why:
        raise AlignError(why)
    if result is None:
        tail = "\n".join(errors)
        if rc in (-9, 137) or "Killed" in tail or _oom(tail):
            raise AlignError("memory", "le système a arrêté le calage faute de mémoire — "
                                       "choisis un modèle plus petit dans les Réglages")
        last = next((l for l in reversed(errors) if l.strip() and not l.startswith(" ")), "")
        raise AlignError("engine", last[-300:] or f"le calage s'est arrêté (code {rc})")
    if not result.get("ok"):
        raise AlignError(result.get("error") or "engine", result.get("message") or "")
    return result


def _oom(text: str) -> bool:
    t = text.lower()
    return "out of memory" in t or "cannot allocate memory" in t or "memoryerror" in t


def align_text(audio: str, text: str, language: str | None = None, model: str | None = None,
               duration: float = 0, on_message=None, cancel: threading.Event | None = None,
               check: bool = True) -> tuple[list[dict], str]:
    """Cale `text` sur `audio`. Renvoie (segments, langue) : un segment par ligne gardée."""
    lines = prepare_lines(text)
    if len(lines) < MIN_LINES:
        raise AlignError("skip", "il faut au moins deux lignes de paroles")
    model = model or DEFAULT_MODEL
    check_memory(model)
    language = language if language in LANGUAGES else (language or guess_language(lines))
    timeout = min(3 * 3600, 600 + (duration or 600) * 8)  # le Pi est lent, mais jamais bloqué à vie
    res = run_job({"audio": os.path.abspath(str(audio)), "lines": lines, "language": language, "model": model,
                   "failure_threshold": MAX_ZERO}, on_message=on_message, timeout=timeout, cancel=cancel)
    segments = match_segments(lines, res.get("segments") or [])
    if check:
        assess(segments)
    return segments, res.get("language") or language or ""


def write_lrc(audio_path: str, lrc: str) -> Path | None:
    """Écrit le .lrc à côté du morceau. Ne remplace jamais un .lrc synchronisé qui n'est pas d'Onde ;
    un .lrc de paroles brutes est gardé en .txt. None si le dossier n'est pas modifiable."""
    p = Path(audio_path)
    target = p.with_suffix(".lrc")
    try:
        for cand in (target, p.with_suffix(".LRC")):
            if cand.is_file():
                old = cand.read_text(encoding="utf-8", errors="replace")
                if lyrics.is_synced(old) and LRC_TAG not in old:
                    return None
                if not lyrics.is_synced(old) and old.strip() and not p.with_suffix(".txt").exists():
                    p.with_suffix(".txt").write_text(old, encoding="utf-8")
                target = cand
                break
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(lrc, encoding="utf-8")
        os.replace(tmp, target)
        return target
    except OSError as exc:
        log.info("fichier .lrc non écrit (%s) : %s", target, exc)
        try:
            target.with_name(target.name + ".tmp").unlink(missing_ok=True)
        except OSError:
            pass
        return None


# ------------------------------------------------------------------ bibliothèque
_lock = threading.Lock()
_priority: collections.deque[int] = collections.deque()  # demandes manuelles, servies en premier
_current: dict = {}
_last: dict = {}
_stop = threading.Event()
_wake = threading.Event()
_cancel = threading.Event()
_thread: threading.Thread | None = None
install_state: dict = {"running": False, "step": "", "error": None, "done": False}


def due(limit: int = 20, now: float | None = None) -> list[int]:
    """Titres aux paroles brutes, à caler (jamais essayés, ou échec passager dont l'heure est venue)."""
    rows = db.q("""SELECT l.track_id FROM lyrics l JOIN tracks t ON t.id = l.track_id
                   WHERE l.plain IS NOT NULL AND l.synced IS NULL AND l.instrumental = 0
                     AND (l.align_state IS NULL OR (l.align_state = 'retry' AND COALESCE(l.align_next, 0) <= ?))
                     AND COALESCE(t.duration, 0) <= ?
                   ORDER BY l.align_state IS NOT NULL, COALESCE(t.last_played, 0) DESC, t.plays DESC, t.id DESC
                   LIMIT ?""", time.time() if now is None else now, MAX_DURATION, limit)
    return [r["track_id"] for r in rows]


def _set_state(tid: int, state: str | None, error: str | None = None, tries: int | None = None,
               next_try: float | None = None) -> None:
    db.x("UPDATE lyrics SET align_state=?, align_error=?, align_tries=COALESCE(?, align_tries), align_next=? "
         "WHERE track_id=?", state, error, tries, next_try, tid)


def align_track(tid: int, manual: bool = False) -> dict:
    """Cale les paroles brutes d'un titre de la bibliothèque. Renvoie {"ok": bool, "message": …}."""
    t = db.q1("""SELECT t.id, t.path, t.title, t.artist, t.duration, t.album_id, a.name AS album
                 FROM tracks t JOIN albums a ON a.id = t.album_id WHERE t.id = ?""", tid)
    row = db.q1("SELECT * FROM lyrics WHERE track_id = ?", tid)
    if not t or not row or not row["plain"] or row["synced"]:
        return {"ok": False, "message": "rien à caler"}
    plain = row["plain"]
    with _lock:
        _current.clear()
        _current.update({"track_id": tid, "title": t["title"], "artist": t["artist"], "phase": "audio",
                         "progress": 0.0, "started": time.time()})

    def on_message(msg):
        with _lock:
            if "phase" in msg:
                _current["phase"] = msg["phase"]
            if "progress" in msg:
                _current["progress"] = msg["progress"]

    model = model_name()
    lang = settings.get("align_language")
    lang = lang if lang in LANGUAGES else None
    try:
        if not manual and (t["duration"] or 0) > MAX_DURATION:
            raise AlignError("skip", "morceau de plus de 20 minutes")
        segments, _language = align_text(t["path"], plain, language=lang, model=model,
                                         duration=t["duration"] or 0, on_message=on_message, cancel=_cancel)
    except AlignError as exc:
        return _failed(tid, row, exc, t)
    finally:
        with _lock:
            _current.clear()
    lrc = build_lrc(segments, t["duration"] or 0, t["title"], t["artist"],
                    "" if t["album"] == config.SINGLES else t["album"])
    source = (row["source"] or "paroles").replace(SOURCE_SUFFIX, "") + SOURCE_SUFFIX
    cur = db.x("""UPDATE lyrics SET synced=?, source=?, align_state='done', align_error=NULL, align_next=NULL
                  WHERE track_id=? AND plain=? AND synced IS NULL""", lrc, source, tid, plain)
    if not getattr(cur, "rowcount", 1):
        return {"ok": False, "message": "paroles modifiées entre-temps"}
    written = write_lrc(t["path"], lrc) if settings.get("align_write_lrc") else None
    msg = f"{len(segments)} lignes calées" + (f", fichier {written.name}" if written else "")
    _remember(tid, t, True, msg)
    log.info("paroles calées sur la voix : %s — %s (%s)", t["artist"], t["title"], msg)
    try:
        notify.lyrics_synced(t["title"], t["artist"], t["album_id"])
    except Exception:
        log.exception("notification des paroles")
    return {"ok": True, "message": msg, "lrc": lrc, "file": str(written) if written else None}


def _failed(tid: int, row, exc: AlignError, t) -> dict:
    kind, text = exc.kind, str(exc)
    if kind == "cancelled":
        return {"ok": False, "message": text}
    if kind in ("memory", "engine", "timeout"):
        tries = (row["align_tries"] or 0) + 1
        if tries >= MAX_TRIES:
            _set_state(tid, "gaveup", text, tries)
        else:
            _set_state(tid, "retry", text, tries, time.time() + RETRY_DELAY)
    elif kind == "model":  # le plus souvent : pas d'Internet pour télécharger le modèle la 1re fois
        _set_state(tid, "retry", text, None, time.time() + OFFLINE_DELAY)
    elif kind == "missing":
        _set_state(tid, None, None)
    else:  # audio, mismatch, skip : inutile de réessayer tant que rien ne change
        _set_state(tid, kind, text)
    _remember(tid, t, False, text)
    log.info("calage des paroles impossible pour %s — %s : %s", t["artist"], t["title"], text)
    return {"ok": False, "message": text, "error": kind}


def _remember(tid, t, ok, message):
    with _lock:
        _last.clear()
        _last.update({"track_id": tid, "title": t["title"], "artist": t["artist"], "ok": ok,
                      "message": message, "at": time.time()})


def request(tid: int) -> dict:
    """Calage demandé depuis l'interface : en tête de file, même si un essai a échoué avant."""
    row = db.q1("SELECT plain, synced FROM lyrics WHERE track_id=?", tid)
    if not row or not row["plain"] or row["synced"]:
        raise AlignError("skip", "il faut d'abord des paroles non synchronisées")
    db.x("UPDATE lyrics SET align_state=NULL, align_error=NULL, align_tries=0, align_next=NULL WHERE track_id=?",
         tid)
    with _lock:
        if tid in _priority:
            _priority.remove(tid)
        _priority.appendleft(tid)
    start_background(delay=0)
    wake()
    return track_status(tid)


def track_status(tid: int, row=None) -> dict:
    """État du calage d'un titre, pour le panneau des paroles."""
    reason = unavailable_reason()
    if row is None:
        row = db.q1("SELECT * FROM lyrics WHERE track_id=?", tid)
    with _lock:
        running = _current.get("track_id") == tid
        cur = dict(_current) if running else {}
        queued = tid in _priority
    state = row["align_state"] if row is not None else None
    auto = bool(settings.get("auto_align"))
    soon = state is None or (state == "retry" and (row["align_next"] or 0) <= time.time() + 60)
    return {"available": reason is None, "reason": reason, "state": "running" if running else state,
            "error": row["align_error"] if row is not None else None,
            "queued": not running and (queued or (auto and reason is None and soon)),
            "next_try": row["align_next"] if row is not None and state == "retry" else None,
            "phase": cur.get("phase"), "progress": cur.get("progress"), "auto": auto}


def decorate(tid: int, data: dict) -> dict:
    """Ajoute l'état du calage aux paroles renvoyées par l'API (seulement pour des paroles brutes)."""
    if data.get("found") and data.get("plain") and not data.get("synced"):
        return {**data, "align": track_status(tid)}
    return data


def status() -> dict:
    reason = unavailable_reason()
    counts = db.q1("""SELECT SUM(align_state='done') done,
                             SUM(plain IS NOT NULL AND synced IS NULL AND instrumental=0
                                 AND (align_state IS NULL OR align_state='retry')) pending,
                             SUM(align_state IN ('mismatch','audio','gaveup','skip')) failed
                      FROM lyrics""")
    with _lock:
        current, last = dict(_current), dict(_last)
    free = mem_available()
    return {"available": reason is None, "reason": reason, "frozen": config.FROZEN,
            "install": dict(install_state), "auto": bool(settings.get("auto_align")),
            "writeLrc": bool(settings.get("align_write_lrc")),
            "model": model_name(), "models": [{"name": m, "memory": n, "fits": free is None or free >= n}
                                              for m, n in MODELS.items()],
            "language": settings.get("align_language"), "memory": round(free, 1) if free else None,
            "current": current or None, "last": last or None,
            "done": counts["done"] or 0, "pending": counts["pending"] or 0, "failed": counts["failed"] or 0}


def run_once() -> str:
    """Un titre. "idle" (rien à faire), "done", "stopped" ou "missing" (module absent)."""
    if _stop.is_set():
        return "stopped"
    if not available():
        return "missing"
    with _lock:
        tid = _priority.popleft() if _priority else None
    manual = tid is not None
    if tid is None:
        if not settings.get("auto_align"):
            return "idle"
        ids = due(1)
        if not ids:
            return "idle"
        tid = ids[0]
    try:
        res = align_track(tid, manual=manual)
    except Exception:
        log.exception("calage des paroles du titre %s", tid)
        db.x("UPDATE lyrics SET align_state='retry', align_error=?, align_tries=align_tries+1, align_next=? "
             "WHERE track_id=?", MESSAGES["engine"], time.time() + RETRY_DELAY, tid)
        return "done"
    if res.get("error") == "missing":
        return "missing"
    return "done"


def _sleep(seconds: float) -> None:
    _wake.wait(seconds)
    _wake.clear()


def _loop(delay: float) -> None:
    # attente de démarrage (Onde scanne et cherche les paroles d'abord), écourtée par une demande manuelle
    deadline = time.monotonic() + delay
    while not _stop.is_set() and not _priority and time.monotonic() < deadline:
        _wake.wait(max(0.0, deadline - time.monotonic()))
        _wake.clear()
    if _stop.is_set():
        return
    while not _stop.is_set():
        _wake.clear()
        try:
            state = run_once()
        except Exception:
            log.exception("calage automatique des paroles")
            state = "idle"
        if state in ("idle", "missing"):
            _sleep(AUTO_IDLE)
        elif state == "done":
            _stop.wait(AUTO_PAUSE)


def start_background(delay: float | None = None) -> None:
    global _thread
    delay = AUTO_START_DELAY if delay is None else delay
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _cancel.clear()
        _thread = threading.Thread(target=_loop, args=(delay,), name="onde-align", daemon=True)
        _thread.start()


def wake() -> None:
    _wake.set()


def stop() -> None:
    _stop.set()
    _cancel.set()  # arrête aussi le calage en cours (le processus de calage est tué)
    _wake.set()


def on_lyrics(track_id: int, res: dict) -> None:
    """Appelé par lyrics quand des paroles brutes sont enregistrées : le calage reprend tout de suite."""
    if res.get("plain") and not res.get("synced"):
        wake()


# ------------------------------------------------------------------ installation
_install_lock = threading.Lock()


def _pip(args: list[str], timeout: float = 3600) -> None:
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", *args]
    log.info("installation du calage IA : %s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AlignError("install", "installation trop longue (connexion lente ?) — réessaie")
    except OSError as exc:
        raise AlignError("install", str(exc))
    if r.returncode != 0:
        out = (r.stderr or "") + (r.stdout or "")
        if "externally-managed" in out:
            raise AlignError("install", "ce Python est géré par le système — lance Onde avec ./start.sh")
        if "No space left" in out:
            raise AlignError("install", "plus de place sur le disque (il faut environ 1,5 Go)")
        lines = [line.strip() for line in out.splitlines() if line.strip() and not line.startswith("[notice]")]
        last = lines[-1] if lines else f"pip a échoué (code {r.returncode})"
        if "No matching distribution" in last or "connection" in out.lower() or "403" in out:
            last += " — vérifie que le serveur a accès à Internet, puis réessaie"
        raise AlignError("install", last)


def install_commands() -> list[list[str]]:
    """Commandes pip : torch d'abord (version processeur seul sur PC Linux : 10 fois plus légère
    que la version pour carte graphique), puis stable-ts et imageio-ffmpeg."""
    cmds = []
    if sys.platform.startswith("linux") and platform.machine().lower() in ("x86_64", "amd64"):
        cmds.append(["torch", "torchaudio", "--index-url", "https://download.pytorch.org/whl/cpu"])
    cmds.append(["stable-ts>=2.17", "imageio-ffmpeg>=0.5"])
    return cmds


def install() -> None:
    """Installe le calage IA en arrière-plan (bouton des Réglages) et télécharge le modèle."""
    if not _install_lock.acquire(blocking=False):
        return
    try:
        install_state.update(running=True, error=None, done=False, step="Préparation…")
        if config.FROZEN:
            raise AlignError("missing", "non disponible dans Onde.exe")
        if not available():
            cmds = install_commands()
            for n, args in enumerate(cmds, 1):
                install_state["step"] = (f"Téléchargement du moteur d'IA ({n}/{len(cmds)}) — quelques minutes, "
                                         "environ 1 Go")
                _pip(args)
            importlib.invalidate_caches()
            if not available():
                raise AlignError("missing", unavailable_reason() or "")
        install_state["step"] = f"Téléchargement du modèle « {model_name()} »…"
        run_job({"download_only": True, "model": model_name()}, timeout=3600)
        install_state.update(done=True, step="Prêt")
        start_background(delay=5)
        wake()
    except AlignError as exc:
        install_state["error"] = str(exc)
    except Exception as exc:
        log.exception("installation du calage IA")
        install_state["error"] = str(exc)
    finally:
        install_state["running"] = False
        _install_lock.release()


def start_install() -> dict:
    if not install_state["running"]:
        threading.Thread(target=install, name="onde-align-install", daemon=True).start()
        time.sleep(0.05)
    return dict(install_state)


# ------------------------------------------------------------------ ligne de commande
def _install_cli() -> int:
    """`python -m onde.align install` (./start.sh ia) : même installation que le bouton des Réglages."""
    logging.basicConfig(level=logging.WARNING)
    db.init()
    print("Installation du calage des paroles par IA (environ 1 Go, quelques minutes)…", file=sys.stderr)
    done = threading.Event()

    def show():
        last = ""
        while not done.wait(2):
            if install_state["step"] != last:
                last = install_state["step"]
                print(f"  {last}", file=sys.stderr)

    threading.Thread(target=show, daemon=True).start()
    install()
    done.set()
    if install_state["error"]:
        print(f"✖ {install_state['error']}", file=sys.stderr)
        return 1
    print(f"✔ Prêt (modèle « {model_name()} »). Onde calera tout seul les paroles non synchronisées "
          "— redémarre-le s'il tournait : ./start.sh restart", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["install"]:
        return _install_cli()
    ap = argparse.ArgumentParser(prog="python -m onde.align",
                                 description="Cale des paroles brutes sur la voix et écrit un fichier .lrc.")
    ap.add_argument("audio", help="fichier audio (MP3, FLAC, WAV, M4A…)")
    ap.add_argument("paroles", help="fichier texte des paroles, une ligne par ligne chantée (- = entrée standard)")
    ap.add_argument("-o", "--sortie", help="fichier .lrc à écrire (par défaut : à côté du morceau)")
    ap.add_argument("--langue", default=None, help="fr, en, es… (par défaut : devinée)")
    ap.add_argument("--modele", default=os.environ.get("ONDE_ALIGN_MODEL") or DEFAULT_MODEL,
                    help=f"{', '.join(MODELS)} (par défaut : {DEFAULT_MODEL})")
    ap.add_argument("--sans-controle", action="store_true", help="garder le résultat même s'il semble douteux")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    reason = unavailable_reason()
    if reason:
        print(f"Calage impossible : {reason}.\n  pip install -r requirements-ai.txt", file=sys.stderr)
        return 2
    try:
        text = sys.stdin.read() if a.paroles == "-" else Path(a.paroles).read_text(encoding="utf-8-sig")
    except OSError as exc:
        print(f"Paroles illisibles : {exc}", file=sys.stderr)
        return 2

    def show(msg):
        if "phase" in msg:
            print({"audio": "Lecture du son…", "model": "Chargement du modèle…", "language": "Langue…",
                   "align": "Calage…"}.get(msg["phase"], ""), file=sys.stderr)
        elif "progress" in msg:
            print(f"  {msg['progress']:.0%}", file=sys.stderr)

    duration = 0.0
    try:
        import mutagen
        f = mutagen.File(a.audio)
        duration = float(getattr(getattr(f, "info", None), "length", 0) or 0)
    except Exception:
        pass
    try:
        segments, lang = align_text(a.audio, text, language=a.langue, model=a.modele, duration=duration,
                                    on_message=show, check=not a.sans_controle)
    except AlignError as exc:
        print(f"✖ {exc}", file=sys.stderr)
        return 1
    lrc = build_lrc(segments, duration, Path(a.audio).stem)
    out = Path(a.sortie) if a.sortie else Path(a.audio).with_suffix(".lrc")
    out.write_text(lrc, encoding="utf-8")
    print(f"✔ {len(segments)} lignes calées ({lang}) → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
