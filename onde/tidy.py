"""Rangement automatique de la bibliothèque, au démarrage et après chaque scan :

- les titres sont raccourcis (« MOONLIGHT🌙~ Kali Uchis || [AMV] 4K » → « MOONLIGHT — Kali Uchis ») ;
  seul le nom affiché change, jamais les fichiers ;
- les morceaux dont le titre ou le nom de fichier contient « Slowed » ou « Reverb » (réglable) sont
  déplacés dans une corbeille récupérable (`corbeille/` dans les données d'Onde, vidée après 30 jours).
  Garde-fou : si ces mots concernent plus de la moitié de la bibliothèque, rien n'est supprimé.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import clean, config, db, library, settings

log = logging.getLogger("onde.tidy")
TRASH = config.DATA_DIR / "corbeille"
KEEP_DAYS = 30
MAX_SHARE = 0.5
_lock = threading.Lock()


def words() -> list[str]:
    return [w for w in (clean.fold(x) for x in re.split(r"[,;\n]", str(settings.get("unwanted_words")))) if w]


def unwanted(title: str | None, path: str | None, ws: list[str] | None = None) -> str | None:
    """Le mot interdit trouvé dans le titre ou le nom du fichier (mot entier, sans accents ni casse)."""
    ws = words() if ws is None else ws
    if not ws:
        return None
    text = clean.fold(f"{title or ''} {Path(path or '').stem}")
    for w in ws:
        if re.search(rf"(?<![^\W_]){re.escape(w)}(?![^\W_])", text):
            return w
    return None


def _metas() -> list[Path]:
    """Les fiches des morceaux de la corbeille (pas la liste des morceaux autorisés)."""
    return sorted(m for m in TRASH.glob("*.json") if re.fullmatch(r"[0-9a-f]{12}", m.stem)) if TRASH.is_dir() else []


def _allowed() -> set[str]:
    return set(_index(TRASH / "autorises.json").get("paths", []))


def allow(path: str) -> None:
    """Un morceau récupéré à la main n'est plus remis à la corbeille."""
    TRASH.mkdir(parents=True, exist_ok=True)
    (TRASH / "autorises.json").write_text(json.dumps({"paths": sorted(_allowed() | {path})}), encoding="utf-8")


def shorten_all() -> int:
    """Raccourcit les titres déjà en bibliothèque ; renvoie le nombre de titres modifiés."""
    rows = db.q("""SELECT t.id, t.title, t.artist, t.search, a.name AS album, a.artist AS album_artist, t.genre
                   FROM tracks t JOIN albums a ON a.id = t.album_id""")
    changes = []
    for r in rows:
        new = clean.title(r["title"])
        if new and new != r["title"]:
            search = db.norm(" ".join(filter(None, (new, r["artist"], r["album"], r["album_artist"], r["genre"]))))
            changes.append((new, search, r["id"]))
    if changes:
        with db.tx():
            db.many("UPDATE tracks SET title=?, search=? WHERE id=?", changes)
        library.bump()
        log.info("%d titres raccourcis", len(changes))
    return len(changes)


def _index(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def trash(row, word: str) -> bool:
    """Déplace un morceau dans la corbeille et le retire de la bibliothèque."""
    src = Path(row["path"])
    TRASH.mkdir(parents=True, exist_ok=True)
    key = uuid.uuid4().hex[:12]
    dest = TRASH / f"{key}{src.suffix}"
    try:
        if src.exists():
            shutil.move(str(src), str(dest))
    except OSError as exc:
        log.warning("corbeille : %s impossible (%s)", src.name, exc)
        return False
    info = {"id": key, "path": str(src), "file": dest.name, "title": row["title"], "artist": row["artist"],
            "word": word, "at": time.time()}
    (TRASH / f"{key}.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    with db.tx():
        db.x("DELETE FROM tracks WHERE id=?", row["id"])
    d = src.parent  # dossiers devenus vides
    while d not in config.EXTRA_DIRS and d != config.MUSIC_DIR and config.MUSIC_DIR in d.parents:
        try:
            d.rmdir()
        except OSError:
            break
        d = d.parent
    return True


def sweep() -> dict:
    """Met à la corbeille les morceaux « Slowed » / « Reverb » ; renvoie {removed, skipped}."""
    ws = words()
    rows = db.q("SELECT id, path, title, artist FROM tracks")
    ok = _allowed()
    hits = [(r, w) for r in rows if r["path"] not in ok and (w := unwanted(r["title"], r["path"], ws))]
    if not hits:
        return {"removed": 0, "skipped": 0}
    if len(hits) > max(10, len(rows) * MAX_SHARE):
        log.warning("%d morceaux sur %d contiennent %s : trop pour une suppression automatique, rien n'est supprimé",
                    len(hits), len(rows), ", ".join(ws))
        return {"removed": 0, "skipped": len(hits)}
    done = 0
    for r, w in hits:
        if trash(r, w):
            done += 1
            log.info("corbeille : « %s » — %s (%s)", r["title"], r["artist"], w)
    if done:
        with db.tx():
            library.cleanup_albums()
        library.bump()
    return {"removed": done, "skipped": len(hits) - done}


def purge() -> int:
    n = 0
    for meta in _metas():
        info = _index(meta)
        if time.time() - info.get("at", time.time()) > KEEP_DAYS * 86400:
            (TRASH / info.get("file", "")).unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            n += 1
    return n


def run() -> dict:
    out = {"shortened": 0, "removed": 0}
    with _lock:
        if settings.get("shorten_titles"):
            out["shortened"] = shorten_all()
        if settings.get("remove_unwanted"):
            out["removed"] = sweep()["removed"]
        purge()
    return out


def items() -> list[dict]:
    got = [_index(m) for m in _metas()]
    return sorted((i for i in got if i), key=lambda i: -i.get("at", 0))


def restore(key: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", key or ""):
        raise KeyError(key)
    meta = TRASH / f"{key}.json"
    info = _index(meta)
    src = TRASH / info.get("file", "x")
    if not info or not src.exists():
        raise KeyError(key)
    dest = Path(info["path"])
    if dest.exists():
        dest = dest.with_name(f"{dest.stem} (récupéré){dest.suffix}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    meta.unlink(missing_ok=True)
    allow(str(dest))
    return {"path": str(dest)}


def empty() -> int:
    n = 0
    for meta in _metas():
        (TRASH / _index(meta).get("file", "x")).unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        n += 1
    return n


# ------------------------------------------------------------------ doublons et qualité
def merge_into(keep: int, remove: list[int], why: str = "doublon") -> int:
    """Garde `keep` et met les autres à la corbeille : likes, playlists et historique passent sur celui gardé."""
    remove = [r for r in dict.fromkeys(int(x) for x in remove) if r != keep]
    if not db.q1("SELECT 1 FROM tracks WHERE id=?", keep):
        raise KeyError(keep)
    done = 0
    for rid in remove:
        row = db.q1("SELECT id, path, title, artist, plays, last_played FROM tracks WHERE id=?", rid)
        if not row:
            continue
        with db.tx():
            db.x("INSERT OR IGNORE INTO likes(user_id, track_id, liked_at) SELECT user_id, ?, liked_at FROM likes WHERE track_id=?",
                 keep, rid)
            db.x("UPDATE playlist_tracks SET track_id=? WHERE track_id=?", keep, rid)
            db.x("UPDATE plays SET track_id=? WHERE track_id=?", keep, rid)
            db.x("""UPDATE tracks SET plays = plays + ?, last_played = MAX(COALESCE(last_played, 0), ?) WHERE id=?""",
                 row["plays"] or 0, row["last_played"] or 0, keep)
            db.x("INSERT OR IGNORE INTO lyrics(track_id, synced, plain, source, instrumental, fetched) "
                 "SELECT ?, synced, plain, source, instrumental, fetched FROM lyrics WHERE track_id=?", keep, rid)
        if trash(row, why):
            done += 1
    if done:
        with db.tx():
            library.cleanup_albums()
        library.bump()
    return done


def low_quality(max_kbps: int = 128) -> list[dict]:
    """Titres compressés à faible débit (MP3, AAC… à 128 kbit/s ou moins) : à remplacer par une meilleure version."""
    rows = db.q("""SELECT t.id, t.title, t.artist, t.bitrate, t.ext, t.duration FROM tracks t
                   WHERE t.bitrate > 0 AND t.bitrate <= ? ORDER BY t.bitrate, t.artist COLLATE NOCASE""", max_kbps * 1000 + 999)
    return [{"id": r["id"], "title": r["title"], "artist": r["artist"], "kbps": round(r["bitrate"] / 1000),
             "ext": r["ext"] or "", "duration": r["duration"]} for r in rows if (r["ext"] or "").lower() not in library.LOSSLESS]
