"""Point d'entrée d'Onde.exe (construit par PyInstaller avec onde.spec).
En développement, on lance plutôt :  python -m onde"""
import multiprocessing
import sys


def _prepare() -> None:
    """Onde.exe sous Windows n'a pas de console : print() et les journaux vont dans un fichier, et
    les programmes lancés en arrière-plan (ffmpeg…) n'ouvrent pas de fenêtre noire."""
    if sys.stdout is None or sys.stderr is None:
        from onde import config

        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        log = config.DATA_DIR / "onde.log"
        try:
            if log.exists() and log.stat().st_size > 5_000_000:
                log.replace(log.with_suffix(".log.1"))
        except OSError:
            pass
        stream = open(log, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
        if sys.platform == "win32":
            import subprocess

            original = subprocess.Popen.__init__

            def quiet(self, *args, **kwargs):
                kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x08000000  # CREATE_NO_WINDOW
                original(self, *args, **kwargs)

            subprocess.Popen.__init__ = quiet


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if not getattr(sys, "frozen", False):  # Onde installé : pythonw onde_app.py (pas de console)
        import os
        os.environ.setdefault("ONDE_LAUNCHER", "onde_app")
        if sys.stdout is None:
            os.environ.setdefault("ONDE_WINDOWED", "1")
    _prepare()
    try:
        from onde.__main__ import main

        main()
    except SystemExit:
        raise
    except BaseException:
        import traceback

        text = traceback.format_exc()
        print(text, file=sys.stderr)
        try:
            from onde import tray

            tray.alert("Onde n'a pas pu démarrer.\n\n" + text.strip().splitlines()[-1]
                       + "\n\nDétails dans le fichier onde.log (dossier .local\\share\\onde de ton profil).",
                       error=True)
        except Exception:
            pass
        sys.exit(1)
