"""Outils partagés par les routes d'Onde : réponse JSON rapide, erreurs, lecture d'un titre…

Ces fonctions étaient dans main.py ; elles sont ici pour que chaque famille de routes
(`routes_*.py`) puisse les utiliser sans importer main.py (pas d'import circulaire).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
import socket
import threading
import urllib.parse

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from . import api_models as M
from . import config, covers, db, library, users

log = logging.getLogger("onde.web")


class Shared:
    """État commun au serveur : la boucle asyncio principale (remplie au démarrage)."""
    loop: asyncio.AbstractEventLoop | None = None


shared = Shared()
STREAMS_CLOSING = threading.Event()  # arrêt en cours : les flux en direct (SSE) se terminent


class J(Response):
    """JSON rapide : évite jsonable_encoder de FastAPI (beaucoup plus lent sur les grosses listes).
    Jamais de NaN ni d'Infinity (JSON invalide, refusé par les navigateurs) : une vieille valeur
    infinie gardée en base devient null au lieu de casser la page."""
    media_type = "application/json"

    def render(self, content) -> bytes:
        try:
            text = json.dumps(content, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except ValueError:
            text = json.dumps(_finite(content), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return text.encode("utf-8")


def _finite(v):
    """Copie de `v` où les nombres non finis (nan, inf) deviennent None."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_finite(x) for x in v]
    return v


def _404(what: str = "Introuvable"):
    raise HTTPException(status_code=404, detail=what)


# ------------------------------------------------------------------ entrées des requêtes
# Une entrée invalide (texte à la place d'un nombre, tableau à la place d'un objet, « Infinity »…)
# donne une erreur 400 lisible, jamais une erreur 500.
def _bad(what: str) -> HTTPException:
    return HTTPException(400, f"Requête invalide : {what}.")


def _num(v, lo: float, hi: float, what: str = "nombre attendu") -> float:
    """Nombre fini (texte numérique accepté), ramené entre `lo` et `hi`. nan et inf refusés."""
    if v is None or isinstance(v, (list, dict)):
        raise _bad(what)
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise _bad(what) from None
    if not math.isfinite(f):
        raise _bad(what)
    return min(hi, max(lo, f))


def _int(v, lo: int, hi: int, what: str = "nombre entier attendu") -> int:
    """Entier (« 12 » et 12.0 acceptés), ramené entre `lo` et `hi`."""
    return int(_num(v, lo, hi, what))


def _ints(v, max_n: int, what: str = "liste de numéros attendue") -> list[int]:
    """Liste de numéros (ex. trackIds) : au plus `max_n`, chacun entier et positif."""
    if v is None:
        return []
    if not isinstance(v, list):
        raise _bad(what)
    return [_int(x, 0, 2 ** 62, what) for x in v[:max_n]]


def _text(v, max_len: int, what: str = "texte attendu") -> str:
    """Texte sans espaces au bord, tronqué à `max_len`. Nombres acceptés ; objets et listes refusés."""
    if v is None:
        return ""
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise _bad(what)
    return str(v).strip()[:max_len]


def _obj(body: bytes | None) -> dict:
    """Corps JSON qui doit être un objet {…}."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise _bad("JSON illisible") from None
    if not isinstance(data, dict):
        raise _bad("objet JSON attendu")
    return data


# ------------------------------------------------------------------ liens donnés par un profil (SSRF)
class LocalAddress(ValueError):
    """Lien qui mène au réseau local (box, NAS, autre service de la maison) : refusé."""


def _check_public_url(url: str) -> None:
    """Refuse un lien vers le réseau local ou la machine elle-même : sinon un ami pourrait faire sonder
    la maison par le serveur (« ce port répond-il ? »). L'administrateur garde l'accès au réseau
    local (NAS…) : il a déjà tous les droits. Bloquant (résolution DNS) : hors de la boucle asyncio.
    Les redirections ne sont pas revérifiées ici (il faudrait un contrôle dans net.py, à chaque requête)."""
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        raise ValueError("URL invalide")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"adresse introuvable ({exc})") from exc
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        # (non « global » : réseau privé, la machine elle-même, lien local, 100.64/10 de Tailscale, réservé…)
        if not ip.is_global or ip.is_multicast:
            raise LocalAddress("Adresse locale refusée : colle un lien vers Internet.")


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
        url = _text(_obj(body).get("url"), 2000, "lien de l'image attendu")
        if not url:
            raise HTTPException(400, "URL manquante")
        try:
            if not users.is_admin():
                await run_in_threadpool(_check_public_url, url)
            raw = await run_in_threadpool(covers.fetch_image, url)
        except LocalAddress as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            # message générique : le détail (« connexion refusée », « pas une image »…) dirait si une
            # adresse répond ; il reste dans le journal
            log.info("pochette par lien non récupérée (%s) : %s", url[:200], exc)
            raise HTTPException(400, "Image inaccessible depuis Onde.")
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
