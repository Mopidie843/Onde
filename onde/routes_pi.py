"""Mode enceinte (lecture sur le Raspberry Pi)."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse

from . import settings
from .pi_player import player
from .web import STREAMS_CLOSING

router = APIRouter()


# --------------------------------------------------------------------------- #
# Mode enceinte (lecture sur le Pi)
# --------------------------------------------------------------------------- #
@router.get("/api/pi/state")
def pi_state():
    return player.state()


@router.get("/api/pi/devices")
def pi_devices():
    if not player.available:
        return {"devices": [], "current": ""}
    try:
        devices = player.audio_devices()
    except Exception as exc:
        raise HTTPException(503, f"mpv ne répond pas : {exc}")
    return {"devices": devices, "current": settings.get("pi_audio_device")}


@router.post("/api/pi/cmd")
def pi_cmd(body: dict = Body(...)):
    if not player.available:
        raise HTTPException(503, "Lecture sur le Pi indisponible : installe mpv (sudo apt install mpv)")
    try:
        return player.command(str(body.get("action")), body)
    except (RuntimeError, OSError) as exc:
        raise HTTPException(503, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/pi/events")
async def pi_events(request: Request):
    q = player.subscribe()

    async def gen():
        try:
            yield f"data: {json.dumps(player.state())}\n\n"
            while True:
                try:
                    state = await asyncio.wait_for(q.get(), timeout=15)
                    while not q.empty():  # ne garde que le plus récent
                        state = q.get_nowait()
                    if STREAMS_CLOSING.is_set():
                        break
                    yield f"data: {json.dumps(state)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                if STREAMS_CLOSING.is_set() or await request.is_disconnected():
                    break
        finally:
            player.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
