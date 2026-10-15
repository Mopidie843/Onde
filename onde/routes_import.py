"""Import (envoi direct ou lien)."""
from __future__ import annotations

import logging

from fastapi import APIRouter

log = logging.getLogger("onde")
import os
import re
import urllib.parse

from fastapi import Body, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import config, downloader, library, net, notify, quota, users
from .web import J, LocalAddress, _check_public_url, _text

router = APIRouter()


# --------------------------------------------------------------------------- #
# Import (upload direct ou lien)
# --------------------------------------------------------------------------- #
@router.post("/api/upload")
async def upload(request: Request):
    name = urllib.parse.unquote(request.headers.get("x-filename") or "import.mp3")
    name = os.path.basename(name.replace("\\", "/")) or "import.mp3"
    ext = os.path.splitext(name)[1].lower()
    if ext not in config.IMPORT_TYPES:
        return JSONResponse({"status": "error", "name": name,
                             "error": "Format non pris en charge"}, status_code=415)
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    try:
        await run_in_threadpool(quota.check, None, int(request.headers.get("content-length") or 0))
    except quota.QuotaFull as exc:
        return JSONResponse({"status": "error", "name": name, "error": str(exc), "quota": True}, status_code=413)
    tmp = library.new_incoming(ext)
    size = 0
    fh = await run_in_threadpool(open, tmp, "wb")
    try:
        buf = bytearray()
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, "Fichier trop volumineux")
            buf += chunk
            if len(buf) >= 1 << 20:
                data = bytes(buf)
                buf.clear()
                await run_in_threadpool(fh.write, data)
        if buf:
            await run_in_threadpool(fh.write, bytes(buf))
    except BaseException:
        fh.close()
        tmp.unlink(missing_ok=True)
        raise
    fh.close()
    if size == 0:
        tmp.unlink(missing_ok=True)
        return JSONResponse({"status": "error", "name": name, "error": "Fichier vide"}, status_code=400)
    res = await run_in_threadpool(library.ingest_any, tmp, name)
    if res.get("status") == "added":
        await run_in_threadpool(quota.mark, res)
        await run_in_threadpool(users.log_activity, "upload", name)
    elif res.get("status") == "error":
        await run_in_threadpool(users.log_activity, "import_error", f"{name} : {res.get('error')}")
    return J(res)


_ctype_ext = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
              "audio/aac": ".aac", "audio/flac": ".flac", "audio/x-flac": ".flac", "audio/ogg": ".ogg",
              "audio/opus": ".opus", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
              "application/zip": ".zip", "application/x-zip-compressed": ".zip"}


@router.get("/api/download-available")
def download_available():
    return {"available": downloader.available()}


class _NotDirectAudio(Exception):
    """Le lien n'est pas un fichier audio direct (probablement une page : YouTube, SoundCloud…)."""


def _fetch_direct(url: str) -> dict:
    """Télécharge un lien pointant DIRECTEMENT vers un fichier audio (pas une page de site).
    En flux, sur le disque : pas de durée totale (gros FLAC, albums .zip), mais 20 s au plus sans
    recevoir de données."""
    started = False  # en-têtes reçus : une erreur ensuite est une coupure en plein téléchargement
    tmp = None
    try:
        with net.stream(url, read_timeout=20.0) as resp:
            started = True
            name = ""
            cd = resp.headers.get("Content-Disposition") or ""
            m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)|filename=\"?([^\";]+)", cd, re.I)
            if m:
                name = urllib.parse.unquote((m.group(1) or m.group(2) or "").strip())
            if not name:
                name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(str(resp.url)).path))
            ext = os.path.splitext(name)[1].lower()
            if ext not in config.IMPORT_TYPES:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                ext = _ctype_ext.get(ctype, "")
                if not ext:
                    raise _NotDirectAudio("ce lien ne pointe pas directement vers un fichier audio")
                name = (os.path.splitext(name)[0] or "import") + ext
            tmp = library.new_incoming(ext)
            limit = config.MAX_UPLOAD_MB * 1024 * 1024
            size = 0
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, "Fichier trop volumineux")
                    fh.write(chunk)
    except BaseException as exc:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        if isinstance(exc, (net.NetError, ValueError)) and not isinstance(exc, _NotDirectAudio):
            if not started:  # lien injoignable ou erreur HTTP : peut-être une page (YouTube…)
                raise _NotDirectAudio(str(exc)) from exc
            raise HTTPException(400, f"Téléchargement interrompu : {exc}") from exc
        raise
    return library.ingest_any(tmp, name)


def _notify_download(res: dict) -> dict:
    """Notification « musique téléchargée » (l'appli Android la montre si on a quitté l'écran), pour
    celui qui a lancé le téléchargement seulement."""
    try:
        if res.get("status") == "added":
            tracks = res.get("tracks") or ([res["track"]] if res.get("track") else [])
            users.log_activity("download", ", ".join(f"{t.get('title')} — {t.get('artist')}" for t in tracks[:3]))
            if len(tracks) == 1:
                notify.downloaded(tracks[0], user_id=users.uid())
            elif tracks:
                notify.add("download", "Musique téléchargée", f"{len(tracks)} musiques téléchargées", "#/library",
                           count_text="{n} musiques téléchargées", amount=len(tracks), user_id=users.uid())
    except Exception:
        log.exception("notification de téléchargement")
    return res


@router.post("/api/import-url")
def import_url(body: dict = Body(...)):
    url = _text(body.get("url"), 4000, "lien ou nom de musique attendu")
    try:
        quota.check()
    except quota.QuotaFull as exc:
        raise HTTPException(413, str(exc))
    try:
        return J(quota.mark(_import_url(url)))
    except HTTPException as exc:
        users.log_activity("import_error", f"{url[:120]} : {exc.detail}")
        raise


def _import_url(url: str) -> dict:
    if not re.match(r"^https?://", url, re.I):
        # pas un lien : on cherche et on télécharge ce titre en ligne
        try:
            return (_notify_download(downloader.search_download(url)))
        except ValueError:
            raise HTTPException(400, "Tape le nom d'une musique (ex. « Artiste - Titre ») ou colle un lien direct")
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))
    if downloader.playlist_kind(url) and not re.search(r"[?&]v=", url):
        raise HTTPException(400, "C'est un lien de playlist : colle-le dans « Importer une playlist » (un peu plus bas).")
    if not users.is_admin():  # un ami ne fait pas sonder le réseau de la maison par le serveur
        try:
            _check_public_url(url)
        except LocalAddress as exc:
            raise HTTPException(400, str(exc))
        except ValueError as exc:
            log.info("lien refusé (%s) : %s", url[:200], exc)
            raise HTTPException(400, "Lien inaccessible depuis Onde : vérifie l'adresse.")
    # lien : d'abord comme fichier audio direct (MP3, M4A…) ; sinon comme page d'un site
    # (YouTube, SoundCloud, Bandcamp…) via le même moteur que la recherche par nom.
    try:
        return (_notify_download(_fetch_direct(url)))
    except _NotDirectAudio:
        pass
    try:
        return (_notify_download(downloader.download_from_url(url)))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/import-playlist")
def import_playlist(body: dict = Body(...)):
    """Résout un lien de playlist (YouTube, SoundCloud, Deezer, Spotify) en liste de titres à télécharger
    (le téléchargement lui-même se fait ensuite titre par titre via /api/import-url, comme une
    liste collée à la main)."""
    url = _text(body.get("url"), 4000, "lien de playlist attendu")
    if not url:
        raise HTTPException(400, "Colle un lien de playlist")
    try:
        return J(downloader.resolve_playlist(url))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))
