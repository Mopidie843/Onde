"""Requêtes SQL et mise en forme JSON des titres, albums, artistes, playlists."""
from __future__ import annotations

import threading

from . import config, db

TRACK_SELECT = """
SELECT t.id, t.title, t.artist, t.album_id, a.name AS album, a.artist AS album_artist,
       t.track_no, t.disc_no, COALESCE(t.year, a.year) AS year, t.genre, t.duration,
       COALESCE(t.cover_id, a.cover_id) AS cover, c.color, (l.track_id IS NOT NULL) AS liked,
       t.plays, t.added_at, t.ext, t.bitrate, t.size, t.lufs
FROM tracks t
JOIN albums a ON a.id = t.album_id
LEFT JOIN covers c ON c.id = COALESCE(t.cover_id, a.cover_id)
LEFT JOIN likes l ON l.track_id = t.id AND l.user_id = onde_uid()
"""

ALBUM_SELECT = """
SELECT a.id, a.name, a.artist, a.year, a.cover_id AS cover, c.color,
       COUNT(t.id) AS count, SUM(t.duration) AS dur, MAX(t.added_at) AS added,
       MAX(t.last_played) AS played, SUM(t.plays) AS plays
FROM albums a
JOIN tracks t ON t.album_id = a.id
LEFT JOIN covers c ON c.id = a.cover_id
"""

def track(r) -> dict:
    return {
        "id": r["id"], "title": r["title"], "artist": r["artist"],
        "albumId": r["album_id"], "album": r["album"], "albumArtist": r["album_artist"],
        "no": r["track_no"], "disc": r["disc_no"], "year": r["year"], "genre": r["genre"],
        "dur": round(r["duration"] or 0, 2), "cover": r["cover"], "color": r["color"],
        "liked": bool(r["liked"]), "plays": r["plays"], "added": r["added_at"],
        "ext": (r["ext"] or "").lstrip("."), "bitrate": r["bitrate"], "size": r["size"],
        "lufs": r["lufs"],
    }


def album(r) -> dict:
    return {
        "id": r["id"], "name": r["name"], "artist": r["artist"], "year": r["year"],
        "cover": r["cover"], "color": r["color"], "count": r["count"],
        "dur": round(r["dur"] or 0), "added": r["added"],
        "single": r["name"] == config.SINGLES,
    }


def tracks(where: str = "", *args, order: str = "t.added_at DESC", limit: int | None = None) -> list[dict]:
    sql = TRACK_SELECT + (f" WHERE {where}" if where else "") + f" ORDER BY {order}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [track(r) for r in db.q(sql, *args)]


def track_by_id(tid: int) -> dict | None:
    rows = tracks("t.id=?", tid)
    return rows[0] if rows else None


def tracks_by_ids(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    found = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for t in tracks(f"t.id IN ({','.join('?' * len(chunk))})", *chunk, order="t.id"):
            found[t["id"]] = t
    return [found[i] for i in ids if i in found]


def albums(where: str = "", *args, order: str = "added DESC", limit: int | None = None,
           having: str = "") -> list[dict]:
    sql = ALBUM_SELECT + (f" WHERE {where}" if where else "") + " GROUP BY a.id"
    if having:
        sql += f" HAVING {having}"
    sql += f" ORDER BY {order}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [album(r) for r in db.q(sql, *args)]


def album_tracks(album_id: int) -> list[dict]:
    return tracks("t.album_id=?", album_id,
                  order="COALESCE(t.disc_no,1), COALESCE(t.track_no, 9999), t.title COLLATE NOCASE")


# ------------------------------------------------------------------ artistes
_artist_cache: dict = {"key": None, "groups": {}}
# Les routes FastAPI synchrones tournent dans un pool de threads : plusieurs requêtes (recherche,
# accueil, bibliothèque) lisent et remplissent ce cache en même temps. Le verrou ne couvre que la
# lecture et la mise à jour du cache, jamais le calcul (jusqu'à une seconde sur un Pi) : les autres
# requêtes ne restent pas bloquées derrière lui.
_cache_lock = threading.Lock()


def _artist_groups() -> dict[str, dict]:
    """Regroupement des titres par artiste. Coûteux sur une grosse bibliothèque (tous les titres) :
    gardé en mémoire tant que la bibliothèque (imports, modifications) et les écoutes ne changent pas.
    La recherche l'utilise à chaque lettre tapée.

    Le dictionnaire renvoyé est partagé entre les requêtes : il n'est jamais modifié après sa
    création (les appelants le lisent seulement), ce qui permet de le lire sans verrou."""
    from . import library  # import tardif : library importe ce module
    fp = db.q1("SELECT COUNT(*) n, TOTAL(plays) p, MAX(last_played) l FROM tracks")
    key = (library.status["gen"], fp["n"], fp["p"], fp["l"])
    with _cache_lock:
        if _artist_cache["key"] == key:
            return _artist_cache["groups"]
    # calcul hors verrou ; si deux requêtes le font en même temps, elles obtiennent le même résultat
    groups: dict[str, dict] = {}
    rows = db.q("""SELECT t.id, t.artist, a.artist AS aartist, t.plays, t.last_played,
                          COALESCE(t.cover_id, a.cover_id) AS cover
                   FROM tracks t JOIN albums a ON a.id = t.album_id""")
    for r in rows:
        for name in {r["artist"], r["aartist"]}:
            if not name:
                continue
            k = db.norm(name)
            g = groups.get(k)
            if g is None:
                g = groups[k] = {"name": name, "count": 0, "plays": 0, "played": 0, "covers": {}}
            g["count"] += 1
            g["plays"] += r["plays"] or 0
            g["played"] = max(g["played"], r["last_played"] or 0)
            if r["cover"]:
                g["covers"][r["cover"]] = g["covers"].get(r["cover"], 0) + 1
    with _cache_lock:  # clé et données toujours remplacées ensemble
        _artist_cache["key"] = key
        _artist_cache["groups"] = groups
    return groups


def artists(order: str = "name", limit: int | None = None, terms: list[str] | None = None) -> list[dict]:
    """Artistes = artistes des titres + artistes d'album (agrégé en Python : 1 requête)."""
    groups = _artist_groups()
    items = []
    for key, g in groups.items():
        if terms and not all(t in key for t in terms):
            continue
        cover = max(g["covers"], key=g["covers"].get) if g["covers"] else None
        items.append({"name": g["name"], "count": g["count"], "plays": g["plays"],
                      "played": g["played"], "cover": cover})
    if order == "plays":
        items.sort(key=lambda a: (-a["plays"], -a["count"]))
    elif order == "played":
        items.sort(key=lambda a: -a["played"])
    else:
        items.sort(key=lambda a: db.norm(a["name"]))
    if limit:
        items = items[:limit]
    images = artist_images()
    for a in items:
        img = images.get(db.norm(a["name"]))
        if img:
            a["cover"], a["photo"] = img, True
    colors = _colors({a["cover"] for a in items if a["cover"]})
    for a in items:
        a["color"] = colors.get(a["cover"])
    return items


def artist_images() -> dict[str, str]:
    return {r["key"]: r["image_id"] for r in db.q("SELECT key, image_id FROM artists WHERE image_id IS NOT NULL")}


def artist_image(name: str) -> tuple[str | None, bool]:
    """(photo, choix manuel ?) pour un artiste."""
    r = db.q1("SELECT image_id, locked FROM artists WHERE key=?", db.norm(name))
    return (r["image_id"], bool(r["locked"])) if r else (None, False)


def _colors(ids) -> dict:
    ids = list(ids)
    if not ids:
        return {}
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in db.q(f"SELECT id, color FROM covers WHERE id IN ({','.join('?' * len(chunk))})", *chunk):
            out[r["id"]] = r["color"]
    return out


def color_of(cover_id: str | None) -> str | None:
    if not cover_id:
        return None
    r = db.q1("SELECT color FROM covers WHERE id=?", cover_id)
    return r["color"] if r else None


# ------------------------------------------------------------------ playlists
def playlist_summary(pid: int | None = None) -> list[dict]:
    sql = """
    SELECT p.id, p.name, p.description, p.cover_id, p.created_at, p.updated_at,
           COUNT(pt.id) AS count, COALESCE(SUM(t.duration), 0) AS dur
    FROM playlists p
    LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id
    LEFT JOIN tracks t ON t.id = pt.track_id
    """
    args = ()
    sql += " WHERE p.owner_id = onde_uid()"  # chaque profil a ses playlists
    if pid is not None:
        sql += " AND p.id=?"
        args = (pid,)
    sql += " GROUP BY p.id ORDER BY p.updated_at DESC"
    out = []
    for r in db.q(sql, *args):
        mosaic = [m["cover"] for m in db.q(
            """SELECT COALESCE(t.cover_id, a.cover_id) AS cover, MIN(pt.position) AS pos
               FROM playlist_tracks pt JOIN tracks t ON t.id=pt.track_id JOIN albums a ON a.id=t.album_id
               WHERE pt.playlist_id=? AND COALESCE(t.cover_id, a.cover_id) IS NOT NULL
               GROUP BY cover ORDER BY pos LIMIT 4""", r["id"])]
        color = color_of(r["cover_id"] or (mosaic[0] if mosaic else None))
        out.append({
            "id": r["id"], "name": r["name"], "description": r["description"],
            "cover": r["cover_id"], "mosaic": mosaic, "color": color,
            "count": r["count"], "dur": round(r["dur"] or 0),
            "created": r["created_at"], "updated": r["updated_at"],
        })
    return out


def playlist_tracks(pid: int) -> list[dict]:
    rows = db.q(TRACK_SELECT.replace("FROM tracks t", ", pt.id AS entry, pt.added_at AS padded FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id")
                + " WHERE pt.playlist_id=? ORDER BY pt.position", pid)
    out = []
    for r in rows:
        t = track(r)
        t["entry"] = r["entry"]
        t["padded"] = r["padded"]
        out.append(t)
    return out
