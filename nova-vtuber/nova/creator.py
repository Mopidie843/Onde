"""Créateur de personnages : personnages Nova (.nova.json = modèle VRM de base + personnalisation)
et export en véritable fichier VRM (couleurs et taille de tête intégrées)."""
import colorsys
import copy
import json
import logging
import re
import struct
from pathlib import Path

import cv2
import numpy as np

from .settings import AVATARS_DIR, safe_name

log = logging.getLogger("nova.creator")
CHAR_SUFFIX = ".nova.json"
# identifiant, nom affiché, couleur par défaut
ACCESSORIES = [
    ("catears", "Oreilles de chat", "#f4f1f8"),
    ("headphones", "Casque audio", "#2a2b3d"),
    ("halo", "Auréole", "#ffe27a"),
    ("horns", "Cornes", "#3b2547"),
    ("glasses", "Lunettes", "#1f1f24"),
    ("crown", "Couronne", "#f5c542"),
]
# mêmes règles que web/avatar.html (PART_RULES) : nom de matériau -> partie du personnage
PART_RULES = [
    ("_skip", "", r"mouth|eyewhite|eye_white|highlight|eyeextra|tooth|teeth|tongue"),
    ("brows", "Sourcils et cils", r"brow|lash|eyeline"),
    ("iris", "Yeux", r"iris|pupil"),
    ("hair", "Cheveux", r"hair"),
    ("skin", "Peau", r"skin|face|body"),
    ("top", "Haut", r"tops?_|shirt|jacket|hoodie|coat|onepiece|dress|vest|uniform"),
    ("bottom", "Bas", r"bottoms?|pants|skirt|shorts|trouser"),
    ("shoes", "Chaussures", r"shoe|boot|sock"),
    ("acc", "Accessoires", r"accessor|ribbon|hat|cap|glass|earring|necklace"),
    ("cloth", "Vêtements", r"cloth|wear|outfit"),
]


# ---------------------------------------------------------------- personnages
def new_character(base: str) -> dict:
    return {"version": 1, "name": "", "base": base, "colors": {},
            "proportions": {"height": 1.0, "head": 1.0},
            "accessories": {a: {"on": False, "color": c} for a, _, c in ACCESSORIES},
            "custom_acc": []}      # objets 3D importés : {file, bone, x, y, z, rx, ry, rz, scale}


def is_character(name: str) -> bool:
    return name.endswith(CHAR_SUFFIX)


def display_name(name: str) -> str:
    return name[: -len(CHAR_SUFFIX)] if is_character(name) else Path(name).stem


def list_avatar_files() -> list[str]:
    files = [p.name for p in AVATARS_DIR.glob("*.vrm")] + [p.name for p in AVATARS_DIR.glob("*" + CHAR_SUFFIX)]
    return sorted(files, key=lambda n: (not is_character(n), display_name(n).lower()))


def load_character(name: str) -> dict:
    data = json.loads((AVATARS_DIR / name).read_text(encoding="utf-8"))
    cfg = new_character(data.get("base", ""))
    for k in ("name", "base", "colors"):
        if k in data:
            cfg[k] = data[k]
    cfg["custom_acc"] = [a for a in data.get("custom_acc", []) if isinstance(a, dict) and a.get("file")]
    cfg["proportions"].update(data.get("proportions", {}))
    for a, v in (data.get("accessories") or {}).items():
        if a in cfg["accessories"] and isinstance(v, dict):
            cfg["accessories"][a].update(v)
    return cfg


def save_character(cfg: dict) -> str:
    name = safe_name(cfg.get("name") or "Mon personnage")
    fname = name + CHAR_SUFFIX
    (AVATARS_DIR / fname).write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return fname


def resolve(name: str) -> tuple[str, dict | None]:
    """Nom d'avatar -> (fichier VRM de base, personnalisation ou None)."""
    if is_character(name):
        cfg = load_character(name)
        return cfg["base"], cfg
    return name, None


# ---------------------------------------------------------------- lecture / écriture GLB
def read_glb(path: Path) -> tuple[dict, bytearray]:
    data = path.read_bytes()
    magic, _ver, _length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        raise ValueError("fichier glTF binaire attendu")
    off, js, bin_ = 12, None, bytearray()
    while off < len(data):
        clen, ctype = struct.unpack_from("<II", data, off)
        chunk = data[off + 8: off + 8 + clen]
        if ctype == 0x4E4F534A:
            js = json.loads(chunk.decode("utf-8"))
        elif ctype == 0x004E4942:
            bin_ = bytearray(chunk)
        off += 8 + clen
    if js is None:
        raise ValueError("GLB sans JSON")
    return js, bin_


def write_glb(js: dict, bin_: bytes, path: Path) -> None:
    jb = json.dumps(js, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    jb += b" " * (-len(jb) % 4)
    bb = bytes(bin_) + b"\0" * (-len(bin_) % 4)
    total = 12 + 8 + len(jb) + 8 + len(bb)
    with open(path, "wb") as f:
        f.write(struct.pack("<4sII", b"glTF", 2, total))
        f.write(struct.pack("<II", len(jb), 0x4E4F534A) + jb)
        f.write(struct.pack("<II", len(bb), 0x004E4942) + bb)


def _image_bytes(js, bin_, img_idx) -> bytes:
    bv = js["bufferViews"][js["images"][img_idx]["bufferView"]]
    o = bv.get("byteOffset", 0)
    return bytes(bin_[o: o + bv["byteLength"]])


def _add_image(js, bin_, png: bytes, sampler=None) -> int:
    """Ajoute une image PNG (et sa texture) au fichier ; retourne l'index de texture."""
    bin_ += b"\0" * (-len(bin_) % 4)
    js["bufferViews"].append({"buffer": 0, "byteOffset": len(bin_), "byteLength": len(png)})
    bin_ += png
    js["images"].append({"bufferView": len(js["bufferViews"]) - 1, "mimeType": "image/png"})
    tex = {"source": len(js["images"]) - 1}
    if sampler is not None:
        tex["sampler"] = sampler
    js["textures"].append(tex)
    js["buffers"][0]["byteLength"] = len(bin_)
    return len(js["textures"]) - 1


# ---------------------------------------------------------------- couleurs (identique au shader du rendu)
def srgb_to_lin(x):
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def lin_to_srgb(x):
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1 / 2.4) - 0.055)


def hsv_shift_linear(lin: np.ndarray, h: float, s: float, v: float) -> np.ndarray:
    """Même calcul que novaHSV() dans avatar.html : couleur linéaire -> couleur linéaire décalée."""
    g = np.power(np.clip(lin, 0, None), 1 / 2.2).astype(np.float32)
    hsv = cv2.cvtColor(g.reshape(-1, 1, 3), cv2.COLOR_RGB2HSV).reshape(g.shape)
    hsv[..., 0] = (hsv[..., 0] + h) % 360.0
    hsv[..., 1] = np.clip(hsv[..., 1] * s, 0, 1)
    hsv[..., 2] = np.clip(hsv[..., 2] * v, 0, 1)
    rgb = cv2.cvtColor(hsv.reshape(-1, 1, 3), cv2.COLOR_HSV2RGB).reshape(g.shape)
    return np.power(np.clip(rgb, 0, 1), 2.2)


def recolor_texture(png: bytes, factor_lin, h, s, v) -> bytes:
    arr = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED)
    if arr is None:
        raise ValueError("texture illisible")
    if arr.ndim == 2:
        arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    alpha = arr[..., 3:4] if arr.shape[2] == 4 else None
    rgb = arr[..., :3][..., ::-1].astype(np.float32) / 255.0
    lin = srgb_to_lin(rgb) * np.asarray(factor_lin[:3], np.float32)
    out = lin_to_srgb(hsv_shift_linear(lin, h, s, v))
    out8 = np.clip(out * 255 + 0.5, 0, 255).astype(np.uint8)[..., ::-1]
    if alpha is not None:
        out8 = np.concatenate([out8, alpha], axis=2)
    ok, enc = cv2.imencode(".png", out8)
    if not ok:
        raise ValueError("encodage PNG impossible")
    return enc.tobytes()


def texture_avg_srgb(png: bytes) -> np.ndarray:
    """Couleur moyenne (sRGB 0..1) des pixels opaques, comme averageColor() du rendu (vignette 24x24)."""
    arr = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED)
    if arr is None:
        return np.ones(3)
    if arr.ndim == 2:
        arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    small = cv2.resize(arr, (24, 24), interpolation=cv2.INTER_AREA)
    rgb = small[..., :3][..., ::-1].reshape(-1, 3).astype(np.float64) / 255
    if small.shape[2] == 4:
        rgb = rgb[small[..., 3].reshape(-1) > 128]
    return rgb.mean(axis=0) if len(rgb) else np.ones(3)


def material_shift(avg_srgb, c: dict) -> tuple[float, float, float]:
    """Décalage final d'un matériau : réglages fins + éventuelle couleur cible (identique à materialShift() en JS)."""
    h, s, v = float(c.get("h", 0)), float(c.get("s", 1)), float(c.get("v", 1))
    target = c.get("target")
    if target and avg_srgb is not None:
        ah, as_, av = colorsys.rgb_to_hsv(*[float(x) for x in avg_srgb[:3]])
        t = target.lstrip("#")
        th, ts, tv = colorsys.rgb_to_hsv(*(int(t[i:i + 2], 16) / 255 for i in (0, 2, 4)))
        h += ((th - ah) * 360 + 540) % 360 - 180
        s *= min(3.0, ts / max(as_, 0.05))
        v *= min(2.5, max(0.2, tv / max(av, 0.05)))
    return h, s, v


def part_of(material_name: str, extra: list) -> str:
    for pid, _label, rx in PART_RULES:
        if re.search(rx, material_name or "", re.I):
            return pid
    name = re.sub(r"\s*\((instance|outline)\)", "", material_name or "", flags=re.I)
    name = re.sub(r"^N\d+_\d+_\d+_", "", name)
    name = re.sub(r"_\d+_[A-Z]+$", "", name).strip()
    if name not in extra and len(extra) < 10:
        extra.append(name)
    return "m:" + name if name in extra else "other"


# ---------------------------------------------------------------- export VRM
def export_vrm(cfg: dict, dest: Path, thumbnail: Path | None = None) -> list[str]:
    """Crée un vrai fichier VRM à partir du modèle de base et de la personnalisation.
    Retourne la liste des avertissements (éléments non intégrables)."""
    base = AVATARS_DIR / cfg["base"]
    js, bin_ = read_glb(base)
    js = copy.deepcopy(js)
    warnings = []
    ext = js.get("extensions", {})
    vrm0 = ext.get("VRM")
    mprops = {m.get("name"): m for m in (vrm0 or {}).get("materialProperties", [])}
    colors = cfg.get("colors") or {}
    extra: list = []
    cache: dict = {}

    def baked(tex_idx, factor, hsv):
        key = (tex_idx, tuple(round(f, 5) for f in factor[:3]), hsv)
        if key not in cache:
            src = js["textures"][tex_idx]
            png = _image_bytes(js, bin_, src["source"])
            cache[key] = _add_image(js, bin_, recolor_texture(png, factor, *hsv), src.get("sampler"))
        return cache[key]

    def shift_factor(factor_lin):
        out = hsv_shift_linear(np.asarray([[factor_lin[:3]]], np.float32), *hsv)[0, 0]
        return [float(x) for x in out] + list(factor_lin[3:])

    for mat in js.get("materials", []):
        pid = part_of(mat.get("name", ""), extra)
        c = colors.get(pid)
        if pid == "_skip" or not c:
            continue
        pbr = mat.setdefault("pbrMetallicRoughness", {})
        mp = mprops.get(mat.get("name"))
        # couleur moyenne d'origine du matériau (texture x facteur, en sRGB) pour la couleur cible
        avg = None
        if c.get("target"):
            if mp is not None:
                tex = mp.get("textureProperties", {}).get("_MainTex")
                fac = np.asarray(mp.get("vectorProperties", {}).get("_Color", [1, 1, 1, 1])[:3], np.float64)
            else:
                tex = pbr.get("baseColorTexture", {}).get("index")
                fac = lin_to_srgb(np.asarray(pbr.get("baseColorFactor", [1, 1, 1, 1])[:3], np.float64))
            base_avg = texture_avg_srgb(_image_bytes(js, bin_, js["textures"][tex]["source"])) if tex is not None else np.ones(3)
            avg = base_avg * fac
        hsv = material_shift(avg, c)
        if hsv == (0.0, 1.0, 1.0):
            continue
        if mp is not None:                       # VRM 0.x : MToon décrit dans materialProperties (couleurs en sRGB)
            vp, tp = mp.setdefault("vectorProperties", {}), mp.setdefault("textureProperties", {})
            for col_key, tex_key in (("_Color", "_MainTex"), ("_ShadeColor", "_ShadeTexture")):
                col = vp.get(col_key, [1, 1, 1, 1])
                f_lin = [float(srgb_to_lin(np.float32(x))) for x in col[:3]] + list(col[3:4] or [1])
                if tex_key in tp:
                    tp[tex_key] = baked(tp[tex_key], f_lin, hsv)
                    vp[col_key] = [1.0, 1.0, 1.0] + list(col[3:4] or [1.0])
                else:
                    sf = shift_factor(f_lin)
                    vp[col_key] = [float(lin_to_srgb(np.float32(x))) for x in sf[:3]] + sf[3:4]
            if "_MainTex" in tp:
                pbr["baseColorTexture"] = {"index": tp["_MainTex"]}
                pbr["baseColorFactor"] = [1.0, 1.0, 1.0, pbr.get("baseColorFactor", [1, 1, 1, 1])[3]]
        else:                                    # VRM 1.0 / glTF : baseColor + extension MToon
            f = pbr.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0])
            if "baseColorTexture" in pbr:
                pbr["baseColorTexture"]["index"] = baked(pbr["baseColorTexture"]["index"], f, hsv)
                pbr["baseColorFactor"] = [1.0, 1.0, 1.0, f[3]]
            else:
                pbr["baseColorFactor"] = shift_factor(f)
            mt = mat.get("extensions", {}).get("VRMC_materials_mtoon")
            if mt:
                sc = mt.get("shadeColorFactor", [1.0, 1.0, 1.0]) + [1.0]
                if "shadeMultiplyTexture" in mt:
                    mt["shadeMultiplyTexture"]["index"] = baked(mt["shadeMultiplyTexture"]["index"], sc, hsv)
                    mt["shadeColorFactor"] = [1.0, 1.0, 1.0]
                else:
                    mt["shadeColorFactor"] = shift_factor(sc)[:3]

    # taille de la tête : échelle de l'os « head »
    head_scale = float((cfg.get("proportions") or {}).get("head", 1.0))
    if abs(head_scale - 1.0) > 1e-3:
        node = None
        if "VRMC_vrm" in ext:
            node = ext["VRMC_vrm"].get("humanoid", {}).get("humanBones", {}).get("head", {}).get("node")
        elif vrm0:
            node = next((b.get("node") for b in vrm0.get("humanoid", {}).get("humanBones", []) if b.get("bone") == "head"), None)
        if node is not None:
            n = js["nodes"][node]
            n["scale"] = [x * head_scale for x in n.get("scale", [1.0, 1.0, 1.0])]
    if abs(float((cfg.get("proportions") or {}).get("height", 1.0)) - 1.0) > 1e-3:
        warnings.append("la taille globale n'est appliquée que dans Nova (les autres logiciels VRM utilisent la taille d'origine)")
    if any(a.get("on") for a in (cfg.get("accessories") or {}).values()) or cfg.get("custom_acc"):
        warnings.append("les accessoires 3D ne sont visibles que dans Nova (et dans OBS via Nova)")

    # métadonnées : nom du personnage + vignette
    name = cfg.get("name") or dest.stem
    thumb_tex = None
    if thumbnail and thumbnail.exists():
        thumb_tex = _add_image(js, bin_, thumbnail.read_bytes())
    if "VRMC_vrm" in ext:
        meta = ext["VRMC_vrm"].setdefault("meta", {})
        meta["name"] = name
        if thumb_tex is not None:
            meta["thumbnailImage"] = js["textures"][thumb_tex]["source"]
    elif vrm0:
        meta = vrm0.setdefault("meta", {})
        meta["title"] = name
        if thumb_tex is not None:
            meta["texture"] = thumb_tex
    js.setdefault("asset", {})["generator"] = "Nova VTuber (créateur de personnages)"
    write_glb(js, bin_, dest)
    log.info("Personnage exporté : %s (%d textures recolorées)", dest.name, len(cache))
    return warnings
