"""Nouveautés des artistes suivis : Onde regarde régulièrement (Deezer) si un artiste que tu suis a sorti
un album ou un single, et le télécharge tout seul (comme une recherche par nom), une fois.

Seules les sorties parues APRÈS le moment où tu as commencé à suivre l'artiste sont prises (pas toute sa
discographie). Au plus 3 sorties et 30 titres par artiste et par vérification.
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.parse

from . import covers, db, downloader, first_run, notify

log = logging.getLogger("onde.follow")
CHECK_EVERY = 12 * 3600
MAX_RELEASES = 3
MAX_TRACKS = 30
_stop = threading.Event()
_lock = threading.Lock()
status = {"running": False, "last": None, "downloaded": 0, "error": None}

SCHEMA = """
CREATE TABLE IF NOT EXISTS follows(
    user_id INT NOT NULL, artist_key TEXT NOT NULL, name TEXT NOT NULL, since REAL NOT NULL,
    PRIMARY KEY(user_id, artist_key)
);
CREATE TABLE IF NOT EXISTS releases(
    id TEXT PRIMARY KEY, artist_key TEXT NOT NULL, title TEXT NOT NULL, date TEXT, cover TEXT,
    tracks INT NOT NULL DEFAULT 0, downloaded INT NOT NULL DEFAULT 0, seen REAL
);
"""


def ensure() -> None:
    db.connect().executescript(SCHEMA)


def following() -> list[dict]:
    ensure()
    rows = db.q("SELECT * FROM follows WHERE user_id = onde_uid() ORDER BY name COLLATE NOCASE")
    out = []
    for r in rows:
        rel = db.q("SELECT * FROM releases WHERE artist_key=? ORDER BY date DESC LIMIT 5", r["artist_key"])
        out.append({"name": r["name"], "since": r["since"],
                    "releases": [{"title": x["title"], "date": x["date"], "cover": x["cover"], "tracks": x["tracks"],
                                  "downloaded": x["downloaded"]} for x in rel]})
    return out


def is_following(name: str) -> bool:
    ensure()
    return bool(db.q1("SELECT 1 FROM follows WHERE user_id = onde_uid() AND artist_key=?", db.norm(name)))


def follow(name: str) -> None:
    ensure()
    name = " ".join((name or "").split())[:120]
    if not name:
        raise ValueError("Artiste vide")
    db.x("INSERT OR IGNORE INTO follows(user_id, artist_key, name, since) VALUES(onde_uid(), ?, ?, ?)",
         db.norm(name), name, time.time())


def unfollow(name: str) -> None:
    ensure()
    db.x("DELETE FROM follows WHERE user_id = onde_uid() AND artist_key=?", db.norm(name))


def _deezer_artist(name: str) -> dict | None:
    q = urllib.parse.urlencode({"q": name, "limit": 5})
    data = covers.deezer_api(f"https://api.deezer.com/search/artist?{q}")
    for a in (data.get("data") or []) if isinstance(data, dict) else []:
        if db.norm(a.get("name")) == db.norm(name):
            return a
    return None


def _check_artist(key: str, name: str, since: float) -> int:
    artist = _deezer_artist(name)
    if not artist:
        return 0
    data = covers.deezer_api(f"https://api.deezer.com/artist/{artist['id']}/albums?limit=25")
    since_day = time.strftime("%Y-%m-%d", time.localtime(since))
    fresh = [a for a in (data.get("data") or []) if (a.get("release_date") or "") >= since_day
             and not db.q1("SELECT 1 FROM releases WHERE id=?", f"dz:{a.get('id')}")]
    fresh.sort(key=lambda a: a.get("release_date") or "")
    got = 0
    for alb in fresh[:MAX_RELEASES]:
        tracks = covers.deezer_api(f"https://api.deezer.com/album/{alb['id']}/tracks?limit=50").get("data") or []
        rid = f"dz:{alb['id']}"
        db.x("INSERT OR IGNORE INTO releases(id, artist_key, title, date, cover, tracks, seen) VALUES(?,?,?,?,?,?,?)",
             rid, key, alb.get("title") or "", alb.get("release_date") or "", alb.get("cover_medium") or "",
             len(tracks), time.time())
        n = 0
        for t in tracks[:MAX_TRACKS]:
            if _stop.is_set():
                return got
            a_name = (t.get("artist") or {}).get("name") or name
            title = t.get("title") or ""
            if not title or first_run.in_library(a_name, title):
                continue
            try:
                downloader.search_download(f"{a_name} - {title}")
                n += 1
            except Exception as exc:
                log.info("nouveauté « %s — %s » non téléchargée : %s", title, a_name, exc)
            _stop.wait(2)
        db.x("UPDATE releases SET downloaded=? WHERE id=?", n, rid)
        if n:
            got += n
            notify.add("download", f"Nouveauté : {name}", f"« {alb.get('title')} » — {n} titre(s) ajouté(s)",
                       "#/artist/" + urllib.parse.quote(name))
            log.info("nouveauté de %s : « %s » (%d titres)", name, alb.get("title"), n)
    return got


def check_all() -> dict:
    """Vérifie tous les artistes suivis (par n'importe quel profil)."""
    ensure()
    if not _lock.acquire(blocking=False):
        return dict(status)
    try:
        status.update(running=True, error=None)
        if not downloader.available():
            status["error"] = "yt-dlp absent"
            return dict(status)
        total = 0
        for r in db.q("SELECT artist_key, MIN(name) name, MIN(since) since FROM follows GROUP BY artist_key"):
            if _stop.is_set():
                break
            try:
                total += _check_artist(r["artist_key"], r["name"], r["since"])
            except Exception as exc:
                status["error"] = str(exc)[:200]
                log.info("nouveautés de %s non vérifiées : %s", r["name"], exc)
        status.update(last=time.time(), downloaded=total)
        return dict(status)
    finally:
        status["running"] = False
        _lock.release()


def _loop() -> None:
    if _stop.wait(600):  # (10 min après le démarrage)
        return
    while not _stop.is_set():
        try:
            check_all()
        except Exception:
            log.exception("vérification des nouveautés")
        if _stop.wait(CHECK_EVERY):
            return


def start_background() -> None:
    _stop.clear()
    threading.Thread(target=_loop, name="onde-follow", daemon=True).start()


def stop() -> None:
    _stop.set()
