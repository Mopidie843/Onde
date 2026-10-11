"""Santé du live : chaque partie de Nova (suivi, micro, chat, OBS…) y note son état ; la page /sante le lit.
Rien ici n'est visible des viewers : la page n'est servie que sur ce PC."""
import copy
import threading
import time

_lock = threading.Lock()
_data: dict = {}


def report(part: str, **values) -> None:
    """Met à jour l'état d'une partie (ex. report("obs", state="ok", scene="Gameplay"))."""
    with _lock:
        d = _data.setdefault(part, {})
        d.update(values)
        d["t"] = time.time()


def get(part: str, key: str, default=None):
    with _lock:
        return _data.get(part, {}).get(key, default)


def snapshot() -> dict:
    """Copie de l'état, avec l'âge (secondes) de la dernière mise à jour de chaque partie."""
    now = time.time()
    with _lock:
        out = copy.deepcopy(_data)
    for d in out.values():
        d["age"] = round(now - d.pop("t", now), 1)
    return out


def reset() -> None:
    with _lock:
        _data.clear()
