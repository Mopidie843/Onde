"""Fabrique le zip de mise à jour d'Onde : onde-tout.zip + onde.json (version, adresse, empreinte SHA-256).

Usage (après avoir commité ton code) :   python make_update.py
Puis :   git add onde-tout.zip onde.json && git commit -m "Zip de mise à jour" && git push

Dans Réglages → Mise à jour, la source est l'adresse affichée à la fin (celle de onde.json).
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = sys.argv[1] if len(sys.argv) > 1 else "Mopidie843/Onde"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def main() -> None:
    if git("status", "--porcelain", "--untracked-files=no"):
        print("Des changements ne sont pas commités : le zip contient le dernier commit seulement.", file=sys.stderr)
    version = re.search(r'^VERSION\s*=\s*"([^"]+)"', (ROOT / "onde" / "config.py").read_text(encoding="utf-8"), re.M).group(1)
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    zip_path = ROOT / "onde-tout.zip"
    git("archive", "--format=zip", "--prefix=onde/", "-o", str(zip_path), "HEAD")  # (dossier « onde/ » à la racine, comme l'attend la mise à jour)
    sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    base = f"https://raw.githubusercontent.com/{REPO}/{branch}"
    manifest = {"version": version, "url": f"{base}/onde-tout.zip", "sha256": sha, "notes": ""}
    (ROOT / "onde.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Onde {version} : onde-tout.zip ({zip_path.stat().st_size // 1024} Ko) et onde.json prêts.")
    print(f"Source de mise à jour à mettre dans Onde :\n  {base}/onde.json")


if __name__ == "__main__":
    main()
