"""Réglages modifiables depuis l'interface (stockés dans la base, valeurs par défaut = onde.env)."""
from __future__ import annotations

import json
import os
import threading
import urllib.parse

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
    "pi_audio_device": ("", str),                  # sortie audio de mpv ("" = défaut du système)
    "auto_update": (True, bool),                   # mise à jour automatique au démarrage (si une source est choisie)
    "update_source": ("", str),                    # « pseudo/dépôt » GitHub ou https://…/onde.json
    # adresse publique d'Onde (ex. https://onde.exemple.fr) : seule base de confiance pour les liens
    # envoyés par mail (mot de passe oublié) ; "" = adresses connues seulement (réseau local, Tailscale)
    "public_url": (os.environ.get("ONDE_PUBLIC_URL", "").strip(), str),
}

_TRUE, _FALSE = ("true", "1"), ("false", "0")

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


def clean_url(value: str) -> str:
    """« https://hôte[:port][/chemin] » sans / final ; "" si vide. ValueError si ce n'est pas une adresse web."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parts = urllib.parse.urlsplit(value)
        parts.port  # (lève ValueError sur un port invalide)
    except ValueError:
        parts = None
    if (not parts or parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.query
            or parts.fragment or parts.username or any(c.isspace() for c in value)):
        raise ValueError("Adresse publique invalide : écris-la comme https://onde.exemple.fr")
    return f"{parts.scheme.lower()}://{parts.netloc}{parts.path.rstrip('/')}"


def public_url() -> str:
    """Adresse publique d'Onde (réglage public_url, sinon ONDE_PUBLIC_URL) ; "" si absente ou invalide."""
    try:
        return clean_url(str(get("public_url") or ""))
    except ValueError:
        return ""


def _parse(key: str, value):
    """Valeur reçue de l'interface → valeur du réglage (ValueError si elle n'a pas le bon type)."""
    if DEFAULTS[key][1] is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        text = value.strip().lower() if isinstance(value, str) else None
        if text in _TRUE or text in _FALSE:
            return text in _TRUE  # (bool("false") vaudrait True)
        raise ValueError(f"Réglage « {key} » : oui ou non attendu (true ou false).")
    if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int, float))):
        raise ValueError(f"Réglage « {key} » : texte attendu.")
    value = str(value or "")[:300]
    return clean_url(value) if key == "public_url" else value


def update(values: dict) -> dict[str, object]:
    """Enregistre les réglages reçus. Renvoie seulement ceux qui ont vraiment changé ; une valeur
    invalide lève ValueError et rien n'est enregistré."""
    parsed = {key: _parse(key, value) for key, value in values.items() if key in DEFAULTS}
    current = _load()
    changed = {}
    for key, value in parsed.items():
        if current.get(key) == value:
            continue
        db.x("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", f"setting:{key}", json.dumps(value))
        with _lock:
            current[key] = value
        changed[key] = value
    return changed
