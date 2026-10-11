"""Capture webcam (thread dédié) + tracking facial MediaPipe Face Landmarker."""
import atexit
import gc
import logging
import math
import os
import sys
import threading
import time

# Media Foundation : sans cela, l'ouverture de certaines webcams prend 10 à 30 s (transformations matérielles)
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from . import autopause, health, obsws, perf
from .settings import ROOT

log = logging.getLogger("nova.tracking")
MODEL_PATH = ROOT / "assets" / "face_landmarker.task"
POSE_PATH = ROOT / "assets" / "pose_landmarker_full.task"        # modèle précis (processus séparé)
POSE_LITE = ROOT / "assets" / "pose_landmarker_lite.task"
HAND_PATH = ROOT / "assets" / "hand_landmarker.task"
# chaînes de points MediaPipe Hands : poignet puis 4 points par doigt (pouce, index, majeur, annulaire, auriculaire)
FINGERS = [(0, 1, 2, 3, 4), (0, 5, 6, 7, 8), (0, 9, 10, 11, 12), (0, 13, 14, 15, 16), (0, 17, 18, 19, 20)]
ARM_IDX = {"l": (11, 13, 15), "r": (12, 14, 16)}   # épaule, coude, poignet (côté de la personne à l'image)


def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def head_angles(matrix: np.ndarray) -> tuple[float, float, float]:
    """Angles (degrés) yaw, pitch, roll depuis la matrice 4x4 de MediaPipe (ordre d'Euler YXZ)."""
    r = matrix[:3, :3]
    pitch = math.asin(clamp(-r[1, 2], -1.0, 1.0))
    yaw = math.atan2(r[0, 2], r[2, 2])
    roll = math.atan2(r[1, 0], r[1, 1])
    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


class OneEuro:
    """Filtre « One Euro » : très lisse à l'arrêt, quasi sans retard pendant les mouvements rapides."""

    def __init__(self, min_cutoff=1.0, beta=0.05, d_cutoff=1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.x = self.dx = None

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x, dt):
        if self.x is None:
            self.x, self.dx = x, 0.0
            return x
        dx = (x - self.x) / dt
        self.dx += (dx - self.dx) * self._alpha(self.d_cutoff, dt)
        cutoff = self.min_cutoff + self.beta * abs(self.dx)
        self.x += (x - self.x) * self._alpha(cutoff, dt)
        return self.x


# points du maillage MediaPipe (478 points, iris inclus) : coin externe, coin interne, paupière haute, basse, iris
EYE_A = (33, 133, 159, 145, 468)
EYE_B = (263, 362, 386, 374, 473)
VISEMES = ("aa", "ih", "ou", "ee", "oh")


# ouverture des paupières (« eye aspect ratio ») : coin externe, coin interne, 2 points du haut, 2 points du bas
EAR_A = (33, 133, (160, 158), (144, 153))
EAR_B = (263, 362, (387, 385), (373, 380))


def soft_zone(delta: float, dz: float) -> float:
    """Zone morte douce : un petit écart (tremblement) est atténué progressivement au lieu d'être coupé net.
    Avant, tout écart sous le seuil était ignoré puis rattrapé d'un coup : mouvements lents « en escalier »."""
    a = abs(delta)
    if dz <= 0 or a >= 2 * dz:
        return delta
    x = a / (2 * dz)
    return math.copysign(2 * dz * (2 * x * x - x * x * x), delta)   # continue, sans à-coup à la sortie de la zone


def eye_width(lms, eye, aspect: float = 1.0) -> float:
    o, i = lms[eye[0]], lms[eye[1]]
    return math.hypot((o.x - i.x) * aspect, o.y - i.y)


def iris_gaze(lms, eye, aspect: float = 1.0) -> tuple[float, float] | None:
    """Position de l'iris dans l'œil (-1..1), plus précise que les blendshapes pour le regard.
    Vertical mesuré par rapport à la ligne des coins de l'œil (stable même quand les paupières bougent)."""
    if lms is None or len(lms) <= max(eye):
        return None
    o, i, _t, _b, c = (lms[k] for k in eye)
    cx, cy = (o.x + i.x) / 2 * aspect, (o.y + i.y) / 2
    half = math.hypot((o.x - i.x) * aspect, o.y - i.y) / 2
    if half < 1e-4:
        return None
    return (c.x * aspect - cx) / half, -(c.y - cy) / half


def eye_ratio(lms, eye, aspect: float = 1.0) -> float | None:
    """Hauteur de l'ouverture de l'œil divisée par sa largeur (~0.3 ouvert, ~0.1 fermé)."""
    if lms is None or len(lms) <= 400:
        return None
    o, i, ups, downs = eye
    w = math.hypot((lms[o].x - lms[i].x) * aspect, lms[o].y - lms[i].y)
    if w < 1e-4:
        return None
    h = sum(math.hypot((lms[u].x - lms[d].x) * aspect, lms[u].y - lms[d].y) for u, d in zip(ups, downs)) / 2
    return h / w


def visemes(g, jaw: float, ms: float) -> dict:
    """Cinq formes de bouche VRM (a, i, ou, é, o) estimées à partir des blendshapes ARKit de MediaPipe."""
    funnel, pucker = g("mouthFunnel", 0), g("mouthPucker", 0)
    stretch = (g("mouthStretchLeft", 0) + g("mouthStretchRight", 0)) / 2
    smile = (g("mouthSmileLeft", 0) + g("mouthSmileRight", 0)) / 2
    opened = clamp(jaw * 4)
    v = {
        "aa": clamp(jaw * 2.5 * ms) * (1 - 0.6 * clamp(max(funnel, pucker) * 2)),
        "oh": clamp((funnel * 1.8 - 0.1) * ms) * opened,
        "ou": clamp((pucker * 1.6 - 0.1) * ms) * (1 - clamp(jaw * 3)),
        "ee": clamp((stretch * 2.2 + smile * 0.5 - 0.15) * ms) * clamp(jaw * 5),
        "ih": clamp((stretch * 1.5 + smile * 0.4 - 0.2) * ms) * (1 - clamp(jaw * 2.5)),
    }
    total = sum(v.values())
    if total > 1.0:   # la forme dominante est gardée, les autres se partagent le reste (bouche jamais déformée)
        top = max(v, key=v.get)
        rest = total - v[top]
        room = 1.0 - v[top]
        for k in v:
            if k != top:
                v[k] = v[k] * room / rest if rest > 0 else 0.0
    return v


class FaceMapper:
    """Transforme les données brutes (blendshapes, maillage, pose) en paramètres d'avatar.
    Applique calibration, sensibilités, zone morte et lissage. Indépendant de la webcam (testable)."""

    def __init__(self, cfg: dict, expr_cfg: dict):
        self.cfg = cfg            # section "tracking" du profil (référence partagée)
        self.expr_cfg = expr_cfg  # section "expressions"
        self.out = None
        self.last_raw = None
        self.filters = {}
        self._t = None
        self._gaze_hold = (0.0, 0.0)
        cal = cfg.get("calibration", {})
        self._open = [cal.get("earL") or None, cal.get("earR") or None]   # ouverture « normale » apprise par œil
        self._blink_until = [0.0, 0.0]
        self._auto = []                       # échantillons du calibrage automatique
        self.auto_calibrated = False

    def _never_calibrated(self) -> bool:
        cal = self.cfg.get("calibration", {})
        return not any(cal.get(k) for k in ("yaw", "pitch", "roll", "earL", "earR", "tz"))

    def _auto_calibrate(self) -> None:
        """Premier lancement (jamais calibré) : la pose neutre est apprise toute seule après ~1,5 s face à la
        caméra (tête droite, yeux ouverts, bouche fermée). « Calibrer » reste possible à tout moment."""
        r = self.last_raw
        bs = r["bs"]
        if (abs(r["yaw"]) > 25 or abs(r["pitch"]) > 25 or bs.get("jawOpen", 0) > 0.15
                or (bs.get("eyeBlinkLeft", 0) + bs.get("eyeBlinkRight", 0)) / 2 > 0.4):
            return
        self._auto.append(r)
        if len(self._auto) < 45:
            return
        med = lambda vals: float(np.median([v for v in vals if v is not None])) if any(
            v is not None for v in vals) else 0.0
        a = self._auto
        iris = [x["iris"] for x in a if x["iris"]]
        pos = [x["pos"] for x in a if x["pos"]]
        ears = [x.get("ears") or (None, None) for x in a]
        self.cfg["calibration"] = {
            "yaw": med([x["yaw"] for x in a]), "pitch": med([x["pitch"] for x in a]), "roll": med([x["roll"] for x in a]),
            "blink": med([(x["bs"].get("eyeBlinkLeft", 0) + x["bs"].get("eyeBlinkRight", 0)) / 2 for x in a]),
            "mouth": med([x["bs"].get("jawOpen", 0) for x in a]),
            "gx": med([i[0] for i in iris]), "gy": med([i[1] for i in iris]),
            "tx": med([q[0] for q in pos]), "ty": med([q[1] for q in pos]), "tz": med([q[2] for q in pos]),
            "earL": med([e[0] for e in ears]), "earR": med([e[1] for e in ears])}
        self._open = [self.cfg["calibration"]["earL"] or None, self.cfg["calibration"]["earR"] or None]
        self._auto, self.auto_calibrated, self.out, self.filters = [], True, None, {}
        log.info("Calibrage automatique fait : %s", self.cfg["calibration"])

    def calibrate(self) -> bool:
        if not self.last_raw:
            return False
        r = self.last_raw
        gx, gy = r["iris"] or (0.0, 0.0)
        tx, ty, tz = r["pos"] or (0.0, 0.0, 0.0)
        ears = r.get("ears") or (None, None)
        self.cfg["calibration"] = {"yaw": r["yaw"], "pitch": r["pitch"], "roll": r["roll"],
                                   "blink": (r["bs"].get("eyeBlinkLeft", 0) + r["bs"].get("eyeBlinkRight", 0)) / 2,
                                   "mouth": r["bs"].get("jawOpen", 0), "gx": gx, "gy": gy,
                                   "tx": tx, "ty": ty, "tz": tz, "earL": ears[0], "earR": ears[1]}
        self._open = [ears[0], ears[1]]
        self.out = None
        self.filters = {}          # sinon le lissage repartirait de l'ancienne pose et glisserait vers la nouvelle
        return True

    def _blinks(self, g, lms, aspect, t) -> tuple[float, float, tuple]:
        """Fermeture de chaque œil (0 ouvert .. 1 fermé) : fusion de l'ouverture géométrique des paupières
        (adaptée en continu à votre visage) et des blendshapes de MediaPipe, compensée quand on regarde en bas."""
        c, cal = self.cfg, self.cfg["calibration"]
        bsens = c.get("blink", 1.0)
        low = clamp(cal.get("blink", 0) + 0.1, 0.08, 0.6)
        down = (g("eyeLookDownLeft", 0) + g("eyeLookDownRight", 0)) / 2
        bs_side = [g("eyeBlinkLeft", 0), g("eyeBlinkRight", 0)]
        bs_avg = clamp((sum(bs_side) / 2 - low - down * 0.35) / 0.45)
        # œil situé à droite de l'image = œil gauche de l'avatar (l'avatar imite la personne de l'image)
        ea, eb = eye_ratio(lms, EAR_A, aspect), eye_ratio(lms, EAR_B, aspect)
        ears = (None, None)
        if ea is not None and eb is not None:
            right_is_b = lms[EAR_B[0]].x > lms[EAR_A[0]].x
            ears = (eb, ea) if right_is_b else (ea, eb)   # (gauche avatar, droite avatar)
        out = []
        for i in range(2):
            e = ears[i]
            if e is None:      # pas de maillage : blendshapes seules
                fused = clamp((bs_side[i] - low - down * 0.35) / 0.45)
            else:
                base = self._open[i] or e
                # l'ouverture de référence monte vite (yeux bien ouverts) et ne descend que très lentement
                base += (e - base) * (0.08 if e > base else 0.002)
                self._open[i] = base
                closed = clamp((base - e) / (base * 0.62) - down * 0.45)
                fused = 0.6 * closed + 0.4 * bs_avg
            x = clamp((fused * bsens - 0.18) / 0.5)
            v = x * x * (3 - 2 * x)                       # courbe douce, puis « clac » franc en fin de fermeture
            if v > 0.82:
                v = 1.0
                self._blink_until[i] = t + 0.06            # un clignement reste visible au moins 60 ms
            elif t < self._blink_until[i]:
                v = 1.0
            out.append(v)
        bl, br = out
        if c.get("eye_sync", True) and abs(bl - br) < 0.45:
            bl = br = max(bl, br) if max(bl, br) > 0.5 else (bl + br) / 2
        return bl, br, ears

    def map(self, yaw: float, pitch: float, roll: float, bs: dict, t: float | None = None,
            lms=None, pos: tuple | None = None, aspect: float = 1.0) -> dict:
        t = time.monotonic() if t is None else t
        dt = 1 / 60 if self._t is None else min(max(t - self._t, 1 / 120), 0.2)
        self._t = t
        c, cal = self.cfg, self.cfg["calibration"]
        g = bs.get
        hs, es, ms = c["head"], c["eyes"], c["mouth"]

        # --- yeux : clignements (paupières + blendshapes), synchronisés sauf vrai clin d'œil
        blink_l, blink_r, ears = self._blinks(g, lms, aspect, t)

        # --- regard : iris (précis) combiné aux blendshapes (stables) ; figé pendant un clignement
        iris = None
        if c.get("iris", True):
            a, b = iris_gaze(lms, EYE_A, aspect), iris_gaze(lms, EYE_B, aspect)
            if a and b:
                # tête tournée : l'œil vu de profil (plus étroit à l'image) est bien moins fiable -> poids au carré
                wa, wb = eye_width(lms, EYE_A, aspect) ** 2, eye_width(lms, EYE_B, aspect) ** 2
                tot = (wa + wb) or 1.0
                iris = ((a[0] * wa + b[0] * wb) / tot, (a[1] * wa + b[1] * wb) / tot)
        self.last_raw = {"yaw": yaw, "pitch": pitch, "roll": roll, "bs": bs, "iris": iris, "pos": pos, "ears": ears}
        if not self.auto_calibrated and c.get("auto_calibrate", True) and self._never_calibrated():
            self._auto_calibrate()
            cal = self.cfg["calibration"]
        bgx = clamp(((g("eyeLookOutLeft", 0) - g("eyeLookInLeft", 0)) + (g("eyeLookInRight", 0) - g("eyeLookOutRight", 0))) / 2, -1, 1)
        bgy = clamp(((g("eyeLookUpLeft", 0) + g("eyeLookUpRight", 0)) - (g("eyeLookDownLeft", 0) + g("eyeLookDownRight", 0))) / 2, -1, 1)
        if iris:
            gx = 0.7 * clamp((iris[0] - cal.get("gx", 0)) / 0.35, -1, 1) + 0.3 * bgx
            gy = 0.5 * clamp((iris[1] - cal.get("gy", 0)) / 0.2, -1, 1) + 0.5 * bgy
        else:
            gx, gy = bgx, bgy
        gx, gy = clamp(gx * es, -1, 1), clamp(gy * es, -1, 1)
        if max(blink_l, blink_r) > 0.5:
            gx, gy = self._gaze_hold
        else:
            hx, hy = self._gaze_hold
            gx, gy = hx + soft_zone(gx - hx, 0.0125), hy + soft_zone(gy - hy, 0.0125)   # micro-tremblements atténués
            self._gaze_hold = (gx, gy)

        # --- buste : la position de la tête dans l'image fait pencher / avancer le corps
        lean = c.get("lean", 1.0)
        if pos and lean > 0:
            lx = clamp((pos[0] - cal.get("tx", 0)) / 12 * lean, -1, 1)
            ly = clamp((pos[1] - cal.get("ty", 0)) / 12 * lean, -1, 1)
            lz = clamp((pos[2] - cal.get("tz", 0)) / 15 * lean, -1, 1)
        else:
            lx = ly = lz = 0.0
        target = {
            "yaw": (yaw - cal["yaw"]) * hs,
            "pitch": (pitch - cal["pitch"]) * hs,
            "roll": (roll - cal["roll"]) * hs,
            "gazeX": gx, "gazeY": gy, "leanX": lx, "leanY": ly, "leanZ": lz,
        }
        jaw = max(0.0, g("jawOpen", 0) - cal.get("mouth", 0) - 0.03)
        expr = {"blinkLeft": blink_l, "blinkRight": blink_r, **visemes(g, jaw, ms)}
        smile = (g("mouthSmileLeft", 0) + g("mouthSmileRight", 0)) / 2
        auto, strength = self.expr_cfg["auto"], self.expr_cfg["strength"]
        detected = {
            "happy": clamp(smile * 1.8 - 0.3),
            "surprised": clamp(g("browInnerUp", 0) * 1.3 + (g("eyeWideLeft", 0) + g("eyeWideRight", 0)) * 0.4 - 0.35),
            "angry": clamp((g("browDownLeft", 0) + g("browDownRight", 0)) * 1.1 - 0.35),
            "sad": clamp((g("mouthFrownLeft", 0) + g("mouthFrownRight", 0)) * 1.2 + g("browInnerUp", 0) * 0.3 - 0.35),
        }
        for name, v in detected.items():
            expr[name] = v * strength.get(name, 1.0) if auto.get(name) else 0.0

        # --- filtre One Euro (lissage adaptatif) + zone morte (degrés) sur la tête
        sm = clamp(c["smoothing"], 0, 0.95)
        a = 1.0 - sm * 0.9
        dz = c["deadzone"]
        if self.out is None:
            self.out = dict(target, expr=dict(expr))
            return self.out
        o = self.out
        min_cut = 0.3 + (1.0 - sm) ** 2 * 6.0     # lissage élevé = coupure basse à l'arrêt
        for k, v in target.items():
            if k in ("yaw", "pitch", "roll") and k in o:
                v = o[k] + soft_zone(v - o[k], dz)
            if sm <= 0.01 or k not in o:
                o[k] = v
                continue
            f = self.filters.get(k)
            if f is None:
                f = self.filters[k] = OneEuro(beta=0.06 if k in ("yaw", "pitch", "roll") else 4.0)
                f.x = o[k]
                f.dx = 0.0
            f.min_cutoff = min_cut * (0.5 if k.startswith("lean") else 1.0)
            o[k] = f(v, dt)
        eo = o["expr"]
        for k, v in expr.items():
            # clignements instantanés, bouche vive, expressions lissées
            aa = 1.0 if k.startswith("blink") else min(1.0, a * 2.0) if k in VISEMES else a
            eo[k] = eo.get(k, 0.0) + (v - eo.get(k, 0.0)) * aa
        return o


class FaceTracker:
    def __init__(self):
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python import vision
        if not MODEL_PATH.exists():
            raise FileNotFoundError("Modèle de tracking manquant (assets/face_landmarker.task). Lancez install.bat.")
        self._mp = mp
        opts = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(MODEL_PATH)),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            # visage gardé plus longtemps de profil, en contre-jour ou à moitié caché (moins de « visage perdu »)
            min_face_detection_confidence=0.5, min_face_presence_confidence=0.4, min_tracking_confidence=0.4,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
        )
        self.landmarker = vision.FaceLandmarker.create_from_options(opts)
        self._last_ts = 0

    def detect(self, rgb: np.ndarray, t: float | None = None):
        """Retourne (yaw, pitch, roll, blendshapes, landmarks) ou None si aucun visage.
        t = heure de prise de l'image : le suivi interne de MediaPipe s'aligne sur le vrai rythme de la webcam."""
        ts = max(int((time.monotonic() if t is None else t) * 1000), self._last_ts + 1)
        self._last_ts = ts
        img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        res = self.landmarker.detect_for_video(img, ts)
        if not res.face_landmarks:
            return None
        bs = {c.category_name: c.score for c in res.face_blendshapes[0]} if res.face_blendshapes else {}
        m = np.asarray(res.facial_transformation_matrixes[0])
        yaw, pitch, roll = head_angles(m)
        pos = (float(m[0, 3]), float(m[1, 3]), float(m[2, 3]))   # position de la tête (cm, repère caméra)
        return yaw, pitch, roll, bs, res.face_landmarks[0], pos

    def close(self):
        self.landmarker.close()


def _v(lm) -> np.ndarray:
    """Coordonnées monde MediaPipe (x droite, y bas, z vers l'arrière) -> repère de l'avatar (y haut, z vers la caméra)."""
    return np.array([lm.x, -lm.y, -lm.z], dtype=np.float64)


def _unit(v: np.ndarray):
    n = float(np.linalg.norm(v))
    return (v / n) if n > 1e-6 else None


def _angle(a: np.ndarray, b: np.ndarray) -> float:
    ua, ub = _unit(a), _unit(b)
    if ua is None or ub is None:
        return 0.0
    return float(math.acos(clamp(float(np.dot(ua, ub)), -1.0, 1.0)))


def finger_angles(hw) -> list[float]:
    """15 angles de flexion (rad) : 3 articulations x 5 doigts, à partir des points 3D d'une main."""
    return finger_angles_pts([_v(p) for p in hw])


def finger_angles_pts(pts) -> list[float]:
    out = []
    for chain in FINGERS:
        segs = [pts[chain[i + 1]] - pts[chain[i]] for i in range(4)]
        out += [round(_angle(segs[i], segs[i + 1]), 3) for i in range(3)]
    return out


class VecFilter:
    """Filtre One Euro appliqué à un vecteur (positions 3D, points de la main)."""

    def __init__(self, min_cutoff: float, beta: float):
        self.min_cutoff, self.beta = min_cutoff, beta
        self.f = None
        self.t = None

    def __call__(self, v: np.ndarray, t: float) -> np.ndarray:
        v = np.asarray(v, np.float64).ravel()
        if self.f is None or self.t is None or t - self.t > 0.5 or len(self.f) != len(v):
            self.f = [OneEuro(self.min_cutoff, self.beta) for _ in v]   # (re)départ après une perte de suivi
        dt = 1 / 30 if self.t is None else min(max(t - self.t, 1 / 120), 0.2)
        self.t = t
        return np.array([f(float(x), dt) for f, x in zip(self.f, v)])


def two_bone_ik(dw: np.ndarray, reach: float, side: int):
    """Coude naturel quand seule la main est visible : bras de longueurs égales, coude vers le bas et l'extérieur.
    dw = direction épaule -> poignet, reach = distance relative (0..1). Retourne (dir. bras, dir. avant-bras)."""
    reach = clamp(reach, 0.2, 0.98)
    along = reach                                   # segments de longueur 1 : distance totale 2*reach
    h = math.sqrt(max(0.0, 1.0 - along * along))
    pole = np.array([0.5 * side, -1.0, 0.15])
    perp = pole - np.dot(pole, dw) * dw
    perp = _unit(perp)
    if perp is None:
        perp = np.array([0.0, -1.0, 0.0])
    elbow = dw * along + perp * h
    wrist = dw * 2 * reach
    return _unit(elbow), _unit(wrist - elbow)


class BodyTracker:
    """Bras (MediaPipe Pose) + mains et doigts (MediaPipe Hands), filtrés et stabilisés."""

    HOLD = 0.35   # une main perdue un court instant garde sa dernière pose (pas de clignotement)

    def __init__(self):
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python import vision
        pose_model = POSE_PATH if POSE_PATH.exists() else POSE_LITE
        for p in (pose_model, HAND_PATH):
            if not p.exists():
                raise FileNotFoundError(f"Modèle manquant : assets/{p.name}. Relancez install.bat.")
        self._mp = mp
        self.pose = vision.PoseLandmarker.create_from_options(vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(pose_model)), running_mode=vision.RunningMode.VIDEO,
            num_poses=1, min_pose_detection_confidence=0.5, min_pose_presence_confidence=0.5,
            min_tracking_confidence=0.5))
        self.hands = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(HAND_PATH)), running_mode=vision.RunningMode.VIDEO,
            num_hands=2, min_hand_detection_confidence=0.4, min_hand_presence_confidence=0.4,
            min_tracking_confidence=0.4))
        self._ts = 0
        self.filters: dict = {}
        self.last: dict = {}

    def _filt(self, key, v, t, min_cutoff=1.0, beta=3.0):
        f = self.filters.get(key)
        if f is None:
            f = self.filters[key] = VecFilter(min_cutoff, beta)
        return f(v, t)

    def detect(self, rgb: np.ndarray, t: float | None = None) -> tuple[dict, list]:
        """Retourne ({"l": bras|None, "r": bras|None, "t": torse}, points 2D pour l'aperçu)."""
        t = time.monotonic() if t is None else t
        self._ts = max(int(t * 1000), self._ts + 1)
        img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        pr = self.pose.detect_for_video(img, self._ts)
        arms, dots = {"l": None, "r": None}, []
        if pr.pose_world_landmarks:
            world, norm = pr.pose_world_landmarks[0], pr.pose_landmarks[0]
            self._solve(img, world, norm, arms, dots, t)
        for side in ("l", "r"):            # maintien bref de la dernière pose connue
            if arms[side] is None and side in self.last and t - self.last[side][1] < self.HOLD:
                arms[side] = self.last[side][0]
        return arms, dots

    def _solve(self, img, world, norm, arms, dots, t):
        inside = lambda i: -0.05 < norm[i].x < 1.05 and -0.05 < norm[i].y < 1.05
        vis = lambda i: (norm[i].visibility or 0) > 0.5 and inside(i)
        # mains : chacune associée au poignet de la pose le plus proche (plus fiable que l'étiquette gauche/droite)
        hands = {}
        hr = self.hands.detect_for_video(img, self._ts)
        for lm2d, hw in zip(hr.hand_landmarks or [], hr.hand_world_landmarks or []):
            hx, hy = lm2d[0].x, lm2d[0].y
            dist = {k: (norm[ARM_IDX[k][2]].x - hx) ** 2 + (norm[ARM_IDX[k][2]].y - hy) ** 2 for k in ARM_IDX}
            for side in sorted(dist, key=dist.get):
                if side not in hands or dist[side] < hands[side][0]:
                    hands[side] = (dist[side], lm2d, hw)
                    break
        sw = float(np.linalg.norm(_v(world[11]) - _v(world[12]))) or 0.36
        for side, (s, e, w) in ARM_IDX.items():
            hand = hands.get(side)
            if not vis(s) or not (vis(e) or hand):
                continue
            S = self._filt((side, "S"), _v(world[s]), t)
            E = self._filt((side, "E"), _v(world[e]), t)
            W = self._filt((side, "W"), _v(world[w]), t)
            wrist_ok = vis(w) or hand is not None
            if vis(e):                                   # coude visible : directions mesurées
                up = _unit(E - S)
                fo = _unit(W - E) if wrist_ok else None
            else:                                        # seule la main est visible : coude reconstruit (IK)
                dw = _unit(W - S)
                if dw is None:
                    continue
                up, fo = two_bone_ik(dw, float(np.linalg.norm(W - S)) / (1.5 * sw), 1 if side == "l" else -1)
            if up is None:
                continue
            arm = {"u": [round(float(x), 4) for x in up]}
            dots.append((norm[s].x, norm[s].y))
            if inside(e):
                dots.append((norm[e].x, norm[e].y))
            if fo is not None:
                arm["f"] = [round(float(x), 4) for x in fo]
            if hand and fo is not None:
                _, lm2d, hw = hand
                pts = self._filt((side, "hand"), np.array([_v(q) for q in hw]), t, 2.0, 12.0).reshape(21, 3)
                d = _unit(pts[9] - pts[0])
                n = _unit(np.cross(pts[5] - pts[0], pts[17] - pts[0]))
                if d is not None and n is not None:
                    if side == "l":
                        n = -n
                    rel = pts - pts[0]
                    arm.update(h=[round(float(x), 4) for x in d], n=[round(float(x), 4) for x in n],
                               lm=[round(float(x), 4) for x in rel.ravel()],
                               c=finger_angles_pts(pts))
                    dots += [(q.x, q.y) for q in lm2d[::2]]
            arms[side] = arm
            self.last[side] = (arm, t)
        # torse : orientation de la ligne des épaules (rotation et inclinaison du buste)
        if vis(11) and vis(12):
            d = self._filt("torso", _v(world[11]) - _v(world[12]), t, 0.8, 1.0)
            if abs(d[0]) > 1e-3:
                yaw_t = math.degrees(math.atan2(-d[2], d[0])) * 0.6   # profondeur estimée : on l'atténue
                roll_t = math.degrees(math.atan2(d[1], d[0]))
                arms["t"] = [round(clamp(yaw_t, -30, 30), 2), round(clamp(roll_t, -20, 20), 2)]

    def close(self):
        self.pose.close()
        self.hands.close()


def _body_main(conn):
    """Processus séparé : MediaPipe bloque l'interpréteur pendant ses calculs, un processus dédié
    permet au suivi des bras de tourner en vrai parallèle du visage (autre cœur du processeur)."""
    try:
        perf.boost(True)                       # processus séparé : il a besoin de son propre anti-bridage
        perf.boost_thread("Games")
    except Exception:  # noqa: BLE001
        pass
    try:
        tracker = BodyTracker()
    except Exception as e:  # noqa: BLE001
        conn.send(("error", str(e)))
        return
    conn.send(("ready", None))
    try:
        while True:
            frame = conn.recv()
            if frame is None:
                break
            conn.send(("result", tracker.detect(frame)))
    except (EOFError, OSError):
        pass
    finally:
        tracker.close()


class BodyProcess:
    """Envoie au plus ~14 images/s (réduites) au processus de suivi du corps et garde le dernier résultat."""

    def __init__(self):
        import multiprocessing as mproc
        ctx = mproc.get_context("spawn")
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=_body_main, args=(child,), daemon=True, name="nova-body")
        self.proc.start()
        self.result = ({"l": None, "r": None}, [])
        self.error, self.ready, self.busy, self._sent = "", False, False, 0.0

    def poll(self):
        try:
            while self.conn.poll():
                kind, val = self.conn.recv()
                if kind == "error":
                    self.error = val
                elif kind == "ready":
                    self.ready = True
                else:
                    self.result, self.busy = val, False
        except (EOFError, OSError):
            self.error = self.error or "le processus de suivi du corps s'est arrêté"
        if not self.ready and not self.error and not self.proc.is_alive():
            self.error = "le processus de suivi du corps n'a pas démarré"

    def submit(self, rgb):
        self.poll()
        now = time.monotonic()
        if self.ready and not self.busy and now - self._sent >= 0.02:
            h, w = rgb.shape[:2]
            small = cv2.resize(rgb, (960, int(960 * h / w)), interpolation=cv2.INTER_AREA) if w > 960 else rgb
            try:
                self.conn.send(small)
                self.busy, self._sent = True, now
            except (OSError, EOFError):
                self.error = "le processus de suivi du corps s'est arrêté"

    def stop(self):
        try:
            self.conn.send(None)
        except (OSError, EOFError):
            pass
        self.proc.join(3)
        if self.proc.is_alive():
            self.proc.terminate()


# ---------------------------------------------------------------- ouverture de la webcam
if sys.platform == "win32":
    BACKENDS = (("DirectShow", cv2.CAP_DSHOW), ("Media Foundation", cv2.CAP_MSMF))
else:
    BACKENDS = (("auto", cv2.CAP_ANY),)
FIRST_FRAME = 2.5      # secondes : certaines webcams mettent 1 à 2 s à envoyer leur première image
WAIT_UNKNOWN = 8.0     # webcam muette sans coupable identifié : on réessaie ce temps-là avant d'abandonner
PROBE_MAX = 6          # numéros de caméra essayés quand celui choisi ne répond pas


class CameraError(RuntimeError):
    """Webcam impossible à ouvrir. code : denied (bloquée par Windows) | nova (autre fenêtre de Nova) |
    busy (autre application) | failed (raison inconnue) | stopped (arrêt demandé pendant l'attente)."""

    def __init__(self, code: str, message: str, apps: list | None = None):
        super().__init__(message)
        self.code, self.apps = code, apps or []


def _sleep(seconds: float, should_stop) -> bool:
    """Attend par petits pas ; retourne False si l'arrêt a été demandé."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if should_stop():
            return False
        time.sleep(0.05)
    return not should_stop()


def _first_frame(cap, timeout: float | None = None) -> bool:
    """La webcam envoie-t-elle vraiment des images ? « Ouverte » ne suffit pas : une webcam occupée ou un
    format refusé donne une caméra ouverte mais muette."""
    end = time.monotonic() + (FIRST_FRAME if timeout is None else timeout)
    while time.monotonic() < end:
        ok, frame = cap.read()
        if ok and frame is not None and frame.size:
            return True
        time.sleep(0.03)
    return False


def _measure_fps(cap, frames: int = 6) -> float:
    t0 = time.monotonic()
    got = 0
    while got < frames and time.monotonic() - t0 < 1.5:
        ok, _ = cap.read()
        got += bool(ok)
    dt = time.monotonic() - t0
    return got / dt if dt > 0 else 999.0


def _try_open(index: int, api: int, w: int, h: int, fps: int):
    """Ouvre la webcam avec une API vidéo : d'abord en mode rapide (MJPG, résolution voulue), puis avec les
    réglages d'origine de la webcam si elle refuse ce mode. None si elle ne donne aucune image."""
    for custom in (True, False):
        cap = cv2.VideoCapture(index, api)
        if not cap.isOpened():
            cap.release()
            return None
        if custom:
            if api == getattr(cv2, "CAP_DSHOW", -1):
                # avant la résolution, sinon DirectShow ignore le format (MJPG = plus d'images/s en USB)
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # toujours l'image la plus récente (moins de latence)
        if _first_frame(cap):
            if custom and w * h > 640 * 480 and _measure_fps(cap) < 20:
                # webcam qui ne tient pas la HD (souvent sans MJPG) : mieux vaut 30 i/s en 640x480 que 10 en HD
                cap.release()
                log.info("Webcam %d : HD trop lente, passage en 640x480", index)
                return _try_open(index, api, 640, 480, fps)
            return cap
        cap.release()
        log.info("Webcam %d (API %d) : aucune image en %s", index, api, "mode rapide" if custom else "mode d'origine")
    return None


def _open_any(index: int, w: int, h: int, fps: int):
    for name, api in BACKENDS:
        cap = _try_open(index, api, w, h, fps)
        if cap is not None:
            return cap, name
    return None, ""


def diagnose(index: int) -> CameraError:
    """Pourquoi la webcam ne s'ouvre pas, en clair."""
    generic = CameraError("failed", f"Impossible d'ouvrir la webcam n°{index}.")
    if sys.platform != "win32":
        return generic
    try:
        from . import camusers
        blocked = camusers.camera_blocked()
        if blocked:
            return CameraError("denied", f"Windows bloque la webcam : {blocked}. Dans Paramètres › Confidentialité et "
                                         "sécurité › Caméra, activez l'accès à la caméra et « Autoriser les applications "
                                         "de bureau à accéder à votre caméra », puis relancez le suivi.")
        users = camusers.find_camera_users()
        if camusers.other_nova_holds_camera():
            return CameraError("nova", "La webcam est déjà prise par une autre fenêtre de Nova (regardez aussi l'icône "
                                       "près de l'horloge). Fermez-la ou cliquez sur « Libérer la webcam ».",
                               [a for a in users if a.get("nova")])
        busy = [a for a in users if a["in_use"]]
        if busy:
            names = ", ".join(dict.fromkeys(a["name"] for a in busy))
            return CameraError("busy", f"La webcam est utilisée par : {names}. Fermez-la ou cliquez sur "
                                       "« Libérer la webcam ».", busy)
        guards = camusers.webcam_guards()
    except Exception as e:  # noqa: BLE001
        log.warning("Diagnostic webcam : %s", e)
        return generic
    msg = (f"Impossible d'ouvrir la webcam n°{index}, et aucune autre application ne l'utilise. Essayez : choisir "
           "une autre caméra dans la liste ; débrancher puis rebrancher la webcam ; vérifier l'obturateur ou la touche "
           "caméra du PC portable ; redémarrer le PC si une application a planté en la gardant.")
    if guards:
        msg += (f" Votre antivirus ({', '.join(guards)}) a une protection webcam : autorisez "
                f"« {sys.executable} » dans ses réglages.")
    return CameraError("failed", msg)


def open_camera(index: int, w: int, h: int, fps: int, should_stop=lambda: False, on_wait=None,
                wait_busy: float | None = 8.0, wait_unknown: float | None = None) -> tuple:
    """Ouvre la webcam (DirectShow puis Media Foundation sous Windows) et vérifie qu'elle envoie des images.
    - bloquée par Windows : erreur immédiate (attendre ne changerait rien) ;
    - occupée (autre application, autre fenêtre de Nova) : nouvel essai pendant « wait_busy » secondes
      (None = jusqu'à ce qu'elle se libère : c'est le mode « réserver la webcam ») ;
    - raison inconnue : nouvel essai pendant « wait_unknown » secondes (une webcam qui vient d'être libérée met
      parfois 1 à 2 s à redevenir disponible), puis essai des autres numéros : si le numéro choisi ne répond pas
      (caméra virtuelle désinstallée, ordre des caméras changé…), la première webcam qui marche est utilisée.
    Retourne (cap, nom de l'API, numéro réellement ouvert). Lève CameraError."""
    start = time.monotonic()
    wait_unknown = WAIT_UNKNOWN if wait_unknown is None else wait_unknown
    delay, probed = 0.4, False
    while True:
        cap, api = _open_any(index, w, h, fps)
        if cap is not None:
            return cap, api, index
        gc.collect()          # une ancienne capture oubliée en mémoire rend la webcam
        err = diagnose(index)
        if err.code == "denied":
            raise err
        waited = time.monotonic() - start
        if err.code == "failed" and waited >= wait_unknown:
            if not probed:
                probed = True
                for j in range(PROBE_MAX):
                    if j == index or should_stop():
                        continue
                    cap, api = _open_any(j, w, h, fps)
                    if cap is not None:
                        log.warning("Webcam %d inutilisable : webcam %d utilisée à la place", index, j)
                        return cap, api, j
            raise err
        if err.code != "failed" and wait_busy is not None and waited >= wait_busy:
            raise err
        if on_wait:
            on_wait(err)
        if not _sleep(delay, should_stop):
            raise CameraError("stopped", "Ouverture de la webcam annulée.")
        delay = min(delay * 1.5, 3.0)


class CameraHub:
    """La webcam de Nova. Une seule session de suivi à la fois : une nouvelle session attend que la précédente
    ait rendu la webcam (avant, un redémarrage du suivi tombait sur sa propre webcam encore ouverte et affichait
    « webcam introuvable ou déjà utilisée » alors qu'aucune autre application ne l'utilisait).
    Mode « réserver » : la webcam reste ouverte entre deux sessions de suivi et jusqu'à la fermeture de Nova,
    si bien qu'aucune autre application ne peut la prendre pendant que Nova est lancé."""

    def __init__(self):
        self._owner = threading.Lock()      # tenu par la session de suivi en cours
        self._lock = threading.RLock()      # protège _cap
        self._cap = None
        self._key = None
        self._flag = None
        self.backend = ""
        self.index = None
        atexit.register(self.shutdown)

    def acquire(self, should_stop, timeout: float = 15.0) -> bool:
        end = time.monotonic() + timeout
        while not self._owner.acquire(timeout=0.1):
            if should_stop() or time.monotonic() > end:
                return False
        return True

    def open(self, key: tuple, opener):
        """Webcam déjà ouverte avec les mêmes réglages : réutilisée telle quelle ; sinon ouverte par opener()."""
        with self._lock:
            if self._cap is not None and self._key == key and self._cap.isOpened():
                return self._cap
            self._close()
            cap, backend, index = opener()
            self._cap, self._key, self.backend, self.index = cap, (index,) + tuple(key[1:]), backend, index
            if sys.platform == "win32":
                try:
                    from . import camusers
                    self._flag = camusers.flag_camera_held()
                except Exception:  # noqa: BLE001
                    self._flag = None
            log.info("Webcam %s ouverte (%s, %dx%d)", index, backend,
                     int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
            return cap

    def give_back(self, keep: bool) -> None:
        """Fin d'une session de suivi : la webcam est gardée (mode réserver) ou rendue à Windows."""
        if not keep:
            self.close()
        self._owner.release()

    def close(self) -> None:
        with self._lock:
            self._close()

    def _close(self):
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:  # noqa: BLE001
                pass
            log.info("Webcam rendue à Windows")
        self._cap = self._key = None
        if self._flag is not None:
            try:
                from . import camusers
                camusers.release_flag(self._flag)
            except Exception:  # noqa: BLE001
                pass
            self._flag = None

    @property
    def is_open(self) -> bool:
        return self._cap is not None

    def shutdown(self) -> None:
        """Rend la webcam si aucune session de suivi ne l'utilise (fermeture de Nova, réservation désactivée)."""
        if self._owner.acquire(timeout=2.0):
            try:
                self.close()
            finally:
                self._owner.release()


CAMERA = CameraHub()


def release_camera() -> None:
    """Rend la webcam à Windows quand le suivi est arrêté (à appeler si la réservation est désactivée en cours de route)."""
    CAMERA.shutdown()


class FrameGrabber(threading.Thread):
    """Lit la webcam en continu dans son propre fil : l'analyse prend toujours l'image la plus récente, sans
    attendre la webcam pendant ses calculs (moins de latence, cadence plus régulière)."""

    def __init__(self, cap):
        super().__init__(daemon=True, name="nova-webcam")
        self.cap = cap
        self._cond = threading.Condition()
        self._stop = False
        self.frame, self.t, self.seq = None, time.monotonic(), 0

    def run(self):
        token = perf.boost_thread("Capture")
        try:
            self._loop()
        finally:
            perf.unboost_thread(token)

    def _loop(self):
        while not self._stop:
            try:
                ok, frame = self.cap.read()
            except cv2.error:
                ok, frame = False, None
            if ok and frame is not None and frame.size:
                with self._cond:
                    self.frame, self.t = frame, time.monotonic()
                    self.seq += 1
                    self._cond.notify_all()
            else:
                time.sleep(0.03)

    def latest(self, after: int, timeout: float = 0.5):
        """(n°, image, heure) de la prochaine image après « after », ou None si rien n'est arrivé à temps."""
        with self._cond:
            self._cond.wait_for(lambda: self.seq != after or self._stop, timeout)
            if self.seq == after or self.frame is None:
                return None
            return self.seq, self.frame, self.t

    def stop(self):
        self._stop = True
        with self._cond:
            self._cond.notify_all()
        self.join(2.0)


class LowLight:
    """Pièce sombre (stream le soir, éclairage RGB) : éclaircit l'image avant l'analyse pour que le visage, les
    yeux et la bouche restent détectés. Courbe gamma recalculée deux fois par seconde (≈1 ms par image)."""

    TARGET, DARK = 115.0, 85.0

    def __init__(self):
        self.lut = None
        self.level = 255.0
        self._t = 0.0

    def __call__(self, frame, now: float):
        if now - self._t > 0.5:
            self._t = now
            self.level = float(cv2.cvtColor(frame[::8, ::8], cv2.COLOR_BGR2GRAY).mean())
            if self.level < self.DARK:
                g = math.log(self.TARGET / 255) / math.log(max(self.level, 8.0) / 255)
                g = clamp(g, 0.35, 1.0)
                self.lut = np.clip(((np.arange(256) / 255.0) ** g) * 255 + 0.5, 0, 255).astype(np.uint8)
            else:
                self.lut = None
        return cv2.LUT(frame, self.lut) if self.lut is not None else frame

    @property
    def active(self) -> bool:
        return self.lut is not None


class SpikeGuard:
    """Rejette une mesure aberrante isolée (tête qui « saute » de 40° sur une seule image : reflet, main devant
    le visage…) ; un vrai mouvement rapide est confirmé à l'image suivante et passe."""

    LIMITS = (40.0, 32.0, 32.0)      # yaw, pitch, roll (degrés par image)

    def __init__(self):
        self.last = None
        self.suspect = False

    def ok(self, yaw: float, pitch: float, roll: float) -> bool:
        cur = (yaw, pitch, roll)
        if self.last is None:
            self.last = cur
            return True
        jump = any(abs(a - b) > lim for a, b, lim in zip(cur, self.last, self.LIMITS))
        if jump and not self.suspect:
            self.suspect = True
            return False
        self.suspect = False
        self.last = cur
        return True

    def reset(self):
        self.last, self.suspect = None, False


class CameraWorker(QThread):
    frame_ready = Signal(QImage)
    state_ready = Signal(dict)
    status = Signal(str, str)      # (code, message lisible)
    stats = Signal(float, float)   # fps, latence ms
    _privacy_shown = False         # la page « Confidentialité > Caméra » n'est ouverte qu'une fois

    def __init__(self, profile: dict):
        super().__init__()
        self.profile = profile
        self.mapper = FaceMapper(profile["tracking"], profile["expressions"])
        self._running = True
        self.preview_enabled = True
        self.calibrate_requested = False
        self.error_code = ""       # dernier problème de webcam : denied | nova | busy | failed
        self.blockers = []         # applications qui occupent la webcam (pour « Libérer la webcam »)

    def stop(self):
        self._running = False

    def _stopping(self) -> bool:
        return not self._running

    def _settings(self) -> tuple[int, int, int, int]:
        cam_cfg = self.profile["camera"]
        try:
            idx = int(cam_cfg.get("index", 0))
        except (TypeError, ValueError):
            idx = 0
        try:
            w, h = (int(x) for x in str(cam_cfg.get("resolution", "640x480")).lower().split("x"))
        except ValueError:
            w, h = 640, 480
        if (self.profile["tracking"].get("body") or cam_cfg.get("hd_tracking", True)) and w * h < 1280 * 720:
            # visage (et mains) plus grands en pixels : points du visage, iris et paupières nettement plus précis ;
            # une webcam trop lente en HD est automatiquement ramenée à 640x480 (voir _try_open)
            w, h = 1280, 720
        return idx, w, h, max(30, int(cam_cfg.get("fps_limit", 30)))

    def _fail(self, e: CameraError):
        self.error_code, self.blockers = e.code, e.apps
        log.warning("Webcam : %s", e)
        self._say("error", str(e))
        if e.code == "denied" and not CameraWorker._privacy_shown:
            CameraWorker._privacy_shown = True
            try:
                from . import camusers
                camusers.open_privacy_settings()
            except Exception:  # noqa: BLE001
                pass

    def _open(self, idx: int, w: int, h: int, fps: int):
        """Ouvre (ou reprend) la webcam ; si elle est occupée, attend qu'elle se libère (mode réserver) et la
        libère d'office si l'option « auto_free » est activée. Lève CameraError."""
        cam_cfg = self.profile["camera"]
        reserve = bool(cam_cfg.get("reserve", True))
        freed, last = set(), [""]

        def waiting(err: CameraError):
            self.blockers = err.apps
            if err.code in ("busy", "nova"):
                todo = [a for a in err.apps if (a["exe"], a["name"]) not in freed]
                if cam_cfg.get("auto_free") and todo:
                    from . import camusers
                    freed.update((a["exe"], a["name"]) for a in todo)
                    msg = "Libération de la webcam : " + " ; ".join(camusers.close_apps(todo))
                else:
                    msg = f"{err} Nova prendra la webcam dès qu'elle sera libre…" if reserve else f"{err} Nouvel essai…"
            else:
                msg = f"Ouverture de la caméra {idx}… (nouvel essai)"
            if msg != last[0]:
                last[0] = msg
                self._say("starting", msg)

        cap = CAMERA.open((idx, w, h, fps), lambda: open_camera(
            idx, w, h, fps, should_stop=self._stopping, on_wait=waiting, wait_busy=None if reserve else 8.0))
        if CAMERA.index is not None and CAMERA.index != idx:
            self._say("starting", f"La caméra {idx} ne répond pas : Nova utilise la caméra {CAMERA.index}.")
            cam_cfg["index"] = CAMERA.index
        self.error_code, self.blockers = "", []
        return cap

    def _reconnect(self, idx: int, w: int, h: int, fps: int):
        """La webcam ne renvoie plus d'image (câble, mise en veille USB, autre application) : on la rouvre."""
        self._say("starting", "La webcam ne répond plus : reconnexion…")
        CAMERA.close()
        try:
            cap = self._open(idx, w, h, fps)
        except CameraError as e:
            if e.code != "stopped":
                self._fail(e)
            return None
        self._say("running", "Webcam active")
        return cap

    def _say(self, code: str, message: str):
        self.status.emit(code, message)
        if code in ("starting", "running", "error", "stopped"):
            health.report("tracking", state=code, message=message)

    def run(self):
        token = perf.boost_thread("Games")       # suivi prioritaire même quand un jeu est au premier plan
        try:
            self._run()
        finally:
            perf.unboost_thread(token)

    def _run(self):
        idx, w, h, fps = self._settings()
        reserve = bool(self.profile["camera"].get("reserve", True))
        stream_cfg = self.profile.get("stream") or {}
        autopause.AUTO.configure(stream_cfg)
        try:
            link = obsws.ensure_started()
            link.password_override = stream_cfg.get("obs_password", "") or ""
            autopause.AUTO.link = link
        except Exception as e:  # noqa: BLE001
            log.warning("Liaison OBS : %s", e)
        self._say("starting", f"Ouverture de la caméra {idx}…")
        try:
            tracker = FaceTracker()
        except Exception as e:  # noqa: BLE001
            log.exception("Tracking indisponible")
            self._say("error", f"Tracking indisponible : {e}")
            return
        if not CAMERA.acquire(self._stopping):
            tracker.close()
            if self._running:
                self._say("error", "La session de suivi précédente ne rend pas la webcam. Réessayez dans quelques secondes.")
            return
        try:
            cap = self._open(idx, w, h, fps)
        except CameraError as e:
            CAMERA.give_back(keep=False)
            tracker.close()
            if e.code != "stopped":
                self._fail(e)
            return
        except Exception as e:  # noqa: BLE001
            CAMERA.give_back(keep=False)
            tracker.close()
            log.exception("Ouverture de la webcam")
            self._say("error", f"Erreur d'ouverture de la webcam : {e}")
            return
        idx = CAMERA.index if CAMERA.index is not None else idx
        cam_cfg = self.profile["camera"]
        grabber = FrameGrabber(cap)
        grabber.start()
        light, guard = LowLight(), SpikeGuard()
        self._hold = None
        auto_said = self.mapper.auto_calibrated
        body = None
        healthy = True
        self._say("running", "Webcam active")
        health.report("tracking", camera=f"{idx} ({CAMERA.backend})", face=False, fps=0.0, latency=0.0, light="")
        frames, t_fps, last_face = 0, time.monotonic(), time.monotonic()
        face_lost = True
        last_preview = 0.0
        latency = 0.0
        seq = 0
        try:
            while self._running:
                t0 = time.monotonic()
                got = grabber.latest(seq, 0.5)
                if got is None:
                    if time.monotonic() - grabber.t > 2.0:      # plus aucune image depuis 2 s : on reconnecte
                        grabber.stop()
                        cap = self._reconnect(idx, w, h, fps)
                        if cap is None:
                            healthy = False
                            if self._running and self.error_code not in ("busy", "nova", "denied", "failed"):
                                self._say("error", "La webcam ne renvoie plus d'image (débranchée ?).")
                            break
                        grabber = FrameGrabber(cap)
                        grabber.start()
                        seq = 0
                    continue
                seq, frame, t_frame = got
                t_proc = time.perf_counter()   # latence = traitement seul (sans l'attente de l'image suivante)
                if cam_cfg.get("mirror", True):
                    frame = cv2.flip(frame, 1)
                frame = light(frame, t_frame)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                det = tracker.detect(rgb, t_frame)
                if det and not guard.ok(*det[:3]):
                    det = self._hold          # mesure aberrante isolée : on garde la précédente
                arms, dots = None, []
                if self.profile["tracking"].get("body"):
                    if body is None:
                        body = BodyProcess()
                    if body.error:
                        self._say("error", f"Suivi des bras/mains indisponible : {body.error}")
                        self.profile["tracking"]["body"] = False
                    else:
                        body.submit(rgb)
                        arms, dots = body.result
                elif body is not None:
                    body.stop()
                    body = None
                now = time.monotonic()
                autopause.AUTO.feed(bool(det), now)
                if det:
                    self._hold = det
                    yaw, pitch, roll, bs, lms, pos = det
                    out = self.mapper.map(yaw, pitch, roll, bs, t=t_frame, lms=lms, pos=pos,
                                          aspect=rgb.shape[1] / rgb.shape[0])
                    self.state_ready.emit({**out, "expr": dict(out["expr"]), "lost": False, "arms": arms})
                    last_face = now
                    if face_lost:
                        face_lost = False
                        guard.reset()
                        self._say("face", "Visage détecté")
                    if self.mapper.auto_calibrated and not auto_said:
                        auto_said = True
                        self._say("face", "Visage détecté — calibrage automatique fait (pose neutre apprise)")
                elif not face_lost and now - last_face > 0.5:
                    face_lost = True
                    self.state_ready.emit({"lost": True, "arms": arms})
                    self._say("noface", "Visage perdu — replacez-vous face à la caméra")
                elif face_lost and arms is not None:
                    self.state_ready.emit({"lost": True, "arms": arms})
                latency = (time.perf_counter() - t_proc) * 1000
                # aperçu réduit (une seule copie, ~25 i/s max)
                # aperçu : 25 i/s, 8 i/s pendant un jeu (le jeu cache la fenêtre ; le suivi, lui, reste à pleine vitesse)
                if self.preview_enabled and now - last_preview > (0.12 if perf.game_active() else 0.04):
                    last_preview = now
                    # on dessine sur la copie réduite : l'image d'origine est peut-être en cours d'analyse (bras)
                    small = cv2.resize(rgb, (480, int(480 * rgb.shape[0] / rgb.shape[1])))
                    hh, ww = small.shape[:2]
                    if det:
                        for i in range(0, len(det[4]), 6):
                            p = det[4][i]
                            cv2.circle(small, (int(p.x * ww), int(p.y * hh)), 1, (140, 120, 255), -1)
                    for x, y in dots:
                        cv2.circle(small, (int(x * ww), int(y * hh)), 3, (80, 170, 255), -1)
                    img = QImage(small.data, small.shape[1], small.shape[0], small.strides[0], QImage.Format_RGB888).copy()
                    self.frame_ready.emit(img)
                frames += 1
                if now - t_fps >= 1.0:
                    rate = frames / (now - t_fps)
                    self.stats.emit(rate, latency)
                    health.report("tracking", fps=round(rate, 1), latency=round(latency, 1), face=not face_lost,
                                  light="pièce sombre : image éclaircie" if light.active else "")
                    frames, t_fps = 0, now
                # limitation des FPS
                budget = 1.0 / max(5, int(cam_cfg.get("fps_limit", 30)))
                rest = budget - (time.monotonic() - t0)
                if rest > 0:
                    self.msleep(int(rest * 1000))
        except Exception as e:  # noqa: BLE001
            healthy = False
            log.exception("Erreur dans la boucle de capture")
            self._say("error", f"Erreur de capture : {e}")
        finally:
            grabber.stop()
            autopause.AUTO.tracking_stopped()
            # mode « réserver » : la webcam reste à Nova jusqu'à sa fermeture (aucune autre application ne la prend)
            CAMERA.give_back(keep=healthy and reserve and bool(cam_cfg.get("reserve", True)))
            tracker.close()
            if body is not None:
                body.stop()
            health.report("tracking", face=False, fps=0.0)
            self._say("stopped", "Webcam arrêtée")
