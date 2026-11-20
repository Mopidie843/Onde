"""Scrobbling ListenBrainz (ou serveur compatible : Maloja, Koito…), par profil.

Chaque profil colle son jeton (listenbrainz.org → Paramètres) dans Réglages. Une écoute (30 s, comme dans les
statistiques) est envoyée en tâche de fond ; si le serveur ne répond pas, elle attend dans une petite file et
repart à la prochaine écoute (3 essais au plus). Rien ne part sans jeton. Le jeton est gardé dans la base d'Onde
(comme les mots de passe de mail) et n'est jamais renvoyé aux pages.
"""
from __future__ import annotations

import json
import logging
import re
import threading

from . import db, net

log = logging.getLogger("onde.scrobble")

DEFAULT_URL = "https://api.listenbrainz.org"
MAX_TRIES = 3
BATCH = 50
_lock = threading.Lock()


def _prefs(uid: int) -> dict:
    return {r["k"]: r["v"] for r in db.q("SELECT k, v FROM user_prefs WHERE user_id=? AND k LIKE 'lb_%'", uid)}


def _set(uid: int, key: str, value: str | None) -> None:
    if value is None:
        db.x("DELETE FROM user_prefs WHERE user_id=? AND k=?", uid, key)
    else:
        db.x("INSERT OR REPLACE INTO user_prefs(user_id, k, v) VALUES(?,?,?)", uid, key, value)


def clean_url(url: str | None) -> str:
    url = (url or "").strip().rstrip("/") or DEFAULT_URL
    if not re.fullmatch(r"https://[^\s/]+(/[^\s]*)?", url):
        raise ValueError("L'adresse du serveur doit commencer par https://")
    return url


def status(uid: int) -> dict:
    p = _prefs(uid)
    pending = db.q1("SELECT COUNT(*) n FROM scrobble_queue WHERE user_id=?", uid)["n"]
    return {"enabled": bool(p.get("lb_token")), "user": p.get("lb_user") or None,
            "url": p.get("lb_url") or DEFAULT_URL, "pending": pending}


def save(uid: int, token: str, url: str | None = None) -> dict:
    """Vérifie le jeton auprès du serveur, puis le retient pour ce profil."""
    token = (token or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._\-]{8,128}", token):
        raise ValueError("Jeton invalide : copie-le tel quel depuis les paramètres de ListenBrainz.")
    url = clean_url(url)
    try:
        r = net.client().get(f"{url}/1/validate-token", headers={"Authorization": f"Token {token}"}, timeout=10)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as exc:
        raise ValueError(f"Serveur injoignable ({exc})") from exc
    if not data.get("valid"):
        raise ValueError("Ce jeton n'est pas accepté par le serveur.")
    with db.tx():
        _set(uid, "lb_token", token)
        _set(uid, "lb_url", url)
        _set(uid, "lb_user", str(data.get("user_name") or ""))
    return status(uid)


def forget(uid: int) -> None:
    with db.tx():
        for k in ("lb_token", "lb_url", "lb_user"):
            _set(uid, k, None)
        db.x("DELETE FROM scrobble_queue WHERE user_id=?", uid)


def enqueue(uid: int, track_id: int, listened_at: float) -> bool:
    """Met une écoute en file si ce profil a un jeton. Renvoie True s'il faut lancer l'envoi."""
    if not _prefs(uid).get("lb_token"):
        return False
    t = db.q1("""SELECT t.title, t.artist, t.duration, a.name AS album FROM tracks t JOIN albums a ON a.id = t.album_id
                 WHERE t.id=?""", track_id)
    if not t or not t["title"] or not t["artist"]:
        return False
    meta = {"artist_name": t["artist"], "track_name": t["title"],
            "additional_info": {"media_player": "Onde", "submission_client": "Onde",
                                "duration_ms": int((t["duration"] or 0) * 1000) or None}}
    if t["album"] and t["album"] != "Singles":
        meta["release_name"] = t["album"]
    meta["additional_info"] = {k: v for k, v in meta["additional_info"].items() if v is not None}
    db.x("INSERT INTO scrobble_queue(user_id, listened_at, payload, tries) VALUES(?,?,?,0)",
         uid, int(listened_at), json.dumps(meta, ensure_ascii=False))
    return True


def flush(uid: int) -> int:
    """Envoie les écoutes en attente de ce profil (une requête). Renvoie le nombre envoyé."""
    with _lock:
        p = _prefs(uid)
        token = p.get("lb_token")
        if not token:
            return 0
        rows = db.q("SELECT * FROM scrobble_queue WHERE user_id=? ORDER BY listened_at LIMIT ?", uid, BATCH)
        if not rows:
            return 0
        listens = [{"listened_at": r["listened_at"], "track_metadata": json.loads(r["payload"])} for r in rows]
        body = {"listen_type": "import" if len(listens) > 1 else "single", "payload": listens}
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        try:
            resp = net.client().post(f"{p.get('lb_url') or DEFAULT_URL}/1/submit-listens", json=body,
                                     headers={"Authorization": f"Token {token}"}, timeout=15)
            if resp.status_code == 401:  # jeton refusé : on arrête d'envoyer
                _set(uid, "lb_token", None)
                db.x(f"DELETE FROM scrobble_queue WHERE id IN ({marks})", *ids)
                log.info("scrobbling : jeton refusé, désactivé pour le profil %s", uid)
                return 0
            if resp.status_code >= 500 or resp.status_code == 429:
                raise RuntimeError(f"HTTP {resp.status_code}")
            if resp.status_code >= 400:  # écoute refusée (données invalides) : inutile de réessayer
                log.info("scrobbling : %d écoute(s) refusée(s) (HTTP %s)", len(ids), resp.status_code)
        except Exception as exc:
            log.info("scrobbling : envoi différé (%s)", exc)
            db.x(f"UPDATE scrobble_queue SET tries = tries + 1 WHERE id IN ({marks})", *ids)
            db.x("DELETE FROM scrobble_queue WHERE tries >= ?", MAX_TRIES)
            return 0
        db.x(f"DELETE FROM scrobble_queue WHERE id IN ({marks})", *ids)
        return len(ids)


def on_play(uid: int, track_id: int, listened_at: float) -> None:
    """Appelé à chaque écoute comptée (library.record_play) : met en file, puis envoie en tâche de fond."""
    try:
        if enqueue(uid, track_id, listened_at):
            from . import library
            library.bg.submit(flush, uid)
    except Exception:
        log.exception("scrobbling")
