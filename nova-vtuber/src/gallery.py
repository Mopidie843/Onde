"""Galerie de personnages VRM gratuits et libres de droits (CC0) issus du registre Open Source Avatars
(https://github.com/ToxSam/open-source-avatars). Seules les collections CC0 sont proposées."""
import json
import logging
import re
import urllib.request

from setup_assets import OFFICIAL_PACK, download, download_vrm_zip
from .settings import AVATARS_DIR, CONFIG_DIR, ROOT

log = logging.getLogger("nova.gallery")
BASE = "https://raw.githubusercontent.com/ToxSam/open-source-avatars/main/data/"
CACHE = CONFIG_DIR / "gallery_cache.json"
THUMBS = ROOT / "assets" / "thumbs"
VROID = "VRoid officiels (anime)"
# Modèles des versions alpha de VRoid Studio publiés en CC0 par VRoid (pixiv) — https://opengameart.org/content/vroid-studio-cc0-models
VROID_EXTRA = {
    "Avatar D": "avatarsample_d_0.zip", "Avatar D (sombre)": "avatarsample_d_darkness.zip",
    "Avatar F": "avatarsample_f.zip", "Avatar G": "avatarsample_g.zip",
    "Base masculine": "base_male.zip", "Base féminine": "base_female.zip", "Coiffure féminine": "hairsample_female.zip",
}


def vroid_items() -> list[dict]:
    items = [{"name": n.split(" - ")[0], "collection": VROID, "license": "VRoid (redistribution autorisée)",
              "author": "VRoid / pixiv", "url": u, "thumb": "", "id": "vroid-" + n, "file": n}
             for n, u in OFFICIAL_PACK.items()]
    items += [{"name": n, "collection": VROID, "license": "CC0", "author": "VRoid / pixiv",
               "url": "https://opengameart.org/sites/default/files/" + z, "thumb": "", "id": "vroid-" + z}
              for n, z in VROID_EXTRA.items()]
    return items


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_catalog(refresh: bool = False) -> list[dict]:
    if CACHE.exists() and not refresh:
        try:
            cached = json.loads(CACHE.read_text(encoding="utf-8"))
            return vroid_items() + [i for i in cached if i.get("collection") != VROID]
        except ValueError:
            pass
    items = vroid_items()
    for proj in _get_json(BASE + "projects.json"):
        if proj.get("license") != "CC0" or not proj.get("is_public", True):
            continue
        try:
            avatars = _get_json(BASE + proj["avatar_data_file"])
        except Exception as e:  # noqa: BLE001
            log.warning("Collection %s indisponible : %s", proj.get("name"), e)
            continue
        for a in avatars:
            if a.get("format", "VRM").upper() != "VRM" or not a.get("model_file_url") or a.get("is_draft"):
                continue
            items.append({"name": a.get("name", "?"), "collection": proj["name"], "license": "CC0",
                          "author": proj.get("creator_id", ""), "url": a["model_file_url"],
                          "thumb": a.get("thumbnail_url", ""), "id": a.get("id", a["model_file_url"])})
    CACHE.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return items


def thumb_path(item: dict):
    THUMBS.mkdir(parents=True, exist_ok=True)
    p = THUMBS / (re.sub(r"\W", "_", item["id"]) + ".img")
    if not p.exists() and item.get("thumb"):
        download(item["thumb"], p)
        if p.stat().st_size == 0:
            p.unlink()
            raise OSError("miniature vide")
    return p


def download_avatar(item: dict) -> str:
    name = item.get("file") or re.sub(r'[<>:"/\\|?*]', "_", f"{item['name']} ({item['collection']})") + ".vrm"
    dest = AVATARS_DIR / name
    if not dest.exists():
        if item["url"].lower().endswith(".zip"):
            download_vrm_zip(item["url"], dest)
        else:
            download(item["url"], dest)
    save_sidecar_thumb(item, dest)
    return name


def save_sidecar_thumb(item: dict, dest) -> None:
    """Copie la miniature de la galerie à côté du .vrm (pour les modèles sans portrait intégré)."""
    try:
        if item.get("thumb"):
            src = thumb_path(item)
            side = dest.with_suffix(".thumb.img")
            if src.exists() and not side.exists():
                side.write_bytes(src.read_bytes())
    except Exception as e:  # noqa: BLE001
        log.debug("Miniature locale : %s", e)
