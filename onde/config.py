"""Configuration d'Onde.

Tout se règle par variables d'environnement (ou dans le fichier `onde.env`
à la racine du projet, lu automatiquement au démarrage).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Onde.exe (PyInstaller) : onde.env et ffmpeg.exe se placent à côté du programme
FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
# Outils Windows fournis avec Onde (ffmpeg.exe, yt-dlp.exe) : dossier « tools » à côté d'Onde.exe,
# ou « windows/tools » dans le dossier du projet ; un ffmpeg.exe posé à côté d'Onde.exe est trouvé aussi.
TOOLS_DIR = next((p for p in (ROOT / "tools", ROOT / "windows" / "tools") if p.is_dir()), None)
if FROZEN:
    os.environ["PATH"] = str(ROOT) + os.pathsep + os.environ.get("PATH", "")
if TOOLS_DIR and os.name == "nt":
    os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + str(TOOLS_DIR)  # (après le PATH : tes propres outils d'abord)


def _load_env_file() -> None:
    """Charge `onde.env` (format CLE=valeur) sans écraser l'environnement."""
    env_file = Path(os.environ.get("ONDE_ENV_FILE", ROOT / "onde.env"))
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_env_file()


def _default_music_dir() -> Path:
    home = Path.home()
    for name in ("Musique", "Music"):
        if (home / name).is_dir():
            return home / name
    return home / "Music"


def _path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else default


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "non", "off", "")


APP_NAME = "Onde"
VERSION = "1.3.4"

MUSIC_DIR = _path("ONDE_MUSIC_DIR", _default_music_dir())
EXTRA_DIRS = [
    Path(p).expanduser()
    for p in os.environ.get("ONDE_EXTRA_DIRS", "").split(":")
    if p.strip()
]
DATA_DIR = _path("ONDE_DATA_DIR", Path.home() / ".local" / "share" / "onde")

HOST = os.environ.get("ONDE_HOST", "0.0.0.0")
PORT = int(os.environ.get("ONDE_PORT", "8000"))
PASSWORD = os.environ.get("ONDE_PASSWORD", "")
HTTPS = _flag("ONDE_HTTPS", False)  # sert en HTTPS avec un certificat auto-signé (voir onde/tls.py)

# Point d'accès Wi-Fi d'Onde (« ./start.sh hotspot » : voiture, vacances, pas de box Internet)
HOTSPOT_SSID = os.environ.get("ONDE_HOTSPOT_SSID", "").strip() or "Onde-Music"
HOTSPOT_PASSWORD = os.environ.get("ONDE_HOTSPOT_PASSWORD", "").strip() or "ondemusic"  # 8 à 63 caractères
HOTSPOT_IFNAME = os.environ.get("ONDE_HOTSPOT_IFNAME", "").strip() or "wlan0"

SCAN_INTERVAL = int(os.environ.get("ONDE_SCAN_INTERVAL", "300"))  # secondes, 0 = off
ADMIN_NAME = os.environ.get("ONDE_ADMIN_NAME", "Mopidie").strip() or "Mopidie"  # pseudo de l'administrateur
# mot de passe oublié par mail (facultatif) : serveur d'envoi de mails
SMTP_HOST = os.environ.get("ONDE_SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("ONDE_SMTP_PORT", "587") or 587)
SMTP_USER = os.environ.get("ONDE_SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("ONDE_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("ONDE_SMTP_FROM", "").strip() or SMTP_USER
# espace disque par profil (Mo) pour les imports ; l'administrateur n'a pas de limite
QUOTA_MEMBER_MB = int(os.environ.get("ONDE_QUOTA_MB", "5000") or 5000)
QUOTA_LISTENER_MB = int(os.environ.get("ONDE_QUOTA_LISTENER_MB", "2000") or 2000)  # (titres demandés et acceptés)
ASK_AUTO_DELAY = float(os.environ.get("ONDE_ASK_DELAY", "300") or 300)  # demande de titre : décision auto après 5 min
STARTER_PACK = _flag("ONDE_STARTER_PACK", True)  # playlist de départ (onde/first_run.py)
AUTO_COVERS = _flag("ONDE_AUTO_COVERS", True)   # cherche les pochettes manquantes en ligne
EMBED_COVERS = _flag("ONDE_EMBED_COVERS", True)  # écrit la pochette dans le fichier audio
MAX_UPLOAD_MB = int(os.environ.get("ONDE_MAX_UPLOAD_MB", "1024"))
MPV_ARGS = os.environ.get("ONDE_MPV_ARGS", "")
PI_PLAYER = _flag("ONDE_PI_PLAYER", True)
TRANSCODE_CACHE_MB = int(os.environ.get("ONDE_TRANSCODE_CACHE_MB", "2048"))


def _imageio_ffmpeg() -> str | None:
    """ffmpeg fourni par le paquet imageio-ffmpeg (installé avec le calage des paroles par IA)."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = shutil.which("ffmpeg") or _imageio_ffmpeg()
ALIGN_MODEL = os.environ.get("ONDE_ALIGN_MODEL", "").strip()  # modèle Whisper du calage des paroles

DB_PATH = DATA_DIR / "onde.db"
COVERS_DIR = DATA_DIR / "covers"
THUMBS_DIR = DATA_DIR / "thumbs"
TRANSCODE_DIR = DATA_DIR / "transcode"
INCOMING_DIR = MUSIC_DIR / ".onde-incoming"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# formats reconnus -> type MIME (les formats que le navigateur ne sait pas lire sont convertis à la volée)
AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".wma": "audio/x-ms-wma",
    ".ape": "audio/x-ape",
    ".wv": "audio/x-wavpack",
    ".mpc": "audio/x-musepack",
}
IMPORT_TYPES = set(AUDIO_TYPES) | {".zip"}

SINGLES = "Singles"
UNKNOWN_ARTIST = "Artiste inconnu"


def ensure_dirs() -> None:
    for d in (MUSIC_DIR, DATA_DIR, COVERS_DIR, THUMBS_DIR, INCOMING_DIR, TRANSCODE_DIR):
        d.mkdir(parents=True, exist_ok=True)
