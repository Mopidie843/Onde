"""Raccourcis clavier globaux (Windows) : actifs même quand Nova est en arrière-plan (jeu, Discord, OBS…)."""
import ctypes
import logging
import sys
import threading
from ctypes import wintypes

from PySide6.QtCore import QObject, Signal

log = logging.getLogger("nova.hotkeys")
WM_HOTKEY, WM_QUIT, WM_USER = 0x0312, 0x0012, 0x0400
MODS = {"ctrl": 0x2, "control": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8, "meta": 0x8}
MOD_NOREPEAT = 0x4000
NAMED_KEYS = {"space": 0x20, "espace": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
              "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23, "pgup": 0x21,
              "pageup": 0x21, "pgdown": 0x22, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
              "pause": 0x13, "backspace": 0x08}
for _i in range(1, 25):
    NAMED_KEYS[f"f{_i}"] = 0x6F + _i
for _i in range(10):
    NAMED_KEYS[f"num{_i}"] = 0x60 + _i

# actions possibles : (identifiant, libellé)
HOTKEY_ACTIONS = [
    ("manual:happy", "Expression joie (on/off)"), ("manual:angry", "Expression colère (on/off)"),
    ("manual:sad", "Expression tristesse (on/off)"), ("manual:surprised", "Expression surprise (on/off)"),
    ("manual:relaxed", "Expression détendu (on/off)"), ("manual:none", "Expression : aucune"),
    ("gesture:wave", "Geste : coucou"), ("gesture:clap", "Geste : applaudir"), ("gesture:nod", "Geste : oui de la tête"),
    ("gesture:shake", "Geste : non de la tête"), ("gesture:dance", "Geste : danse"), ("gesture:bow", "Geste : révérence"),
    ("gesture:think", "Geste : réfléchir"),
    ("effect:hearts", "Effet : cœurs"), ("effect:confetti", "Effet : confettis"), ("effect:stars", "Effet : étoiles"),
    ("effect:sakura", "Effet : sakura"),
    ("camera:close", "Caméra : gros plan"), ("camera:bust", "Caméra : buste"), ("camera:full", "Caméra : plein pied"),
    ("brb:toggle", "Écran pause (on/off)"), ("mic:toggle", "Micro (on/off)"), ("tracking:toggle", "Tracking (on/off)"),
]
DEFAULT_HOTKEYS = [
    {"keys": "Ctrl+Alt+1", "action": "manual:happy"}, {"keys": "Ctrl+Alt+2", "action": "manual:angry"},
    {"keys": "Ctrl+Alt+3", "action": "manual:sad"}, {"keys": "Ctrl+Alt+4", "action": "manual:surprised"},
    {"keys": "Ctrl+Alt+0", "action": "manual:none"},
    {"keys": "Ctrl+Alt+A", "action": "gesture:wave"}, {"keys": "Ctrl+Alt+S", "action": "gesture:clap"},
    {"keys": "Ctrl+Alt+D", "action": "gesture:dance"}, {"keys": "Ctrl+Alt+Y", "action": "gesture:nod"},
    {"keys": "Ctrl+Alt+N", "action": "gesture:shake"},
    {"keys": "Ctrl+Alt+Q", "action": "effect:hearts"}, {"keys": "Ctrl+Alt+W", "action": "effect:confetti"},
    {"keys": "Ctrl+Alt+F1", "action": "camera:close"}, {"keys": "Ctrl+Alt+F2", "action": "camera:bust"},
    {"keys": "Ctrl+Alt+F3", "action": "camera:full"},
    {"keys": "Ctrl+Alt+P", "action": "brb:toggle"}, {"keys": "Ctrl+Alt+K", "action": "mic:toggle"},
]


def parse_keys(text: str) -> tuple[int, int] | None:
    """« Ctrl+Alt+1 » -> (modificateurs Windows, code de touche) ; None si invalide."""
    parts = [p.strip().lower() for p in (text or "").replace(" ", "").split("+") if p.strip()]
    if not parts:
        return None
    mods, key = 0, parts[-1]
    for p in parts[:-1]:
        if p not in MODS:
            return None
        mods |= MODS[p]
    if key in NAMED_KEYS:
        vk = NAMED_KEYS[key]
    elif len(key) == 1 and (key.isalnum()):
        vk = ord(key.upper())
    else:
        return None
    if mods == 0 and not (0x70 <= vk <= 0x87):    # touche seule : réservé aux touches F (sinon on bloquerait la frappe)
        return None
    return mods, vk


class HotkeyManager(QObject):
    """Enregistre les raccourcis dans un fil dédié (boucle de messages Windows) et émet l'action déclenchée."""
    triggered = Signal(str)
    registered = Signal(dict)       # {raccourci: True si actif, False si déjà pris par un autre logiciel}

    def __init__(self):
        super().__init__()
        self._thread = None
        self._tid = 0
        self._ready = threading.Event()

    @staticmethod
    def available() -> bool:
        return sys.platform == "win32"

    def start(self, bindings: list[dict]):
        self.stop()
        if not self.available():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, args=(list(bindings),), daemon=True, name="hotkeys")
        self._thread.start()
        self._ready.wait(2)

    def stop(self):
        if self._thread and self._thread.is_alive() and self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(2)
        self._thread, self._tid = None, 0

    def simulate(self, hotkey_id: int):
        """Déclenche un raccourci comme si la touche avait été pressée (tests)."""
        if self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_HOTKEY, hotkey_id, 0)

    def _run(self, bindings):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._tid = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, WM_USER, WM_USER, 0)   # crée la file de messages du fil
        actions, status = {}, {}
        for i, b in enumerate(bindings, start=1):
            parsed = parse_keys(b.get("keys", ""))
            if not parsed or not b.get("action"):
                status[b.get("keys", "")] = False
                continue
            ok = bool(user32.RegisterHotKey(None, i, parsed[0] | MOD_NOREPEAT, parsed[1]))
            status[b["keys"]] = ok
            if ok:
                actions[i] = b["action"]
            else:
                log.warning("Raccourci %s déjà utilisé par un autre logiciel", b["keys"])
        self._ready.set()
        self.registered.emit(status)
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in actions:
                    self.triggered.emit(actions[msg.wParam])
        finally:
            for i in actions:
                user32.UnregisterHotKey(None, i)
