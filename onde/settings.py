"""Réglages modifiables depuis l'interface (stockés dans la base, valeurs par défaut = onde.env)."""
from __future__ import annotations

import json
import threading

from . import config, db

# clé -> (valeur par défaut, type)
DEFAULTS: dict[str, tuple[object, type]] = {
    "auto_covers": (config.AUTO_COVERS, bool),     # pochettes manquantes cherchées en ligne
    "embed_covers": (config.EMBED_COVERS, bool),   # pochettes écrites dans les fichiers
    "artist_images": (True, bool),                 # photos d'artistes (Deezer)
    "online_lyrics": (True, bool),                 # paroles en ligne (LRCLIB)
    "notify_lyrics": (True, bool),                 # notification : paroles synchronisées par l'IA
    "notify_import": (True, bool),                 # notification : musique ajoutée à la bibliothèque
    "notify_download": (True, bool),               # notification : musique téléchargée par nom / lien
    "auto_align": (True, bool),                    # paroles brutes calées sur la voix (IA, stable-whisper)
    "align_write_lrc": (True, bool),               # … et écrites en .lrc à côté du morceau
    "align_model": ("", str),                      # modèle Whisper ("" = ONDE_ALIGN_MODEL, sinon « base »)
    "align_language": ("auto", str),               # langue des paroles (auto = devinée)
    "starter_pack": (config.STARTER_PACK, bool),                  # playlist de départ téléchargée au 1er démarrage
    "shorten_titles": (True, bool),                 # titres raccourcis (sans émojis, « [AMV] », « 4K »…)
    "remove_unwanted": (True, bool),               # morceaux « Slowed » / « Reverb » mis à la corbeille
    "unwanted_words": ("slowed, reverb", str),     # mots qui déclenchent la mise à la corbeille
    "normalize": (True, bool),                     # volume égalisé entre les titres
    "analyze_loudness": (True, bool),              # analyse du volume en tâche de fond (ffmpeg)
    "autoplay": (True, bool),                      # titres similaires à la fin de la file (mode enceinte)
    "pi_audio_device": ("", str),
    "auto_update": (True, bool),                   # mise à jour automatique au démarrage (si une source est choisie)
    "update_source": ("", str),                    # « pseudo/dépôt » GitHub ou https://…/onde.json                  # sortie audio de mpv ("" = défaut du système)
}

_lock = threading.Lock()
_cache: dict[str, object] | None = None


def _load() -> dict[str, object]:
    global _cache
    with _lock:
        if _cache is None:
            values = {k: d for k, (d, _) in DEFAULTS.items()}
            for row in db.q("SELECT k, v FROM meta WHERE k LIKE 'setting:%'"):
                key = row["k"].split(":", 1)[1]
                if key in DEFAULTS:
                    try:
                        values[key] = DEFAULTS[key][1](json.loads(row["v"]))
                    except (ValueError, TypeError):
                        pass
            _cache = values
        return _cache


def get(key: str):
    return _load()[key]


def all_settings() -> dict[str, object]:
    return dict(_load())


def update(values: dict) -> dict[str, object]:
    current = _load()
    changed = {}
    for key, value in values.items():
        if key not in DEFAULTS:
            continue
        kind = DEFAULTS[key][1]
        if kind is bool:
            value = bool(value)
        else:
            value = str(value or "")[:300]
        db.x("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", f"setting:{key}", json.dumps(value))
        with _lock:
            current[key] = value
        changed[key] = value
    return changed
