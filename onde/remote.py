"""Accès à Onde depuis l'extérieur (Internet, 4G), via Tailscale.

Onde n'ouvre aucun port sur ta box. `./start.sh remote` installe Tailscale sur le Pi et le configure :

  privé   (par défaut)  seuls TES appareils (téléphone avec l'appli Tailscale, même compte) joignent Onde ;
                         adresse https://nom-du-pi.xxxx.ts.net, certificat valide, trafic chiffré ;
  public  (« Funnel »)   n'importe qui connaissant l'adresse peut ouvrir la page de connexion ; aucune appli
                         à installer sur le téléphone. Le mot de passe d'Onde (8 caractères au moins) est alors
                         obligatoire, et les essais ratés bloquent l'adresse du visiteur 15 minutes.

Ce module ne fait que LIRE l'état de Tailscale (pour les Réglages et l'appli Android).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time

from . import config

log = logging.getLogger("onde.remote")

_cache: dict = {"at": 0.0, "value": None}
_lock = threading.Lock()
TTL = 30.0


def _protected() -> bool:
    """Connexion obligatoire (profils avec mot de passe) ?"""
    from . import users
    return users.accounts_enabled()


def _run(args: list[str], timeout: float = 6.0):
    exe = shutil.which("tailscale")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r if r.returncode == 0 else None


def _json(args: list[str]) -> dict | None:
    r = _run(args)
    if not r:
        return None
    try:
        data = json.loads(r.stdout or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse(status: dict | None, serve: dict | None) -> dict:
    """État lisible à partir des sorties JSON de `tailscale status` et `tailscale serve status`."""
    out = {"installed": True, "running": False, "dns": None, "ip": None, "mode": "off", "url": None,
           "password": _protected()}
    if not status:
        return out
    me = status.get("Self") or {}
    out["running"] = status.get("BackendState") == "Running"
    dns = (me.get("DNSName") or "").rstrip(".")
    out["dns"] = dns or None
    ips = [i for i in (me.get("TailscaleIPs") or []) if ":" not in i]
    out["ip"] = ips[0] if ips else None
    if not out["running"]:
        return out
    serve = serve or {}
    web = serve.get("Web") or {}
    funnel = serve.get("AllowFunnel") or {}
    served = [h for h, cfg in web.items() if _points_here(cfg)]
    if served and dns:
        host = next((h for h in served if h.split(":")[0] == dns), served[0])
        out["mode"] = "public" if funnel.get(host) else "private"
        port = host.rsplit(":", 1)[-1] if ":" in host else "443"
        out["url"] = f"https://{host.split(':')[0]}" + ("" if port == "443" else f":{port}")
    elif out["ip"]:
        out["mode"] = "private"
        out["url"] = f"http://{out['ip']}:{config.PORT}"
    return out


def _points_here(cfg) -> bool:
    try:
        for h in (cfg.get("Handlers") or {}).values():
            if f":{config.PORT}" in str(h.get("Proxy") or ""):
                return True
    except AttributeError:
        pass
    return False


def status(force: bool = False) -> dict:
    """État de l'accès à distance (mis en cache 30 s : lance `tailscale`, qui peut être lent)."""
    with _lock:
        if not force and _cache["value"] is not None and time.time() - _cache["at"] < TTL:
            return dict(_cache["value"])
    if not shutil.which("tailscale"):
        value = {"installed": False, "running": False, "dns": None, "ip": None, "mode": "off", "url": None,
                 "password": _protected()}
    else:
        value = parse(_json(["status", "--json"]), _json(["serve", "status", "--json"]))
    value["warning"] = None
    if value["mode"] == "public" and not _protected():
        value["warning"] = "Onde est joignable par tout Internet sans mot de passe : ajoute-en un (Réglages)."
    with _lock:
        _cache.update(at=time.time(), value=value)
    return dict(value)


def describe(st: dict) -> list[str]:
    """Lignes lisibles pour `./start.sh remote status`."""
    if not st["installed"]:
        return ["Tailscale n'est pas installé.  →  ./start.sh remote"]
    if not st["running"]:
        return ["Tailscale est installé mais pas connecté.  →  ./start.sh remote"]
    lines = []
    if st["mode"] == "off":
        lines.append(f"Connecté à Tailscale ({st['dns'] or st['ip']}), mais Onde n'est pas partagé.  →  ./start.sh remote")
    elif st["mode"] == "private":
        lines.append(f"Accès à distance PRIVÉ (tes appareils Tailscale) : {st['url']}")
    else:
        lines.append(f"Accès à distance PUBLIC (tout Internet) : {st['url']}")
    if st.get("warning"):
        lines.append("! " + st["warning"])
    return lines


if __name__ == "__main__":
    import sys
    result = status(force=True)
    if "--json" in sys.argv:
        print(json.dumps(result))
    else:
        print("\n".join(describe(result)))
