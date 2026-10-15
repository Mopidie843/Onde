"""Conversion à la volée des formats que le navigateur ne sait pas lire (WMA, APE, OGG sur iPhone…).

Le résultat (FLAC, sans perte et très rapide à produire) est gardé en cache sur le disque :
la seconde écoute est instantanée et l'avance rapide fonctionne normalement.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections import defaultdict
from pathlib import Path

from . import config

log = logging.getLogger("onde.transcode")
_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
CONVERT_TIMEOUT = 900  # s


def available() -> bool:
    return bool(config.FFMPEG)


def cached(track_id: int, path: str, mtime: float) -> Path:
    """Chemin du fichier converti (le crée si besoin, bloquant)."""
    if not config.FFMPEG:
        raise RuntimeError("ffmpeg n'est pas installé")
    key = f"{track_id}_{int(mtime)}"
    out = config.TRANSCODE_DIR / f"{key}.flac"
    if out.exists():
        try:
            os.utime(out)  # LRU : le plus récemment écouté reste en cache
            return out
        except FileNotFoundError:
            pass  # supprimé entre-temps par le ménage du cache (prune) : on le refait
    with _locks[key]:
        if out.exists():
            return out
        config.TRANSCODE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f"{key}.part.flac")
        cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", path,
               "-map", "0:a:0", "-vn", "-c:a", "flac", "-compression_level", "0",
               "-sample_fmt", "s16", tmp]
        try:
            subprocess.run(cmd, check=True, timeout=CONVERT_TIMEOUT, capture_output=True)
        except subprocess.CalledProcessError as exc:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(exc.stderr.decode("utf-8", "replace")[-300:] or "conversion impossible")
        except subprocess.TimeoutExpired:  # (ffmpeg est arrêté par subprocess.run)
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"Conversion trop longue (plus de {CONVERT_TIMEOUT // 60} min) : "
                               "le fichier est peut-être abîmé, essaie de le réimporter.")
        os.replace(tmp, out)
    prune()
    return out


def prune() -> None:
    """Garde le cache sous la taille maximale (supprime les plus anciens)."""
    limit = config.TRANSCODE_CACHE_MB * 1024 * 1024
    try:
        files = sorted((p for p in config.TRANSCODE_DIR.glob("*.flac") if ".part." not in p.name),
                       key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    total = sum(p.stat().st_size for p in files)
    for p in files:
        if total <= limit:
            break
        try:
            size = p.stat().st_size
            p.unlink()
            total -= size
        except OSError:
            pass
