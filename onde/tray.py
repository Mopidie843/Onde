"""Onde.exe sous Windows : pas de fenêtre noire, une petite icône près de l'horloge.

Clic ou double-clic sur l'icône = ouvrir Onde ; clic droit = menu (adresse pour le téléphone,
lancement au démarrage de Windows, quitter). Sans console, les messages importants (port déjà
pris, démarrage impossible…) s'affichent dans une boîte de dialogue.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import webbrowser
from typing import Callable

from . import config

log = logging.getLogger("onde.tray")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Onde"
BACKGROUND_FLAG = "--background"  # lancé par Windows au démarrage : on n'ouvre pas de page


def windowed() -> bool:
    """Onde.exe, ou Onde installé (pythonw, sans console), sous Windows."""
    return sys.platform == "win32" and (config.FROZEN or os.environ.get("ONDE_WINDOWED") == "1")


def alert(text: str, title: str = "Onde", error: bool = False) -> None:
    """Message à l'écran quand il n'y a pas de console pour écrire (et dans le journal)."""
    log.log(logging.ERROR if error else logging.INFO, "%s", text)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)
            return
        except Exception:  # pragma: no cover
            pass
    print(text, file=sys.stderr if error else sys.stdout)


# ------------------------------------------------------------------ démarrage avec Windows
def _autostart_command() -> str:
    return f'"{sys.executable}" {BACKGROUND_FLAG}'


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_NAME)[0] == _autostart_command()
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


# ------------------------------------------------------------------ icône
class Tray:
    def __init__(self, port: int, scheme: str, on_quit: Callable[[], None]):
        self.base = f"http://localhost:{port}"  # localhost : marche aussi quand le HTTPS est activé
        self.on_quit = on_quit
        self.icon = None
        self.thread: threading.Thread | None = None

    def open(self, *_args) -> None:
        webbrowser.open(self.base)

    def open_phone(self, *_args) -> None:
        webbrowser.open(self.base + "/#/settings")  # les Réglages affichent l'adresse et le QR code

    def toggle_autostart(self, *_args) -> None:
        try:
            set_autostart(not autostart_enabled())
        except OSError as exc:
            alert(f"Impossible de modifier le démarrage automatique : {exc}", error=True)

    def quit(self, *_args) -> None:
        self.stop()
        self.on_quit()

    def start(self) -> bool:
        try:
            import pystray
            from PIL import Image
            image = Image.open(config.STATIC_DIR / "icons" / "icon-192.png")
            menu = pystray.Menu(
                pystray.MenuItem("Ouvrir Onde", self.open, default=True),
                pystray.MenuItem("Adresse pour le téléphone…", self.open_phone),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Lancer Onde au démarrage de Windows", self.toggle_autostart,
                                 checked=lambda _item: autostart_enabled()),
                pystray.MenuItem("Quitter Onde", self.quit),
            )
            self.icon = pystray.Icon("Onde", image, "Onde", menu)
        except Exception:
            log.exception("icône de la zone de notification indisponible")
            return False
        self.thread = threading.Thread(target=self.icon.run, name="onde-tray", daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        icon, self.icon = self.icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:  # pragma: no cover
                pass
