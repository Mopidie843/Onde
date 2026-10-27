"""Chat Twitch en lecture seule (connexion anonyme, aucun mot de passe ni compte nécessaire).
Les messages, bits, abonnements et raids sont transmis à l'application qui déclenche les réactions configurées."""
import logging
import random
import re
import socket
import ssl
import threading
import time

from PySide6.QtCore import QObject, Signal

from . import health

log = logging.getLogger("nova.twitch")
HOST, PORT = "irc.chat.twitch.tv", 6697
SUB_IDS = {"sub", "resub", "subgift", "submysterygift", "giftpaidupgrade", "primepaidupgrade", "anonsubgift"}
# actions possibles : (identifiant, libellé)
ACTIONS = [("effect:hearts", "Effet : pluie de cœurs"), ("effect:confetti", "Effet : confettis"),
           ("effect:stars", "Effet : étoiles"), ("effect:sakura", "Effet : pétales de sakura"),
           ("expr:happy", "Expression : joie"), ("expr:angry", "Expression : colère"),
           ("expr:sad", "Expression : tristesse"), ("expr:surprised", "Expression : surprise"),
           ("expr:relaxed", "Expression : détendu"),
           ("gesture:wave", "Geste : coucou"), ("gesture:clap", "Geste : applaudir"), ("gesture:dance", "Geste : danse"),
           ("gesture:nod", "Geste : oui de la tête"), ("gesture:shake", "Geste : non de la tête"),
           ("gesture:bow", "Geste : révérence"), ("gesture:think", "Geste : réfléchir"),
           ("acc:catears", "Accessoire : oreilles de chat"), ("acc:headphones", "Accessoire : casque audio"),
           ("acc:halo", "Accessoire : auréole"), ("acc:horns", "Accessoire : cornes"),
           ("acc:glasses", "Accessoire : lunettes"), ("acc:crown", "Accessoire : couronne")]
KINDS = {"command": "Commande du chat", "bits": "Bits (cheer)", "sub": "Abonnement / sub offert", "raid": "Raid"}
_TAG_ESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}


def _unescape_tag(v: str) -> str:
    """Valeurs des tags IRCv3 : \\s = espace, \\: = point-virgule, \\\\ = barre oblique inverse…"""
    return re.sub(r"\\(.?)", lambda m: _TAG_ESCAPES.get(m.group(1), m.group(1)), v)


def clean_channel(name: str) -> str:
    name = (name or "").strip().lower().lstrip("#")
    name = name.rsplit("/", 1)[-1]          # accepte aussi l'adresse twitch.tv/nom
    return name if re.fullmatch(r"[a-z0-9_]{3,25}", name) else ""


def parse_line(line: str) -> dict:
    """Analyse une ligne IRC de Twitch -> événement {kind: chat|bits|sub|raid|irc, ...}."""
    tags = {}
    if line.startswith("@"):
        raw, _, line = line[1:].partition(" ")
        for kv in raw.split(";"):
            k, _, v = kv.partition("=")
            tags[k] = _unescape_tag(v)
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    head, _, trailing = line.partition(" :")
    parts = head.split()
    cmd = parts[0] if parts else ""
    user = tags.get("display-name") or prefix.split("!")[0]
    if cmd == "PRIVMSG":
        bits = int(tags.get("bits") or 0) if (tags.get("bits") or "0").isdigit() else 0
        if bits > 0:
            return {"kind": "bits", "user": user, "text": trailing, "amount": bits}
        return {"kind": "chat", "user": user, "text": trailing}
    if cmd == "USERNOTICE":
        mid = tags.get("msg-id", "")
        if mid in SUB_IDS:
            return {"kind": "sub", "user": user, "text": tags.get("system-msg", "") or trailing}
        if mid == "raid":
            viewers = tags.get("msg-param-viewerCount", "0")
            return {"kind": "raid", "user": tags.get("msg-param-displayName") or user,
                    "amount": int(viewers) if viewers.isdigit() else 0, "text": tags.get("system-msg", "")}
        return {"kind": "irc", "cmd": cmd, "text": trailing}
    return {"kind": "irc", "cmd": cmd, "text": trailing}


def match_rules(event: dict, rules: list) -> list[str]:
    """Actions déclenchées par un événement selon les règles configurées."""
    out = []
    for r in rules:
        kind, trig = r.get("kind"), (r.get("trigger") or "").strip().lower()
        if kind == "command" and event["kind"] == "chat" and trig:
            words = event.get("text", "").strip().lower().split()
            if words and words[0] == trig:
                out.append(r.get("action", ""))
        elif kind in ("bits", "sub", "raid") and event["kind"] == kind:
            out.append(r.get("action", ""))
    return [a for a in out if a]


class TwitchChat(QObject):
    event = Signal(dict)
    status = Signal(str, str)        # (connecting|connected|error|stopped, message lisible)

    def __init__(self):
        super().__init__()
        self.status.connect(lambda code, msg: health.report("twitch", state=code, message=msg))
        self._thread = None
        self._running = False
        self._stop = threading.Event()
        self._sock = None
        self.channel = ""

    def start(self, channel: str) -> bool:
        channel = clean_channel(channel)
        if not channel:
            self.status.emit("error", "Nom de chaîne invalide (lettres, chiffres et _ uniquement).")
            return False
        self.stop()
        self.channel = channel
        self._running = True
        # chaque connexion a son propre signal d'arrêt : une ancienne connexion encore en train de se fermer
        # ne peut plus repartir en parallèle de la nouvelle (messages et réactions en double)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(channel, self._stop), daemon=True, name="twitch")
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        self._stop.set()
        s = self._sock
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(2)
        self._thread = None

    @property
    def running(self):
        return self._running

    def _run(self, channel: str, stop: threading.Event):
        backoff = 2.0
        while not stop.is_set():
            self.status.emit("connecting", f"Connexion au chat de {channel}…")
            sock = None
            try:
                raw = socket.create_connection((HOST, PORT), timeout=10)
                sock = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
                sock.settimeout(1.0)
                if stop.is_set():
                    break
                self._sock = sock
                nick = f"justinfan{random.randint(10000, 99999)}"     # identifiant anonyme officiel (lecture seule)
                for cmd in ("CAP REQ :twitch.tv/tags twitch.tv/commands", f"NICK {nick}", f"JOIN #{channel}"):
                    sock.sendall((cmd + "\r\n").encode())
                buf, last, announced = b"", time.monotonic(), False
                while not stop.is_set():
                    try:
                        data = sock.recv(8192)
                    except socket.timeout:
                        if time.monotonic() - last > 400:
                            raise TimeoutError("plus de nouvelles du serveur") from None
                        continue
                    if not data:
                        raise ConnectionError("connexion fermée par Twitch")
                    last = time.monotonic()
                    buf += data
                    while b"\r\n" in buf:
                        line, buf = buf.split(b"\r\n", 1)
                        text = line.decode("utf-8", "replace")
                        if text.startswith("PING"):
                            sock.sendall(("PONG" + text[4:] + "\r\n").encode())
                            continue
                        ev = parse_line(text)
                        if ev["kind"] == "irc":
                            if ev["cmd"] in ("366", "ROOMSTATE") and not announced:
                                announced, backoff = True, 2.0
                                self.status.emit("connected", f"Connecté au chat de {channel} (lecture seule)")
                            elif ev["cmd"] == "RECONNECT":
                                raise ConnectionError("Twitch demande une reconnexion")
                            continue
                        self.event.emit(ev)
            except Exception as e:  # noqa: BLE001
                if not stop.is_set():
                    log.warning("Chat Twitch : %s", e)
                    self.status.emit("error", f"Chat Twitch déconnecté ({e}). Nouvelle tentative dans {int(backoff)} s…")
                    stop.wait(backoff)
                    backoff = min(backoff * 2, 60)
            finally:
                try:
                    if sock is not None:
                        sock.close()
                except OSError:
                    pass
                if self._sock is sock:
                    self._sock = None
        if self._stop is stop:          # pas de « arrêté » si une nouvelle connexion a déjà pris le relais
            self.status.emit("stopped", "Chat Twitch arrêté")
