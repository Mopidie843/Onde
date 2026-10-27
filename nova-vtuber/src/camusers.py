"""Trouver et fermer les applications qui occupent la webcam (une webcam ne peut servir qu'à une app à la fois),
et diagnostiquer pourquoi elle ne s'ouvre pas.

Sources : 1) le registre de confidentialité de Windows (applications qui utilisent la caméra EN CE MOMENT) ;
          2) les applications ouvertes connues pour utiliser une webcam (OBS, Discord, Teams, Zoom, VTube Studio…) ;
          3) un marqueur Windows posé par Nova tant qu'il tient la webcam (repère une autre fenêtre de Nova).
Rien n'est fermé sans confirmation (sauf si l'option « libérer automatiquement » est activée dans le profil) :
fermeture normale d'abord, puis forcée si l'application ne répond pas."""
import ctypes
import logging
import os
import subprocess
import sys
import time
import winreg
from ctypes import wintypes

import psutil

from .settings import ROOT

log = logging.getLogger("nova.camusers")
KNOWN = {
    "obs64.exe": "OBS Studio", "obs32.exe": "OBS Studio", "discord.exe": "Discord", "discordptb.exe": "Discord PTB",
    "discordcanary.exe": "Discord Canary", "ms-teams.exe": "Microsoft Teams", "teams.exe": "Microsoft Teams",
    "zoom.exe": "Zoom", "skype.exe": "Skype", "windowscamera.exe": "Caméra Windows", "webex.exe": "Webex",
    "slack.exe": "Slack", "whatsapp.exe": "WhatsApp", "messenger.exe": "Messenger",
    "vtube studio.exe": "VTube Studio", "vseeface.exe": "VSeeFace", "warudo.exe": "Warudo", "vnyan.exe": "VNyan",
    "animaze.exe": "Animaze", "facerig.exe": "FaceRig", "3tene.exe": "3tene", "luppet.exe": "Luppet",
    "streamlabs obs.exe": "Streamlabs", "streamlabs desktop.exe": "Streamlabs", "xsplit.core.exe": "XSplit",
    "prismlivestudio.exe": "PRISM Live Studio", "nvidia broadcast.exe": "NVIDIA Broadcast", "camo.exe": "Camo",
    "manycam.exe": "ManyCam", "snapcamera.exe": "Snap Camera", "elgato camera hub.exe": "Elgato Camera Hub",
    "logi capture.exe": "Logi Capture", "camerahub.exe": "Camera Hub",
}
PACKAGED = {"microsoft.windowscamera": "windowscamera.exe", "msteams": "ms-teams.exe"}
NOVA_EXES = {"novavtuber.exe", "nova vtuber.exe", "nova-vtuber.exe", "nova.exe"}
# antivirus dont la « protection webcam » peut bloquer Nova sans rien afficher
WEBCAM_GUARDS = {
    "avp.exe": "Kaspersky", "avpui.exe": "Kaspersky", "ekrn.exe": "ESET", "egui.exe": "ESET",
    "avastsvc.exe": "Avast", "avastui.exe": "Avast", "avgsvc.exe": "AVG", "avgui.exe": "AVG",
    "bdagent.exe": "Bitdefender", "bdservicehost.exe": "Bitdefender", "vsserv.exe": "Bitdefender",
    "nortonsecurity.exe": "Norton", "norton360.exe": "Norton",
}
REG = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"
FLAG_NAME = "Local\\NovaVTuber.webcam"        # présent tant qu'une fenêtre de Nova tient la webcam
PRIVACY_URI = "ms-settings:privacy-webcam"


def _in_use_from_registry() -> tuple[set[str], set[str]]:
    """Ce que Windows signale comme utilisant la caméra maintenant :
    (exécutables en minuscules, familles d'applications du Microsoft Store en minuscules)."""
    exes, packages = set(), set()
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for sub, packaged in ((REG + r"\NonPackaged", False), (REG, True)):
            try:
                key = winreg.OpenKey(hive, sub)
            except OSError:
                continue
            with key:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    i += 1
                    if packaged and name == "NonPackaged":
                        continue
                    try:
                        with winreg.OpenKey(key, name) as k:
                            start = winreg.QueryValueEx(k, "LastUsedTimeStart")[0]
                            stop = winreg.QueryValueEx(k, "LastUsedTimeStop")[0]
                    except OSError:
                        continue
                    if start and not stop:
                        if packaged:
                            fam = name.lower()
                            packages.add(fam)
                            if fam.split("_")[0] in PACKAGED:
                                exes.add(PACKAGED[fam.split("_")[0]])
                        else:
                            exes.add(name.replace("#", "\\").lower())
    return exes, packages


def _packaged_match(exe: str, packages: set[str]) -> bool:
    """Exécutable d'une application du Store (…\\WindowsApps\\<nom>_<version>_<archi>__<éditeur>\\…) signalée par Windows."""
    if "\\windowsapps\\" not in exe:
        return False
    folder = exe.split("\\windowsapps\\", 1)[1].split("\\", 1)[0]
    for fam in packages:
        name, _, publisher = fam.rpartition("_")
        if name and folder.startswith(name + "_") and folder.endswith("_" + publisher):
            return True
    return False


def _protected_pids() -> set[int]:
    me = psutil.Process()
    pids = {me.pid}
    try:
        pids |= {c.pid for c in me.children(recursive=True)}
        if me.parent():
            pids.add(me.parent().pid)       # lanceur (python.exe de l'environnement virtuel, ou chargeur du .exe)
    except psutil.Error:
        pass
    return pids


def _is_other_nova(p, name: str, cmd: str) -> bool:
    """Autre fenêtre de Nova (version .exe ou lancée depuis les sources), y compris celle cachée près de l'horloge."""
    if name in NOVA_EXES:
        return True
    if getattr(sys, "frozen", False) and name == os.path.basename(sys.executable).lower():
        return True
    if not (name.startswith("python") and "main.py" in cmd):
        return False
    if "vtuber" in cmd or str(ROOT).lower() in cmd:
        return True
    try:
        return os.path.normcase(p.cwd()) == os.path.normcase(str(ROOT))
    except (psutil.Error, OSError):
        return False


def find_camera_users() -> list[dict]:
    """Liste [{name, exe, pids, in_use, detail}] des applications à fermer potentiellement."""
    in_use, packages = _in_use_from_registry()
    protected = _protected_pids()
    windir = os.environ.get("WINDIR", r"C:\Windows").lower()
    apps: dict = {}
    for p in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if p.pid in protected or p.pid <= 4:
                continue
            name = (p.info["name"] or "").lower()
            exe = (p.info["exe"] or "").lower()
            if exe.startswith(windir):
                continue                            # composants de Windows : jamais fermés
            using_now = exe in in_use or name in in_use or _packaged_match(exe, packages)
            known = name in KNOWN
            cmd = " ".join(p.info["cmdline"] or []).lower()
            other_nova = _is_other_nova(p, name, cmd)
            if not (using_now or known or other_nova):
                continue
            key = "nova" if other_nova else (exe or name)
            label = "Nova VTuber (autre fenêtre ouverte)" if other_nova else KNOWN.get(name, os.path.basename(p.info["name"] or exe))
            app = apps.setdefault(key, {"name": label,
                                        "exe": exe, "pids": [], "in_use": False, "detail": "", "nova": other_nova})
            app["pids"].append(p.pid)
            app["in_use"] = app["in_use"] or using_now
            if name.startswith("python") and p.info["cmdline"]:
                app["detail"] = " ".join(p.info["cmdline"][1:3])[:80]
        except psutil.Error:
            continue
    return sorted(apps.values(), key=lambda a: (not a["in_use"], a["name"].lower()))


def close_apps(apps: list[dict], grace: float = 3.0) -> list[str]:
    """Ferme les applications choisies : fermeture normale, puis forcée après « grace » secondes."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    pids = [pid for a in apps for pid in a["pids"] if psutil.pid_exists(pid)]
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, creationflags=flags)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(psutil.pid_exists(p) for p in pids):
        time.sleep(0.2)
    forced = [p for p in pids if psutil.pid_exists(p)]
    for pid in forced:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=flags)
    time.sleep(0.5)
    report = []
    for a in apps:
        alive = [p for p in a["pids"] if psutil.pid_exists(p)]
        report.append(f"{'✖ toujours ouvert' if alive else '✔ fermé'} : {a['name']}"
                      + (" (fermeture forcée)" if not alive and any(p in forced for p in a["pids"]) else ""))
        log.info("Libération webcam : %s -> %s", a["name"], "toujours ouvert" if alive else "fermé")
    return report


# ---------------------------------------------------------------- diagnostic
def _reg_value(hive, path: str, name: str):
    try:
        with winreg.OpenKey(hive, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def camera_blocked() -> str:
    """Raison lisible si Windows interdit la caméra aux logiciels comme Nova, sinon ""."""
    hklm, hkcu = winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER
    if _reg_value(hklm, r"SOFTWARE\Policies\Microsoft\Camera", "AllowCamera") == 0:
        return "la caméra est désactivée par une stratégie de l'administrateur du PC"
    if _reg_value(hklm, r"SOFTWARE\Policies\Microsoft\Windows\AppPrivacy", "LetAppsAccessCamera") == 2:
        return "l'accès à la caméra est interdit par une stratégie de l'administrateur du PC"
    if str(_reg_value(hklm, REG, "Value")).lower() == "deny":
        return "« Accès à la caméra » est désactivé pour tout le PC"
    if str(_reg_value(hkcu, REG, "Value")).lower() == "deny":
        return "« Accès à la caméra » est désactivé"
    if str(_reg_value(hkcu, REG + r"\NonPackaged", "Value")).lower() == "deny":
        return "« Autoriser les applications de bureau à accéder à votre caméra » est désactivé"
    return ""


def open_privacy_settings() -> None:
    """Ouvre la page Paramètres > Confidentialité > Caméra de Windows."""
    try:
        os.startfile(PRIVACY_URI)  # type: ignore[attr-defined]
    except OSError as e:
        log.warning("Paramètres de confidentialité : %s", e)


def webcam_guards() -> list[str]:
    """Antivirus ouverts qui ont une « protection webcam » (ils peuvent bloquer Nova en silence)."""
    found = set()
    for p in psutil.process_iter(["name"]):
        n = (p.info["name"] or "").lower()
        if n in WEBCAM_GUARDS:
            found.add(WEBCAM_GUARDS[n])
    return sorted(found)


# ---------------------------------------------------------------- marqueur « Nova tient la webcam »
def _k32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenMutexW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def flag_camera_held():
    """Pose le marqueur (à garder tant que la webcam est ouverte). Retourne un jeton pour release_flag()."""
    try:
        return _k32().CreateMutexW(None, False, FLAG_NAME) or None
    except (OSError, AttributeError):
        return None


def release_flag(token) -> None:
    if token:
        try:
            _k32().CloseHandle(token)
        except (OSError, AttributeError):
            pass


def other_nova_holds_camera() -> bool:
    """Vrai si une autre fenêtre de Nova a la webcam (à appeler quand cette fenêtre-ci ne l'a pas)."""
    try:
        k32 = _k32()
        h = k32.OpenMutexW(0x00100000, False, FLAG_NAME)     # SYNCHRONIZE
    except (OSError, AttributeError):
        return False
    if h:
        k32.CloseHandle(h)
        return True
    return False


if __name__ == "__main__" and sys.platform == "win32":
    print("Blocage Windows :", camera_blocked() or "aucun")
    print("Antivirus avec protection webcam :", ", ".join(webcam_guards()) or "aucun")
    print("Autre fenêtre de Nova avec la webcam :", other_nova_holds_camera())
    for a in find_camera_users():
        print(a)
