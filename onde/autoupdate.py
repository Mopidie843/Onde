"""Mise à jour automatique : à chaque démarrage (puis toutes les 12 h), en arrière-plan, Onde regarde
s'il existe une version plus récente et l'installe toute seule (code remplacé, données gardées,
redémarrage). L'appli Android suit : le serveur lui sert l'APK fourni avec la nouvelle version.

Source des mises à jour (Réglages → Mise à jour, ou ONDE_UPDATE_SOURCE dans onde.env) :
  * « pseudo/depot » : un dépôt GitHub. La dernière « release » doit contenir le zip d'Onde
    (onde-tout.zip ou onde-x.y.z.zip). GitHub fournit l'empreinte SHA-256 du fichier : vérifiée.
  * https://…/onde.json : un petit fichier {"version": "1.3.0", "url": "https://…/onde.zip",
    "sha256": "…", "notes": "…"} (l'empreinte est obligatoire).
Sans source, rien ne se passe (la mise à jour à la main, avec le zip, marche toujours).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time

from . import config, net, restart, settings, updater

log = logging.getLogger("onde.autoupdate")

FIRST_DELAY = 25.0          # après le démarrage (le temps qu'Onde soit prêt)
EVERY = 12 * 3600
_stop = threading.Event()
_lock = threading.Lock()
status: dict = {"checking": False, "last": None, "latest": None, "notes": "", "error": None, "installed": None}


def source() -> str:
    return (str(settings.get("update_source") or "").strip() or os.environ.get("ONDE_UPDATE_SOURCE", "").strip())


def vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4]) or (0,)


def newer(candidate: str, current: str = config.VERSION) -> bool:
    return vtuple(candidate) > vtuple(current)


def latest(src: str | None = None) -> dict | None:
    """{"version", "url", "sha256", "notes"} de la dernière version publiée (ou None sans source)."""
    src = (src if src is not None else source()).strip()
    if not src:
        return None
    if re.fullmatch(r"[\w.-]+/[\w.-]+", src):
        rel = net.get_json(f"https://api.github.com/repos/{src}/releases/latest",
                           headers={"Accept": "application/vnd.github+json"}, deadline=20)
        version = str(rel.get("tag_name") or rel.get("name") or "").lstrip("vV ")
        assets = rel.get("assets") or []
        zips = [a for a in assets if str(a.get("name", "")).lower().endswith(".zip") and "onde" in str(a.get("name", "")).lower()]
        if not zips:
            raise ValueError("La dernière release GitHub ne contient pas de zip d'Onde.")
        a = zips[0]
        sha = str(a.get("digest") or "").removeprefix("sha256:") or None
        if not sha:  # fichier « onde-tout.zip.sha256 » à côté
            side = next((x for x in assets if str(x.get("name", "")).lower() == str(a["name"]).lower() + ".sha256"), None)
            if side:
                sha = net.get_text(side["browser_download_url"], deadline=20).split()[0]
        return {"version": version, "url": a["browser_download_url"], "sha256": sha, "notes": str(rel.get("body") or "")[:2000]}
    if not src.startswith("https://"):
        raise ValueError("Source de mise à jour : « pseudo/dépôt » GitHub ou une adresse https://")
    m = net.get_json(src, deadline=20)
    if not isinstance(m, dict) or not m.get("version") or not str(m.get("url", "")).startswith("https://"):
        raise ValueError("Fichier de mise à jour invalide (version et url https:// obligatoires).")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(m.get("sha256") or "")):
        raise ValueError("Fichier de mise à jour sans empreinte sha256 : refusé (sécurité).")
    return {"version": str(m["version"]), "url": m["url"], "sha256": m["sha256"].lower(), "notes": str(m.get("notes") or "")[:2000]}


def check(install: bool = True) -> dict:
    """Regarde (et installe si `install`) la dernière version. Renvoie l'état."""
    if not _lock.acquire(blocking=False):
        return dict(status)
    try:
        status.update(checking=True, error=None, installed=None)
        info = latest()
        status.update(last=time.time(), latest=(info or {}).get("version"), notes=(info or {}).get("notes", ""))
        if not info or not newer(info["version"]):
            return dict(status)
        ok, why = updater.available()
        if not install or not ok:
            if not ok:
                status["error"] = why
            return dict(status)
        data = net.get_bytes(info["url"], limit=updater.MAX_BYTES, deadline=900, retries=2)
        if info.get("sha256"):
            got = hashlib.sha256(data).hexdigest()
            if got.lower() != info["sha256"].lower():
                raise ValueError("Empreinte du zip différente de celle annoncée : mise à jour refusée.")
        res = updater.apply(data)
        status["installed"] = res
        log.warning("mise à jour automatique : %s → %s", res["from"], res["to"])
        restart.request(2.0)
        return dict(status)
    except Exception as exc:
        status["error"] = str(exc)[:300]
        log.info("mise à jour automatique : %s", exc)
        return dict(status)
    finally:
        status["checking"] = False
        _lock.release()


def _loop() -> None:
    if _stop.wait(FIRST_DELAY):
        return
    while True:
        if settings.get("auto_update") and source():
            check(install=True)
        if _stop.wait(EVERY):
            return


def start_background() -> None:
    _stop.clear()
    threading.Thread(target=_loop, name="onde-autoupdate", daemon=True).start()


def stop() -> None:
    _stop.set()


# ------------------------------------------------------------------ l'APK fournie avec cette version
_apk_hash: dict = {}
def apk_path():
    for p in (config.ROOT / "android" / "Onde.apk", config.ROOT / "Onde.apk", config.DATA_DIR / "Onde.apk"):
        if p.is_file():
            return p
    return None


def apk_info() -> dict | None:
    """Version de l'APK livrée avec le serveur (lue dans android/version.json à côté)."""
    p = apk_path()
    if not p:
        return None
    meta = p.with_name("Onde.apk.json")
    code, name = 0, ""
    try:
        import json
        j = json.loads(meta.read_text(encoding="utf-8"))
        code, name = int(j.get("versionCode") or 0), str(j.get("versionName") or "")
    except (OSError, ValueError):
        pass
    st = p.stat()
    key = (str(p), st.st_mtime_ns, st.st_size)
    if _apk_hash.get("key") != key:
        _apk_hash.update(key=key, sha=hashlib.sha256(p.read_bytes()).hexdigest())
    h = _apk_hash["sha"]
    return {"versionCode": code, "versionName": name, "size": p.stat().st_size, "sha256": h, "url": "/api/app/apk"}
