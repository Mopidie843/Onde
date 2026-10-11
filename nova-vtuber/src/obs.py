"""Intégration OBS Studio, sans jamais toucher à vos propres collections de scènes ni à votre profil OBS.

- Lancement d'OBS tel quel (votre collection et votre profil habituels), avec la caméra virtuelle si demandé.
- Création d'une collection séparée « Nova Stream » : scènes Début / Gameplay / Pause / Fin (widgets StreamElements)
  + scène « avatar seul » pour Discord, avec l'avatar Nova placé et cadré pour chaque scène.
  Le son (bureau + micro) et la capture d'écran sont recopiés depuis votre collection habituelle (lecture seule)."""
import configparser
import copy
import json
import os
import subprocess
import uuid
from pathlib import Path

COLLECTION = "Nova Stream"
COLLECTION_FILE = "Nova_Stream.json"
CANDIDATES = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "obs-studio/bin/64bit/obs64.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "obs-studio/bin/64bit/obs64.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Steam/steamapps/common/OBS Studio/bin/64bit/obs64.exe",
]
# cadres du widget « Talk Scene » (thème Japan Ink), en pixels sur un canevas 1920x1080
GAME_FRAME = (67, 79, 1260, 711)
CAM_FRAME = (1373, 89, 487, 269)
SCENES = {"start": "🎬 Début", "gameplay": "🎮 Gameplay", "pause": "☕ Pause", "end": "👋 Fin", "discord": "🎥 Discord (avatar seul)"}
WIDGET_LABELS = {"start": "Début (Starting soon)", "gameplay": "Gameplay", "pause": "Pause (Be right back)", "end": "Fin (Stream ended)",
                 "chat": "Chat (Streamlabs / StreamElements)"}
CHAT_SIZE = (500, 660)
CHAT_POS = {"gameplay": (1368, 388), "start": (60, 300), "pause": (60, 300)}
# style du chat assorti au thème : cartes « papier », trait rouge, pseudo au pinceau, fond transparent
CHAT_CSS = """@import url('https://fonts.googleapis.com/css2?family=Permanent+Marker&family=Nunito:wght@700;800&display=swap');
html, body { background: rgba(0,0,0,0) !important; margin: 0; overflow: hidden; }
body { text-shadow: none !important; font-family: 'Nunito', sans-serif !important; font-size: 22px !important; line-height: 1.35em !important; }
#log { display: block !important; table-layout: auto !important; width: 100% !important; position: absolute !important;
  bottom: 0 !important; left: 0 !important; padding: 0 0 6px !important; }
#log > div { display: block !important; opacity: 1 !important; animation: novaIn .35s ease-out both !important;
  background: rgba(250, 245, 236, 0.93) !important; color: #2a1c1c !important; border-left: 6px solid #c0392b;
  border-radius: 10px; padding: 8px 14px !important; margin: 10px 8px !important; box-shadow: 0 4px 14px rgba(60, 20, 20, 0.28); }
#log .meta, #log .message { display: block !important; width: auto !important; max-width: none !important; text-align: left !important;
  overflow: visible !important; white-space: normal !important; text-overflow: clip !important; padding: 0 !important; }
#log .meta { margin-bottom: 2px; }
#log .name { font-family: 'Permanent Marker', cursive !important; color: #b3261e !important; font-size: 0.95em; letter-spacing: .5px; }
#log .message { color: #2a1c1c !important; word-wrap: break-word; }
#log .badges img { height: 0.9em; vertical-align: middle; margin-right: 4px; }
#log .message img { height: 1.3em; vertical-align: middle; }
@keyframes novaIn { from { opacity: 0; transform: translateX(-24px); } to { opacity: 1; transform: none; } }"""
SE_CSS = "body { background-color: rgba(0, 0, 0, 0); margin: 0px auto; overflow: hidden; }"


def find_obs() -> Path | None:
    return next((p for p in CANDIDATES if p.exists()), None)


def obs_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/NH"], capture_output=True,
                             text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return "obs64.exe" in out.lower()
    except OSError:
        return False


def obs_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "obs-studio"


def scenes_dir() -> Path:
    return obs_dir() / "basic" / "scenes"


def active_collection_file() -> str:
    """Fichier de la collection actuellement choisie dans OBS (lecture seule)."""
    for ini in (obs_dir() / "user.ini", obs_dir() / "global.ini"):
        if ini.exists():
            cp = configparser.ConfigParser(strict=False, interpolation=None)
            try:
                cp.read(ini, encoding="utf-8-sig")
                f = cp.get("Basic", "SceneCollectionFile", fallback="")
                if f:
                    return f if f.endswith(".json") else f + ".json"
            except configparser.Error:
                pass
    return ""


def user_collection() -> dict | None:
    """Votre collection habituelle (la collection active si ce n'est pas une collection Nova, sinon la plus récente)."""
    d = scenes_dir()
    if not d.exists():
        return None
    files = [p for p in d.glob("*.json") if not p.name.startswith("Nova_")]
    active = active_collection_file()
    files.sort(key=lambda p: (p.name != active, -p.stat().st_mtime))
    for p in files:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def _uid() -> str:
    return str(uuid.uuid4())


def _browser(name, url, w, h, audio=False, css=SE_CSS) -> dict:
    return {"name": name, "uuid": _uid(), "id": "browser_source", "versioned_id": "browser_source", "enabled": True,
            "mixers": 255 if audio else 0, "volume": 1.0, "muted": False,
            "settings": {"url": url, "width": w, "height": h, "css": css, "reroute_audio": audio,
                         "shutdown": False, "restart_when_active": False, "fps_custom": False}}


def _item(src, iid, x=0.0, y=0.0, scale=1.0, bounds=None, visible=True) -> dict:
    it = {"name": src["name"], "source_uuid": src["uuid"], "id": iid, "visible": visible, "locked": False, "rot": 0.0,
          "pos": {"x": float(x), "y": float(y)}, "scale": {"x": scale, "y": scale}, "align": 5,
          "bounds_type": 0, "bounds_align": 0, "bounds": {"x": 0.0, "y": 0.0},
          "crop_left": 0, "crop_top": 0, "crop_right": 0, "crop_bottom": 0, "scale_filter": "disable",
          "blend_method": "default", "blend_type": "normal", "group_item_backup": False, "private_settings": {}}
    if bounds:
        it["bounds_type"] = 2                     # ajuster à l'intérieur du cadre en gardant les proportions
        it["bounds_align"] = 0
        it["bounds"] = {"x": float(bounds[0]), "y": float(bounds[1])}
    return it


def _scene(name, items) -> dict:
    return {"name": name, "uuid": _uid(), "id": "scene", "versioned_id": "scene", "enabled": True,
            "settings": {"id_counter": len(items), "custom_size": False, "items": items}}


def avatar_url(port: int, **params) -> str:
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return f"http://127.0.0.1:{port}/avatar.html?mode=obs" + (f"&{q}" if q else "")


def build_stream_collection(port: int, widgets: dict) -> dict:
    """Collection « Nova Stream » complète (dict JSON au format OBS 28+)."""
    sources = []

    def add(src):
        sources.append(src)
        return src

    # avatars : une source par usage, chacune avec son cadrage (voir paramètres d'adresse de avatar.html)
    av_start = add(_browser("Nova - Avatar (Début)", avatar_url(port, dist=4.0, bg="transparent", particles="none",
                                                                 shadow=1, idle="wave", every=9, notext=1), 760, 900))
    av_pause = add(_browser("Nova - Avatar (Pause)", avatar_url(port, dist=4.0, bg="transparent", particles="none",
                                                                 shadow=1, sleep=1), 600, 900))
    av_end = add(_browser("Nova - Avatar (Fin)", avatar_url(port, dist=4.0, bg="transparent", particles="none",
                                                             shadow=1, idle="wave", every=6, notext=1), 760, 900))
    av_cam = add(_browser("Nova - Avatar (cadre caméra)", avatar_url(
        port, dist=1.45, bg="gradient", c1="f7efe6", c2="ecc9c3", particles="sakura", density=0.5, shadow=0),
        CAM_FRAME[2], CAM_FRAME[3]))
    av_full = add(_browser("Nova - Avatar (Discord)", avatar_url(port), 1920, 1080))
    se = {k: add(_browser(f"StreamElements - {WIDGET_LABELS[k]}", url, 1920, 1080, audio=True))
          for k, url in widgets.items() if url and k != "chat"}
    chat = add(_browser("Chat", widgets["chat"], *CHAT_SIZE, css=CHAT_CSS)) if widgets.get("chat") else None

    def chat_item(scene_key):
        if not chat:
            return []
        x, y = CHAT_POS[scene_key]
        return [_item(chat, nid(), x, y)]
    # capture du jeu / de l'écran : reprise de votre collection habituelle, sinon capture de jeu plein écran
    mine = user_collection() or {}
    captures = [copy.deepcopy(s) for s in mine.get("sources", [])
                if s.get("id") in ("monitor_capture", "game_capture", "window_capture")]
    for c in captures:
        c["uuid"] = _uid()
    if not captures:
        captures = [{"name": "Capture du jeu", "uuid": _uid(), "id": "game_capture", "versioned_id": "game_capture",
                     "enabled": True, "settings": {"capture_mode": "any_fullscreen"}}]
    for c in captures:
        add(c)

    scenes = []
    n = 0

    def nid():
        nonlocal n
        n += 1
        return n

    # 🎬 Début : widget plein écran + avatar debout à droite (coucou de temps en temps)
    items = ([_item(se["start"], nid())] if "start" in se else []) + chat_item("start") + [_item(av_start, nid(), 1220, 95)]
    scenes.append(_scene(SCENES["start"], items))
    # 🎮 Gameplay : capture dans le grand cadre, avatar dans le cadre caméra, widget par-dessus (bordures)
    gx, gy, gw, gh = GAME_FRAME
    cx, cy, _cw, _ch = CAM_FRAME
    items = [_item(c, nid(), gx, gy, bounds=(gw, gh), visible=(i == 0)) for i, c in enumerate(captures)]
    items.append(_item(av_cam, nid(), cx, cy))
    if "gameplay" in se:
        items.append(_item(se["gameplay"], nid()))
    items += chat_item("gameplay")
    scenes.append(_scene(SCENES["gameplay"], items))
    # ☕ Pause : widget + avatar endormi à droite
    items = ([_item(se["pause"], nid())] if "pause" in se else []) + chat_item("pause") + [_item(av_pause, nid(), 1300, 95)]
    scenes.append(_scene(SCENES["pause"], items))
    # 👋 Fin : widget + avatar à gauche (la pagode est à droite) qui dit au revoir
    items = ([_item(se["end"], nid())] if "end" in se else []) + [_item(av_end, nid(), 0, 95)]
    scenes.append(_scene(SCENES["end"], items))
    # 🎥 Discord : avatar seul, réglé depuis Nova (page Scènes)
    scenes.append(_scene(SCENES["discord"], [_item(av_full, nid())]))

    data = {"name": COLLECTION, "current_scene": SCENES["start"], "current_program_scene": SCENES["start"],
            "scene_order": [{"name": s["name"]} for s in scenes], "sources": scenes + sources,
            "groups": [], "transitions": [], "quick_transitions": [], "saved_projectors": [], "modules": {},
            "current_transition": "Fondu", "transition_duration": 300}
    # son : mêmes périphériques que votre collection habituelle (bureau + micro), actifs
    for key in ("DesktopAudioDevice1", "DesktopAudioDevice2", "AuxAudioDevice1", "AuxAudioDevice2", "AuxAudioDevice3"):
        if key in mine:
            dev = copy.deepcopy(mine[key])
            dev["uuid"] = _uid()
            dev["muted"], dev["volume"] = False, 1.0
            data[key] = dev
    if "DesktopAudioDevice1" not in data:
        data["DesktopAudioDevice1"] = {"name": "Audio du bureau", "uuid": _uid(), "id": "wasapi_output_capture",
                                       "versioned_id": "wasapi_output_capture", "settings": {"device_id": "default"},
                                       "mixers": 255, "volume": 1.0, "muted": False, "enabled": True}
    if "AuxAudioDevice1" not in data:
        data["AuxAudioDevice1"] = {"name": "Mic/Aux", "uuid": _uid(), "id": "wasapi_input_capture",
                                   "versioned_id": "wasapi_input_capture", "settings": {"device_id": "default"},
                                   "mixers": 255, "volume": 1.0, "muted": False, "enabled": True}
    return data


def preview_html(collection: dict) -> str:
    """Page d'aperçu des scènes (même disposition qu'OBS) : sources navigateur en iframes, captures en zone grisée."""
    by_uuid = {s["uuid"]: s for s in collection["sources"]}
    scenes = [s for s in collection["sources"] if s["id"] == "scene"]
    blocks = []
    for k, sc in enumerate(scenes):
        layers = []
        for it in sc["settings"]["items"]:
            src = by_uuid.get(it["source_uuid"])
            if not src or not it.get("visible", True):
                continue
            x, y = it["pos"]["x"], it["pos"]["y"]
            if src["id"] == "browser_source" and src["name"] == "Chat":
                w, h = src["settings"]["width"], src["settings"]["height"]
                cards = "".join(f'<div class="msg"><b>{n}</b>{m}</div>' for n, m in (
                    ("Sakura_Fan", "Salut ! Trop beau l'avatar 😍"), ("Raiden42", "!coeur"),
                    ("NekoChan", "gg pour hier soir 🔥")))
                layers.append(f'<div class="chat" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px">{cards}</div>')
            elif src["id"] == "browser_source":
                w, h = src["settings"]["width"], src["settings"]["height"]
                layers.append(f'<iframe src="{src["settings"]["url"]}" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px" '
                              f'scrolling="no"></iframe>')
            else:
                w, h = it["bounds"]["x"] or 1920, it["bounds"]["y"] or 1080
                layers.append(f'<div class="cap" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px">'
                              f'{src["name"]}<br><small>(votre jeu / écran)</small></div>')
        blocks.append(f'<div class="scene" id="s{k}">{"".join(layers)}</div>')
    tabs = "".join(f'<button onclick="show({k})">{sc["name"]}</button>' for k, sc in enumerate(scenes))
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Aperçu Nova Stream</title><style>
html,body{{margin:0;background:#0d0f1a;font-family:'Segoe UI',sans-serif;overflow:hidden}}
#bar{{position:fixed;top:0;left:0;right:0;z-index:9;display:flex;gap:6px;padding:6px;background:#12142a}}
#bar button{{background:#232750;color:#fff;border:1px solid #343a70;border-radius:6px;padding:6px 12px;cursor:pointer}}
#stage{{position:absolute;top:44px;left:0;width:1920px;height:1080px;transform-origin:0 0}}
.scene{{position:absolute;inset:0;display:none;background:#000}} .scene iframe{{position:absolute;border:0;background:transparent}}
.chat{{position:absolute;display:flex;flex-direction:column;justify-content:flex-end;padding-bottom:6px;box-sizing:border-box}}
.chat .msg{{background:rgba(250,245,236,.93);color:#2a1c1c;border-left:6px solid #c0392b;border-radius:10px;padding:8px 14px;
margin:10px 8px;box-shadow:0 4px 14px rgba(60,20,20,.28);font:700 22px Nunito,'Segoe UI',sans-serif}}
.chat .msg b{{display:block;color:#b3261e;font-family:'Permanent Marker','Segoe Print',cursive;font-size:.95em}}
.cap{{position:absolute;background:repeating-linear-gradient(45deg,#2b2f55,#2b2f55 20px,#262a4c 20px,#262a4c 40px);
color:#c8bfff;display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:36px;text-align:center}}
</style></head><body><div id="bar">{tabs}</div><div id="stage">{"".join(blocks)}</div><script>
function fit(){{const s=Math.min(innerWidth/1920,(innerHeight-44)/1080);document.getElementById('stage').style.transform='scale('+s+')';}}
function show(k){{document.querySelectorAll('.scene').forEach((e,i)=>e.style.display=i==k?'block':'none');history.replaceState(null,'','?s='+k);}}
addEventListener('resize',fit);fit();show(+(new URLSearchParams(location.search).get('s')||0));
</script></body></html>"""


def write_stream_collection(port: int, widgets: dict) -> Path:
    """Crée / met à jour uniquement le fichier « Nova Stream ». Refuse si OBS est ouvert sur cette collection
    (OBS réécrirait l'ancienne version en se fermant)."""
    if obs_running() and active_collection_file() == COLLECTION_FILE:
        raise RuntimeError("OBS est ouvert sur la collection « Nova Stream » : fermez OBS, puis recommencez.")
    d = scenes_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / COLLECTION_FILE
    if path.exists():
        path.with_suffix(".json.bak").write_bytes(path.read_bytes())
    path.write_text(json.dumps(build_stream_collection(port, widgets), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def launch(start_virtualcam: bool = True) -> str:
    """Lance OBS sans changer de collection ni de profil (vos réglages habituels sont conservés)."""
    exe = find_obs()
    if not exe:
        raise FileNotFoundError("OBS Studio n'est pas installé (https://obsproject.com).")
    if obs_running():
        return "OBS est déjà ouvert. Cliquez sur « Démarrer la caméra virtuelle » dans OBS si ce n'est pas déjà fait."
    args = [str(exe), "--disable-shutdown-check"]
    if start_virtualcam:
        args.append("--startvirtualcam")
    subprocess.Popen(args, cwd=str(exe.parent))
    return "OBS démarre avec votre collection habituelle et la caméra virtuelle."
