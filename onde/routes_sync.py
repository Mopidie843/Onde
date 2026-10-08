"""Lecture synchronisée entre appareils."""
from __future__ import annotations

import asyncio
import json
import threading
import time

from fastapi import APIRouter, Body, Request
from fastapi.responses import StreamingResponse

from . import restart
from .pi_player import player
from .web import STREAMS_CLOSING, shared

router = APIRouter()


# --------------------------------------------------------------------------- #
# Lecture synchronisée entre appareils du même réseau (« suivre » un autre appareil)
# --------------------------------------------------------------------------- #
class _SyncHub:
    """Diffuse en direct (SSE) le dernier état de lecture publié par n'importe quel appareil, pour
    que d'autres appareils du même réseau puissent le « suivre ». Pas de notion de salle/compte : un
    seul état partagé, à l'échelle du serveur — largement suffisant pour un usage familial sur un LAN."""

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


sync_hub = _SyncHub()


@restart.on_exit
def _close_event_streams() -> None:
    """Termine tout de suite les flux en direct ouverts, sinon l'arrêt (Ctrl+C, redémarrage)
    attendrait qu'ils se ferment d'eux-mêmes."""
    STREAMS_CLOSING.set()
    loop = shared.loop
    if loop is None or loop.is_closed():
        return

    def wake() -> None:
        for q in list(player.subscribers) + list(sync_hub._subs):
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
        "position": max(0.0, float(body.get("position") or 0)),
        "paused": bool(body.get("paused")),
        "device": str(body.get("device") or "Un appareil")[:60],
        "ts": time.time(),
    }
    sync_hub.publish(state)
    return {"ok": True}


@router.get("/api/sync/events")
async def sync_events(request: Request):
    q = sync_hub.subscribe()

    async def gen():
        try:
            if sync_hub.state:
                yield f"data: {json.dumps(sync_hub.state)}\n\n"
            while True:
                try:
                    state = await asyncio.wait_for(q.get(), timeout=15)
                    while not q.empty():
                        state = q.get_nowait()
                    if STREAMS_CLOSING.is_set():
                        break
                    yield f"data: {json.dumps(state)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                if STREAMS_CLOSING.is_set() or await request.is_disconnected():
                    break
        finally:
            sync_hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
