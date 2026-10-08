"""Statistiques d'écoute (à partir de l'historique des lectures)."""
from __future__ import annotations

import datetime
import time

from . import api_models as M
from . import db


def compute(days: int = 30) -> dict:
    now = time.time()
    since = now - days * 86400 if days > 0 else 0
    tot = db.q1("""SELECT COUNT(*) n, COALESCE(SUM(seconds), 0) s, COUNT(DISTINCT track_id) tracks
                   FROM plays WHERE user_id = onde_uid() AND played_at >= ?""", since)
    artists_n = db.q1("""SELECT COUNT(DISTINCT t.artist COLLATE NOCASE) n FROM plays p
                         JOIN tracks t ON t.id = p.track_id WHERE p.user_id = onde_uid() AND p.played_at >= ?""", since)["n"]
    first = db.q1("SELECT MIN(played_at) m FROM plays WHERE user_id = onde_uid()")["m"]

    top_t = db.q("""SELECT track_id, COUNT(*) n FROM plays WHERE user_id = onde_uid() AND played_at >= ?
                    GROUP BY track_id ORDER BY n DESC, MAX(played_at) DESC LIMIT 10""", since)
    counts = {r["track_id"]: r["n"] for r in top_t}
    tracks = M.tracks_by_ids([r["track_id"] for r in top_t])
    for t in tracks:
        t["count"] = counts.get(t["id"], 0)

    images = M.artist_images()
    top_a = db.q("""SELECT t.artist AS name, COUNT(*) n, SUM(p.seconds) s,
                           (SELECT COALESCE(t2.cover_id, a2.cover_id) FROM tracks t2 JOIN albums a2 ON a2.id = t2.album_id
                            WHERE t2.artist = t.artist COLLATE NOCASE AND COALESCE(t2.cover_id, a2.cover_id) IS NOT NULL
                            ORDER BY t2.plays DESC LIMIT 1) AS cover
                    FROM plays p JOIN tracks t ON t.id = p.track_id WHERE p.user_id = onde_uid() AND p.played_at >= ?
                    GROUP BY t.artist COLLATE NOCASE ORDER BY n DESC LIMIT 10""", since)
    artists = [{"name": r["name"], "count": r["n"], "seconds": round(r["s"] or 0),
                "cover": images.get(db.norm(r["name"])) or r["cover"]} for r in top_a]

    top_al = db.q("""SELECT t.album_id, COUNT(*) n FROM plays p JOIN tracks t ON t.id = p.track_id
                     WHERE p.user_id = onde_uid() AND p.played_at >= ? GROUP BY t.album_id ORDER BY n DESC LIMIT 10""", since)
    al_counts = {r["album_id"]: r["n"] for r in top_al}
    albums = []
    if al_counts:
        ids = list(al_counts)
        found = {a["id"]: a for a in M.albums(f"a.id IN ({','.join('?' * len(ids))})", *ids)}
        for i in ids:
            if i in found:
                albums.append({**found[i], "plays": al_counts[i]})

    genres = [{"name": r["genre"], "count": r["n"]} for r in db.q(
        """SELECT t.genre, COUNT(*) n FROM plays p JOIN tracks t ON t.id = p.track_id
           WHERE p.user_id = onde_uid() AND p.played_at >= ? AND t.genre <> '' GROUP BY t.genre COLLATE NOCASE ORDER BY n DESC LIMIT 6""", since)]

    # évolution : par jour (≤ 90 jours) ou par mois
    if 0 < days <= 90:
        rows = db.q("""SELECT date(played_at, 'unixepoch', 'localtime') d, SUM(seconds) s FROM plays
                       WHERE user_id = onde_uid() AND played_at >= ? GROUP BY d""", since)
        per = {r["d"]: r["s"] for r in rows}
        series = []
        for i in range(days - 1, -1, -1):
            d = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
            series.append({"label": d, "minutes": round((per.get(d) or 0) / 60)})
        unit = "day"
    else:
        rows = db.q("""SELECT strftime('%Y-%m', played_at, 'unixepoch', 'localtime') m, SUM(seconds) s FROM plays
                       WHERE user_id = onde_uid() AND played_at >= ? GROUP BY m ORDER BY m""", since)
        series = [{"label": r["m"], "minutes": round((r["s"] or 0) / 60)} for r in rows][-24:]
        unit = "month"
    hours = [0] * 24
    for r in db.q("""SELECT CAST(strftime('%H', played_at, 'unixepoch', 'localtime') AS INT) h, COUNT(*) n
                     FROM plays WHERE user_id = onde_uid() AND played_at >= ? GROUP BY h""", since):
        if r["h"] is not None:
            hours[r["h"]] = r["n"]
    return {
        "days": days, "plays": tot["n"], "seconds": round(tot["s"]), "tracks": tot["tracks"],
        "artists": artists_n, "first": first, "topTracks": tracks, "topArtists": artists,
        "topAlbums": albums, "topGenres": genres, "series": series, "unit": unit, "hours": hours,
    }


def history(limit: int = 100, before: float | None = None) -> list[dict]:
    rows = db.q("""SELECT track_id, played_at, source FROM plays WHERE user_id = onde_uid() AND played_at < ?
                   ORDER BY played_at DESC LIMIT ?""", before or time.time() + 1, limit)
    tracks = {t["id"]: t for t in M.tracks_by_ids(list({r["track_id"] for r in rows}))}
    out = []
    for r in rows:
        t = tracks.get(r["track_id"])
        if t:
            out.append({**t, "playedAt": r["played_at"], "source": r["source"]})
    return out


def _daypart(hours: list[int]) -> str:
    """Le moment de la journée où l'on écoute le plus (matin / après-midi / soir / nuit)."""
    parts = {"matin": range(5, 12), "après-midi": range(12, 18), "soir": range(18, 23), "nuit": (23, 0, 1, 2, 3, 4)}
    return max(parts, key=lambda k: sum(hours[h] for h in parts[k]))


def wrapped(year: int | None = None) -> dict:
    """Le bilan d'une année civile (façon « Wrapped ») : temps d'écoute, tops, mois et jour les plus
    chargés, série de jours d'affilée, nouveaux artistes découverts… Propre au profil connecté."""
    now = time.localtime()
    years = [int(r["y"]) for r in db.q(
        """SELECT DISTINCT strftime('%Y', played_at, 'unixepoch', 'localtime') y FROM plays
           WHERE user_id = onde_uid() ORDER BY y DESC""")]
    year = year if year in years else (years[0] if years else now.tm_year)
    since = time.mktime((year, 1, 1, 0, 0, 0, 0, 0, -1))
    until = time.mktime((year + 1, 1, 1, 0, 0, 0, 0, 0, -1))
    out: dict = {"year": year, "years": years, "plays": 0}
    tot = db.q1("""SELECT COUNT(*) n, COALESCE(SUM(seconds), 0) s, COUNT(DISTINCT track_id) tracks FROM plays
                   WHERE user_id = onde_uid() AND played_at >= ? AND played_at < ?""", since, until)
    if not tot["n"]:
        return out
    artists_n = db.q1("""SELECT COUNT(DISTINCT t.artist COLLATE NOCASE) n FROM plays p JOIN tracks t ON t.id = p.track_id
                         WHERE p.user_id = onde_uid() AND p.played_at >= ? AND p.played_at < ?""", since, until)["n"]

    top_t = db.q("""SELECT track_id, COUNT(*) n FROM plays WHERE user_id = onde_uid() AND played_at >= ? AND played_at < ?
                    GROUP BY track_id ORDER BY n DESC, MAX(played_at) DESC LIMIT 5""", since, until)
    counts = {r["track_id"]: r["n"] for r in top_t}
    tracks = M.tracks_by_ids(list(counts))
    for t in tracks:
        t["count"] = counts.get(t["id"], 0)
    tracks.sort(key=lambda t: -t["count"])

    images = M.artist_images()
    top_a = db.q("""SELECT t.artist AS name, COUNT(*) n, SUM(p.seconds) s,
                           (SELECT COALESCE(t2.cover_id, a2.cover_id) FROM tracks t2 JOIN albums a2 ON a2.id = t2.album_id
                            WHERE t2.artist = t.artist COLLATE NOCASE AND COALESCE(t2.cover_id, a2.cover_id) IS NOT NULL
                            ORDER BY t2.plays DESC LIMIT 1) AS cover
                    FROM plays p JOIN tracks t ON t.id = p.track_id
                    WHERE p.user_id = onde_uid() AND p.played_at >= ? AND p.played_at < ?
                    GROUP BY t.artist COLLATE NOCASE ORDER BY n DESC, s DESC LIMIT 5""", since, until)
    top_artists = [{"name": r["name"], "count": r["n"], "seconds": round(r["s"] or 0),
                    "cover": images.get(db.norm(r["name"])) or r["cover"]} for r in top_a]

    top_al = db.q("""SELECT t.album_id, COUNT(*) n FROM plays p JOIN tracks t ON t.id = p.track_id
                     WHERE p.user_id = onde_uid() AND p.played_at >= ? AND p.played_at < ?
                     GROUP BY t.album_id ORDER BY n DESC LIMIT 1""", since, until)
    album = None
    if top_al:
        found = M.albums("a.id = ?", top_al[0]["album_id"])
        if found:
            album = {**found[0], "plays": top_al[0]["n"]}

    genres = [{"name": r["genre"], "count": r["n"]} for r in db.q(
        """SELECT t.genre, COUNT(*) n FROM plays p JOIN tracks t ON t.id = p.track_id
           WHERE p.user_id = onde_uid() AND p.played_at >= ? AND p.played_at < ? AND t.genre <> ''
           GROUP BY t.genre COLLATE NOCASE ORDER BY n DESC LIMIT 3""", since, until)]

    months = [0] * 12
    for r in db.q("""SELECT CAST(strftime('%m', played_at, 'unixepoch', 'localtime') AS INT) m, SUM(seconds) s FROM plays
                     WHERE user_id = onde_uid() AND played_at >= ? AND played_at < ? GROUP BY m""", since, until):
        months[r["m"] - 1] = round((r["s"] or 0) / 60)
    day_rows = db.q("""SELECT date(played_at, 'unixepoch', 'localtime') d, SUM(seconds) s FROM plays
                       WHERE user_id = onde_uid() AND played_at >= ? AND played_at < ? GROUP BY d ORDER BY d""", since, until)
    best_day = max(day_rows, key=lambda r: r["s"] or 0)
    streak = best = 0
    prev = None
    for r in day_rows:
        n = datetime.date.fromisoformat(r["d"]).toordinal()
        streak = streak + 1 if prev is not None and n - prev == 1 else 1
        prev = n
        best = max(best, streak)

    hours = [0] * 24
    for r in db.q("""SELECT CAST(strftime('%H', played_at, 'unixepoch', 'localtime') AS INT) h, COUNT(*) n FROM plays
                     WHERE user_id = onde_uid() AND played_at >= ? AND played_at < ? GROUP BY h""", since, until):
        hours[r["h"]] = r["n"]

    new_artists = db.q1("""SELECT COUNT(*) n FROM (
                             SELECT t.artist FROM plays p JOIN tracks t ON t.id = p.track_id WHERE p.user_id = onde_uid()
                             GROUP BY t.artist COLLATE NOCASE HAVING MIN(p.played_at) >= ? AND MIN(p.played_at) < ?)""",
                        since, until)["n"]
    first = db.q1("""SELECT track_id, played_at FROM plays WHERE user_id = onde_uid() AND played_at >= ? AND played_at < ?
                     ORDER BY played_at LIMIT 1""", since, until)
    first_track = (M.tracks_by_ids([first["track_id"]]) or [None])[0]
    busy = max(range(12), key=lambda m: months[m])
    out.update({
        "plays": tot["n"], "seconds": round(tot["s"]), "tracks": tot["tracks"], "artists": artists_n,
        "topTracks": tracks, "topArtists": top_artists, "topAlbum": album, "topGenres": genres,
        "months": months, "busiestMonth": {"month": busy + 1, "minutes": months[busy]},
        "busiestDay": {"date": best_day["d"], "minutes": round((best_day["s"] or 0) / 60)},
        "bestStreak": best, "daypart": _daypart(hours), "newArtists": new_artists,
        "firstTrack": first_track, "firstPlayedAt": first["played_at"],
    })
    return out
