"""Paramètres persistants : un fichier d'application + des profils JSON."""
import copy
import json
import logging
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PROFILES_DIR = ROOT / "profiles"
AVATARS_DIR = ROOT / "avatars"
ACCESSORIES_DIR = ROOT / "accessories"
BACKGROUNDS_DIR = ROOT / "backgrounds"
LOGS_DIR = ROOT / "logs"
for _d in (CONFIG_DIR, PROFILES_DIR, AVATARS_DIR, LOGS_DIR, ACCESSORIES_DIR, BACKGROUNDS_DIR):
    _d.mkdir(exist_ok=True)

APP_FILE = CONFIG_DIR / "app.json"
log = logging.getLogger("nova.settings")

DEFAULT_PROFILE = {
    "avatar": "Fumiriya - Mec anime (VRoid).vrm",
    "camera": {"index": 0, "resolution": "640x480", "fps_limit": 30, "mirror": True},
    "tracking": {
        "head": 1.0, "eyes": 1.0, "mouth": 1.0, "smoothing": 0.5, "deadzone": 0.5, "body": False,
        "eye_sync": True, "iris": True, "lean": 1.0, "blink": 1.0, "autostart": False,
        "calibration": {"yaw": 0.0, "pitch": 0.0, "roll": 0.0, "blink": 0.0, "mouth": 0.0,
                        "gx": 0.0, "gy": 0.0, "tx": 0.0, "ty": 0.0, "tz": 0.0,
                        "earL": 0.0, "earR": 0.0},
    },
    "audio": {"enabled": False, "device": None, "sensitivity": 1.0, "threshold": 0.02,
              "smoothing": 0.5, "mode": "both", "phonemes": True, "vowels": {}},
    "expressions": {
        "auto": {"happy": True, "surprised": True, "angry": False, "sad": False},
        "strength": {"happy": 1.0, "surprised": 1.0, "angry": 1.0, "sad": 1.0},
        "manual": "",
    },
    "scene": {"bg": "transparent", "color": "#00ff00", "distance": 2.2, "height": 1.35,
              "light": 1.0, "quality": 1.0, "bg2": "#2b1b5e", "image": "", "particles": "none", "density": 1.0,
              "light_color": "#ffffff", "rim": 0.5, "rim_color": "#8f7bff", "shadow": True,
              "brb_text": "Je reviens vite !"},
    "hotkeys": {"enabled": True, "bindings": []},
    "twitch": {
        "channel": "", "auto": False, "cooldown": 3.0, "duration": 5.0,
        "rules": [
            {"kind": "command", "trigger": "!coeur", "action": "effect:hearts"},
            {"kind": "command", "trigger": "!confettis", "action": "effect:confetti"},
            {"kind": "command", "trigger": "!joie", "action": "expr:happy"},
            {"kind": "command", "trigger": "!colere", "action": "expr:angry"},
            {"kind": "command", "trigger": "!surprise", "action": "expr:surprised"},
            {"kind": "command", "trigger": "!chat", "action": "acc:catears"},
            {"kind": "command", "trigger": "!couronne", "action": "acc:crown"},
            {"kind": "bits", "trigger": "", "action": "effect:confetti"},
            {"kind": "sub", "trigger": "", "action": "effect:stars"},
            {"kind": "raid", "trigger": "", "action": "effect:hearts"},
        ],
    },
}
def _default_hotkeys():
    from .hotkeys import DEFAULT_HOTKEYS
    return [dict(b) for b in DEFAULT_HOTKEYS]


DEFAULT_APP = {"profile": "Défaut", "port": 8765, "last_page": 0, "close_to_tray": False}


def _merge(base: dict, data: dict) -> dict:
    """Fusionne data dans une copie de base (garde les clés manquantes / ignore les types invalides)."""
    out = copy.deepcopy(base)
    for k, v in (data or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        elif k not in out or out[k] is None or v is None or isinstance(v, type(out[k])) or (
                isinstance(out[k], float) and isinstance(v, int)):
            out[k] = v
        else:
            log.warning("Valeur ignorée pour %s : %r", k, v)
    return out


def _read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return copy.deepcopy(default)
    try:
        return _merge(default, json.loads(path.read_text(encoding="utf-8")))
    except Exception as e:  # noqa: BLE001
        backup = path.with_suffix(".corrompu.json")
        shutil.copy(path, backup)
        log.error("Configuration illisible (%s). Copie de sauvegarde : %s. Valeurs par défaut utilisées.", e, backup.name)
        return copy.deepcopy(default)


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "Profil"


class Settings:
    def __init__(self):
        self.app = _read_json(APP_FILE, DEFAULT_APP)
        self.profile_name = self.app["profile"]
        self.data = self.load_profile(self.profile_name)

    # --- profils ---
    def profiles(self) -> list[str]:
        names = sorted(p.stem for p in PROFILES_DIR.glob("*.json") if not p.stem.endswith(".corrompu"))
        return names or [self.profile_name]

    def load_profile(self, name: str) -> dict:
        data = _read_json(PROFILES_DIR / f"{safe_name(name)}.json", DEFAULT_PROFILE)
        hk = data.setdefault("hotkeys", {"enabled": True, "bindings": []})
        if not hk.get("bindings") and not hk.get("customized"):
            hk["bindings"] = _default_hotkeys()
        if not hk.get("customized"):     # ancienne valeur par défaut (souvent déjà prise par un autre logiciel)
            for b in hk["bindings"]:
                if b.get("keys") == "Ctrl+Alt+M" and b.get("action") == "mic:toggle":
                    b["keys"] = "Ctrl+Alt+K"
        return data

    def switch_profile(self, name: str) -> None:
        self.save()
        self.profile_name = safe_name(name)
        self.data = self.load_profile(self.profile_name)
        self.save()

    def new_profile(self, name: str) -> None:
        self.save()
        self.profile_name = safe_name(name)
        self.data = copy.deepcopy(self.data)
        self.save()

    def delete_profile(self, name: str) -> None:
        p = PROFILES_DIR / f"{safe_name(name)}.json"
        if p.exists():
            p.unlink()

    def export_profile(self, dest: str) -> None:
        _write_json(Path(dest), self.data)

    def import_profile(self, src: str) -> str:
        data = json.loads(Path(src).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Ce fichier n'est pas un profil Nova.")
        self.save()
        self.profile_name = safe_name(Path(src).stem)
        self.data = _merge(DEFAULT_PROFILE, data)
        self.save()
        return self.profile_name

    def reset(self, section: str | None = None) -> None:
        if section:
            self.data[section] = copy.deepcopy(DEFAULT_PROFILE[section])
        else:
            avatar = self.data.get("avatar", "")
            self.data = copy.deepcopy(DEFAULT_PROFILE)
            self.data["avatar"] = avatar
        self.save()

    def save(self) -> None:
        try:
            self.app["profile"] = self.profile_name
            _write_json(PROFILES_DIR / f"{self.profile_name}.json", self.data)
            _write_json(APP_FILE, self.app)
        except OSError as e:
            log.error("Impossible d'enregistrer les paramètres : %s", e)
