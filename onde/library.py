"""Bibliothèque : tags, scan incrémental + surveillance des dossiers, import, pochettes,
photos d'artistes, historique d'écoute, analyse du volume."""
from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mutagen
from mutagen.apev2 import APEv2
from mutagen.asf import ASFTags
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover, MP4Tags

from . import clean, config, covers, db, net, notify, settings

log = logging.getLogger("onde.library")

COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "pochette", "artwork")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# tâches réseau (pochettes, photos d'artistes) : un seul thread, poli avec les API publiques
jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="onde-net")
# tâches lourdes en arrière-plan (miniatures, analyse du volume) : un seul thread, basse priorité
bg = ThreadPoolExecutor(max_workers=1, thread_name_prefix="onde-bg")

status = {
    "scanning": False, "scan_total": 0, "scan_done": 0, "last_scan": None,
    "added": 0, "updated": 0, "removed": 0, "errors": 0,
    "covers_running": 0, "covers_found": 0, "artists_running": False,
    "loudness_running": False, "gen": 0, "watching": False,
    "offlineRoots": [],  # dossiers introuvables ou vidés d'un coup : leurs titres sont gardés
}
SAFE_MIN_TRACKS = 20     # en dessous, les titres disparus d'un dossier sont retirés sans se poser de question
SAFE_MAX_GONE = 0.5      # au-delà de cette part des titres d'un dossier disparus d'un coup : disque absent ?
ARTIST_RETRY = 7 * 86400  # artiste sans photo : nouvel essai au bout d'une semaine
_scan_lock = threading.Lock()
_wake = threading.Event()
_stop = threading.Event()
_full_requested = threading.Event()
_bg_pending: set[str] = set()
_bg_lock = threading.Lock()


def bump() -> None:
    """Signale un changement de bibliothèque (invalide les caches)."""
    status["gen"] += 1


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #
def _txt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        parts = dict.fromkeys(str(v).strip() for v in value if str(v).strip())  # sans doublons
        value = ", ".join(parts)
    return str(value).replace("\x00", ", ").strip()


def _int(value) -> int | None:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if isinstance(value, tuple):
        value = value[0]
    m = re.match(r"\s*(\d+)", str(value if value is not None else ""))
    return int(m.group(1)) if m else None


def _year(value) -> int | None:
    m = re.search(r"(1[89]\d\d|20\d\d)", _txt(value))
    return int(m.group(1)) if m else None


def _rg_lufs(value) -> float | None:
    """ReplayGain 2 (référence -18 LUFS) -> volume intégré estimé."""
    m = re.search(r"(-?\+?\d+(?:[.,]\d+)?)", _txt(value))
    if not m:
        return None
    try:
        gain = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return round(-18.0 - gain, 2) if -30 < gain < 30 else None


def _asf_picture(raw: bytes) -> bytes | None:
    """Décode une image WM/Picture (type, taille, mime UTF-16, description UTF-16, données)."""
    try:
        size = struct.unpack_from("<I", raw, 1)[0]
        pos = 5
        for _ in range(2):  # mime puis description, terminés par \x00\x00
            while raw[pos:pos + 2] != b"\x00\x00":
                pos += 2
            pos += 2
        return raw[pos:pos + size]
    except Exception:
        return None


_junk_title = re.compile(
    r"\s*[\(\[][^\)\]]*(official|officiel|lyrics?|paroles|audio|video|vidéo|clip|hd|hq|4k|visuali[sz]er)[^\)\]]*[\)\]]",
    re.I,
)


def parse_filename(stem: str) -> tuple[str, str]:
    """« 03 - Artiste - Titre (Official Video) » -> ("Artiste", "Titre")."""
    s = stem.replace("_", " ").strip()
    s = re.sub(r"^\s*\d{1,3}\s*[-.)\]]\s*", "", s) or s
    s = _junk_title.sub("", s).strip()
    if " - " in s:
        artist, title = s.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", s


def read_meta(path: str | Path, name_hint: str | None = None) -> dict:
    """Lit les tags, la pochette intégrée et le ReplayGain en un seul passage."""
    f = mutagen.File(path)
    if f is None or not getattr(f, "info", None):
        raise ValueError("format audio non reconnu")
    info = f.info
    m = {
        "title": "", "artist": "", "album": "", "album_artist": "",
        "track_no": None, "disc_no": None, "year": None, "genre": "",
        "duration": float(getattr(info, "length", 0) or 0),
        "bitrate": int(getattr(info, "bitrate", 0) or 0),
        "cover": None, "lufs": None,
    }
    tags = f.tags
    if isinstance(tags, ID3):  # MP3, AIFF, WAV
        def g(key):
            return _txt(tags[key].text) if key in tags else ""
        m.update(title=g("TIT2"), artist=g("TPE1"), album=g("TALB"), album_artist=g("TPE2"),
                 track_no=_int(g("TRCK")), disc_no=_int(g("TPOS")),
                 year=_year(g("TDRC") or g("TYER") or g("TDOR")))
        if "TCON" in tags and tags["TCON"].genres:
            m["genre"] = tags["TCON"].genres[0].strip()
        pics = tags.getall("APIC")
        if pics:
            front = next((p for p in pics if p.type == 3), pics[0])
            m["cover"] = front.data
        for fr in tags.getall("TXXX"):
            if fr.desc.lower() == "replaygain_track_gain":
                m["lufs"] = _rg_lufs(fr.text)
    elif isinstance(tags, MP4Tags):
        def g(key):
            return _txt(tags.get(key))
        m.update(title=g("\xa9nam"), artist=g("\xa9ART"), album=g("\xa9alb"), album_artist=g("aART"),
                 year=_year(g("\xa9day")), genre=_txt((tags.get("\xa9gen") or [""])[0]))
        if tags.get("trkn"):
            m["track_no"] = tags["trkn"][0][0] or None
        if tags.get("disk"):
            m["disc_no"] = tags["disk"][0][0] or None
        if tags.get("covr"):
            m["cover"] = bytes(tags["covr"][0])
        rg = tags.get("----:com.apple.iTunes:replaygain_track_gain")
        if rg:
            m["lufs"] = _rg_lufs(bytes(rg[0]).decode("utf-8", "ignore"))
    elif isinstance(tags, APEv2):  # Monkey's Audio, WavPack, Musepack
        def g(key):
            v = tags.get(key)
            return _txt(str(v)) if v is not None and getattr(v, "kind", 0) == 0 else ""
        m.update(title=g("Title"), artist=g("Artist"), album=g("Album"),
                 album_artist=g("Album Artist") or g("AlbumArtist"), track_no=_int(g("Track")),
                 disc_no=_int(g("Disc")), year=_year(g("Year")), genre=g("Genre"),
                 lufs=_rg_lufs(g("REPLAYGAIN_TRACK_GAIN")) if g("REPLAYGAIN_TRACK_GAIN") else None)
        cov = tags.get("Cover Art (Front)")
        if cov is not None and getattr(cov, "kind", 0) == 1:
            raw = bytes(cov.value)
            m["cover"] = raw.split(b"\x00", 1)[1] if b"\x00" in raw[:256] else raw
    elif isinstance(tags, ASFTags):  # WMA
        def g(key):
            return _txt([str(v) for v in tags.get(key, [])])
        m.update(title=g("Title"), artist=g("Author"), album=g("WM/AlbumTitle"),
                 album_artist=g("WM/AlbumArtist"), track_no=_int(g("WM/TrackNumber")),
                 disc_no=_int(g("WM/PartOfSet")), year=_year(g("WM/Year")), genre=g("WM/Genre"))
        pics = tags.get("WM/Picture")
        if pics:
            m["cover"] = _asf_picture(bytes(pics[0].value))
        if g("replaygain_track_gain"):
            m["lufs"] = _rg_lufs(g("replaygain_track_gain"))
    elif tags is not None:  # Vorbis comments (FLAC, Ogg, Opus)
        def g(key):
            return _txt(tags.get(key))
        m.update(title=g("title"), artist=g("artist"), album=g("album"),
                 album_artist=g("albumartist") or g("album artist"),
                 track_no=_int(tags.get("tracknumber")), disc_no=_int(tags.get("discnumber")),
                 year=_year(g("date") or g("year")), genre=_txt((tags.get("genre") or [""])[0]))
        if g("replaygain_track_gain"):
            m["lufs"] = _rg_lufs(g("replaygain_track_gain"))
        pictures = getattr(f, "pictures", None)
        if pictures:
            front = next((p for p in pictures if p.type == 3), pictures[0])
            m["cover"] = front.data
        elif tags.get("metadata_block_picture"):
            try:
                m["cover"] = Picture(base64.b64decode(tags["metadata_block_picture"][0])).data
            except Exception:
                pass
    # valeurs de repli à partir du nom de fichier
    m["guessed"] = {}
    if not m["title"] or not m["artist"]:
        stem = Path(name_hint or str(path)).stem
        guess_artist, guess_title = parse_filename(stem)
        if not m["title"]:
            m["title"] = guess_title or stem
            m["guessed"]["title"] = m["title"]
        if not m["artist"] and guess_artist:
            m["artist"] = guess_artist
            m["guessed"]["artist"] = guess_artist
    m["artist"] = m["artist"] or m["album_artist"] or config.UNKNOWN_ARTIST
    if settings.get("shorten_titles"):
        m["title"] = clean.title(m["title"]) or m["title"]
    return m


TAG_KEYS = {
    "title": "title", "artist": "artist", "album": "album", "album_artist": "albumartist",
    "track_no": "tracknumber", "disc_no": "discnumber", "year": "date", "genre": "genre",
}
APE_KEYS = {"title": "Title", "artist": "Artist", "album": "Album", "album_artist": "Album Artist",
            "track_no": "Track", "disc_no": "Disc", "year": "Year", "genre": "Genre"}
ASF_KEYS = {"title": "Title", "artist": "Author", "album": "WM/AlbumTitle", "album_artist": "WM/AlbumArtist",
            "track_no": "WM/TrackNumber", "disc_no": "WM/PartOfSet", "year": "WM/Year", "genre": "WM/Genre"}
ID3_FRAMES = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "album_artist": "TPE2",
              "track_no": "TRCK", "disc_no": "TPOS", "year": "TDRC", "genre": "TCON"}


def _write_id3_frames(path: str, fields: dict) -> None:
    """WAV / AIFF : mutagen n'a pas de variante « easy » pour ces formats (les tags sont des trames
    ID3 brutes, `tags["title"] = [...]` y lève TypeError) : on écrit directement les trames."""
    from mutagen import id3
    f = mutagen.File(path)
    if f is None:
        raise ValueError("format non modifiable")
    if f.tags is None:
        f.add_tags()
    for key, value in fields.items():
        frame = ID3_FRAMES.get(key)
        if not frame:
            continue
        value = "" if value is None else str(value).strip()
        f.tags.delall(frame)
        if value:
            f.tags.add(getattr(id3, frame)(encoding=3, text=[value]))
    f.save(v2_version=3)  # ID3v2.3, comme pour les MP3


def write_tags(path: str, fields: dict) -> None:
    ext = Path(path).suffix.lower()
    if ext in (".wav", ".aif", ".aiff"):
        _write_id3_frames(path, fields)
        return
    if ext in (".ape", ".wv", ".mpc", ".wma"):
        f = mutagen.File(path)
        if f is None:
            raise ValueError("format non modifiable")
        if f.tags is None:
            f.add_tags()
        keys = ASF_KEYS if ext == ".wma" else APE_KEYS
        for key, value in fields.items():
            tag = keys.get(key)
            if not tag:
                continue
            value = "" if value is None else str(value).strip()
            if value:
                f.tags[tag] = [value] if ext == ".wma" else value
            elif tag in f.tags:
                del f.tags[tag]
        f.save()
        return
    f = mutagen.File(path, easy=True)
    if f is None:
        raise ValueError("format non modifiable")
    if f.tags is None:
        f.add_tags()
    for key, value in fields.items():
        tag = TAG_KEYS.get(key)
        if not tag:
            continue
        value = "" if value is None else str(value).strip()
        if value:
            f.tags[tag] = [value]
        elif tag in f.tags:
            del f.tags[tag]
    try:
        f.save(v2_version=3)  # ID3v2.3 : meilleure compatibilité (Windows, voitures…)
    except TypeError:
        f.save()


def embed_cover(path: str, jpeg: bytes) -> None:
    ext = Path(path).suffix.lower()
    if ext in (".mp3", ".aif", ".aiff", ".wav"):
        if ext == ".mp3":
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
        else:
            f = mutagen.File(path)
            if f.tags is None:
                f.add_tags()
            tags = f.tags
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpeg))
        if ext == ".mp3":
            tags.save(path, v2_version=3)
        else:
            f.save()
    elif ext == ".flac":
        f = FLAC(path)
        f.clear_pictures()
        f.add_picture(_picture(jpeg))
        f.save()
    elif ext in (".m4a", ".aac"):
        f = MP4(path)
        f["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
        f.save()
    elif ext in (".ogg", ".oga", ".opus"):
        f = mutagen.File(path)
        f["metadata_block_picture"] = [base64.b64encode(_picture(jpeg).write()).decode("ascii")]
        f.save()
    elif ext in (".ape", ".wv", ".mpc"):
        from mutagen.apev2 import APEValue, BINARY
        f = mutagen.File(path)
        if f.tags is None:
            f.add_tags()
        f.tags["Cover Art (Front)"] = APEValue(b"cover.jpg\x00" + jpeg, BINARY)
        f.save()


def _picture(jpeg: bytes) -> Picture:
    pic = Picture()
    pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Cover", jpeg
    return pic


def fingerprint(path: str, size: int | None = None) -> str:
    """Empreinte rapide (taille + début + fin) pour détecter doublons et déplacements."""
    import hashlib
    size = size if size is not None else os.path.getsize(path)
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(65536))
        if size > 131072:
            fh.seek(-65536, os.SEEK_END)
            h.update(fh.read(65536))
    return h.hexdigest()[:24]


# --------------------------------------------------------------------------- #
# Indexation
# --------------------------------------------------------------------------- #
def _folder_cover(directory: str, cache: dict) -> str | None:
    if directory in cache:
        return cache[directory]
    cid = None
    try:
        names = {n.lower(): n for n in os.listdir(directory)}
        for base in COVER_NAMES:
            for ext in IMAGE_EXTS:
                real = names.get(base + ext)
                if real:
                    with open(os.path.join(directory, real), "rb") as fh:
                        cid = covers.store_cover(fh.read())
                    if cid:
                        break
            if cid:
                break
    except OSError:
        pass
    cache[directory] = cid
    return cid


def album_key(name: str, artist: str) -> str:
    return db.norm(artist) + "\x1f" + db.norm(name)


def ensure_album(name: str, artist: str, year: int | None, cover_id: str | None) -> tuple[int, str | None, bool]:
    key = album_key(name, artist)
    row = db.q1("SELECT id, cover_id, cover_locked, year FROM albums WHERE key=?", key)
    if row:
        if cover_id and not row["cover_id"]:
            db.x("UPDATE albums SET cover_id=? WHERE id=?", cover_id, row["id"])
        if year and not row["year"]:
            db.x("UPDATE albums SET year=? WHERE id=?", year, row["id"])
        return row["id"], row["cover_id"] or cover_id, bool(row["cover_locked"])
    cur = db.x(
        "INSERT INTO albums(key, name, artist, year, cover_id, added_at, search) VALUES(?,?,?,?,?,?,?)",
        key, name, artist, year, cover_id, time.time(), db.norm(f"{name} {artist}"),
    )
    return cur.lastrowid, cover_id, False


def upsert_track(path: str, st: os.stat_result, meta: dict, track_id: int | None = None,
                 fp: str | None = None, folder_cache: dict | None = None, ofp: str | None = None) -> int:
    folder_cache = folder_cache if folder_cache is not None else {}
    cover_id = covers.store_cover(meta.get("cover")) if meta.get("cover") else None
    if not cover_id:
        cover_id = _folder_cover(os.path.dirname(path), folder_cache)
    album_artist = meta["album_artist"] or meta["artist"]
    album_name = meta["album"] or config.SINGLES
    album_id, album_cover, locked = ensure_album(album_name, album_artist, meta["year"], cover_id)
    if locked and album_cover:
        cover_id = album_cover
    fp = fp or fingerprint(path, st.st_size)
    search = db.norm(" ".join(filter(None, (meta["title"], meta["artist"], meta["album"],
                                             meta["album_artist"], meta["genre"]))))
    values = (path, fp, meta["title"], meta["artist"], album_id, meta["track_no"], meta["disc_no"],
              meta["year"], meta["genre"], meta["duration"], meta["bitrate"], st.st_size,
              st.st_mtime, Path(path).suffix.lower(), cover_id, search)
    lufs = meta.get("lufs")
    if not track_id:  # déjà indexé entre-temps (scan et import simultanés) ?
        row = db.q1("SELECT id FROM tracks WHERE path=?", path)
        track_id = row["id"] if row else None
    if track_id:
        db.x("""UPDATE tracks SET path=?, fp=?, title=?, artist=?, album_id=?, track_no=?, disc_no=?,
                year=?, genre=?, duration=?, bitrate=?, size=?, mtime=?, ext=?, cover_id=?, search=?,
                lufs=COALESCE(?, lufs), lufs_tried=CASE WHEN ? IS NULL THEN lufs_tried ELSE 1 END
                WHERE id=?""", *values, lufs, lufs, track_id)
        return track_id
    cur = db.x("""INSERT INTO tracks(path, fp, title, artist, album_id, track_no, disc_no, year, genre,
                  duration, bitrate, size, mtime, ext, cover_id, search, added_at, ofp, lufs, lufs_tried)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               *values, time.time(), ofp or fp, lufs, 1 if lufs is not None else 0)
    return cur.lastrowid


def cleanup_albums() -> None:
    db.x("DELETE FROM albums WHERE id NOT IN (SELECT DISTINCT album_id FROM tracks)")


def roots() -> list[Path]:
    return [config.MUSIC_DIR, *config.EXTRA_DIRS]


def _hidden(path: str, root: str) -> bool:
    rel = os.path.relpath(path, root)
    return any(part.startswith(".") for part in rel.split(os.sep))


def _under(path: str, root: str) -> bool:
    return path.startswith(root + os.sep)


def _online(root: Path) -> bool:
    """Dossier présent et non vide. Un point de montage vide (disque ou clé USB démonté sur le Pi,
    partage réseau coupé) ressemble à un dossier dont on aurait tout effacé : on le traite comme
    absent, sinon la suppression des titres emporterait likes, playlists, écoutes et paroles."""
    try:
        return root.is_dir() and any(root.iterdir())
    except OSError:
        return False


def _vanished_roots(existing: dict, missing: dict) -> list[str]:
    """Dossiers dont la moitié des titres connus (ou plus) a disparu d'un coup : disque mal monté,
    dossier renommé… Mieux vaut quelques titres fantômes qu'une bibliothèque vidée par erreur."""
    out: list[str] = []
    if not missing:
        return out
    for root in map(str, roots()):
        known = sum(1 for p in existing if _under(p, root))
        gone = sum(1 for p in missing if _under(p, root))
        if known > SAFE_MIN_TRACKS and gone >= known * SAFE_MAX_GONE:
            out.append(root)
    return out


def _report_offline(found: list[str]) -> None:
    """Expose les dossiers hors ligne (/api/library/status) et prévient l'admin, une fois par disparition."""
    new = [r for r in found if r not in status["offlineRoots"]]
    status["offlineRoots"] = found
    for root in new:
        log.warning("dossier de musique introuvable ou vidé : %s — rien n'a été supprimé", root)
        try:
            notify.add("storage", "Dossier de musique introuvable — rien n'a été supprimé",
                       f"{root} : vérifie que le disque est bien branché, tout reviendra au prochain scan. "
                       "Fichiers supprimés exprès ? Lance « Scan complet » dans les Réglages.", "#/settings")
        except Exception:
            log.exception("notification de dossier introuvable")


def scan(full: bool = False) -> dict:
    """Scan incrémental : ne relit que les fichiers nouveaux ou modifiés. `full` relit tout, et retire
    aussi les titres d'un dossier dont plus de la moitié a disparu (suppression voulue)."""
    if not _scan_lock.acquire(blocking=False):
        return status
    try:
        status.update(scanning=True, scan_total=0, scan_done=0, added=0, updated=0, removed=0, errors=0)
        existing = {r["path"]: r for r in db.q("SELECT id, path, mtime, size, fp FROM tracks")}
        seen: set[str] = set()
        todo: list[tuple[str, os.stat_result, object]] = []
        offline_roots: list[str] = []
        for root in roots():
            if not _online(root):
                offline_roots.append(str(root))  # disque absent ou vide : on ne supprime rien
                continue
            for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    if fn.startswith(".") or os.path.splitext(fn)[1].lower() not in config.AUDIO_TYPES:
                        continue
                    p = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    seen.add(p)
                    row = existing.get(p)
                    if row and not full and row["mtime"] == st.st_mtime and row["size"] == st.st_size:
                        continue
                    todo.append((p, st, row))
        missing = {path: row for path, row in existing.items()
                   if path not in seen and not any(_under(path, r) for r in offline_roots)}
        moved_by_fp = {row["fp"]: row for row in missing.values() if row["fp"]}
        status["scan_total"] = len(todo)
        folder_cache: dict = {}
        conn = db.connect()
        for i, (p, st, row) in enumerate(todo):
            if i % 100 == 0:
                if conn.in_transaction:
                    conn.execute("COMMIT")
                conn.execute("BEGIN IMMEDIATE")
            try:
                meta = read_meta(p)
                if row:
                    upsert_track(p, st, meta, track_id=row["id"], folder_cache=folder_cache)
                    status["updated"] += 1
                else:
                    fp = fingerprint(p, st.st_size)
                    old = moved_by_fp.pop(fp, None)
                    if old and old["path"] in missing:  # fichier déplacé/renommé : on garde likes & playlists
                        missing.pop(old["path"])
                        upsert_track(p, st, meta, track_id=old["id"], fp=fp, folder_cache=folder_cache)
                        status["updated"] += 1
                    else:
                        upsert_track(p, st, meta, fp=fp, folder_cache=folder_cache)
                        status["added"] += 1
            except Exception as exc:
                status["errors"] += 1
                log.warning("impossible de lire %s : %s", p, exc)
            status["scan_done"] = i + 1
        if conn.in_transaction:
            conn.execute("COMMIT")
        vanished = [] if full else _vanished_roots(existing, missing)  # (déplacements déjà retrouvés)
        if vanished:
            missing = {p: r for p, r in missing.items() if not any(_under(p, v) for v in vanished)}
        _report_offline([r for r in offline_roots + vanished if any(_under(p, r) for p in existing)])
        if missing:
            with db.tx():
                db.many("DELETE FROM tracks WHERE id=?", [(r["id"],) for r in missing.values()])
            status["removed"] = len(missing)
        cleanup_albums()
        status["last_scan"] = time.time()
        changed = status["added"] or status["updated"] or status["removed"]
        if changed:
            bump()
            log.info("scan terminé : +%d ~%d -%d (%d erreurs)", status["added"], status["updated"],
                     status["removed"], status["errors"])
            if status["added"]:
                try:
                    notify.music_added(status["added"])
                except Exception:
                    log.exception("notification d'import")
            if settings.get("auto_covers"):
                queue_missing_covers(force=False)
        try:
            from . import tidy
            tidy.run()  # titres raccourcis, « Slowed » / « Reverb » mis à la corbeille
        except Exception:
            log.exception("rangement automatique")
        schedule_background(cleanup=bool(status["removed"]))
        return status
    finally:
        status["scanning"] = False
        _scan_lock.release()


def _scan_loop() -> None:
    full = False
    while not _stop.is_set():
        try:
            scan(full=full)
        except Exception:
            log.exception("échec du scan")
        _wake.wait(timeout=config.SCAN_INTERVAL if config.SCAN_INTERVAL > 0 else None)
        full = _full_requested.is_set()
        _full_requested.clear()
        _wake.clear()


def _watch_loop() -> None:
    """Détection instantanée des fichiers copiés dans les dossiers (inotify via watchfiles)."""
    try:
        from watchfiles import watch
    except ImportError:
        return
    exts = set(config.AUDIO_TYPES) | set(IMAGE_EXTS)
    while not _stop.is_set():
        paths = [str(r) for r in roots() if r.is_dir()]
        if not paths:
            _stop.wait(60)
            continue

        def keep(_change, path: str) -> bool:
            root = next((r for r in paths if path.startswith(r)), paths[0])
            if _hidden(path, root):
                return False
            ext = os.path.splitext(path)[1].lower()
            return ext in exts or not ext  # sans extension : dossier ajouté, déplacé ou supprimé

        try:
            status["watching"] = True
            for _changes in watch(*paths, watch_filter=keep, stop_event=_stop, debounce=2500,
                                  step=250, recursive=True, ignore_permission_denied=True):
                request_scan()
        except Exception as exc:
            log.info("surveillance des dossiers indisponible (%s) — rescan périodique uniquement", exc)
            status["watching"] = False
            _stop.wait(120)
    status["watching"] = False


def start_background() -> None:
    threading.Thread(target=_scan_loop, name="onde-scan", daemon=True).start()
    threading.Thread(target=_watch_loop, name="onde-watch", daemon=True).start()


def request_scan(full: bool = False) -> None:
    if full:
        _full_requested.set()
    _wake.set()


def stop() -> None:
    _stop.set()
    _wake.set()


# --------------------------------------------------------------------------- #
# Tâches de fond : miniatures, volume, photos d'artistes, ménage
# --------------------------------------------------------------------------- #
def _once(name: str, fn, executor=None) -> None:
    """Planifie une tâche de fond si elle n'est pas déjà en attente."""
    with _bg_lock:
        if name in _bg_pending:
            return
        _bg_pending.add(name)

    def run():
        try:
            fn()
        except Exception:
            log.exception("tâche %s échouée", name)
        finally:
            with _bg_lock:
                _bg_pending.discard(name)

    (executor or bg).submit(run)


def schedule_background(cleanup: bool = False) -> None:
    _once("thumbs", pregen_thumbs)
    if cleanup:
        _once("cleanup", cleanup_covers)
    if config.FFMPEG and settings.get("analyze_loudness"):
        _once("loudness", analyze_pending)
    if settings.get("artist_images"):
        _once("artists", fetch_artist_images, jobs)
    from . import lyrics  # nouveaux titres : la recherche automatique des paroles s'en occupe
    lyrics.wake()


def pregen_thumbs() -> None:
    """Prépare les miniatures les plus utilisées pour un affichage instantané."""
    for row in db.q("SELECT id FROM covers ORDER BY created DESC"):
        if _stop.is_set():
            return
        for size in (96, 192, 384):
            if not (config.THUMBS_DIR / f"{row['id']}_{size}.webp").exists():
                try:
                    covers.thumb_file(row["id"], size)
                except Exception:
                    break
                time.sleep(0.005)


def cleanup_covers() -> None:
    """Supprime les pochettes qui ne servent plus (après au moins une heure)."""
    used = set()
    for sql in ("SELECT cover_id c FROM albums", "SELECT cover_id c FROM tracks",
                "SELECT cover_id c FROM playlists", "SELECT image_id c FROM artists"):
        used.update(r["c"] for r in db.q(sql) if r["c"])
    limit = time.time() - 3600
    for row in db.q("SELECT id, created FROM covers"):
        if row["id"] in used or (row["created"] or 0) > limit:
            continue
        for p in [covers.cover_path(row["id"]), *config.THUMBS_DIR.glob(f"{row['id']}_*")]:
            try:
                p.unlink()
            except OSError:
                pass
        db.x("DELETE FROM covers WHERE id=?", row["id"])


def measure_loudness(path: str) -> tuple[float, float | None] | None:
    """Volume intégré (LUFS, norme EBU R128) et crête, mesurés par ffmpeg."""
    if not config.FFMPEG:
        return None
    prefix = []
    if shutil.which("nice"):
        prefix += ["nice", "-n", "19"]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c", "3"]
    for af in ("ebur128=peak=true:framelog=quiet", "ebur128=peak=true"):
        cmd = [*prefix, config.FFMPEG, "-hide_banner", "-nostats", "-nostdin", "-threads", "1", "-i", path,
               "-map", "0:a:0", "-af", af, "-f", "null", "-"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=900, errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            return None
        out = res.stderr
        if "framelog" in af and ("Option not found" in out or "not found" in out.lower() and res.returncode):
            continue
        seg = out[out.rfind("Summary:"):] if "Summary:" in out else ""
        m = re.search(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", seg)
        if not m:
            return None
        lufs = float(m.group(1))
        p = re.search(r"Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", seg)
        if lufs <= -69:
            return None
        return lufs, float(p.group(1)) if p else None
    return None


def analyze_pending() -> None:
    status["loudness_running"] = True
    try:
        while not _stop.is_set() and settings.get("analyze_loudness"):
            rows = db.q("SELECT id, path FROM tracks WHERE lufs IS NULL AND lufs_tried=0 LIMIT 20")
            if not rows:
                break
            for r in rows:
                if _stop.is_set() or not settings.get("analyze_loudness"):
                    return
                res = measure_loudness(r["path"])
                if res:
                    db.x("UPDATE tracks SET lufs=?, peak=?, lufs_tried=1 WHERE id=?", res[0], res[1], r["id"])
                else:
                    db.x("UPDATE tracks SET lufs_tried=1 WHERE id=?", r["id"])
    finally:
        status["loudness_running"] = False


SKIP_ARTISTS = {db.norm(x) for x in ("Artiste inconnu", "Unknown Artist", "Various Artists", "Artistes variés",
                                     "Artistes divers", "Divers", "VA", "V.A.", "Compilation", "Various")}
_split_artist = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|avec|vs\.?|x|&)\s+|\s*[,;/]\s*", re.I)


def primary_artist(name: str) -> str:
    return _split_artist.split(name or "", maxsplit=1)[0].strip() or name


def artist_names() -> list[str]:
    names: dict[str, str] = {}
    for r in db.q("SELECT DISTINCT artist AS n FROM albums UNION SELECT DISTINCT artist FROM tracks"):
        if r["n"]:
            names.setdefault(db.norm(r["n"]), r["n"])
    return list(names.values())


def fetch_artist_images(force: bool = False) -> int:
    """Photos d'artistes depuis Deezer pour ceux qui n'en ont pas encore."""
    if not settings.get("artist_images"):
        return 0
    status["artists_running"] = True
    found = 0
    try:
        known = {r["key"]: r for r in db.q("SELECT * FROM artists")}
        retry_before = time.time() - ARTIST_RETRY
        for name in artist_names():
            if _stop.is_set() or not settings.get("artist_images"):
                break
            key = db.norm(name)
            row = known.get(key)
            recent = row and row["tried"] and row["tried"] > retry_before  # essayé il y a moins d'une semaine
            if key in SKIP_ARTISTS or (row and (row["image_id"] or row["locked"] or (recent and not force))):
                continue
            cid = None
            try:
                url = covers.find_artist_image(primary_artist(name))
                if url:
                    cid = covers.store_cover(covers.fetch_image(url))
            except Exception as exc:
                if net.is_offline(exc):  # pas d'Internet : rien n'est marqué, on réessaiera plus tard
                    log.info("photos d'artistes : réseau indisponible (%s), nouvel essai plus tard", exc)
                    break
                log.info("photo de %s introuvable : %s", name, exc)
            db.x("INSERT OR REPLACE INTO artists(key, name, image_id, locked, tried) VALUES(?,?,?,0,?)",
                 key, name, cid, time.time())
            if cid:
                found += 1
            time.sleep(0.35)
    finally:
        status["artists_running"] = False
    if found:
        bump()
    return found


def set_artist_image(name: str, cover_id: str | None) -> None:
    db.x("INSERT OR REPLACE INTO artists(key, name, image_id, locked, tried) VALUES(?,?,?,1,?)",
         db.norm(name), name, cover_id, time.time())
    bump()


# --------------------------------------------------------------------------- #
# Historique d'écoute
# --------------------------------------------------------------------------- #
def record_play(track_id: int, source: str = "web", at: float | None = None) -> None:
    row = db.q1("SELECT duration FROM tracks WHERE id=?", track_id)
    if not row:
        return
    when = at or time.time()
    with db.tx():
        # une écoute hors connexion arrivée en retard ne doit pas reculer « écouté récemment »
        db.x("UPDATE tracks SET plays=plays+1, last_played=MAX(COALESCE(last_played, 0), ?) WHERE id=?",
             when, track_id)
        db.x("INSERT INTO plays(track_id, played_at, seconds, source, user_id) VALUES(?,?,?,?,onde_uid())",
             track_id, when, row["duration"] or 0, source)


# --------------------------------------------------------------------------- #
# Import d'un fichier (upload ou lien) — audio seul ou archive .zip
# --------------------------------------------------------------------------- #
_unsafe = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_name(name: str, fallback: str) -> str:
    name = _unsafe.sub(" ", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)[:120].strip()
    return name or fallback


def new_incoming(ext: str) -> Path:
    config.INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    return config.INCOMING_DIR / f"{uuid.uuid4().hex}{ext}"


_ingest_lock = threading.Lock()


def ingest_any(tmp: Path, original_name: str) -> dict:
    if tmp.suffix.lower() == ".zip":
        return ingest_zip(tmp, original_name)
    return ingest(tmp, original_name)


def ingest(tmp: Path, original_name: str) -> dict:
    """Range un fichier reçu dans Musique/Artiste/Album/ et l'indexe."""
    with _ingest_lock:
        res = _ingest(tmp, original_name)
    if res["status"] == "added":
        bump()
        schedule_background()
    return res


def _ingest(tmp: Path, original_name: str) -> dict:
    from .api_models import track_by_id  # import local (évite un cycle)
    try:
        meta = read_meta(tmp, name_hint=original_name)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return {"status": "error", "name": original_name, "error": f"Fichier illisible ({exc})"}
    st = tmp.stat()
    fp = fingerprint(str(tmp), st.st_size)
    original_fp = fp
    dup = db.q1("SELECT id FROM tracks WHERE fp=? OR ofp=?", fp, fp)
    if dup:
        tmp.unlink(missing_ok=True)
        return {"status": "duplicate", "name": original_name, "track": track_by_id(dup["id"])}
    ext = tmp.suffix.lower()
    artist_dir = safe_name(meta["album_artist"] or meta["artist"], config.UNKNOWN_ARTIST)
    album_dir = safe_name(meta["album"], config.SINGLES)
    dest_dir = config.MUSIC_DIR / artist_dir / album_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    title = safe_name(meta["title"], "Sans titre")
    base = f"{meta['track_no']:02d} - {title}" if meta["track_no"] else title
    dest = dest_dir / f"{base}{ext}"
    n = 2
    while dest.exists():
        dest = dest_dir / f"{base} ({n}){ext}"
        n += 1
    shutil.move(str(tmp), dest)
    if meta.get("guessed"):
        # infos déduites du nom de fichier : on les écrit dans les tags pour qu'elles restent
        try:
            write_tags(str(dest), meta["guessed"])
        except Exception as exc:
            log.info("tags non écrits pour %s : %s", dest, exc)
    st = dest.stat()
    fp = fingerprint(str(dest), st.st_size)
    with db.tx():
        tid = upsert_track(str(dest), st, meta, fp=fp, ofp=original_fp)
    row = db.q1("SELECT album_id FROM tracks WHERE id=?", tid)
    if settings.get("auto_covers") and row:
        a = db.q1("SELECT cover_id FROM albums WHERE id=?", row["album_id"])
        t = db.q1("SELECT cover_id FROM tracks WHERE id=?", tid)
        if not (t["cover_id"] or (a and a["cover_id"])):
            jobs.submit(_safe, auto_cover_album, row["album_id"], False)
    return {"status": "added", "name": original_name, "track": track_by_id(tid)}


def ingest_zip(tmp: Path, original_name: str) -> dict:
    """Album en .zip (Bandcamp, Qobuz…) : importe chaque fichier audio + la pochette du dossier."""
    added, dup, errors, tracks = 0, 0, 0, []
    cover_raw = None
    try:
        with zipfile.ZipFile(tmp) as zf:
            members = [m for m in zf.infolist() if not m.is_dir() and "__MACOSX" not in m.filename
                       and not os.path.basename(m.filename).startswith(".")]
            if sum(m.file_size for m in members) > config.MAX_UPLOAD_MB * 1024 * 1024 * 3:
                raise ValueError("archive trop volumineuse")
            images = [m for m in members if os.path.splitext(m.filename)[1].lower() in IMAGE_EXTS]
            if images:
                def rank(m):
                    stem = os.path.splitext(os.path.basename(m.filename))[0].lower()
                    return (0 if stem in COVER_NAMES else 1, -m.file_size)
                best = sorted(images, key=rank)[0]
                if best.file_size < 25_000_000:
                    cover_raw = zf.read(best)
            for m in members:
                ext = os.path.splitext(m.filename)[1].lower()
                if ext not in config.AUDIO_TYPES:
                    continue
                part = new_incoming(ext)
                with zf.open(m) as src, open(part, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                res = ingest(part, os.path.basename(m.filename))
                if res["status"] == "added":
                    added += 1
                    tracks.append(res["track"])
                elif res["status"] == "duplicate":
                    dup += 1
                    if res.get("track"):
                        tracks.append(res["track"])
                else:
                    errors += 1
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        return {"status": "error", "name": original_name, "error": f"Archive illisible ({exc})"}
    finally:
        tmp.unlink(missing_ok=True)
    if cover_raw and tracks:
        cid = covers.store_cover(cover_raw)
        if cid:
            for album_id in {t["albumId"] for t in tracks}:
                a = db.q1("SELECT cover_id FROM albums WHERE id=?", album_id)
                if a and not a["cover_id"]:
                    set_album_cover(album_id, cid, settings.get("embed_covers"), lock=False)
    if not added and not dup:
        return {"status": "error", "name": original_name,
                "error": "Aucun fichier audio dans l'archive" if not errors else f"{errors} fichier(s) illisible(s)"}
    return {"status": "added" if added else "duplicate", "name": original_name, "added": added,
            "duplicates": dup, "errors": errors, "track": tracks[0] if tracks else None, "tracks": tracks[:200]}


# --------------------------------------------------------------------------- #
# Pochettes d'album
# --------------------------------------------------------------------------- #
def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        log.exception("tâche %s échouée", getattr(fn, "__name__", fn))


def _refresh_file_stats(track_id: int, path: str) -> None:
    st = os.stat(path)
    db.x("UPDATE tracks SET size=?, mtime=?, fp=? WHERE id=?",
         st.st_size, st.st_mtime, fingerprint(path, st.st_size), track_id)


def canonical_cover(cover_id: str) -> str:
    """Version de la pochette qui sera écrite dans les fichiers : même hash au prochain scan."""
    jpeg = covers.embed_bytes(cover_id)
    return (covers.store_cover(jpeg) if jpeg else None) or cover_id


def embed_cover_in_tracks(track_rows, cover_id: str) -> int:
    jpeg = covers.cover_path(cover_id).read_bytes() if covers.cover_path(cover_id).exists() else None
    if not jpeg:
        return 0
    done = 0
    for r in track_rows:
        try:
            embed_cover(r["path"], jpeg)
            _refresh_file_stats(r["id"], r["path"])
            done += 1
        except Exception as exc:
            log.warning("pochette non écrite dans %s : %s", r["path"], exc)
    return done


def set_album_cover(album_id: int, cover_id: str, embed: bool, lock: bool = True) -> str:
    if embed:
        cover_id = canonical_cover(cover_id)
    with db.tx():
        db.x("UPDATE albums SET cover_id=?, cover_locked=MAX(cover_locked, ?) WHERE id=?",
             cover_id, 1 if lock else 0, album_id)
        db.x("UPDATE tracks SET cover_id=? WHERE album_id=?", cover_id, album_id)
    if embed:
        rows = db.q("SELECT id, path FROM tracks WHERE album_id=?", album_id)
        jobs.submit(_safe, embed_cover_in_tracks, rows, cover_id)
    bump()
    _once("thumbs", pregen_thumbs)
    return cover_id


def set_track_cover(track_id: int, cover_id: str, embed: bool) -> str:
    if embed:
        cover_id = canonical_cover(cover_id)
    db.x("UPDATE tracks SET cover_id=? WHERE id=?", cover_id, track_id)
    row = db.q1("SELECT t.id, t.path, t.album_id, a.cover_id AS acover FROM tracks t "
                "JOIN albums a ON a.id=t.album_id WHERE t.id=?", track_id)
    if row and not row["acover"]:
        db.x("UPDATE albums SET cover_id=? WHERE id=?", cover_id, row["album_id"])
    if embed and row:
        jobs.submit(_safe, embed_cover_in_tracks, [row], cover_id)
    bump()
    return cover_id


def auto_cover_album(album_id: int, force: bool) -> bool:
    album = db.q1("SELECT * FROM albums WHERE id=?", album_id)
    if not album or (album["cover_tried"] and not force):
        return False
    embed = settings.get("embed_covers")
    found = offline = False
    try:
        if album["name"] == config.SINGLES:
            for t in db.q("SELECT id, title, artist FROM tracks WHERE album_id=? AND cover_id IS NULL", album_id):
                res = covers.search_online(f"{t['artist']} {t['title']}", "song", 8)
                if res.get("offline"):
                    offline = True
                    break
                best = covers.best_match(res["results"], t["title"], t["artist"], "song")
                if best:
                    cid = covers.store_cover(covers.fetch_image(best["url"]))
                    if cid:
                        set_track_cover(t["id"], cid, embed)
                        found = True
                time.sleep(0.4)
        elif not album["cover_id"] or force:
            query = album["name"] if album["artist"] == config.UNKNOWN_ARTIST else f"{album['artist']} {album['name']}"
            res = covers.search_online(query, "album", 10)
            offline = bool(res.get("offline"))
            best = covers.best_match(res["results"], album["name"], album["artist"], "album")
            if best:
                cid = covers.store_cover(covers.fetch_image(best["url"]))
                if cid:
                    set_album_cover(album_id, cid, embed, lock=False)
                    found = True
    except Exception as exc:  # (image refusée, coupure pendant le téléchargement…)
        offline = net.is_offline(exc)
        if not offline:
            log.warning("pochette de l'album %s non récupérée : %s", album_id, exc)
    if offline:  # importé sans Internet : l'album n'est pas marqué, la recherche sera refaite plus tard
        log.info("pochette de l'album %s : réseau indisponible, nouvel essai plus tard", album_id)
    else:
        db.x("UPDATE albums SET cover_tried=1 WHERE id=?", album_id)
    if found:
        status["covers_found"] += 1
    return found


def _cover_job(album_id: int, force: bool) -> None:
    try:
        auto_cover_album(album_id, force)
    finally:
        status["covers_running"] = max(0, status["covers_running"] - 1)
    time.sleep(0.3)  # reste poli avec les API publiques


def queue_missing_covers(force: bool = True) -> int:
    rows = db.q("""SELECT DISTINCT a.id FROM albums a JOIN tracks t ON t.album_id=a.id
                   WHERE (a.cover_id IS NULL OR (a.name=? AND t.cover_id IS NULL))
                   AND (? OR a.cover_tried=0)""", config.SINGLES, 1 if force else 0)
    for r in rows:
        status["covers_running"] += 1
        jobs.submit(_safe, _cover_job, r["id"], force)
    return len(rows)


# --------------------------------------------------------------------------- #
# Zip en streaming (téléchargement d'album / playlist)
# --------------------------------------------------------------------------- #
class _Sink:
    def __init__(self):
        self.buf = bytearray()

    def write(self, b):
        self.buf += b
        return len(b)

    def flush(self):
        pass

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def zip_stream(files: list[tuple[str, str]]):
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for path, arcname in files:
            try:
                info = zipfile.ZipInfo.from_file(path, arcname)
            except OSError:
                continue
            info.compress_type = zipfile.ZIP_STORED
            with open(path, "rb") as src, zf.open(info, "w", force_zip64=True) as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
                    if len(sink.buf) >= 1 << 20:
                        yield sink.take()
            yield sink.take()
    yield sink.take()


# --------------------------------------------------------------------------- #
# Quasi-doublons (même morceau importé plusieurs fois sous un titre légèrement différent)
# --------------------------------------------------------------------------- #
_dup_junk = re.compile(
    r"\s*[\(\[][^\)\]]*(remaster\w*|deluxe|explicit|clean|mono|stereo|bonus track|"
    r"single version|album version|radio edit|\d{4})[^\)\]]*[\)\]]\s*|\s+-\s+(remaster\w*|radio edit|single version)\s*$",
    re.I,
)


LOSSLESS = {".flac", ".wav", ".aiff", ".aif", ".ape", ".wv", ".alac"}


def quality_key(i) -> tuple:
    """Pour trier du meilleur au moins bon : sans perte d'abord, puis le débit, puis la durée et la taille."""
    lossless = (i["ext"] or "").lower() in LOSSLESS
    return (0 if lossless else 1, -(i["bitrate"] or 0), -round(i["duration"] or 0), -(i["size"] or 0))


def find_near_duplicates() -> list[dict]:
    """Regroupe les titres qui semblent être le même morceau (même artiste, titre très proche une fois
    les mentions « remaster/deluxe/radio edit… » retirées) mais qui ne sont pas des doublons stricts
    (fichiers différents, déjà filtrés à l'import). Ne compare jamais deux artistes différents entre eux :
    reste en O(n), pas de comparaison exhaustive coûteuse sur toute la bibliothèque."""
    rows = db.q("SELECT id, title, artist, album_id, duration, size, path, bitrate, ext FROM tracks")
    groups: dict[tuple[str, str], list] = {}
    for r in rows:
        title = _dup_junk.sub(" ", r["title"] or "").strip()
        key = (db.norm(r["artist"] or ""), db.norm(title))
        if not key[1]:
            continue
        groups.setdefault(key, []).append(r)
    out = []
    for (artist_n, title_n), items in groups.items():
        if len(items) < 2:
            continue
        items.sort(key=quality_key)  # garde en tête la meilleure version (qualité, puis la plus complète)
        out.append({
            "artist": items[0]["artist"],
            "title": items[0]["title"],
            "tracks": [{"id": i["id"], "title": i["title"], "duration": i["duration"], "size": i["size"],
                        "albumId": i["album_id"], "bitrate": i["bitrate"] or 0, "ext": i["ext"] or ""} for i in items],
        })
    out.sort(key=lambda g: -len(g["tracks"]))
    return out
