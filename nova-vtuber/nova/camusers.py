"""Trouver et fermer les applications qui occupent la webcam (une webcam ne peut servir qu'à une app à la fois).

Sources : 1) le registre de confidentialité de Windows (applications qui utilisent la caméra EN CE MOMENT) ;
          2) les applications ouvertes connues pour utiliser une webcam (OBS, Discord, Teams, Zoom, VTube Studio…).
Rien n'est fermé sans confirmation : fermeture normale d'abord, puis forcée si l'application ne répond pas."""
import logging
import os
import subprocess
import sys
import time
import winreg

import psutil

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
REG = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"


def _in_use_from_registry() -> set[str]:
    """Exécutables (minuscules) signalés par Windows comme utilisant la caméra maintenant."""
    found = set()
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
                            fam = name.split("_")[0].lower()
                            if fam in PACKAGED:
                                found.add(PACKAGED[fam])
                        else:
                            found.add(name.replace("#", "\\").lower())
    return found


def _protected_pids() -> set[int]:
    me = psutil.Process()
    pids = {me.pid}
    try:
        pids |= {c.pid for c in me.children(recursive=True)}
        if me.parent():
            pids.add(me.parent().pid)       # lanceur (python.exe de l'environnement virtuel)
    except psutil.Error:
        pass
    return pids


def find_camera_users() -> list[dict]:
    """Liste [{name, exe, pids, in_use, detail}] des applications à fermer potentiellement."""
    in_use = _in_use_from_registry()
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
            using_now = exe in in_use or name in in_use
            known = name in KNOWN
            cmd = " ".join(p.info["cmdline"] or []).lower()
            other_nova = name == "novavtuber.exe" or (name.startswith("python") and "main.py" in cmd and "vtuber" in cmd)
            if not (using_now or known or other_nova):
                continue
            key = "nova" if other_nova else (exe or name)
            label = "Nova VTuber (autre fenêtre ouverte)" if other_nova else KNOWN.get(name, os.path.basename(p.info["name"] or exe))
            app = apps.setdefault(key, {"name": label,
                                        "exe": exe, "pids": [], "in_use": False, "detail": ""})
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


if __name__ == "__main__" and sys.platform == "win32":
    for a in find_camera_users():
        print(a)
