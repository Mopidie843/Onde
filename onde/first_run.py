"""Musique installée par défaut : la playlist de départ est téléchargée toute seule, en arrière-plan,
au premier démarrage (le serveur démarre tout de suite, sans attendre).

Chaque titre n'est téléchargé qu'une fois : ceux déjà présents dans la bibliothèque sont sautés,
ceux qui échouent (pas d'Internet…) sont retentés aux démarrages suivants (3 essais au plus).
Réglage « Playlist de départ » pour le désactiver. Suivi : `starter.json` dans les données d'Onde.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from . import config, db, downloader, library, settings

log = logging.getLogger("onde.starter")

# (artiste, titre) — la playlist de départ
TRACKS = [
    ("Shakira, Ed Sheeran & Beéle", "Hips Don't Lie (Anniversary Version)"),
    ("Mastu", "Quotidien"),
    ("Mastu", "Déprime"),
    ("Mastu", "Balade à deux"),
    ("JOYCA", "Confinés"),
    ("JOYCA", "Clash Assassins"),
    ("Post Malone & Swae Lee", "Sunflower (Spider-Man: Into the Spider-Verse)"),
    ("Dominic Fike", "Babydoll"),
    ("GIMS, La Mano 1.9 & SCH", "Un monde à l'autre"),
    ("jimmysquare", "Overwatch rap battle (Version Coupé)"),
    ("Nafeesisboujee", "Stephanie"),
    ("Black M", "Mme Pavoshko"),
    ("Sexion d'Assaut", "Ma direction"),
    ("JOYCA", "Millionaire"),
]
MAX_TRIES = 3
STATE = config.DATA_DIR / "starter.json"
_stop = threading.Event()
_started: list = []
state_info = {"running": False, "done": 0, "total": len(TRACKS), "current": None}


def _key(artist: str, title: str) -> str:
    return db.norm(f"{artist} {title}")


def _load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        log.warning("suivi de la playlist de départ non enregistré : %s", exc)


def _main_artist(artist: str) -> str:
    for sep in (",", "&", " feat", " ft.", " x "):
        artist = artist.split(sep)[0]
    return artist.strip()


def in_library(artist: str, title: str) -> bool:
    """Déjà là ? (titre et artiste principal présents dans la recherche de la bibliothèque)"""
    words = [w for w in (db.norm(title.split("(")[0]), db.norm(_main_artist(artist))) if w]
    if not words:
        return False
    sql = " AND ".join("search LIKE ? ESCAPE '\\'" for _ in words)
    esc = lambda w: "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return bool(db.q1(f"SELECT 1 FROM tracks WHERE {sql} LIMIT 1", *map(esc, words)))


def pending() -> list[tuple[str, str]]:
    data = _load()
    out = []
    for artist, title in TRACKS:
        st = data.get(_key(artist, title), {})
        if not st.get("ok") and st.get("tries", 0) < MAX_TRIES:
            out.append((artist, title))
    return out


def _worker() -> None:
    # laisse le serveur démarrer et le premier scan se terminer (pour ne pas retélécharger ce qui est là)
    for _ in range(120):
        if _stop.is_set() or library.status.get("last_scan"):
            break
        time.sleep(1)
    if _stop.is_set() or not settings.get("starter_pack"):
        return
    todo = pending()
    if not todo:
        return
    if not downloader.available():
        log.info("playlist de départ : yt-dlp absent, téléchargement remis à plus tard")
        return
    state_info.update(running=True, done=0, total=len(todo))
    log.info("playlist de départ : %d titres à installer", len(todo))
    data = _load()
    for i, (artist, title) in enumerate(todo):
        if _stop.is_set() or not settings.get("starter_pack"):
            break
        key = _key(artist, title)
        st = data.setdefault(key, {"tries": 0})
        state_info["current"] = f"{title} — {artist}"
        if in_library(artist, title):
            st["ok"] = True
            st["skipped"] = True
        else:
            st["tries"] += 1
            try:
                res = downloader.search_download(f"{artist} - {title}")
                st["ok"] = True
                got = res.get("track") or (res.get("tracks") or [{}])[0]
                log.info("playlist de départ : « %s » installé (%s)", title, got.get("title", "?"))
            except Exception as exc:
                st["error"] = str(exc)[:200]
                log.warning("playlist de départ : « %s — %s » non téléchargé : %s", title, artist, exc)
        _save(data)
        state_info["done"] = i + 1
        _stop.wait(2)  # (pas de rafale de requêtes vers YouTube / SoundCloud)
    state_info.update(running=False, current=None)


def run() -> None:
    """Lance l'installation en arrière-plan (ne bloque jamais le démarrage)."""
    if _started:
        return
    _stop.clear()
    t = threading.Thread(target=_worker, name="onde-starter", daemon=True)
    _started.append(t)
    t.start()


def stop() -> None:
    _stop.set()
    _started.clear()
