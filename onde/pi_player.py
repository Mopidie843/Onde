"""Mode enceinte : le Raspberry Pi joue lui-même la musique via mpv.

La file d'attente est gérée ici (côté serveur) : la musique continue même si
le téléphone qui la contrôle est éteint. Tous les appareils connectés voient
le même état en temps réel (Server-Sent Events). Minuterie de sommeil,
lecture automatique de titres similaires et volume égalisé fonctionnent aussi
sans téléphone.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import shlex
import shutil
import socket
import subprocess
import threading
import time

from . import config, db, discover, library, settings

log = logging.getLogger("onde.pi")

TARGET_LUFS = -14.0  # référence de volume (proche de Spotify « Normal »)


def gain_factor(lufs: float | None, peak: float | None, allow_boost: bool) -> float:
    """Facteur de volume pour ramener un titre à la référence, sans saturer."""
    if lufs is None:
        return 1.0
    factor = 10 ** ((TARGET_LUFS - lufs) / 20)
    ceiling = 1.0
    if allow_boost and peak is not None:
        ceiling = min(1.6, 10 ** ((-1.0 - peak) / 20))  # garde 1 dB de marge sous la crête
    return max(0.15, min(factor, max(ceiling, 1.0) if allow_boost else 1.0))


class PiPlayer:
    def __init__(self) -> None:
        self.available = config.PI_PLAYER and os.name == "posix" and shutil.which("mpv") is not None
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self.queue: list[int] = []
        self.index = -1
        self.rev = 0
        self.paused = True
        self.loaded = False
        self.position = 0.0
        self.duration = 0.0
        self.volume = 70          # volume choisi par l'utilisateur (0-100)
        self.gain = 1.0           # correction du titre en cours (volume égalisé)
        self.repeat = "off"
        self.auto_from = -1       # index du premier titre ajouté par la lecture automatique
        self.pending_seek: float | None = None
        self.errors_in_row = 0
        self.sleep_at: float | None = None   # time.monotonic() de l'arrêt programmé
        self.sleep_end = False               # arrêt à la fin du titre en cours
        self._fade_step = 0.0
        self._ids = itertools.count(1)
        self._pending: dict[int, dict] = {}

    # ------------------------------------------------------------ process mpv
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def _sock_path(self) -> str:
        return str(config.DATA_DIR / "mpv.sock")

    def _ensure(self) -> None:
        if not self.available:
            raise RuntimeError("mpv n'est pas installé sur le Raspberry Pi")
        if self.sock and self.proc and self.proc.poll() is None:
            return
        path = self._sock_path()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        args = [
            "mpv", "--idle=yes", "--no-video", "--no-terminal", "--audio-display=no",
            "--gapless-audio=weak", "--keep-open=no", "--msg-level=all=error",
            f"--input-ipc-server={path}", f"--volume={self.volume}",
        ]
        device = settings.get("pi_audio_device")
        if device and "--audio-device" not in config.MPV_ARGS:
            args.append(f"--audio-device={device}")
        args += shlex.split(config.MPV_ARGS)
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 6
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(path)
                break
            except OSError:
                s.close()
                if time.monotonic() > deadline or self.proc.poll() is not None:
                    raise RuntimeError("impossible de démarrer mpv (sortie audio ?)")
                time.sleep(0.08)
        self.sock = s
        threading.Thread(target=self._read_loop, args=(s,), name="onde-mpv", daemon=True).start()
        for i, prop in enumerate(("time-pos", "pause", "duration"), start=1):
            self._send(["observe_property", i, prop])

    def _send(self, cmd: list, request_id: int | None = None) -> None:
        data = (json.dumps({"command": cmd, "request_id": request_id or next(self._ids)}) + "\n").encode()
        for attempt in (1, 2):
            try:
                self._ensure()
                self.sock.sendall(data)
                return
            except OSError:
                self.sock = None
                if attempt == 2:
                    raise

    def _request(self, cmd: list, timeout: float = 3.0):
        """Commande avec réponse (ne jamais appeler en tenant self.lock)."""
        rid = next(self._ids)
        slot = {"ev": threading.Event(), "resp": None}
        self._pending[rid] = slot
        try:
            self._send(cmd, rid)
            if not slot["ev"].wait(timeout):
                raise RuntimeError("mpv ne répond pas")
            resp = slot["resp"] or {}
            if resp.get("error") not in (None, "success"):
                raise RuntimeError(resp.get("error"))
            return resp.get("data")
        finally:
            self._pending.pop(rid, None)

    def _read_loop(self, s: socket.socket) -> None:
        buf = b""
        while True:
            try:
                chunk = s.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                rid = msg.get("request_id")
                if "event" not in msg and rid in self._pending:
                    self._pending[rid]["resp"] = msg
                    self._pending[rid]["ev"].set()
                    continue
                try:
                    self._on_message(msg)
                except Exception:
                    log.exception("message mpv")
        with self.lock:
            if self.sock is s:
                self.sock = None
                self.loaded = False
                self.paused = True
        self.broadcast()

    def _on_message(self, m: dict) -> None:
        event = m.get("event")
        if event == "property-change":
            name, data = m.get("name"), m.get("data")
            if name == "time-pos":
                if data is not None:
                    self.position = float(data)
                    self._check_sleep()
                return
            if name == "pause":
                self.paused = bool(data)
            elif name == "duration" and data:
                self.duration = float(data)
            self.broadcast()
        elif event == "file-loaded":
            with self.lock:
                self.loaded = True
                self.errors_in_row = 0
                if self.pending_seek:
                    self._send(["seek", self.pending_seek, "absolute"])
                    self.pending_seek = None
                tid = self.current_id()
            if tid and not self.paused:
                try:
                    library.record_play(tid, "pi")
                except Exception:
                    log.exception("historique")
            self.broadcast()
        elif event == "end-file":
            reason = m.get("reason")
            if reason == "eof":
                if self.sleep_end:
                    with self.lock:
                        self.sleep_end = False
                        nxt = self.index + 1 if self.index + 1 < len(self.queue) else self.index
                        self._load(nxt, play=False)
                    return
                self._advance(auto=True)
            elif reason == "error":
                self.errors_in_row += 1
                if self.errors_in_row < max(3, len(self.queue)):
                    self._advance(auto=True)
        elif event in ("playback-restart", "seek", "idle"):
            self.broadcast()

    def shutdown(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # ------------------------------------------------------------ volume
    def _applied_volume(self) -> int:
        return max(0, min(130, round(self.volume * self.gain)))

    def _update_gain(self, tid: int | None) -> None:
        self.gain = 1.0
        if tid and settings.get("normalize"):
            row = db.q1("SELECT lufs, peak FROM tracks WHERE id=?", tid)
            if row:
                self.gain = gain_factor(row["lufs"], row["peak"], allow_boost=True)

    def refresh_volume(self) -> None:
        if not self.available or not self.sock:
            return
        self._update_gain(self.current_id())
        try:
            self._send(["set_property", "volume", self._applied_volume()])
        except OSError:
            pass

    # ------------------------------------------------------------ minuterie
    def _check_sleep(self) -> None:
        if not self.sleep_at or self.paused:
            return
        remaining = self.sleep_at - time.monotonic()
        if remaining <= 0:
            with self.lock:
                self.sleep_at = None
                self._send(["set_property", "pause", True])
                self.paused = True
                self._send(["set_property", "volume", self._applied_volume()])
            self.broadcast()
        elif remaining < 12 and time.monotonic() - self._fade_step > 0.5:  # fondu de fin
            self._fade_step = time.monotonic()
            self._send(["set_property", "volume", round(self._applied_volume() * remaining / 12)])

    # ------------------------------------------------------------ lecture
    def current_id(self) -> int | None:
        if 0 <= self.index < len(self.queue):
            return self.queue[self.index]
        return None

    def _load(self, index: int, position: float = 0.0, play: bool = True) -> None:
        self.index = index
        tid = self.current_id()
        row = db.q1("SELECT path, duration FROM tracks WHERE id=?", tid) if tid else None
        if not row:
            self._advance(auto=True)
            return
        self.position = position
        self.duration = row["duration"] or 0
        self.pending_seek = position if position > 0.5 else None
        self.loaded = False
        self._update_gain(tid)
        from . import lyrics  # paroles du titre lancé : cherchées avant les autres
        lyrics.prioritize(tid)
        self._send(["loadfile", row["path"], "replace"])
        self._send(["set_property", "volume", self._applied_volume()])
        self._send(["set_property", "pause", not play])
        self.paused = not play
        self.broadcast()

    def _extend_autoplay(self) -> bool:
        if not settings.get("autoplay"):
            return False
        ids = discover.autoplay_ids(self.queue)
        if not ids:
            return False
        if self.auto_from < 0:
            self.auto_from = len(self.queue)
        self.queue.extend(ids)
        self.rev += 1
        return True

    def _advance(self, auto: bool) -> None:
        with self.lock:
            if not self.queue:
                return
            if auto and self.repeat == "one":
                self._load(self.index)
            elif self.index + 1 < len(self.queue):
                self._load(self.index + 1)
            elif self.repeat == "all":
                self._load(0, play=auto or not self.paused)
            elif self._extend_autoplay():
                self._load(self.index + 1, play=auto or not self.paused)
            elif not auto:
                self._load(0, play=not self.paused)
            else:
                self.paused, self.loaded, self.position = True, False, 0.0
                self.broadcast()

    # ------------------------------------------------------------ commandes
    def command(self, action: str, p: dict) -> dict:
        with self.lock:
            if action == "set_queue":
                ids = [int(i) for i in p.get("ids", [])]
                index = int(p.get("index", 0))
                keep = bool(p.get("keep")) and self.loaded and ids[index:index + 1] == [self.current_id()]
                self.queue, self.rev = ids, self.rev + 1
                self.auto_from = int(p.get("autoFrom", -1)) if p.get("autoFrom") is not None else -1
                if not ids:
                    self._send(["stop"])
                    self.index, self.loaded, self.paused = -1, False, True
                elif keep:
                    self.index = index
                else:
                    self._load(max(0, min(index, len(ids) - 1)), float(p.get("position") or 0),
                               bool(p.get("play", True)))
                if "repeat" in p:
                    self.repeat = p["repeat"]
            elif action in ("play", "pause", "toggle"):
                if not self.loaded and self.current_id():
                    self._load(self.index, self.position, True)
                else:
                    want = {"play": False, "pause": True}.get(action, not self.paused)
                    self._send(["set_property", "pause", want])
                    if not want:
                        self._send(["set_property", "volume", self._applied_volume()])
                    self.paused = want
            elif action == "next":
                self._advance(auto=False)
            elif action == "prev":
                if self.position > 3 or self.index <= 0:
                    self.seek(0)
                else:
                    self._load(self.index - 1)
            elif action == "jump":
                self._load(int(p.get("index", 0)))
            elif action == "seek":
                self.seek(float(p.get("position", 0)))
            elif action == "volume":
                self.volume = max(0, min(100, int(p.get("value", 70))))
                self._send(["set_property", "volume", self._applied_volume()])
            elif action == "repeat":
                self.repeat = p.get("mode", "off")
            elif action == "sleep":
                minutes = p.get("minutes")
                self.sleep_end = minutes == "end"
                self.sleep_at = (time.monotonic() + float(minutes) * 60
                                 if isinstance(minutes, (int, float)) and minutes > 0 else None)
                self._send(["set_property", "volume", self._applied_volume()])
            elif action == "stop":
                self._send(["stop"])
                self.loaded, self.paused = False, True
            else:
                raise ValueError(f"commande inconnue : {action}")
        self.broadcast()
        return self.state()

    def seek(self, position: float) -> None:
        if self.loaded:
            self._send(["seek", max(0.0, position), "absolute"])
        else:
            self.pending_seek = position
        self.position = position

    # ------------------------------------------------------------ sorties audio
    def audio_devices(self) -> list[dict]:
        devices = self._request(["get_property", "audio-device-list"]) or []
        return [{"name": d.get("name", ""), "description": d.get("description") or d.get("name", "")}
                for d in devices if d.get("name")]

    def set_audio_device(self, name: str) -> None:
        if self.sock:
            self._send(["set_property", "audio-device", name or "auto"])

    # ------------------------------------------------------------ état / SSE
    def state(self) -> dict:
        sleep_in = None
        if self.sleep_at:
            sleep_in = max(0, round(self.sleep_at - time.monotonic()))
        return {
            "available": self.available,
            "running": bool(self.sock),
            "active": bool(self.queue) and self.index >= 0,
            "ids": self.queue, "rev": self.rev, "index": self.index,
            "trackId": self.current_id(), "paused": self.paused, "loaded": self.loaded,
            "position": round(self.position, 2), "duration": round(self.duration, 2),
            "volume": self.volume, "repeat": self.repeat, "autoFrom": self.auto_from,
            "sleepIn": sleep_in, "sleepEnd": self.sleep_end, "gain": round(self.gain, 3),
        }

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def broadcast(self) -> None:
        if not self.loop or not self.subscribers:
            return
        state = self.state()

        def push():
            for q in list(self.subscribers):
                try:
                    q.put_nowait(state)
                except asyncio.QueueFull:
                    pass

        try:
            self.loop.call_soon_threadsafe(push)
        except RuntimeError:
            pass


player = PiPlayer()
