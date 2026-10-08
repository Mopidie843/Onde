"""Outils partagés par les routes d'Onde : réponse JSON rapide, erreurs, lecture d'un titre…

Ces fonctions étaient dans main.py ; elles sont ici pour que chaque famille de routes
(`routes_*.py`) puisse les utiliser sans importer main.py (pas d'import circulaire).
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import urllib.parse

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from . import api_models as M
from . import config, covers, db, library


class Shared:
    """État commun au serveur : la boucle asyncio principale (remplie au démarrage)."""
    loop: asyncio.AbstractEventLoop | None = None


shared = Shared()
STREAMS_CLOSING = threading.Event()  # arrêt en cours : les flux en direct (SSE) se terminent


class J(Response):
    """JSON rapide : évite jsonable_encoder de FastAPI (beaucoup plus lent sur les grosses listes)."""
    media_type = "application/json"

    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _404(what: str = "Introuvable"):
    raise HTTPException(status_code=404, detail=what)


def _disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode() or "onde"
    ascii_name = ascii_name.replace('"', "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=utf-8''{urllib.parse.quote(filename)}"


def _stats() -> dict:
    r = db.q1("SELECT COUNT(*) n, COALESCE(SUM(size),0) size, COALESCE(SUM(duration),0) dur FROM tracks")
    albums = db.q1("SELECT COUNT(*) n FROM albums WHERE name<>?", config.SINGLES)["n"]
    artists = db.q1("SELECT COUNT(DISTINCT artist COLLATE NOCASE) n FROM tracks")["n"]
    liked = db.q1("SELECT COUNT(*) n FROM likes WHERE user_id=onde_uid()")["n"]
    return {"tracks": r["n"], "size": r["size"], "duration": round(r["dur"]), "albums": albums,
            "artists": artists, "liked": liked}


async def _read_limited(request: Request, limit: int) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise HTTPException(413, "Fichier trop volumineux")
    return bytes(data)


async def _cover_from_request(request: Request) -> str:
    """Accepte soit une image brute (image/*), soit du JSON {"url": "..."}."""
    ctype = request.headers.get("content-type", "")
    body = await _read_limited(request, 25_000_000)
    if ctype.startswith("application/json"):
        url = (json.loads(body or b"{}").get("url") or "").strip()
        if not url:
            raise HTTPException(400, "URL manquante")
        try:
            raw = await run_in_threadpool(covers.fetch_image, url)
        except Exception as exc:
            raise HTTPException(400, f"Image inaccessible : {exc}")
    else:
        raw = body
    cid = await run_in_threadpool(covers.store_cover, raw)
    if not cid:
        raise HTTPException(400, "Ce fichier n'est pas une image valide")
    return cid


def _flag(request: Request, name: str, default: bool) -> bool:
    v = request.query_params.get(name)
    return default if v is None else v not in ("0", "false", "no")


def _track_row(tid: int):
    row = db.q1("SELECT * FROM tracks WHERE id=?", tid)
    if not row:
        _404("Titre introuvable")
    return row


def _reindex(tid: int, path: str, old_album) -> None:
    meta = library.read_meta(path)
    st = os.stat(path)
    with db.tx():
        library.upsert_track(path, st, meta, track_id=tid)
        new = db.q1("SELECT a.* FROM tracks t JOIN albums a ON a.id=t.album_id WHERE t.id=?", tid)
        if (old_album and new and new["id"] != old_album["id"] and old_album["cover_id"]
                and (not new["cover_id"] or (old_album["cover_locked"] and not new["cover_locked"]))):
            db.x("UPDATE albums SET cover_id=?, cover_locked=? WHERE id=?",
                 old_album["cover_id"], old_album["cover_locked"], new["id"])
            if old_album["cover_locked"]:
                db.x("UPDATE tracks SET cover_id=? WHERE id=?", old_album["cover_id"], tid)
        library.cleanup_albums()


def _playlist(pid: int) -> dict:
    rows = M.playlist_summary(pid)
    if not rows:
        _404("Playlist introuvable")
    return rows[0]
