"""Services d'entrée/sortie : serveur local (rendu VRM) et microphone (lip sync)."""
import functools
import hashlib
import json
import logging
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QHostAddress
from PySide6.QtWebSockets import QWebSocketServer

from .phonemes import VOWELS, VowelClassifier, envelope
from .settings import ACCESSORIES_DIR, AVATARS_DIR, BACKGROUNDS_DIR, ROOT

log = logging.getLogger("nova.io")


# ---------------------------------------------------------------- serveur local
class _Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                      ".js": "application/javascript", ".html": "text/html; charset=utf-8",
                      ".vrm": "application/octet-stream", ".task": "application/octet-stream",
                      ".glb": "model/gltf-binary", ".webp": "image/webp"}

    SHARED = {"/avatars/": AVATARS_DIR, "/accessories/": ACCESSORIES_DIR, "/backgrounds/": BACKGROUNDS_DIR}

    def translate_path(self, path):
        p = unquote(path.split("?", 1)[0])
        for prefix, folder in self.SHARED.items():
            if p.startswith(prefix):
                target = (folder / p[len(prefix):]).resolve()
                return str(target) if folder.resolve() in target.parents else str(folder / "__interdit__")
        return super().translate_path(path)

    def end_headers(self):
        # les avatars sont versionnés (?v=date du fichier) : on peut les garder en cache
        versioned = self.path.startswith(("/avatars/", "/accessories/", "/backgrounds/")) and "?v=" in self.path
        cache = "max-age=604800" if versioned else "no-store"
        self.send_header("Cache-Control", cache)
        super().end_headers()

    def log_message(self, *args):
        pass


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False  # sous Windows, SO_REUSEADDR laisserait deux serveurs partager le port
    daemon_threads = True


class RenderServer(QObject):
    """HTTP (page de rendu + fichiers VRM) et WebSocket (données de tracking) sur 127.0.0.1 uniquement."""
    client_message = Signal(dict)
    clients_changed = Signal(int, int)  # nb clients app, nb clients OBS/autres

    def __init__(self, port: int):
        super().__init__()
        self.port = port
        self.httpd = _Server(("127.0.0.1", port),
                                         functools.partial(_Handler, directory=str(ROOT / "web")))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="http").start()
        self.ws = QWebSocketServer("Nova", QWebSocketServer.NonSecureMode, self)
        if not self.ws.listen(QHostAddress.LocalHost, port + 1):
            self.httpd.shutdown()
            self.httpd.server_close()
            raise OSError(f"Port {port + 1} déjà utilisé : {self.ws.errorString()}")
        self.ws.newConnection.connect(self._on_new)
        self.clients = {}       # socket -> rôle
        self.last = {}          # derniers messages par type (renvoyés aux nouveaux clients)

    @staticmethod
    def page_version() -> str:
        """Empreinte des fichiers de rendu (change à chaque mise à jour de Nova)."""
        h = hashlib.md5()
        for p in (ROOT / "web" / "avatar.html", ROOT / "web" / "libs" / "three-vrm.module.min.js"):
            try:
                h.update(p.read_bytes())
            except OSError:
                pass
        return h.hexdigest()[:12]

    def url(self, mode="obs"):
        return f"http://127.0.0.1:{self.port}/avatar.html?mode={mode}"

    def _on_new(self):
        sock = self.ws.nextPendingConnection()
        self.clients[sock] = "?"
        sock.textMessageReceived.connect(lambda txt, s=sock: self._on_text(s, txt))
        sock.disconnected.connect(lambda s=sock: self._on_gone(s))
        # version de la page de rendu : une page restée ouverte (ex. source OBS) se recharge seule après une mise à jour
        sock.sendTextMessage(json.dumps({"type": "version", "v": self.page_version()}))
        for t in ("scene", "load", "unload", "brb"):
            if t in self.last:
                sock.sendTextMessage(self.last[t])

    def _on_text(self, sock, txt):
        try:
            msg = json.loads(txt)
        except ValueError:
            return
        if msg.get("type") == "hello":
            self.clients[sock] = msg.get("role", "?")
            self._emit_clients()
        msg["role"] = self.clients.get(sock, "?")
        self.client_message.emit(msg)

    def _on_gone(self, sock):
        if self.clients.pop(sock, None) is None:
            return
        try:
            sock.deleteLater()
        except RuntimeError:
            pass
        self._emit_clients()

    def _emit_clients(self):
        app = sum(1 for r in self.clients.values() if r == "app")
        self.clients_changed.emit(app, len(self.clients) - app)

    def send(self, msg: dict):
        txt = json.dumps(msg, separators=(",", ":"))
        t = msg.get("type")
        if t in ("load", "unload"):
            self.last.pop("load", None)
            self.last.pop("unload", None)
        if t != "state":
            self.last[t] = txt
        for s in list(self.clients):
            s.sendTextMessage(txt)

    def close(self):
        socks, self.clients = list(self.clients), {}
        for s in socks:
            s.disconnected.disconnect()
            s.close()
        self.ws.close()
        self.httpd.shutdown()
        self.httpd.server_close()


# ---------------------------------------------------------------- microphone
def list_microphones() -> list[tuple[int, str]]:
    """Micros de l'API audio par défaut (évite les doublons MME/WASAPI/DirectSound)."""
    import sounddevice as sd
    try:
        default_api = sd.default.hostapi if isinstance(sd.default.hostapi, int) else 0
        return [(i, d["name"]) for i, d in enumerate(sd.query_devices())
                if d["max_input_channels"] > 0 and d["hostapi"] == default_api]
    except Exception as e:  # noqa: BLE001
        log.error("Liste des micros impossible : %s", e)
        return []


class Microphone:
    """Lit le son dans un thread audio ; process() calcule l'ouverture de bouche et la voyelle prononcée."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.stream = None
        self.level = 0.0
        self.mouth = 0.0
        self.meter = 0.0
        self.error = ""
        self._t = time.monotonic()
        self.fs = 48000.0
        self._buf = np.zeros(0, np.float32)
        self.vowels = {v: (1.0 if v == "aa" else 0.0) for v in VOWELS}
        self.current_vowel = ""
        self.classifier = VowelClassifier(cfg.get("vowels") or {})
        self._rec = None
        self.calib_result = None

    def start(self) -> bool:
        import sounddevice as sd
        self.stop()
        try:
            self.stream = sd.InputStream(device=self.cfg.get("device"), channels=1, samplerate=None,
                                         blocksize=0, callback=self._cb)
            self.stream.start()
            self.fs = float(self.stream.samplerate or 48000)
            self.error = ""
            return True
        except Exception as e:  # noqa: BLE001
            self.stream = None
            self.error = f"Micro indisponible : {e}"
            log.error(self.error)
            return False

    def _cb(self, data, frames, t, status):
        rms = float(np.sqrt(np.mean(np.square(data, dtype=np.float32))))
        self.level = max(rms, self.level * 0.85)  # garde les pics entre deux lectures
        self._buf = np.concatenate((self._buf, data[:, 0]))[-4096:]   # derniers ~85 ms pour l'analyse des voyelles

    def process(self) -> float:
        now = time.monotonic()
        dt, self._t = min(now - self._t, 0.1), now
        c = self.cfg
        lvl = self.level * 6.0 * c["sensitivity"]
        self.meter = min(1.0, lvl)  # niveau affiché (0..1)
        self.level *= 0.5
        target = min(1.0, max(0.0, (lvl - c["threshold"] * 6.0) * 2.0))
        speed = 25.0 if target > self.mouth else 6.0 + (1.0 - c["smoothing"]) * 14.0  # fermeture progressive
        self.mouth += (target - self.mouth) * min(1.0, speed * dt)
        if target > 0.05 and self.cfg.get("phonemes", True):
            self._analyse()
        if self._rec and now > self._rec["end"]:
            self._finish_calibration()
        return self.mouth

    def _analyse(self):
        n = int(0.035 * self.fs)
        x = self._buf[-n:]
        if len(x) < n:
            return
        env = envelope(x, self.fs)
        if env is None:
            return
        w = self.classifier.weights(env)
        for v in VOWELS:
            self.vowels[v] += (w[v] - self.vowels[v]) * 0.45
        self.current_vowel = max(self.vowels, key=self.vowels.get)
        if self._rec:
            self._rec["envs"].append(env)

    def visemes(self) -> dict:
        """Formes de bouche (a, i, ou, é, o) : voyelle reconnue x ouverture due au volume."""
        if not self.cfg.get("phonemes", True):
            return {"aa": self.mouth}
        sharp = {v: max(0.0, w) ** 1.5 for v, w in self.vowels.items()}
        total = sum(sharp.values()) or 1.0
        return {v: self.mouth * sharp[v] / total for v in VOWELS}

    def calibrate_vowel(self, vowel: str, seconds: float = 1.5):
        """Enregistre la voyelle prononcée pendant quelques secondes comme modèle personnel."""
        self._rec = {"v": vowel, "end": time.monotonic() + seconds, "envs": []}
        self.calib_result = None

    def _finish_calibration(self):
        rec, self._rec = self._rec, None
        if len(rec["envs"]) >= 8:
            mean = np.mean(np.array(rec["envs"]), axis=0)
            self.cfg.setdefault("vowels", {})[rec["v"]] = [round(float(x), 2) for x in mean]
            self.classifier.set_calibration(self.cfg["vowels"])
            self.calib_result = (rec["v"], True, len(rec["envs"]))
        else:
            self.calib_result = (rec["v"], False, len(rec["envs"]))

    def reset_calibration(self):
        self.cfg["vowels"] = {}
        self.classifier.set_calibration({})

    @property
    def active(self):
        return self.stream is not None

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:  # noqa: BLE001
                pass
        self.stream = None
        self.level = self.mouth = 0.0
