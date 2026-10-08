"""Journal en mémoire pour la console développeur (F7) : les derniers messages de tout Onde
(paroles, IA, bibliothèque, téléchargements, lecteur du Pi, erreurs…), les requêtes HTTP reçues
de tous les appareils, et les erreurs remontées par les pages (téléphone, appli Android, iPhone).

Rien n'est écrit sur le disque : le journal repart à zéro au redémarrage (le journal système,
`journalctl -u onde`, garde le reste). Les mots de passe n'y apparaissent jamais.
"""
from __future__ import annotations

import collections
import logging
import os
import platform
import shutil
import socket
import sys
import threading
import time
import traceback

MAX_LOGS = 3000
MAX_HTTP = 600
started = time.time()

_lock = threading.Lock()
_logs: collections.deque = collections.deque(maxlen=MAX_LOGS)
_http: collections.deque = collections.deque(maxlen=MAX_HTTP)
_seq = [0]
_http_seq = [0]
_installed: list = []


def add(level: str, source: str, message: str, ts: float | None = None, **extra) -> int:
    with _lock:
        _seq[0] += 1
        item = {"id": _seq[0], "ts": ts or time.time(), "level": level.upper(), "source": source,
                "message": message[:8000]}
        if extra:
            item.update(extra)
        _logs.append(item)
        return _seq[0]


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        if record.exc_info:
            msg += "\n" + "".join(traceback.format_exception(*record.exc_info))[-6000:]
        add(record.levelname, record.name, msg, record.created, thread=record.threadName)


def install() -> None:
    """Branche le journal en mémoire sur tous les messages (une seule fois)."""
    if _installed:
        return
    h = RingHandler(level=logging.DEBUG)
    root = logging.getLogger()
    for other in root.handlers:  # la console système reste en INFO, même en mode détaillé
        if other.level == logging.NOTSET:
            other.setLevel(logging.INFO)
    root.addHandler(h)
    if root.getEffectiveLevel() > logging.INFO:  # (racine déjà configurée ailleurs : on garde INFO pour Onde)
        logging.getLogger("onde").setLevel(logging.INFO)
    _installed.append(h)


def attach_uvicorn() -> None:
    """uvicorn garde ses messages pour lui (propagate=False, configuré au démarrage du serveur) :
    à appeler une fois le serveur lancé, pour voir aussi ses erreurs (« Exception in ASGI application »)."""
    if not _installed:
        return
    uv = logging.getLogger("uvicorn")
    if not uv.propagate and _installed[0] not in uv.handlers:
        uv.addHandler(_installed[0])


def set_debug(on: bool) -> None:
    logging.getLogger("onde").setLevel(logging.DEBUG if on else logging.INFO)


def debug_on() -> bool:
    return logging.getLogger("onde").level == logging.DEBUG


def _page(items: list, after: int | None, first: int, limit: int) -> tuple[list, bool]:
    if after is None:
        return items[-limit:], True
    reset = bool(items) and after < first - 1  # des messages ont été perdus (journal plein)
    out = [i for i in items if i["id"] > after]
    return out[-limit:], reset


def logs(after: int | None = None, limit: int = 1000) -> dict:
    with _lock:
        items = list(_logs)
        last = _seq[0]
    if after is not None and after > last:  # Onde a redémarré : on renvoie tout
        after = None
    first = items[0]["id"] if items else last + 1
    out, reset = _page(items, after, first, limit)
    return {"items": out, "last": last, "reset": reset}


SKIP_HTTP = ("/api/dev/", "/api/notifications", "/api/pi/events", "/api/sync/events")


def http(method: str, path: str, query: str, status: int, ms: float, client: str, size: int | None) -> None:
    """Une requête reçue (les flux et les attentes longues de la console ne sont pas notés)."""
    if path.startswith(SKIP_HTTP) or not path.startswith("/api/"):
        return
    if (path.startswith("/api/cover/") or path.endswith("/stream")) and status < 400:
        return  # pochettes et flux audio : trop nombreux, notés seulement en cas d'erreur
    with _lock:
        _http_seq[0] += 1
        _http.append({"id": _http_seq[0], "ts": time.time(), "method": method, "path": path,
                      "query": query[:300], "status": status, "ms": round(ms, 1), "client": client, "size": size})


def http_log(after: int | None = None, limit: int = 600) -> dict:
    with _lock:
        items = list(_http)
        last = _http_seq[0]
    if after is not None and after > last:
        after = None
    first = items[0]["id"] if items else last + 1
    out, reset = _page(items, after, first, limit)
    return {"items": out, "last": last, "reset": reset}


# ---------------------------------------------------------------- erreurs des pages
CLIENT_PER_MINUTE = 60
_client_counts: dict[str, list] = {}


def client(ip: str, device: str, items: list, page_id: str = "") -> int:
    """Erreurs et avertissements envoyés par une page (téléphone, appli…). Au plus 60 par minute
    et par adresse. Renvoie le nombre de messages gardés."""
    now = time.time()
    with _lock:
        count = _client_counts.get(ip)
        if not count or now - count[1] > 60:
            count = _client_counts[ip] = [0, now]
        if len(_client_counts) > 200:
            for k in [k for k, v in _client_counts.items() if now - v[1] > 60]:
                _client_counts.pop(k, None)
    kept = 0
    for it in items[:50]:
        if not isinstance(it, dict):
            continue
        with _lock:
            if count[0] >= CLIENT_PER_MINUTE:
                break
            count[0] += 1
        level = str(it.get("level") or "error").upper()
        level = level if level in ("DEBUG", "INFO", "WARNING", "ERROR") else ("WARNING" if level == "WARN" else "ERROR")
        where = str(it.get("page") or "")[:120]
        msg = str(it.get("message") or "")[:4000]
        add(level, "page", f"[{str(device)[:60]} · {ip}{' · ' + where if where else ''}] {msg}", pageId=str(page_id)[:16])
        kept += 1
    return kept


# ---------------------------------------------------------------- informations système
def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _meminfo() -> dict:
    out = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            out[k] = int(v.split()[0]) * 1024
    rss = 0
    for line in _read("/proc/self/status").splitlines():
        if line.startswith("VmRSS:"):
            rss = int(line.split()[1]) * 1024
    return {"total": out.get("MemTotal"), "available": out.get("MemAvailable"),
            "swapTotal": out.get("SwapTotal"), "swapFree": out.get("SwapFree"), "onde": rss or None}


def _disk(path) -> dict | None:
    try:
        u = shutil.disk_usage(path)
        return {"path": str(path), "total": u.total, "free": u.free}
    except OSError:
        return None


def system() -> dict:
    temp = _read("/sys/class/thermal/thermal_zone0/temp").strip()
    try:
        load = os.getloadavg()
    except (AttributeError, OSError):
        load = None
    up = _read("/proc/uptime").split()
    return {
        "python": sys.version.split()[0], "platform": platform.platform(), "machine": platform.machine(),
        "model": _read("/proc/device-tree/model").strip("\x00\n ") or None,
        "hostname": socket.gethostname(), "pid": os.getpid(),
        "uptime": time.time() - started, "systemUptime": float(up[0]) if up else None,
        "cpus": os.cpu_count(), "load": load, "temperature": int(temp) / 1000 if temp.isdigit() else None,
        "memory": _meminfo(), "threads": sorted(t.name for t in threading.enumerate()),
    }


def disks(*paths) -> list:
    return [d for d in (_disk(p) for p in paths) if d]
