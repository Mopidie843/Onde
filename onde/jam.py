"""Mode soirée : une file d'attente commune. L'hôte affiche un QR code ; les invités (sans compte, sans
appli) cherchent dans la bibliothèque et ajoutent des titres, qui passent sur l'appareil de l'hôte.

En mémoire seulement (s'arrête quand Onde redémarre). Lien secret, valable 8 heures. Un invité ne peut
qu'ajouter des titres de la bibliothèque (pas de suppression, pas de réglages, pas de téléchargement).
"""
from __future__ import annotations

import secrets
import threading
import time

from . import api_models as M
from . import db

TTL = 8 * 3600
PER_GUEST_SECONDS = 4
MAX_ITEMS = 500
_lock = threading.Lock()
_jams: dict[str, dict] = {}       # jeton -> soirée
_by_host: dict[int, str] = {}     # profil hôte -> jeton


def _clean() -> None:
    now = time.time()
    for tok in [t for t, j in _jams.items() if j["expires"] < now]:
        _by_host.pop(_jams[tok]["host"], None)
        _jams.pop(tok, None)


def start(host_id: int, host_name: str) -> dict:
    with _lock:
        _clean()
        tok = _by_host.get(host_id)
        if tok and tok in _jams:
            return _jams[tok]
        tok = secrets.token_urlsafe(12)
        jam = {"token": tok, "host": host_id, "hostName": host_name, "created": time.time(),
               "expires": time.time() + TTL, "items": [], "seq": 0, "guests": {}, "now": None}
        _jams[tok] = jam
        _by_host[host_id] = tok
        return jam


def stop(host_id: int) -> None:
    with _lock:
        tok = _by_host.pop(host_id, None)
        _jams.pop(tok, None)


def of_host(host_id: int) -> dict | None:
    with _lock:
        _clean()
        tok = _by_host.get(host_id)
        return _jams.get(tok) if tok else None


def get(token: str) -> dict | None:
    with _lock:
        _clean()
        return _jams.get(token or "")


def set_now(host_id: int, track_id: int | None) -> None:
    with _lock:
        tok = _by_host.get(host_id)
        if tok in _jams:
            _jams[tok]["now"] = track_id


def add(token: str, track_id: int, by: str, guest: str) -> dict:
    jam = get(token)
    if not jam:
        raise KeyError(token)
    if not db.q1("SELECT 1 FROM tracks WHERE id=?", track_id):
        raise ValueError("Titre introuvable")
    now = time.time()
    with _lock:
        last = jam["guests"].get(guest, 0)
        if now - last < PER_GUEST_SECONDS:
            raise ValueError("Doucement : attends quelques secondes avant d'ajouter un autre titre.")
        if len(jam["items"]) >= MAX_ITEMS:
            raise ValueError("La file de la soirée est pleine.")
        jam["guests"][guest] = now
        jam["seq"] += 1
        item = {"n": jam["seq"], "trackId": track_id, "by": " ".join((by or "Invité").split())[:30] or "Invité", "at": now}
        jam["items"].append(item)
        return item


def public_view(jam: dict) -> dict:
    ids = [i["trackId"] for i in jam["items"][-30:]]
    if jam["now"]:
        ids.append(jam["now"])
    tracks = {t["id"]: t for t in M.tracks_by_ids(list(dict.fromkeys(ids)))}
    lite = lambda t: t and {"id": t["id"], "title": t["title"], "artist": t["artist"], "cover": t["cover"]}  # noqa: E731
    return {"host": jam["hostName"], "expires": jam["expires"], "now": lite(tracks.get(jam["now"])),
            "added": [{**i, "track": lite(tracks.get(i["trackId"]))} for i in reversed(jam["items"][-30:])]}
