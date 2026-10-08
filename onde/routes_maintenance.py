"""Maintenance de la bibliothèque."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter

log = logging.getLogger("onde")
import os
import tempfile
import time

from fastapi import Body, HTTPException
from fastapi.responses import StreamingResponse

from . import api_models as M
from . import clean, config, covers, db, downloader, library, tidy, users
from .web import _404, _disposition, _reindex, _stats, _track_row

router = APIRouter()


# --------------------------------------------------------------------------- #
# Maintenance
# --------------------------------------------------------------------------- #
@router.post("/api/library/scan")
def rescan(body: dict = Body(default={})):
    library.request_scan(full=bool(body.get("full")))
    return library.status


@router.get("/api/library/status")
def scan_status():
    lufs = db.q1("""SELECT SUM(lufs IS NOT NULL) done, SUM(lufs IS NULL AND lufs_tried=0) pending,
                    COUNT(*) total FROM tracks""")
    photos = db.q1("SELECT COUNT(*) n FROM artists WHERE image_id IS NOT NULL")["n"]
    return {**library.status, "stats": _stats(), "loudness": {"done": lufs["done"] or 0, "pending": lufs["pending"] or 0,
            "total": lufs["total"] or 0, "ffmpeg": bool(config.FFMPEG)}, "artistPhotos": photos}


@router.post("/api/library/covers")
def fetch_missing_covers():
    return {"queued": library.queue_missing_covers(force=True)}


@router.post("/api/library/artists")
def fetch_artist_photos():
    library.jobs.submit(library._safe, library.fetch_artist_images, True)
    return {"ok": True}


@router.get("/api/library/export/info")
def library_export_info():
    r = db.q1("SELECT COUNT(*) n, COALESCE(SUM(size), 0) size FROM tracks")
    return {"tracks": r["n"], "size": r["size"]}


def _export_manifest() -> bytes:
    """Playlists et titres likés en JSON (les fichiers audio, eux, gardent leurs tags/pochette)."""
    playlists = []
    owners = {u["id"]: u["name"] for u in db.q("SELECT id, name FROM users")}
    for p in db.q("SELECT id, name, description, owner_id FROM playlists ORDER BY created_at"):
        tracks = db.q("""SELECT t.title, t.artist, a.name AS album FROM playlist_tracks pt
                          JOIN tracks t ON t.id = pt.track_id JOIN albums a ON a.id = t.album_id
                          WHERE pt.playlist_id = ? ORDER BY pt.position""", p["id"])
        playlists.append({"name": p["name"], "description": p["description"], "owner": owners.get(p["owner_id"], ""),
                          "tracks": [{"title": t["title"], "artist": t["artist"], "album": t["album"]} for t in tracks]})
    liked = db.q("""SELECT t.title, t.artist, a.name AS album, l.user_id FROM likes l
                     JOIN tracks t ON t.id = l.track_id JOIN albums a ON a.id = t.album_id
                     ORDER BY l.liked_at DESC""")
    manifest = {
        "app": "Onde", "version": config.VERSION, "exported_at": time.time(),
        "note": "Les fichiers audio de cette sauvegarde ont déjà leurs tags et leur pochette intégrés. "
                "Ce fichier ne sert qu'à retrouver tes playlists et tes titres likés si besoin.",
        "playlists": playlists,
        "liked": [{"title": t["title"], "artist": t["artist"], "album": t["album"], "profile": owners.get(t["user_id"], "")}
                  for t in liked],
    }
    return json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")


@router.get("/api/library/export")
def library_export():
    """Sauvegarde complète : tous les morceaux (Artiste/Album/…, tags et pochettes déjà dedans) + un
    manifeste JSON listant playlists et titres likés, en un seul .zip streamé (peut être volumineux)."""
    tracks = db.q("""SELECT t.path, t.title, t.track_no, t.ext, a.name AS album, a.artist AS album_artist
                      FROM tracks t JOIN albums a ON a.id = t.album_id
                      ORDER BY a.artist COLLATE NOCASE, a.name COLLATE NOCASE, t.disc_no, t.track_no""")
    files = []
    seen: dict[str, int] = {}
    for t in tracks:
        artist_dir = library.safe_name(t["album_artist"], config.UNKNOWN_ARTIST)
        album_dir = library.safe_name(t["album"], config.SINGLES)
        base = f"{t['track_no']:02d} - {t['title']}" if t["track_no"] else t["title"]
        arc = f"{artist_dir}/{album_dir}/{library.safe_name(base, 'titre')}{t['ext']}"
        if arc in seen:
            seen[arc] += 1
            stem, ext = os.path.splitext(arc)
            arc = f"{stem} ({seen[arc]}){ext}"
        else:
            seen[arc] = 0
        files.append((t["path"], arc))

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    try:
        tmp.write(_export_manifest())
    finally:
        tmp.close()
    files.insert(0, (tmp.name, "onde-playlists-et-likes.json"))

    def gen():
        try:
            yield from library.zip_stream(files)
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    name = f"onde-sauvegarde-{time.strftime('%Y-%m-%d')}.zip"
    return StreamingResponse(gen(), media_type="application/zip", headers={"Content-Disposition": _disposition(name)})


@router.get("/api/library/duplicates")
def library_duplicates():
    return {"groups": library.find_near_duplicates()}


@router.post("/api/library/duplicates/merge")
def duplicates_merge(body: dict = Body(...)):
    """Garde un titre, met les autres à la corbeille (likes, playlists et historique passent sur celui gardé).
    Sans `keep` : tous les groupes, en gardant le meilleur de chacun."""
    if body.get("keep") is None:
        n = 0
        for g in library.find_near_duplicates():
            n += tidy.merge_into(g["tracks"][0]["id"], [t["id"] for t in g["tracks"][1:]])
    else:
        try:
            n = tidy.merge_into(int(body["keep"]), [int(i) for i in body.get("remove") or []])
        except KeyError:
            _404("Titre introuvable")
    users.log_activity("trash", f"doublons : {n} titre(s) mis à la corbeille")
    return {"removed": n}


@router.get("/api/library/quality")
def library_quality(kbps: int = 128):
    return {"tracks": tidy.low_quality(max(32, min(kbps, 320)))}


@router.post("/api/library/quality/{tid}/replace")
def quality_replace(tid: int):
    """Cherche une meilleure version en ligne ; si elle est meilleure, elle remplace l'ancienne (likes et
    playlists gardés, l'ancienne va à la corbeille)."""
    row = _track_row(tid)
    try:
        res = downloader.search_download(f"{row['artist']} - {row['title']}")
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))
    new = res.get("track") or {}
    nid = new.get("id")
    if res.get("status") != "added" or not nid or nid == tid:
        return {"replaced": False, "reason": "Pas d'autre version trouvée."}
    nrow = db.q1("SELECT bitrate, ext, duration, size FROM tracks WHERE id=?", nid)
    better = nrow and library.quality_key(nrow) < library.quality_key(row) \
        and abs((nrow["duration"] or 0) - (row["duration"] or 0)) < 15
    if better:
        tidy.merge_into(nid, [tid], "remplacé par une meilleure version")
        users.log_activity("import", f"meilleure version : {row['title']} — {row['artist']}")
        return {"replaced": True, "track": M.track_by_id(nid)}
    tidy.merge_into(tid, [nid], "pas meilleure")
    return {"replaced": False, "reason": "La version trouvée n'est pas meilleure : on garde la tienne."}


@router.post("/api/tracks/batch-edit")
def tracks_batch_edit(body: dict = Body(...)):
    """Mêmes infos pour plusieurs titres (artiste, album, année, genre…) ; un champ vide ne change rien."""
    ids = [int(i) for i in body.get("ids") or []][:500]
    fields = {k: v for k, v in (body.get("fields") or {}).items() if k in library.TAG_KEYS and k != "title" and str(v).strip() != ""}
    if not ids or not fields:
        raise HTTPException(400, "Rien à modifier")
    done, errors = 0, []
    for tid in ids:
        row = db.q1("SELECT * FROM tracks WHERE id=?", tid)
        if not row:
            continue
        old_album = db.q1("SELECT * FROM albums WHERE id=?", row["album_id"])
        try:
            library.write_tags(row["path"], fields)
            _reindex(tid, row["path"], old_album)
            done += 1
        except Exception as exc:
            errors.append(f"{row['title']} : {exc}")
    library.bump()
    return {"done": done, "errors": errors[:10]}


@router.post("/api/tracks/batch-fix")
def tracks_batch_fix(body: dict = Body(...)):
    """Répare les infos de quelques titres (10 au plus par appel) avec Apple Music / Deezer : titre, artiste,
    album, année, genre et pochette — seulement quand le résultat correspond vraiment au morceau."""
    ids = [int(i) for i in body.get("ids") or []][:10]
    fixed, skipped = [], []
    for tid in ids:
        row = db.q1("SELECT * FROM tracks WHERE id=?", tid)
        if not row:
            continue
        parts = [row["artist"], clean.title(row["title"])] if row["artist"] != config.UNKNOWN_ARTIST else [row["title"]]
        meta = downloader._lookup_meta(" ".join(parts), parts)
        if not meta:
            skipped.append(tid)
            continue
        fields = {k: v for k, v in {"title": meta.get("title"), "artist": meta.get("artist"), "album": meta.get("album"),
                                     "album_artist": meta.get("albumArtist"), "year": meta.get("year"),
                                     "genre": meta.get("genre"), "track_no": meta.get("trackNo")}.items() if v}
        old_album = db.q1("SELECT * FROM albums WHERE id=?", row["album_id"])
        try:
            library.write_tags(row["path"], fields)
            cover_url = meta.get("cover") or meta.get("thumb")
            if cover_url and not (row["cover_id"] or (old_album and old_album["cover_id"])):
                try:
                    cid = covers.store_cover(covers.fetch_image(cover_url))
                    jpeg = covers.embed_bytes(cid) if cid else None
                    if jpeg:
                        library.embed_cover(row["path"], jpeg)
                except Exception as exc:
                    log.info("pochette non ajoutée pour %s : %s", row["path"], exc)
            _reindex(tid, row["path"], old_album)
            fixed.append(tid)
        except Exception as exc:
            log.info("infos non réparées pour %s : %s", row["path"], exc)
            skipped.append(tid)
    if fixed:
        library.bump()
    return {"fixed": fixed, "skipped": skipped}
