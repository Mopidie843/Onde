"""Import de modèles 3D :
- personnages glTF/GLB riggés (Mixamo, Ready Player Me, Blender, Unity/Unreal, VRoid…) convertis automatiquement
  en VRM 1.0 : reconnaissance du squelette humanoïde, des expressions (clignements, bouche, émotions) et des yeux ;
- objets 3D (.glb/.gltf) utilisés comme accessoires dans le créateur."""
import base64
import copy
import json
import logging
import re
import shutil
from pathlib import Path

import numpy as np

from .creator import read_glb, write_glb
from .settings import ACCESSORIES_DIR, AVATARS_DIR, safe_name

log = logging.getLogger("nova.importer")
REQUIRED = ("hips", "spine", "head", "leftUpperArm", "leftLowerArm", "leftHand", "rightUpperArm", "rightLowerArm", "rightHand")
FINGER_NAMES = {"thumb": "Thumb", "index": "Index", "middle": "Middle", "ring": "Ring", "pinky": "Little", "little": "Little"}
# nom d'os (sans côté) -> os VRM
BASE_BONES = {
    "hips": "hips", "hip": "hips", "pelvis": "hips", "root_hips": "hips",
    "neck": "neck", "head": "head", "jaw": "jaw",
    "shoulder": "Shoulder", "clavicle": "Shoulder", "collar": "Shoulder",
    "arm": "UpperArm", "upperarm": "UpperArm", "uparm": "UpperArm",
    "forearm": "LowerArm", "lowerarm": "LowerArm", "elbow": "LowerArm",
    "hand": "Hand", "wrist": "Hand",
    "upleg": "UpperLeg", "upperleg": "UpperLeg", "thigh": "UpperLeg",
    "leg": "LowerLeg", "lowerleg": "LowerLeg", "calf": "LowerLeg", "shin": "LowerLeg", "knee": "LowerLeg",
    "foot": "Foot", "ankle": "Foot",
    "toebase": "Toes", "toe": "Toes", "toes": "Toes", "ball": "Toes",
    "eye": "Eye",
}
SPINE_BONES = ("spine", "chest", "upperChest")


# ---------------------------------------------------------------- utilitaires glTF
def load_gltf(path: Path) -> tuple[dict, bytearray]:
    """Charge un .glb, .vrm ou .gltf (fichiers externes et data: URI intégrés) -> (json, binaire unique)."""
    if path.suffix.lower() in (".glb", ".vrm"):
        return read_glb(path)
    js = json.loads(path.read_text(encoding="utf-8"))
    blobs = []
    for buf in js.get("buffers", []):
        uri = buf.get("uri", "")
        if uri.startswith("data:"):
            blobs.append(base64.b64decode(uri.split(",", 1)[1]))
        else:
            blobs.append((path.parent / uri).read_bytes())
    bin_, starts = bytearray(), []
    for b in blobs:
        bin_ += b"\0" * (-len(bin_) % 4)
        starts.append(len(bin_))
        bin_ += b
    for bv in js.get("bufferViews", []):
        bv["byteOffset"] = bv.get("byteOffset", 0) + starts[bv.get("buffer", 0)]
        bv["buffer"] = 0
    for img in js.get("images", []):
        uri = img.pop("uri", None)
        if uri is None:
            continue
        if uri.startswith("data:"):
            mime = uri[5:].split(";", 1)[0]
            data = base64.b64decode(uri.split(",", 1)[1])
        else:
            data = (path.parent / uri).read_bytes()
            mime = "image/png" if uri.lower().endswith(".png") else "image/jpeg"
        bin_ += b"\0" * (-len(bin_) % 4)
        js.setdefault("bufferViews", []).append({"buffer": 0, "byteOffset": len(bin_), "byteLength": len(data)})
        bin_ += data
        img["bufferView"] = len(js["bufferViews"]) - 1
        img["mimeType"] = img.get("mimeType", mime)
    js["buffers"] = [{"byteLength": len(bin_)}]
    return js, bin_


def _quat_mat(q) -> np.ndarray:
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _local(node) -> np.ndarray:
    if "matrix" in node:
        return np.array(node["matrix"], np.float64).reshape(4, 4).T
    m = np.eye(4)
    m[:3, :3] = _quat_mat(node.get("rotation", [0, 0, 0, 1])) * np.array(node.get("scale", [1, 1, 1]))
    m[:3, 3] = node.get("translation", [0, 0, 0])
    return m


def world_matrices(js) -> tuple[dict, dict]:
    """Matrices monde et profondeur (dans la hiérarchie) de chaque nœud."""
    nodes = js.get("nodes", [])
    parent = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}
    mats, depth = {}, {}

    def get(i):
        if i not in mats:
            p = parent.get(i)
            mats[i] = (get(p) @ _local(nodes[i])) if p is not None else _local(nodes[i])
            depth[i] = depth[p] + 1 if p is not None else 0
        return mats[i]
    for i in range(len(nodes)):
        get(i)
    return mats, depth


# ---------------------------------------------------------------- squelette
def clean_bone_name(name: str) -> tuple[str, str]:
    """Nom d'os -> (côté 'l' | 'r' | '', nom de base sans préfixes)."""
    s = (name or "").split(":")[-1].split("|")[-1].lower()
    s = re.sub(r"[.\s\-]+", "_", s)
    for pre in ("mixamorig", "j_bip_", "bip001_", "bip01_", "def_", "cc_base_", "armature_", "rig_", "b_", "bone_"):
        if s.startswith(pre):
            s = s[len(pre):].lstrip("_")
    side = ""
    for sd, words in (("l", ("left",)), ("r", ("right",))):
        for w in words:
            if s.startswith(w) or s.endswith(w):
                side, s = sd, s.replace(w, "", 1)
                break
        if side:
            break
        if re.search(rf"(^{sd}_)|(_{sd}$)|(_{sd}_)", s):
            side = sd
            s = re.sub(rf"(^{sd}_)|(_{sd}$)", "", s, count=1)
            s = re.sub(rf"_{sd}_", "_", s, count=1)
            break
    if s.startswith("c_"):
        s = s[2:]
    return side, s.strip("_")


def map_humanoid(js) -> dict:
    """Associe les os du modèle aux os humanoïdes VRM (par leurs noms)."""
    nodes = js.get("nodes", [])
    joints = sorted({j for sk in js.get("skins", []) for j in sk.get("joints", [])}) or list(range(len(nodes)))
    _, depth = world_matrices(js)
    found: dict = {}
    spine_like = []

    def put(bone, i):
        if bone not in found or depth[i] < depth[found[bone]]:
            found[bone] = i

    for i in joints:
        side, base = clean_bone_name(nodes[i].get("name", ""))
        compact = base.replace("_", "")
        if not compact or compact.endswith("end") or "twist" in compact or "roll" in compact or compact.endswith("top"):
            continue
        m = re.fullmatch(r"(?:hand)?(thumb|index|middle|ring|pinky|little)(?:finger)?0?([0-4])?", compact)
        if m and side:
            num = int(m.group(2) or 1)
            if num == 0:
                continue
            fname = FINGER_NAMES[m.group(1)]
            if fname == "Thumb":
                part = {1: "Metacarpal", 2: "Proximal", 3: "Distal"}.get(num)
            else:
                part = {1: "Proximal", 2: "Intermediate", 3: "Distal"}.get(num)
            if part:
                put(("left" if side == "l" else "right") + fname + part, i)
            continue
        if not side and (compact.startswith("spine") or compact in ("chest", "upperchest", "torso", "abdomen")):
            spine_like.append(i)
            continue
        core = re.sub(r"\d+$", "", compact)
        target = BASE_BONES.get(core)
        if not target:
            continue
        if target[0].isupper():          # os latéral
            if side:
                put(("left" if side == "l" else "right") + target, i)
        elif not side:
            put(target, i)
    # colonne : spine / chest / upperChest dans l'ordre de la hiérarchie
    explicit = {clean_bone_name(nodes[i].get("name", ""))[1].replace("_", ""): i for i in spine_like}
    if "chest" in explicit or "upperchest" in explicit:
        for key, bone in (("chest", "chest"), ("upperchest", "upperChest")):
            if key in explicit:
                found[bone] = explicit[key]
        rest = sorted((i for i in spine_like if i not in found.values()), key=lambda i: depth[i])
        if rest:
            found["spine"] = rest[0]
    else:
        for bone, i in zip(SPINE_BONES, sorted(spine_like, key=lambda i: depth[i])):
            found[bone] = i
    # main absente (rigs atypiques : paumes séparées…) : premier os sous l'avant-bras
    joint_set = set(joints)
    for side in ("left", "right"):
        if side + "Hand" not in found and side + "LowerArm" in found:
            kids = [c for c in nodes[found[side + "LowerArm"]].get("children", []) if c in joint_set]
            kids.sort(key=lambda c: ("palm" not in nodes[c].get("name", "").lower() and "hand" not in nodes[c].get("name", "").lower(),
                                     -len(nodes[c].get("children", []))))
            if kids:
                found[side + "Hand"] = kids[0]
    return found


# ---------------------------------------------------------------- expressions
def _norm(n: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (n or "").lower())


EXPR_RULES = [   # (expression VRM, noms de blendshapes reconnus (normalisés), correspondance exacte ?)
    ("blinkLeft", ["eyeblinkleft", "blinkleft", "blinkl", "eyeclosel", "fcleyeclosel", "eyeclosedleft", "eyeclosedl"], True),
    ("blinkRight", ["eyeblinkright", "blinkright", "blinkr", "eyecloser", "fcleyecloser", "eyeclosedright", "eyeclosedr"], True),
    ("blink", ["blink", "eyeclose", "fcleyeclose", "eyesclosed", "eyeclosed", "blinks"], True),
    ("aa", ["visemeaa", "vrcvaa", "fclmtha", "mtha", "a", "aa", "mouthopen"], True),
    ("ih", ["visemei", "vrcvih", "fclmthi", "mthi", "i", "ih"], True),
    ("ou", ["visemeu", "vrcvou", "fclmthu", "mthu", "u", "ou"], True),
    ("ee", ["visemee", "vrcve", "fclmthe", "mthe", "e", "ee"], True),
    ("oh", ["visemeo", "vrcvoh", "fclmtho", "mtho", "o", "oh"], True),
    ("happy", ["happy", "joy", "smile", "fclalljoy", "mouthsmile", "mouthsmileleft", "mouthsmileright"], True),
    ("angry", ["angry", "fclallangry", "browdownleft", "browdownright"], True),
    ("sad", ["sad", "sorrow", "fclallsorrow", "mouthfrownleft", "mouthfrownright"], True),
    ("surprised", ["surprised", "surprise", "fclallsurprised", "browinnerup"], True),
    ("relaxed", ["relaxed", "fun", "fclallfun"], True),
]


def map_expressions(js) -> dict:
    """Blendshapes (morph targets) -> expressions VRM 1.0 (clignements, voyelles, émotions)."""
    mesh_nodes: dict = {}
    for ni, n in enumerate(js.get("nodes", [])):
        if "mesh" in n:
            mesh_nodes.setdefault(n["mesh"], []).append(ni)
    names = []        # (nom normalisé, nœud, index)
    for mi, mesh in enumerate(js.get("meshes", [])):
        tn = (mesh.get("extras") or {}).get("targetNames") or []
        if not tn and mesh.get("primitives"):
            tn = (mesh["primitives"][0].get("extras") or {}).get("targetNames") or []
        for ti, t in enumerate(tn):
            for ni in mesh_nodes.get(mi, []):
                names.append((_norm(t), ni, ti))
    out = {}
    for expr, keys, _exact in EXPR_RULES:
        binds = [{"node": ni, "index": ti, "weight": 1.0} for (nm, ni, ti) in names if nm in keys]
        if binds:
            out[expr] = {"morphTargetBinds": binds, "isBinary": False,
                         "overrideBlink": "none", "overrideLookAt": "none", "overrideMouth": "none"}
    # bouche ouverte de secours : ouverture de la mâchoire (ARKit)
    if "aa" not in out:
        binds = [{"node": ni, "index": ti, "weight": 1.0} for (nm, ni, ti) in names if nm == "jawopen"]
        if binds:
            out["aa"] = {"morphTargetBinds": binds, "isBinary": False,
                         "overrideBlink": "none", "overrideLookAt": "none", "overrideMouth": "none"}
    if "blink" not in out and "blinkLeft" in out and "blinkRight" in out:
        out["blink"] = {"morphTargetBinds": out["blinkLeft"]["morphTargetBinds"] + out["blinkRight"]["morphTargetBinds"],
                        "isBinary": False, "overrideBlink": "none", "overrideLookAt": "none", "overrideMouth": "none"}
    return out


# ---------------------------------------------------------------- conversion
def convert_to_vrm(src: Path, name: str | None = None) -> tuple[str, dict]:
    """Convertit un personnage .glb/.gltf riggé en VRM 1.0 dans la bibliothèque. Retourne (fichier, rapport)."""
    js, bin_ = load_gltf(src)
    js = copy.deepcopy(js)
    if "VRMC_vrm" in js.get("extensions", {}) or "VRM" in js.get("extensions", {}):
        raise ValueError("ce fichier est déjà un VRM : importez-le directement")
    if not js.get("skins"):
        raise ValueError("aucun squelette (rig) dans ce modèle : il ne peut pas être animé. "
                         "Importez-le plutôt comme accessoire dans le Créateur, ou riggez-le (ex. Mixamo).")
    bones = map_humanoid(js)
    missing = [b for b in REQUIRED if b not in bones]
    if missing:
        raise ValueError("squelette humanoïde non reconnu (os manquants : " + ", ".join(missing) + ")")
    mats, _ = world_matrices(js)
    pos = {b: mats[i][:3, 3] for b, i in bones.items()}
    # orientation : VRM regarde vers +Z (main gauche du côté +X) ; échelle : mètres
    flip = pos["leftHand"][0] < pos["rightHand"][0]
    height = pos["head"][1] - min(p[1] for p in pos.values())       # pieds -> base de la tête
    # unités inconnues (cm, pouces, « unités Blender »…) : on ramène le personnage à une taille humaine
    scale = 1.0 if 1.0 <= height <= 2.2 or height <= 0 else round(1.45 / height, 6)
    scene = js["scenes"][js.get("scene", 0)]
    if flip or scale != 1.0:
        root = {"name": "NovaRoot", "children": list(scene["nodes"])}
        if flip:
            root["rotation"] = [0.0, 1.0, 0.0, 0.0]
        if scale != 1.0:
            root["scale"] = [scale] * 3
        js["nodes"].append(root)
        scene["nodes"] = [len(js["nodes"]) - 1]
    expressions = map_expressions(js)
    title = name or src.stem
    vrm = {
        "specVersion": "1.0",
        "meta": {"name": title, "version": "1.0", "authors": ["(auteur du modèle d'origine)"],
                 "licenseUrl": "https://vrm.dev/licenses/1.0/", "avatarPermission": "onlyAuthor",
                 "allowExcessivelyViolentUsage": False, "allowExcessivelySexualUsage": False,
                 "commercialUsage": "personalNonProfit", "allowPoliticalOrReligiousUsage": False,
                 "allowAntisocialOrHateUsage": False, "creditNotation": "required", "allowRedistribution": False,
                 "modification": "prohibited", "otherLicenseUrl": ""},
        "humanoid": {"humanBones": {b: {"node": i} for b, i in bones.items()}},
        "expressions": {"preset": {k: v for k, v in expressions.items()}},
    }
    if "leftEye" in bones and "rightEye" in bones:
        rng = {"inputMaxValue": 90.0, "outputScale": 10.0}
        vrm["lookAt"] = {"type": "bone", "offsetFromHeadBone": [0.0, 0.06, 0.0],
                         "rangeMapHorizontalInner": dict(rng), "rangeMapHorizontalOuter": dict(rng),
                         "rangeMapVerticalDown": dict(rng), "rangeMapVerticalUp": dict(rng)}
    js.setdefault("extensions", {})["VRMC_vrm"] = vrm
    used = js.setdefault("extensionsUsed", [])
    if "VRMC_vrm" not in used:
        used.append("VRMC_vrm")
    js.setdefault("asset", {})["generator"] = "Nova VTuber (import de personnage)"
    fname = safe_name(title) + ".vrm"
    write_glb(js, bin_, AVATARS_DIR / fname)
    fingers = sum(1 for b in bones if any(f in b for f in ("Thumb", "Index", "Middle", "Ring", "Little")))
    report = {"bones": len(bones), "fingers": fingers, "eyes": "leftEye" in bones,
              "expressions": sorted(expressions), "flipped": flip, "scaled": scale != 1.0,
              "legs": "leftUpperLeg" in bones}
    log.info("Personnage importé : %s %s", fname, report)
    return fname, report


def import_accessory(src: Path) -> str:
    """Copie un objet 3D dans le dossier des accessoires (les .gltf sont regroupés en un seul .glb)."""
    if src.suffix.lower() == ".gltf":
        js, bin_ = load_gltf(src)
        fname = safe_name(src.stem) + ".glb"
        write_glb(js, bin_, ACCESSORIES_DIR / fname)
    elif src.suffix.lower() == ".glb":
        read_glb(src)                                  # vérifie que c'est bien un glTF binaire
        fname = safe_name(src.stem) + ".glb"
        if src.resolve() != (ACCESSORIES_DIR / fname).resolve():
            shutil.copy2(src, ACCESSORIES_DIR / fname)
    else:
        raise ValueError("format non pris en charge (utilisez .glb ou .gltf)")
    return fname


def describe(report: dict) -> str:
    lines = [f"Squelette : {report['bones']} os reconnus" + (f" dont {report['fingers']} os de doigts" if report["fingers"] else ""),
             "Yeux : " + ("oui (regard animé)" if report["eyes"] else "non"),
             "Expressions : " + (", ".join(report["expressions"]) if report["expressions"] else
                                 "aucune (pas de blendshapes : le visage restera fixe)")]
    if report["flipped"]:
        lines.append("Orientation corrigée (le modèle regardait vers l'arrière)")
    if report["scaled"]:
        lines.append("Taille ajustée à une taille humaine (le modèle utilisait d'autres unités)")
    return "\n".join(lines)
