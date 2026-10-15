"""Partage d'une playlist en lecture seule : un lien secret (comme le mode soirée), sans compte.

La personne qui ouvre le lien voit la playlist et peut l'écouter dans son navigateur, rien d'autre :
pas de recherche dans la bibliothèque, pas de téléchargement, pas de modification. Le lien reste
valable tant que le propriétaire ne le coupe pas (et suit la playlist : un titre ajouté y apparaît).
"""
from __future__ import annotations

import secrets
import time

from . import api_models as M
from . import config, db, users

_ready: set[str] = set()  # bases déjà préparées


def _ensure_unique() -> None:
    """Un seul lien par playlist, garanti par la base : deux créations simultanées (double clic, deux
    appareils) donnaient deux jetons. Les doublons déjà là sont retirés (on garde le plus ancien)."""
    key = str(config.DB_PATH)
    if key in _ready:
        return
    outer = db.connect().in_transaction  # (transaction de l'appelant : elle pourrait encore être annulée)
    with db.tx():
        db.x("""DELETE FROM shares WHERE EXISTS (SELECT 1 FROM shares s2 WHERE s2.playlist_id = shares.playlist_id
                AND (s2.created < shares.created OR (s2.created = shares.created AND s2.rowid < shares.rowid)))""")
        db.x("CREATE UNIQUE INDEX IF NOT EXISTS ux_shares_playlist ON shares(playlist_id)")
    if not outer:
        _ready.add(key)


def _playlist(pid: int):
    return db.q1("SELECT * FROM playlists WHERE id=?", pid)


def of_playlist(pid: int) -> dict | None:
    r = db.q1("SELECT * FROM shares WHERE playlist_id=? ORDER BY created, rowid LIMIT 1", pid)  # (le plus ancien)
    return {"token": r["token"], "path": f"/share/{r['token']}", "created": r["created"], "views": r["views"]} if r else None


def create(pid: int) -> dict:
    p = _playlist(pid)
    if not p:
        raise KeyError(pid)
    if p["owner_id"] != users.uid() and not users.is_admin():
        raise PermissionError("Ce n'est pas ta playlist.")
    _ensure_unique()
    cur = of_playlist(pid)
    if cur:
        return cur
    token = secrets.token_urlsafe(12)
    added = db.x("INSERT OR IGNORE INTO shares(token, playlist_id, owner_id, created) VALUES(?,?,?,?)",
                 token, pid, p["owner_id"], time.time()).rowcount
    if added:  # (sinon : créé au même instant par une autre requête, on renvoie celui-là)
        users.log_activity("share", f"playlist « {p['name']} » partagée")
    return of_playlist(pid)


def revoke(pid: int) -> None:
    p = _playlist(pid)
    if not p:
        raise KeyError(pid)
    if p["owner_id"] != users.uid() and not users.is_admin():
        raise PermissionError("Ce n'est pas ta playlist.")
    db.x("DELETE FROM shares WHERE playlist_id=?", pid)


def resolve(token: str):
    if not token or len(token) > 40:
        return None
    return db.q1("SELECT s.*, p.name, p.description, p.cover_id FROM shares s JOIN playlists p ON p.id = s.playlist_id "
                 "WHERE s.token=?", token)


def track_ids(pid: int) -> list[int]:
    return [r["track_id"] for r in db.q("SELECT track_id FROM playlist_tracks WHERE playlist_id=? ORDER BY position", pid)]


def public_view(token: str, count_view: bool = False) -> dict | None:
    s = resolve(token)
    if not s:
        return None
    if count_view:
        db.x("UPDATE shares SET views = views + 1 WHERE token=?", token)
    owner = users.get(s["owner_id"])
    tracks = M.tracks_by_ids(track_ids(s["playlist_id"]))
    keep = ("id", "title", "artist", "album", "dur", "cover")
    return {"name": s["name"], "description": s["description"] or "", "owner": owner["name"] if owner else "",
            "cover": s["cover_id"] or next((t.get("cover") for t in tracks if t.get("cover")), None),
            "tracks": [{k: t.get(k) for k in keep} for t in tracks],
            "duration": sum(t.get("dur") or 0 for t in tracks)}


def allows(token: str, track_id: int) -> bool:
    s = resolve(token)
    return bool(s and db.q1("SELECT 1 FROM playlist_tracks WHERE playlist_id=? AND track_id=?", s["playlist_id"], track_id))


def allows_cover(token: str, cid: str) -> bool:
    s = resolve(token)
    if not s:
        return False
    if s["cover_id"] == cid:
        return True
    return bool(db.q1("""SELECT 1 FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id
                         LEFT JOIN albums a ON a.id = t.album_id
                         WHERE pt.playlist_id=? AND (t.cover_id=? OR a.cover_id=?) LIMIT 1""", s["playlist_id"], cid, cid))
