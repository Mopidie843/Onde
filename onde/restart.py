"""Redémarrage d'Onde depuis l'interface (par ex. après avoir activé ou désactivé le HTTPS).

On ne « tue » pas le process : on demande à uvicorn un arrêt propre (le même que Ctrl+C), ce qui
exécute l'arrêt normal d'Onde — lecteur mpv du mode enceinte arrêté, scans stoppés, base de données
refermée — puis `__main__` relance Onde une fois tout refermé :
  * Linux / Raspberry Pi : `os.execv` (même PID : systemd, `./start.sh` et le terminal ne voient rien) ;
  * Windows avec start.bat : sortie avec le code RESTART_CODE, et start.bat relance Onde ;
  * Windows sans start.bat : on relance Onde et on attend qu'il se termine.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable

from . import config

log = logging.getLogger("onde.restart")

RESTART_CODE = 3  # code de sortie compris par start.bat comme « relance-moi »

requested = False
_server = None
_hooks: list[Callable[[], None]] = []


def attach(server) -> None:
    """Appelé par `__main__` avec le serveur uvicorn en cours."""
    global _server
    _server = server


def available() -> bool:
    """Le redémarrage automatique est possible (Onde lancé via `python -m onde`)."""
    return _server is not None


def on_exit(fn: Callable[[], None]) -> Callable[[], None]:
    """Enregistre une fonction appelée juste avant l'arrêt (pour fermer les flux en direct)."""
    _hooks.append(fn)
    return fn


def run_exit_hooks() -> None:
    for fn in list(_hooks):
        try:
            fn()
        except Exception:  # un crochet défaillant ne doit pas bloquer l'arrêt
            log.exception("erreur pendant la préparation de l'arrêt")


def shutdown() -> None:
    """Arrêt propre d'Onde (menu « Quitter » de l'icône) : même chemin que Ctrl+C."""
    run_exit_hooks()
    if _server is not None:
        _server.should_exit = True


def request(delay: float = 0.6) -> bool:
    """Demande un redémarrage propre dans `delay` s (le temps que la réponse HTTP parte)."""
    if _server is None:
        return False

    def _go() -> None:
        global requested
        time.sleep(delay)
        requested = True
        run_exit_hooks()
        _server.should_exit = True

    threading.Thread(target=_go, daemon=True, name="onde-restart").start()
    return True


def tray_flag() -> str:
    from . import tray
    return tray.BACKGROUND_FLAG  # (relancé : pas besoin de rouvrir une page)


def relaunch() -> None:
    """Relance Onde (appelé par `__main__` une fois le serveur complètement arrêté)."""
    print("\n  ↻  Redémarrage d'Onde pour appliquer les nouveaux réglages…\n", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os.chdir(str(config.ROOT))
    if config.FROZEN:  # Onde.exe : le programme lui-même, qui se décompresse à nouveau
        args = [sys.executable, *sys.argv[1:]]
        os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    elif os.environ.get("ONDE_LAUNCHER") == "onde_app":  # Onde installé (pythonw onde_app.py)
        args = [sys.executable, str(config.ROOT / "onde_app.py"), tray_flag()]
        subprocess.Popen([a for a in args if a], close_fds=True)
        sys.exit(0)
    else:
        args = [sys.executable, "-m", "onde"]
    if os.name != "nt":
        os.execv(sys.executable, args)
    if config.FROZEN:  # Onde.exe : le nouveau programme prend le relais, celui-ci se termine
        subprocess.Popen(args, close_fds=True)
        sys.exit(0)
    if os.environ.get("ONDE_LAUNCHER") == "start.bat":
        sys.exit(RESTART_CODE)
    sys.exit(subprocess.call(args))
