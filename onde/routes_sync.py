"""Lecture synchronisée entre appareils."""
from __future__ import annotations

import asyncio
import json
import math
import threading
import time

from fastapi import APIRouter, Body, Request
from fastapi.responses import StreamingResponse

from . import restart, users
from .pi_player import player
from .web import STREAMS_CLOSING, shared

router = APIRouter()

# Au-delà, un état n'est plus envoyé à l'abonnement : l'appareil qui l'a publié s'est arrêté depuis
# longtemps, et le rejouer remplacerait la file d'attente de l'appareil qui suit par un vieux titre.
STALE_AFTER = 600  # s
PING_EVERY = 15    # s sans nouvel état : petit message pour garder le flux ouvert


# --------------------------------------------------------------------------- #
# Lecture synchronisée entre appareils du même profil (« suivre » un autre appareil)
# --------------------------------------------------------------------------- #
class _SyncHub:
    """Diffuse en direct (SSE) le dernier état de lecture publié par un appareil, pour que d'autres
    appareils du même profil puissent le « suivre ». Un hub par profil (voir `_hub`) : un profil ne
    voit ni ne pilote ce qu'écoute un autre."""

    def __init__(self):
        self.state: dict = {}
        self._subs: set[asyncio.Queue] = set()
        self._lock = threading.Lock()

    def publish(self, state: dict) -> None:
        with self._lock:
            self.state = state
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(state)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)


_hubs: dict[int, _SyncHub] = {}
_hubs_lock = threading.Lock()


def _hub(uid: int | None = None) -> _SyncHub:
    """Hub du profil `uid` (par défaut : celui de la requête), créé au premier besoin."""
    uid = users.uid() if uid is None else uid
    with _hubs_lock:
        hub = _hubs.get(uid)
        if hub is None:
            hub = _hubs[uid] = _SyncHub()
        return hub


def _position(value) -> float:
    """Position en secondes envoyée par l'appareil : nombre fini et positif, sinon 0."""
    try:
        pos = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, pos) if math.isfinite(pos) else 0.0


def _event(state: dict) -> str:
    """Message SSE. `age` (en secondes, calculé par le serveur) permet à l'appareil qui suit d'écarter
    un vieil état sans comparer `ts` à sa propre horloge (qui peut être décalée)."""
    now = time.time()
    return f"data: {json.dumps({**state, 'age': round(max(0.0, now - state.get('ts', now)), 1)})}\n\n"


@restart.on_exit
def _close_event_streams() -> None:
    """Termine tout de suite les flux en direct ouverts, sinon l'arrêt (Ctrl+C, redémarrage)
    attendrait qu'ils se ferment d'eux-mêmes."""
    STREAMS_CLOSING.set()
    loop = shared.loop
    if loop is None or loop.is_closed():
        return

    def wake() -> None:
        with _hubs_lock:
            hubs = list(_hubs.values())
        for q in list(player.subscribers) + [q for hub in hubs for q in list(hub._subs)]:
            try:
                q.put_nowait({})
            except asyncio.QueueFull:
                pass  # file pleine : l'attente se termine de toute façon tout de suite

    try:
        loop.call_soon_threadsafe(wake)
    except RuntimeError:
        pass


@router.post("/api/sync/broadcast")
async def sync_broadcast(body: dict = Body(...)):  # async : les files asyncio ne se touchent que depuis la boucle
    tid = body.get("trackId")
    state = {
        "trackId": int(tid) if isinstance(tid, (int, float, str)) and str(tid).lstrip("-").isdigit() else None,
        "position": _position(body.get("position")),
        "paused": bool(body.get("paused")),
        "device": str(body.get("device") or "Un appareil")[:60],
        "ts": time.time(),
    }
    _hub().publish(state)
    return {"ok": True}


@router.get("/api/sync/events")
async def sync_events(request: Request):
    hub = _hub()  # (lu ici : le profil de la requête, pas celui du générateur)
    q = hub.subscribe()

    async def gen():
        try:
            last = hub.state
            if last and time.time() - last["ts"] < STALE_AFTER:
                yield _event(last)
            while True:
                try:
                    state = await asyncio.wait_for(q.get(), timeout=PING_EVERY)
                    while not q.empty():
                        state = q.get_nowait()
                    if STREAMS_CLOSING.is_set():
                        break
                    yield _event(state)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                if STREAMS_CLOSING.is_set() or await request.is_disconnected():
                    break
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
