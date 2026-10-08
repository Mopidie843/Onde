"""Mise à jour en un clic : l'administrateur envoie le nouveau zip d'Onde (onde-tout.zip) depuis les
Réglages ; Onde remplace son code, garde tes réglages (onde.env), ta musique et tes données, puis redémarre.
L'ancienne version est gardée dans les données d'Onde (ancienne-version.zip) au cas où.

Pas pour Onde.exe (programme figé) : là, remplace simplement Onde.exe.
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from . import config

log = logging.getLogger("onde.update")
MAX_BYTES = 80 * 1024 * 1024
KEEP = {"onde.env", ".venv", "build", "dist", "android/build"}  # jamais touchés
_SKIP = re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.git)(/|$)|\.pyc$")


def available() -> tuple[bool, str]:
    if config.FROZEN:
        return False, "Onde.exe : pour mettre à jour, remplace simplement Onde.exe par le nouveau."
    if not (config.ROOT / "onde" / "main.py").is_file():
        return False, "Dossier d'Onde introuvable."
    if not os.access(config.ROOT, os.W_OK):
        return False, f"Pas le droit d'écrire dans {config.ROOT}."
    return True, ""


def _members(z: zipfile.ZipFile) -> tuple[str, list[zipfile.ZipInfo]]:
    """Le dossier racine du zip (« onde/ » en général) et les fichiers à copier."""
    names = [i for i in z.infolist() if not i.is_dir()]
    roots = {n.filename.split("/", 1)[0] for n in names if "/" in n.filename}
    prefix = ""
    for r in roots:
        if any(n.filename == f"{r}/onde/main.py" for n in names):
            prefix = r + "/"
            break
    if not prefix and not any(n.filename == "onde/main.py" for n in names):
        raise ValueError("Ce zip n'est pas une version d'Onde (onde/main.py introuvable).")
    out = []
    for i in names:
        rel = i.filename[len(prefix):] if i.filename.startswith(prefix) else None
        if not rel or _SKIP.search(rel):
            continue
        parts = Path(rel).parts
        if rel.startswith("/") or ".." in parts or ":" in parts[0]:
            raise ValueError(f"Chemin dangereux dans le zip : {rel}")
        if parts[0] in KEEP or "/".join(parts[:2]) in KEEP:
            continue
        out.append((rel, i))
    return prefix, out


def version_of(z: zipfile.ZipFile, prefix: str) -> str:
    try:
        text = z.read(prefix + "onde/config.py").decode("utf-8", "replace")
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
        return m.group(1) if m else "?"
    except KeyError:
        return "?"


def apply(data: bytes) -> dict:
    ok, why = available()
    if not ok:
        raise ValueError(why)
    if len(data) > MAX_BYTES:
        raise ValueError("Zip trop gros")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("Fichier zip invalide") from exc
    prefix, members = _members(z)
    new_version = version_of(z, prefix)
    root = config.ROOT
    # 1) l'ancienne version, au cas où
    backup = config.DATA_DIR / "ancienne-version.zip"
    with zipfile.ZipFile(backup, "w", zipfile.ZIP_DEFLATED) as old:
        for rel, _ in members:
            p = root / rel
            if p.is_file():
                old.write(p, rel)
    # 2) le nouveau code, fichier par fichier (écriture atomique)
    written = 0
    for rel, info in members:
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".onde-maj-")
        with os.fdopen(fd, "wb") as fh, z.open(info) as src:
            shutil.copyfileobj(src, fh)
        mode = (info.external_attr >> 16) & 0o777
        if mode:
            os.chmod(tmp, mode)
        elif dest.suffix in (".sh",):
            os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
        written += 1
    log.warning("mise à jour : %s → %s (%d fichiers)", config.VERSION, new_version, written)
    return {"from": config.VERSION, "to": new_version, "files": written, "at": time.time()}
