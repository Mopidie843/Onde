"""Reconnaissance des voyelles dans la voix (lip sync par phonèmes).
Méthode : enveloppe spectrale LPC de la voix (40 bandes sur une échelle perceptive) comparée à des modèles de voyelles
(a, i, ou, é, o) à plusieurs hauteurs de voix. Après calibrage, vos propres enveloppes servent de modèles."""
import math

import numpy as np

VOWELS = ("aa", "ih", "ou", "ee", "oh")
VOWEL_FR = {"aa": "A", "ih": "I", "ou": "OU", "ee": "É", "oh": "O"}
# formants moyens (Hz) des voyelles, voix d'adulte
DEFAULT_CENTROIDS = {"aa": (780.0, 1300.0), "ih": (320.0, 2250.0), "ou": (360.0, 1100.0),
                     "ee": (480.0, 1950.0), "oh": (500.0, 850.0)}
SCALES = (0.82, 0.92, 1.0, 1.1, 1.22)       # voix grave -> aiguë


def _mel(f):
    return 2595 * np.log10(1 + np.asarray(f) / 700)


def _imel(m):
    return 700 * (10 ** (np.asarray(m) / 2595) - 1)


GRID = _imel(np.linspace(_mel(150), _mel(3800), 40))   # fréquences d'analyse (Hz)


def downsample(x: np.ndarray, fs: float, target: float = 16000.0) -> tuple[np.ndarray, float]:
    factor = max(1, int(round(fs / target)))
    if factor > 1:
        x = np.convolve(x, np.ones(factor) / factor, mode="same")[::factor]   # passe-bas simple puis décimation
    return x, fs / factor


def lpc(x: np.ndarray, order: int) -> np.ndarray | None:
    """Coefficients LPC (méthode d'autocorrélation, Levinson-Durbin)."""
    r = np.correlate(x, x, mode="full")[len(x) - 1: len(x) + order]
    if r[0] <= 1e-9:
        return None
    a = np.zeros(order + 1)
    a[0] = 1.0
    err = r[0]
    for i in range(1, order + 1):
        acc = r[i] + np.dot(a[1:i], r[i - 1:0:-1])
        k = -acc / err
        a[1:i] = a[1:i] + k * a[i - 1:0:-1]
        a[i] = k
        err *= (1 - k * k)
        if err <= 1e-12:
            break
    return a


def _detrend(env: np.ndarray) -> np.ndarray:
    """Retire la pente spectrale (timbre, micro, volume) : seule la forme des résonances compte."""
    lf = np.log(GRID)
    p = np.polyfit(lf, env, 1)
    return env - np.polyval(p, lf)


def envelope(x: np.ndarray, fs: float) -> np.ndarray | None:
    """Enveloppe spectrale (dB, sans pente) d'une trame de voix de ~30-40 ms, ou None si trop faible."""
    x, fs = downsample(np.asarray(x, np.float64), fs)
    if len(x) < 200:
        return None
    x = x - x.mean()
    x = np.append(x[0], x[1:] - 0.97 * x[:-1])           # pré-accentuation
    x = x * np.hamming(len(x))
    a = lpc(x, 14)
    if a is None or not np.all(np.isfinite(a)):
        return None
    w = 2 * math.pi * GRID / fs
    resp = np.abs(np.exp(-1j * np.outer(w, np.arange(len(a)))) @ a)
    return _detrend(-20 * np.log10(resp + 1e-9))


def template(f1: float, f2: float, f3: float = 2700.0) -> np.ndarray:
    """Enveloppe théorique d'une voyelle (résonances F1, F2, F3)."""
    w = 2 * math.pi * GRID / 16000
    z1 = np.exp(-1j * w)
    env = np.zeros(len(GRID))
    for f, bw in ((f1, 90), (f2, 110), (f3, 160)):
        r = math.exp(-math.pi * bw / 16000)
        th = 2 * math.pi * f / 16000
        env -= 20 * np.log10(np.abs(1 - 2 * r * math.cos(th) * z1 + r * r * z1 * z1))
    return _detrend(env)


class VowelClassifier:
    def __init__(self, calibrated: dict | None = None):
        self.set_calibration(calibrated or {})

    def set_calibration(self, calibrated: dict):
        """calibrated : {voyelle: enveloppe (40 valeurs)} enregistrées sur la voix de l'utilisateur."""
        self.templates = {}
        for v, (f1, f2) in DEFAULT_CENTROIDS.items():
            env = calibrated.get(v)
            if env is not None and len(env) == len(GRID):
                self.templates[v] = [np.asarray(env, np.float64)]
            else:
                self.templates[v] = [template(f1 * s, f2 * s, 2700 * s) for s in SCALES]

    def weights(self, env: np.ndarray) -> dict:
        """Probabilité de chaque voyelle (somme = 1)."""
        d = {v: min(float(np.mean((env - t) ** 2)) for t in ts) for v, ts in self.templates.items()}
        dmin = min(d.values())
        tau = 0.35 * dmin + 4.0
        sc = {v: math.exp(-(d[v] - dmin) / tau) for v in VOWELS}
        total = sum(sc.values())
        return {v: sc[v] / total for v in VOWELS}


def synth_vowel(f1: float, f2: float, fs: float = 48000, f0: float = 120, dur: float = 0.05,
                f3: float = 2800) -> np.ndarray:
    """Voyelle de synthèse (impulsions glottiques + résonateurs) : sert aux tests."""
    n = int(fs * dur)
    src = np.zeros(n)
    src[:: int(fs / f0)] = 1.0
    y = src
    for f, bw in ((f1, 80), (f2, 100), (f3, 150)):
        r = math.exp(-math.pi * bw / fs)
        th = 2 * math.pi * f / fs
        b1, b2 = 2 * r * math.cos(th), -r * r
        out = np.zeros(n)
        for i in range(n):
            out[i] = y[i] + b1 * (out[i - 1] if i > 0 else 0) + b2 * (out[i - 2] if i > 1 else 0)
        y = out
    return y / (np.max(np.abs(y)) + 1e-9)
