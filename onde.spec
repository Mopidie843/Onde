# -*- mode: python ; coding: utf-8 -*-
# Recette PyInstaller d'Onde.exe — un seul fichier, à lancer d'un double-clic.
#   Windows :  build_exe.bat           (ou à la main :  pyinstaller --noconfirm --clean onde.spec)
#   Résultat : dist/Onde.exe  (sous Linux/macOS : dist/Onde, même recette)
# PyInstaller ne fait pas de « compilation croisée » : un .exe Windows se construit sous Windows.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)

# uvicorn charge sa boucle et ses protocoles par leur nom (texte) : PyInstaller ne les voit pas seul ;
# idem pour l'application, donnée à uvicorn sous la forme « onde.main:app »
# (liste tirée des fichiers : collect_submodules("onde") ne voit pas le projet depuis son sous-processus)
hidden = sorted(f"onde.{p.stem}" for p in (ROOT / "onde").glob("*.py") if p.stem != "__init__") + collect_submodules("uvicorn")
hidden += ["websockets", "wsproto", "httptools", "watchfiles", "cryptography", "segno", "yt_dlp"]
if sys.platform == "win32":
    hidden += collect_submodules("pystray")   # icône près de l'horloge

# icône Windows (.ico) fabriquée à partir de celle de l'appli web
icon = None
if sys.platform == "win32":
    from PIL import Image
    icon = str(ROOT / "build" / "onde.ico")
    (ROOT / "build").mkdir(exist_ok=True)
    Image.open(ROOT / "onde" / "static" / "icons" / "icon-512.png").save(
        icon, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

a = Analysis(
    [str(ROOT / "onde_app.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "onde" / "static"), "onde/static")],
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "_pytest", "IPython", "matplotlib", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Onde",
    console=False,         # Windows : pas de fenêtre noire (icône près de l'horloge, journal : onde.log)
    icon=icon,
    upx=False,             # UPX fait souvent réagir les antivirus à tort
    runtime_tmpdir=None,
)
