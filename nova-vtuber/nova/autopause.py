"""Pause automatique : quand votre visage n'est plus détecté depuis « pause_delay » secondes (vous avez quitté
le bureau), OBS passe sur la scène Pause et l'avatar affiche l'écran pause. Dès votre retour, OBS revient sur la
scène où vous étiez (en général Gameplay) et l'écran pause disparaît.
Une scène changée à la main pendant la pause n'est jamais écrasée."""
import logging
import threading
import time

from . import health, hotkeys

log = logging.getLogger("nova.autopause")
PAUSE_WORDS = ("pause", "brb", "afk", "reviens", "be right back")
DEFAULTS = {"auto_pause": True, "pause_delay": 30.0, "back_delay": 1.5, "pause_scene": "", "avatar_brb": True}


class AutoPause:
    def __init__(self, link=None, trigger=hotkeys.trigger):
        self.link = link
        self.trigger = trigger
        self.cfg = dict(DEFAULTS)
        self.paused = False
        self._away = None
        self._here = None
        self._prev_scene = ""
        self._pause_scene = ""
        self._brb = False
        self._lock = threading.Lock()

    def configure(self, stream_cfg: dict | None) -> None:
        for k in DEFAULTS:
            if stream_cfg and k in stream_cfg:
                self.cfg[k] = stream_cfg[k]
        if not self.cfg["auto_pause"]:
            self._away = None

    def feed(self, face: bool, now: float | None = None) -> None:
        """Appelé à chaque image analysée : visage détecté ou non."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if face:
                self._away = None
                self._here = self._here or now
                if self.paused and now - self._here >= float(self.cfg["back_delay"]):
                    self.paused = False
                    self._run(self._resume)
            else:
                self._here = None
                if not self.cfg["auto_pause"]:
                    return
                self._away = self._away or now
                left = float(self.cfg["pause_delay"]) - (now - self._away)
                if not self.paused:
                    health.report("autopause", state="on", message="Actif" if left > 0 else "Pause en cours",
                                  away=round(now - self._away, 1))
                if not self.paused and left <= 0:
                    self.paused = True
                    self._run(self._pause)

    def tracking_stopped(self) -> None:
        """Suivi arrêté : on ne peut plus savoir si vous êtes là, le compte à rebours repart de zéro."""
        with self._lock:
            self._away = self._here = None

    def _run(self, fn) -> None:
        threading.Thread(target=fn, daemon=True, name="autopause").start()   # OBS peut mettre 1 à 3 s à répondre

    def _pause(self) -> None:
        log.info("Absence détectée : pause automatique")
        link = self.link
        self._prev_scene = self._pause_scene = ""
        if link is not None and link.connected:
            target = self.cfg["pause_scene"] or link.find_scene(PAUSE_WORDS)
            current = link.current_scene
            if target and current != target and link.set_scene(target):
                self._prev_scene, self._pause_scene = current, target
        if self.cfg["avatar_brb"]:
            self._brb = self.trigger("brb:toggle")
        health.report("autopause", state="paused",
                      message=f"En pause (scène « {self._pause_scene} »)" if self._pause_scene else "En pause")

    def _resume(self) -> None:
        log.info("Retour détecté : fin de la pause automatique")
        if self._brb:
            self.trigger("brb:toggle")
            self._brb = False
        link = self.link
        if (link is not None and link.connected and self._prev_scene
                and link.current_scene == self._pause_scene):       # scène changée à la main : on n'y touche pas
            link.set_scene(self._prev_scene)
        health.report("autopause", state="on", message=f"De retour (scène « {self._prev_scene} »)"
                      if self._prev_scene else "De retour", away=0)
        self._prev_scene = self._pause_scene = ""


AUTO = AutoPause()
