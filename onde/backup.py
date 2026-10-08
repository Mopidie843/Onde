"""Sauvegarde automatique de la base d'Onde (profils, likes, playlists, historique, réglages, paroles).

Une fois par jour, une copie cohérente de `onde.db` est écrite dans le dossier choisi (par défaut
`sauvegardes/` dans les données d'Onde ; idéalement un autre disque ou un dossier synchronisé : Google
Drive, Dropbox, OneDrive…). Avec une phrase secrète, la copie est chiffrée (AES-256-GCM, clé dérivée par
scrypt) : sans elle, personne ne peut la lire. Les 14 dernières sont gardées.

Restaurer : Réglages → Sauvegarde automatique → Restaurer (Onde redémarre), ou à la main :
    python -m onde.backup ouvrir FICHIER [phrase-secrète]   → écrit FICHIER.db
"""
from __future__ import annotations

import gzip
import logging
import os
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import config, db

log = logging.getLogger("onde.backup")
MAGIC = b"ONDEBK1\n"
KEEP = 14
EVERY = 24 * 3600
_stop = threading.Event()
_lock = threading.Lock()
status = {"last": None, "error": None, "running": False}


def _meta(key: str, default: str = "") -> str:
    r = db.q1("SELECT v FROM meta WHERE k=?", f"backup:{key}")
    return r["v"] if r else default


def _set_meta(key: str, value: str) -> None:
    db.x("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", f"backup:{key}", value)


def folder() -> Path:
    return Path(_meta("dir") or (config.DATA_DIR / "sauvegardes"))


def settings_view() -> dict:
    return {"dir": str(folder()), "custom": bool(_meta("dir")), "encrypted": bool(_meta("pass")),
            "auto": _meta("auto", "1") == "1", "canEncrypt": _crypto() is not None, "keep": KEEP,
            "files": files(), **{k: status[k] for k in ("last", "error", "running")}}


def configure(*, directory: str | None = None, passphrase: str | None = None, auto: bool | None = None) -> dict:
    if directory is not None:
        d = directory.strip()
        if d:
            p = Path(os.path.expanduser(d))
            try:
                p.mkdir(parents=True, exist_ok=True)
                probe = p / ".onde-test"
                probe.write_text("ok")
                probe.unlink()
            except OSError as exc:
                raise ValueError(f"Dossier inutilisable : {exc}")
            d = str(p.resolve())
        _set_meta("dir", d)
    if passphrase is not None:
        if passphrase and len(passphrase) < 8:
            raise ValueError("Phrase secrète : 8 caractères au moins.")
        if passphrase and _crypto() is None:
            raise ValueError("Chiffrement indisponible : installe le paquet « cryptography » (pip install cryptography).")
        _set_meta("pass", passphrase)
    if auto is not None:
        _set_meta("auto", "1" if auto else "0")
    return settings_view()


def _crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM
    except Exception:
        return None


def _key(passphrase: str, salt: bytes) -> bytes:
    import hashlib
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def encrypt(data: bytes, passphrase: str) -> bytes:
    AESGCM = _crypto()
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    return MAGIC + salt + nonce + AESGCM(_key(passphrase, salt)).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes, passphrase: str) -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError("Ce n'est pas une sauvegarde chiffrée d'Onde")
    AESGCM = _crypto()
    if AESGCM is None:
        raise ValueError("Paquet « cryptography » nécessaire pour ouvrir cette sauvegarde")
    salt, nonce, body = blob[8:24], blob[24:36], blob[36:]
    try:
        return AESGCM(_key(passphrase, salt)).decrypt(nonce, body, MAGIC)
    except Exception as exc:
        raise ValueError("Phrase secrète incorrecte (ou fichier abîmé)") from exc


def _snapshot() -> bytes:
    """Copie cohérente de la base, même pendant qu'Onde s'en sert (API de sauvegarde de SQLite)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "onde.db"
        dst = sqlite3.connect(out)
        try:
            db.connect().backup(dst)
        finally:
            dst.close()
        return gzip.compress(out.read_bytes(), 6)


def files() -> list[dict]:
    d = folder()
    if not d.is_dir():
        return []
    out = [{"name": f.name, "size": f.stat().st_size, "at": f.stat().st_mtime, "encrypted": f.suffix == ".ondebk"}
           for f in d.glob("onde-*.ondebk")] + \
          [{"name": f.name, "size": f.stat().st_size, "at": f.stat().st_mtime, "encrypted": False}
           for f in d.glob("onde-*.db.gz")]
    return sorted(out, key=lambda x: -x["at"])


def run(force: bool = False) -> dict:
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "Sauvegarde déjà en cours"}
    try:
        status.update(running=True, error=None)
        data = _snapshot()
        pw = _meta("pass")
        d = folder()
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H%M")
        if pw:
            target, blob = d / f"onde-{stamp}.ondebk", encrypt(data, pw)
        else:
            target, blob = d / f"onde-{stamp}.db.gz", data
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, target)
        for old in files()[KEEP:]:
            (d / old["name"]).unlink(missing_ok=True)
        status["last"] = time.time()
        _set_meta("last", str(status["last"]))
        log.info("sauvegarde écrite : %s (%d Ko)", target, len(blob) // 1024)
        return {"ok": True, "file": target.name}
    except Exception as exc:
        status["error"] = str(exc)[:200]
        log.warning("sauvegarde impossible : %s", exc)
        return {"ok": False, "error": status["error"]}
    finally:
        status["running"] = False
        _lock.release()


def open_file(path: Path, passphrase: str = "") -> bytes:
    """Le contenu de la base (fichier SQLite) d'une sauvegarde."""
    blob = path.read_bytes()
    if blob.startswith(MAGIC):
        blob = decrypt(blob, passphrase)
    raw = gzip.decompress(blob)
    if not raw.startswith(b"SQLite format 3\x00"):
        raise ValueError("Fichier de sauvegarde invalide")
    return raw


def stage_restore(name: str, passphrase: str = "") -> None:
    """Prépare la restauration : la base est remplacée au prochain démarrage (l'actuelle est gardée à côté)."""
    if "/" in name or "\\" in name or not name.startswith("onde-"):
        raise ValueError("Nom de sauvegarde invalide")
    path = folder() / name
    if not path.is_file():
        raise ValueError("Sauvegarde introuvable")
    raw = open_file(path, passphrase or _meta("pass"))
    pending = config.DB_PATH.with_name("onde.db.restore")
    pending.write_bytes(raw)


def apply_pending_restore() -> bool:
    """Au démarrage, avant d'ouvrir la base : applique une restauration préparée."""
    pending = config.DB_PATH.with_name("onde.db.restore")
    if not pending.is_file():
        return False
    keep = config.DB_PATH.with_name(f"onde-avant-restauration-{time.strftime('%Y%m%d-%H%M%S')}.db")
    if config.DB_PATH.is_file():
        os.replace(config.DB_PATH, keep)
    for ext in ("-wal", "-shm"):
        Path(str(config.DB_PATH) + ext).unlink(missing_ok=True)
    os.replace(pending, config.DB_PATH)
    log.warning("base restaurée depuis une sauvegarde (l'ancienne : %s)", keep.name)
    return True


def _loop() -> None:
    if _stop.wait(120):
        return
    while not _stop.is_set():
        try:
            last = float(_meta("last", "0") or 0)
            if _meta("auto", "1") == "1" and time.time() - last >= EVERY:
                run()
        except Exception:
            log.exception("sauvegarde automatique")
        if _stop.wait(3600):
            return


def start_background() -> None:
    _stop.clear()
    threading.Thread(target=_loop, name="onde-backup", daemon=True).start()


def stop() -> None:
    _stop.set()


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] in ("ouvrir", "open"):
        src = Path(argv[1])
        out = src.with_suffix(".db")
        out.write_bytes(open_file(src, argv[2] if len(argv) > 2 else ""))
        print(f"✔ base écrite : {out}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
