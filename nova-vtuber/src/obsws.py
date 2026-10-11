"""Liaison avec OBS Studio par son serveur WebSocket intégré (OBS 28+, protocole v5) : scène en cours,
changement de scène (pause automatique). Le port et le mot de passe sont lus dans les réglages d'OBS
(lecture seule) : rien à recopier. Reconnexion automatique quand OBS est ouvert ou redémarré."""
import base64
import hashlib
import json
import logging
import os
import socket
import struct
import threading
import uuid

from . import health
from .obs import obs_dir

log = logging.getLogger("nova.obsws")
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC11B85"
SUB_SCENES = 1 << 2          # événements de scènes (changement de scène)
RETRY = 5.0


# ---------------------------------------------------------------- WebSocket minimal (client)
class _WS:
    def __init__(self, sock: socket.socket, buf: bytes = b""):
        self.sock, self.buf = sock, bytearray(buf)
        self._send_lock = threading.Lock()
        self._parts: list[bytes] = []

    @classmethod
    def connect(cls, host: str, port: int, timeout: float = 3.0) -> "_WS":
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                          "Sec-WebSocket-Protocol: obswebsocket.json\r\n\r\n").encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("réponse incomplète")
                resp += chunk
                if len(resp) > 65536:
                    raise ConnectionError("réponse trop longue")
            head, rest = resp.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            if " 101 " not in lines[0] + " ":
                raise ConnectionError(f"refusé : {lines[0]}")
            expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
            if headers.get("sec-websocket-accept") != expected:
                raise ConnectionError("poignée de main WebSocket invalide")
            return cls(sock, rest)
        except BaseException:
            sock.close()
            raise

    def send(self, text: str) -> None:
        data = text.encode("utf-8")
        n = len(data)
        head = bytearray([0x81])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self._send_lock:
            self.sock.sendall(bytes(head) + body)

    def _control(self, op: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        with self._send_lock:
            self.sock.sendall(bytes([0x80 | op, 0x80 | len(payload)]) + mask
                              + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _fill(self, n: int) -> None:
        """Lit jusqu'à avoir n octets en mémoire (rien n'est consommé : un délai dépassé ne perd rien)."""
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connexion fermée par OBS")
            self.buf += chunk

    def recv(self) -> str:
        """Prochain message texte. Lève socket.timeout (rien de reçu) ou ConnectionError."""
        while True:
            self._fill(2)
            b1, b2 = self.buf[0], self.buf[1]
            ln, off = b2 & 0x7F, 2
            if ln == 126:
                self._fill(4)
                ln, off = struct.unpack(">H", self.buf[2:4])[0], 4
            elif ln == 127:
                self._fill(10)
                ln, off = struct.unpack(">Q", self.buf[2:10])[0], 10
            masked = b2 & 0x80
            if masked:
                off += 4
            self._fill(off + ln)
            payload = bytes(self.buf[off: off + ln])
            if masked:
                m = self.buf[off - 4: off]
                payload = bytes(b ^ m[i % 4] for i, b in enumerate(payload))
            del self.buf[: off + ln]
            op, fin = b1 & 0x0F, b1 & 0x80
            if op == 0x9:
                self._control(0xA, payload)
            elif op == 0x8:
                code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005
                raise ConnectionError(f"fermée par OBS (code {code}) {payload[2:].decode('utf-8', 'replace')}".strip())
            elif op in (0x0, 0x1):
                self._parts.append(payload)
                if fin:
                    text = b"".join(self._parts).decode("utf-8", "replace")
                    self._parts = []
                    return text

    def close(self) -> None:
        try:
            self._control(0x8, struct.pack(">H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- réglages d'OBS
def obs_config() -> dict:
    """Serveur WebSocket d'OBS : {enabled, port, password} lus dans plugin_config/obs-websocket/config.json."""
    out = {"enabled": False, "port": 4455, "password": ""}
    try:
        cfg = json.loads((obs_dir() / "plugin_config" / "obs-websocket" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    out["enabled"] = bool(cfg.get("server_enabled", False))
    out["port"] = int(cfg.get("server_port", 4455) or 4455)
    if cfg.get("auth_required", True):
        out["password"] = cfg.get("server_password", "") or ""
    return out


def auth_string(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


# ---------------------------------------------------------------- liaison
class OBSLink:
    """Connexion permanente à OBS dans un fil dédié ; request() est utilisable depuis n'importe quel fil."""

    def __init__(self, host: str = "127.0.0.1"):
        self.host = host
        self.password_override = ""
        self.ws: _WS | None = None
        self.current_scene = ""
        self.scenes: list[str] = []
        self._pending: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    @property
    def connected(self) -> bool:
        return self.ws is not None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="obs-websocket")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self.ws
        if ws:
            ws.close()
        if self._thread:
            self._thread.join(3)

    def _report(self, state: str, message: str) -> None:
        health.report("obs", state=state, message=message, scene=self.current_scene)

    def _run(self) -> None:
        while not self._stop.is_set():
            cfg = obs_config()
            if not cfg["enabled"]:
                self._report("off", "Serveur WebSocket d'OBS désactivé : dans OBS, menu Outils › Paramètres du "
                                    "serveur WebSocket › cochez « Activer le serveur WebSocket ».")
            else:
                try:
                    self._session(cfg["port"], self.password_override or cfg["password"])
                except (ConnectionRefusedError, socket.timeout, TimeoutError):
                    self._report("absent", "OBS n'est pas ouvert.")
                except (OSError, ConnectionError, ValueError, KeyError) as e:
                    msg = str(e)
                    if "4009" in msg:
                        msg = "Mot de passe refusé par OBS (réglages du serveur WebSocket d'OBS)."
                    self._report("error", f"OBS : {msg}")
                    log.info("OBS WebSocket : %s", e)
                finally:
                    self._disconnect()
            self._stop.wait(RETRY)

    def _session(self, port: int, password: str) -> None:
        ws = _WS.connect(self.host, port)
        ws.sock.settimeout(5)
        hello = json.loads(ws.recv())
        if hello.get("op") != 0:
            raise ValueError("ce n'est pas OBS WebSocket 5")
        ident = {"rpcVersion": 1, "eventSubscriptions": SUB_SCENES}
        auth = hello["d"].get("authentication")
        if auth:
            ident["authentication"] = auth_string(password, auth["salt"], auth["challenge"])
        ws.send(json.dumps({"op": 1, "d": ident}))
        if json.loads(ws.recv()).get("op") != 2:
            raise ValueError("identification refusée")
        self.ws = ws
        ws.sock.settimeout(1.0)
        ws.send(json.dumps({"op": 6, "d": {"requestType": "GetSceneList", "requestId": "nova-init"}}))
        self._report("ok", "Connecté à OBS")
        log.info("Connecté à OBS (port %d)", port)
        while not self._stop.is_set():
            try:
                msg = json.loads(ws.recv())
            except socket.timeout:
                continue
            self._handle(msg)

    def _handle(self, msg: dict) -> None:
        op, d = msg.get("op"), msg.get("d") or {}
        if op == 7:
            rid = d.get("requestId")
            if d.get("requestType") == "GetSceneList" and d.get("requestStatus", {}).get("result"):
                data = d.get("responseData") or {}
                self.scenes = [s.get("sceneName", "") for s in reversed(data.get("scenes", []))]
                self.current_scene = data.get("currentProgramSceneName", self.current_scene)
                self._report("ok", "Connecté à OBS")
            with self._lock:
                slot = self._pending.get(rid)
            if slot:
                slot[1] = d
                slot[0].set()
        elif op == 5:
            ev, data = d.get("eventType"), d.get("eventData") or {}
            if ev == "CurrentProgramSceneChanged":
                self.current_scene = data.get("sceneName", "")
                self._report("ok", "Connecté à OBS")
            elif ev in ("SceneCreated", "SceneRemoved", "SceneNameChanged", "SceneListChanged"):
                self._send({"op": 6, "d": {"requestType": "GetSceneList", "requestId": "nova-init"}})

    def _send(self, msg: dict) -> bool:
        ws = self.ws
        if ws is None:
            return False
        try:
            ws.send(json.dumps(msg))
            return True
        except OSError:
            return False

    def _disconnect(self) -> None:
        ws, self.ws = self.ws, None
        if ws:
            ws.close()
        with self._lock:
            for slot in self._pending.values():
                slot[0].set()

    def request(self, request_type: str, data: dict | None = None, timeout: float = 3.0) -> dict | None:
        """Requête OBS ; retourne la réponse ({requestStatus, responseData}) ou None."""
        rid = uuid.uuid4().hex
        slot = [threading.Event(), None]
        with self._lock:
            self._pending[rid] = slot
        try:
            d = {"requestType": request_type, "requestId": rid}
            if data:
                d["requestData"] = data
            if not self._send({"op": 6, "d": d}):
                return None
            slot[0].wait(timeout)
            return slot[1]
        finally:
            with self._lock:
                self._pending.pop(rid, None)

    def set_scene(self, name: str) -> bool:
        r = self.request("SetCurrentProgramScene", {"sceneName": name})
        ok = bool(r and r.get("requestStatus", {}).get("result"))
        if ok:
            self.current_scene = name
        return ok

    def find_scene(self, words) -> str:
        """Première scène dont le nom contient un des mots (sans tenir compte des majuscules)."""
        for s in self.scenes:
            if any(w in s.lower() for w in words):
                return s
        return ""


LINK = OBSLink()


def ensure_started() -> OBSLink:
    LINK.start()
    return LINK
