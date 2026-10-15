"""Playlists : intelligentes (règles), pistes, import, export, pochette, téléchargement."""
from __future__ import annotations

import math
import os
import re
import time
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse

from . import api_models as M
from . import config, db, library, smart
from .web import J, _404, _cover_from_request, _disposition, _playlist, _read_limited

router = APIRouter()


# --------------------------------------------------------------------------- #
# Playlists intelligentes (règles)
# --------------------------------------------------------------------------- #
@router.get("/api/smart")
def smart_list():
    return J(smart.all_smart())


@router.post("/api/smart")
def smart_create(body: dict = Body(...)):
    try:
        return J(smart.create(body.get("name", ""), body.get("spec") or {}))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/smart/preview")
def smart_preview(body: dict = Body(...)):
    """Aperçu pendant l'édition : nombre de titres et premiers résultats."""
    try:
        items = smart.tracks(smart.validate(body.get("spec") or {}))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return J({"count": len(items), "dur": round(sum(t["dur"] for t in items)), "tracks": items[:30]})


@router.get("/api/smart/{sid}")
def smart_get(sid: int):
    out = smart.get(sid)
    if not out:
        _404("Playlist intelligente introuvable")
    return J(out)


@router.patch("/api/smart/{sid}")
def smart_update(sid: int, body: dict = Body(...)):
    try:
        out = smart.update(sid, body.get("name"), body.get("spec"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not out:
        _404("Playlist intelligente introuvable")
    return J(out)


@router.delete("/api/smart/{sid}")
def smart_delete(sid: int):
    if not smart.delete(sid):
        _404("Playlist intelligente introuvable")
    return {"ok": True}


def _normalize_positions(pid: int) -> int:
    """Renumérote les positions 0, 1, 2… (le rang d'une entrée est alors aussi sa position) ; renvoie le nombre d'entrées."""
    rows = db.q("SELECT id FROM playlist_tracks WHERE playlist_id=? ORDER BY position, id", pid)
    db.many("UPDATE playlist_tracks SET position=? WHERE id=?", [(i, r["id"]) for i, r in enumerate(rows)])
    return len(rows)


def _add_to_playlist(pid: int, ids: list[int], position: int | None = None) -> int:
    now = time.time()
    with db.tx():
        valid = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            valid |= {r["id"] for r in db.q(f"SELECT id FROM tracks WHERE id IN ({','.join('?' * len(chunk))})", *chunk)}
        keep = [t for t in ids if t in valid]
        if position is None:
            start = db.q1("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM playlist_tracks WHERE playlist_id=?", pid)["p"]
        else:  # insertion à un rang précis (annuler un retrait) : on renumérote d'abord, rang = position
            start = min(max(0, position), _normalize_positions(pid))
            db.x("UPDATE playlist_tracks SET position = position + ? WHERE playlist_id=? AND position >= ?",
                 len(keep), pid, start)
        rows = [(pid, tid, start + i, now) for i, tid in enumerate(keep)]
        db.many("INSERT INTO playlist_tracks(playlist_id, track_id, position, added_at) VALUES(?,?,?,?)", rows)
        db.x("UPDATE playlists SET updated_at=? WHERE id=?", now, pid)
    return len(rows)


@router.post("/api/playlists/{pid}/tracks")
def add_tracks(pid: int, body: dict = Body(...)):
    _playlist(pid)
    ids = [int(i) for i in body.get("trackIds") or []]
    if body.get("skipDuplicates", True):
        present = {r["track_id"] for r in db.q("SELECT track_id FROM playlist_tracks WHERE playlist_id=?", pid)}
        ids = [i for i in dict.fromkeys(ids) if i not in present]
    pos = body.get("position")
    at = int(pos) if isinstance(pos, (int, float)) and not isinstance(pos, bool) and math.isfinite(pos) else None
    added = _add_to_playlist(pid, ids, at)
    return {"added": added, "playlist": _playlist(pid)}


@router.delete("/api/playlists/{pid}/entries/{eid}")
def remove_entry(pid: int, eid: int):
    """Retire une entrée. Son rang (`index`) se renvoie tel quel en `position` pour annuler le retrait."""
    _playlist(pid)
    with db.tx():
        row = db.q1("SELECT track_id, (SELECT COUNT(*) FROM playlist_tracks p2 WHERE p2.playlist_id=pt.playlist_id "
                    "AND (p2.position < pt.position OR (p2.position = pt.position AND p2.id < pt.id))) AS idx "
                    "FROM playlist_tracks pt WHERE id=? AND playlist_id=?", eid, pid)
        if not row:
            _404("Entrée introuvable (déjà retirée de la playlist ?)")
        db.x("DELETE FROM playlist_tracks WHERE id=? AND playlist_id=?", eid, pid)
        _normalize_positions(pid)  # sinon, après plusieurs retraits, rang et position ne coïncident plus
        db.x("UPDATE playlists SET updated_at=? WHERE id=?", time.time(), pid)
    return {"ok": True, "trackId": row["track_id"], "index": row["idx"]}


@router.get("/api/playlists/{pid}/export")
def playlist_export(pid: int):
    p = _playlist(pid)
    lines = ["#EXTM3U", f"#PLAYLIST:{p['name']}"]
    for t in db.q("""SELECT t.path, t.title, t.artist, t.duration FROM playlist_tracks pt
                     JOIN tracks t ON t.id=pt.track_id WHERE pt.playlist_id=? ORDER BY pt.position""", pid):
        path = t["path"]
        try:
            path = os.path.relpath(path, config.MUSIC_DIR) if Path(path).is_relative_to(config.MUSIC_DIR) else path
        except ValueError:
            pass
        lines += [f"#EXTINF:{round(t['duration'] or 0)},{t['artist']} - {t['title']}", path.replace(os.sep, "/")]
    return Response("\n".join(lines) + "\n", media_type="audio/x-mpegurl",
                    headers={"Content-Disposition": _disposition(library.safe_name(p["name"], "playlist") + ".m3u8")})


def _parse_m3u(text: str) -> list[dict]:
    entries, extinf = [], None
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue
        if line.upper().startswith("#EXTINF:"):
            dur, _, label = line[8:].partition(",")
            extinf = {"label": label.strip(), "dur": float(dur) if re.fullmatch(r"-?\d+(\.\d+)?", dur.strip()) else 0}
        elif line.startswith("#"):
            continue
        else:
            path = urllib.parse.unquote(line[7:]) if line.lower().startswith("file://") else line
            entries.append({"path": path.replace("\\", "/"), **(extinf or {})})
            extinf = None
    return entries


@router.post("/api/playlists/import")
async def playlist_import(request: Request, name: str = ""):
    raw = await _read_limited(request, 5_000_000)
    text = raw.decode("utf-8", "replace")
    if "\ufffd" in text:
        text = raw.decode("latin-1")
    entries = _parse_m3u(text)
    if not entries:
        raise HTTPException(400, "Aucune entrée trouvée dans cette playlist (.m3u / .m3u8)")

    def work():
        rows = db.q("SELECT id, path, title, artist FROM tracks")
        by_path = {r["path"].replace(os.sep, "/"): r["id"] for r in rows}
        music = str(config.MUSIC_DIR).replace(os.sep, "/").rstrip("/") + "/"
        by_rel = {p[len(music):].lower(): i for p, i in by_path.items() if p.startswith(music)}
        by_tail = {"/".join(p.lower().split("/")[-2:]): i for p, i in by_path.items()}
        by_file = {p.lower().rsplit("/", 1)[-1]: i for p, i in by_path.items()}
        by_label = {db.norm(f"{r['artist']} - {r['title']}"): r["id"] for r in rows}
        by_title = {db.norm(r["title"]): r["id"] for r in rows}
        ids, missing = [], []
        for e in entries:
            p = e["path"]
            tid = (by_path.get(p) or by_rel.get(p.lower().lstrip("./")) or by_tail.get("/".join(p.lower().split("/")[-2:]))
                   or by_file.get(p.lower().rsplit("/", 1)[-1]))
            if not tid and e.get("label"):
                tid = by_label.get(db.norm(e["label"])) or by_title.get(db.norm(e["label"].split(" - ", 1)[-1]))
            if tid:
                ids.append(tid)
            else:
                missing.append(e.get("label") or p.rsplit("/", 1)[-1])
        title = name.strip() or next((l[10:].strip() for l in text.splitlines() if l.startswith("#PLAYLIST:")), "") or "Playlist importée"
        now = time.time()
        cur = db.x("INSERT INTO playlists(name, description, created_at, updated_at, owner_id) VALUES(?,?,?,?,onde_uid())",
                   title[:120], "", now, now)
        pid = cur.lastrowid
        if ids:
            _add_to_playlist(pid, ids)
        return {"playlist": _playlist(pid), "matched": len(ids), "total": len(entries), "missing": missing[:30]}

    return await run_in_threadpool(work)


@router.put("/api/playlists/{pid}/order")
def reorder(pid: int, body: dict = Body(...)):
    _playlist(pid)
    entries = [int(e) for e in body.get("entries") or []]
    with db.tx():
        db.many("UPDATE playlist_tracks SET position=? WHERE id=? AND playlist_id=?",
                [(i, e, pid) for i, e in enumerate(entries)])
        _normalize_positions(pid)
        db.x("UPDATE playlists SET updated_at=? WHERE id=?", time.time(), pid)
    return {"ok": True}


@router.post("/api/playlists/{pid}/cover")
async def playlist_cover(pid: int, request: Request):
    await run_in_threadpool(_playlist, pid)
    cid = await _cover_from_request(request)
    await run_in_threadpool(db.x, "UPDATE playlists SET cover_id=? WHERE id=?", cid, pid)
    return {"cover": cid, "color": M.color_of(cid)}


@router.delete("/api/playlists/{pid}/cover")
def playlist_cover_reset(pid: int):
    _playlist(pid)
    db.x("UPDATE playlists SET cover_id=NULL WHERE id=?", pid)
    return _playlist(pid)


@router.get("/api/playlists/{pid}/download")
def playlist_zip(pid: int):
    p = _playlist(pid)
    files = []
    for i, t in enumerate(db.q("""SELECT t.path, t.title, t.artist, t.ext FROM playlist_tracks pt
                                  JOIN tracks t ON t.id=pt.track_id WHERE pt.playlist_id=? ORDER BY pt.position""", pid), 1):
        files.append((t["path"], library.safe_name(f"{i:02d} - {t['artist']} - {t['title']}", f"{i:02d}") + t["ext"]))
    return StreamingResponse(library.zip_stream(files), media_type="application/zip",
                             headers={"Content-Disposition": _disposition(library.safe_name(p["name"], "playlist") + ".zip")})
