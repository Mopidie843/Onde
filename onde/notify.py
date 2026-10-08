"""Notifications d'Onde : un petit journal d'événements (paroles synchronisées, musique ajoutée…) que
l'appli Android interroge, sur le Wi-Fi comme depuis Internet.

Pas de serveur de notifications tiers (Google, Apple…) : l'appli demande « quoi de neuf depuis le
numéro N ? » en attente longue (`/api/notifications?after=N&wait=25`) — la réponse arrive dès qu'il se
passe quelque chose, sans que le téléphone interroge le Pi en boucle. Sans appli, rien ne se passe :
le journal reste simplement dans la base (200 événements au plus).

Plusieurs événements du même genre rapprochés (40 titres calés d'affilée) forment UNE notification
qui se met à jour (« 12 titres… »), au lieu d'inonder le téléphone.
"""
from __future__ import annotations

import logging
import threading
import time

from . import db, settings

log = logging.getLogger("onde.notify")

KINDS = {  # genre -> réglage qui l'autorise
    "lyrics": "notify_lyrics",
    "import": "notify_import",
    "download": "notify_download",
}
ADMIN_KINDS = ("ask", "password")  # réservés à l'admin : les amis ne voient jamais ces notifications
GROUP_WINDOW = 180.0   # secondes : événements du même genre regroupés
KEEP = 200
MAX_WAIT = 30.0

_lock = threading.Lock()
_last = 0          # dernier numéro connu (évite d'interroger la base à chaque attente)
_loaded = False


def _load() -> int:
    global _last, _loaded
    if not _loaded:
        row = db.q1("SELECT MAX(id) AS m FROM notifications")
        _last = max(_last, (row["m"] or 0) if row else 0)
        _loaded = True
    return _last


def last_id() -> int:
    with _lock:
        return _load()


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def add(kind: str, title: str, text: str = "", link: str = "", count_text: str | None = None,
        amount: int = 1) -> int | None:
    """Ajoute un événement. `count_text` : texte à afficher quand plusieurs événements sont regroupés,
    avec « {n} » pour leur nombre (ex. « {n} titres synchronisés »). Renvoie le numéro, ou None si ce
    genre de notification est désactivé dans les Réglages. `amount` : combien d'éléments cet événement
    représente (12 titres ajoutés d'un coup)."""
    key = KINDS.get(kind)
    if key and not settings.get(key):
        return None
    now = time.time()
    with _lock:
        global _last
        _load()
        n = amount
        prev = db.q1("SELECT id, n FROM notifications WHERE kind=? AND ts > ? ORDER BY id DESC LIMIT 1",
                     kind, now - GROUP_WINDOW)
        if prev and count_text:
            n = prev["n"] + amount
            db.x("DELETE FROM notifications WHERE id=?", prev["id"])
            text = count_text.format(n=n)
        cur = db.x("INSERT INTO notifications(ts, kind, title, text, link, n) VALUES(?,?,?,?,?,?)",
                   now, kind, title[:120], text[:300], link[:200], n)
        _last = cur.lastrowid
        db.x("DELETE FROM notifications WHERE id <= ?", _last - KEEP)
        return _last


def since(after: int, limit: int = 20, admin: bool = True) -> list[dict]:
    if admin:
        rows = db.q("SELECT * FROM notifications WHERE id > ? ORDER BY id LIMIT ?", max(0, after), limit)
    else:
        marks = ",".join("?" * len(ADMIN_KINDS))
        rows = db.q(f"SELECT * FROM notifications WHERE id > ? AND kind NOT IN ({marks}) ORDER BY id LIMIT ?",
                    max(0, after), *ADMIN_KINDS, limit)
    return [{"id": r["id"], "ts": r["ts"], "kind": r["kind"], "title": r["title"], "text": r["text"],
             "link": r["link"], "n": r["n"]} for r in rows]


def feed(after: int | None, admin: bool = True) -> dict:
    """Réponse de l'API. `after` None : première connexion → pas d'historique, juste le dernier numéro
    (l'appli ne rejoue jamais d'anciens événements)."""
    last = last_id()
    if after is None or after > last:  # base remplacée / numéro d'un autre serveur : on repart d'ici
        return {"last": last, "events": [], "reset": True}
    if after == last:  # rien de neuf : pas de requête sur la base
        return {"last": last, "events": [], "reset": False}
    return {"last": last, "events": since(after, admin=admin), "reset": False}


# --------------------------------------------------------------- raccourcis pour le reste d'Onde
def lyrics_synced(title: str, artist: str, album_id: int | None) -> None:
    link = f"#/album/{album_id}" if album_id else ""
    add("lyrics", "Paroles synchronisées", f"{artist} — {title}" if artist else title, link,
        count_text="{n} titres ont maintenant leurs paroles synchronisées sur la voix")


def music_added(count: int) -> None:
    if count <= 0:
        return
    add("import", "Musique ajoutée", f"{_plural(count, 'titre ajouté', 'titres ajoutés')} à ta bibliothèque",
        "#/library", count_text="{n} titres ajoutés à ta bibliothèque", amount=count)


def downloaded(track: dict | None) -> None:
    if not track:
        return
    title, artist = track.get("title") or "", track.get("artist") or ""
    album_id = track.get("albumId")
    add("download", "Musique téléchargée", f"{artist} — {title}" if artist else title,
        f"#/album/{album_id}" if album_id else "", count_text="{n} musiques téléchargées")
