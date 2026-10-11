# -*- mode: python ; coding: utf-8 -*-
"""Fabrique « Nova VTuber.exe » (dossier dist/Nova VTuber) : python -m PyInstaller --noconfirm packaging/nova.spec
Tout est rangé à côté du .exe (contents_directory='.') : les chemins de Nova (ROOT = dossier du programme) restent
valables, et l'installateur place le programme dans un dossier où l'on peut écrire (réglages, profils, avatars)."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

HERE = Path(SPECPATH)
PROJECT = HERE.parent
NAME = "Nova VTuber"

datas, binaries, hidden = [], [], []
for pkg in ("mediapipe",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hidden += h
hidden += collect_submodules("src") + ["setup_assets", "sounddevice", "psutil"]

# ressources de Nova : page de rendu, modèles de suivi, avatars et accessoires fournis
for folder in ("web", "assets", "avatars", "accessories", "backgrounds"):
    if (PROJECT / folder).is_dir():
        datas.append((str(PROJECT / folder), folder))
for extra in ("install.bat", "LISEZ-MOI.txt", "README.md"):
    if (PROJECT / extra).is_file():
        datas.append((str(PROJECT / extra), "."))

a = Analysis(
    [str(PROJECT / "main.py")],
    pathex=[str(PROJECT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    icon=str(HERE / "nova.ico"),
    console=False,                 # pas de fenêtre noire
    contents_directory=".",
    version=str(HERE / "version_info.txt") if (HERE / "version_info.txt").exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
