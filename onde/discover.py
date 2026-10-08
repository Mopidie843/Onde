"""Découverte : radio (titres similaires), mixes par genre, lecture automatique."""
from __future__ import annotations

import math
import random
import threading
import time

from . import api_models as M
from . import db

CATALOG_TTL = 60.0  # s : les écoutes et les likes (qui ne changent pas la génération) sont pris en compte

_cache: dict = {"gen": None, "rows": [], "at": 0.0, "epoch": 0}
# Lu et rempli par plusieurs threads à la fois (accueil, radio, lecture automatique, mixes). Le verrou
# protège seulement la lecture et la mise à jour du cache : la requête SQL et la préparation des
# lignes (le plus long) se font hors verrou, sans bloquer les autres requêtes.
_cache_lock = threading.Lock()


def _catalog() -> list[dict]:
    """Toutes les pistes avec les attributs utiles au calcul de similarité (mis en cache).

    La liste renvoyée est partagée : ni elle ni ses lignes ne sont jamais modifiées après leur
    création (les appelants construisent leurs propres listes), ce qui permet de la lire sans verrou."""
    from . import library
    gen = (library.status["gen"], db.current_uid.get())  # (likes propres à chaque profil)
    with _cache_lock:
        if _cache["gen"] == gen and _cache["rows"] and time.time() - _cache["at"] <= CATALOG_TTL:
            return _cache["rows"]
        epoch = _cache["epoch"]
    rows = db.q("""SELECT t.id, t.artist, a.artist AS aartist, t.genre, COALESCE(t.year, a.year) AS year,
                          t.plays, t.last_played, t.album_id, (l.track_id IS NOT NULL) AS liked,
                          COALESCE(t.cover_id, a.cover_id) AS cover
                   FROM tracks t JOIN albums a ON a.id = t.album_id
                   LEFT JOIN likes l ON l.track_id = t.id AND l.user_id = onde_uid()""")
    fresh = [{
        "id": r["id"], "artist": db.norm(r["artist"]), "aartist": db.norm(r["aartist"]),
        "artist_name": r["artist"], "genre": db.norm(r["genre"]), "year": r["year"],
        "plays": r["plays"] or 0, "last": r["last_played"] or 0, "album": r["album_id"],
        "liked": bool(r["liked"]), "cover": r["cover"],
    } for r in rows]
    with _cache_lock:
        # invalidate() pendant le calcul : ce résultat est peut-être déjà périmé, on ne le garde pas
        # (sinon l'invalidation serait perdue jusqu'à l'expiration du cache)
        if _cache["epoch"] == epoch:
            _cache.update(gen=gen, rows=fresh, at=time.time())
    return fresh


def invalidate() -> None:
    with _cache_lock:
        _cache["gen"] = None
        _cache["epoch"] += 1


def _diversify(scored: list[tuple[float, dict]], limit: int) -> list[int]:
    """Prend les meilleurs en évitant d'enchaîner le même artiste ou le même album."""
    scored.sort(key=lambda x: -x[0])
    pool = [c for _, c in scored[: max(limit * 3, 60)]]
    out, last_artists, album_count = [], [], {}
    while pool and len(out) < limit:
        pick = None
        for i, c in enumerate(pool):
            if c["artist"] in last_artists[-1:] and len(pool) > 3:
                continue
            if album_count.get(c["album"], 0) >= 3 and len(pool) > 3:
                continue
            pick = pool.pop(i)
            break
        if pick is None:
            pick = pool.pop(0)
        out.append(pick["id"])
        last_artists.append(pick["artist"])
        album_count[pick["album"]] = album_count.get(pick["album"], 0) + 1
    return out


def radio(seed_tracks: list[int] | None = None, artist: str | None = None, genre: str | None = None,
          exclude: set[int] | None = None, limit: int = 40) -> list[int]:
    rows = _catalog()
    if not rows:
        return []
    by_id = {r["id"]: r for r in rows}
    seeds = [by_id[i] for i in (seed_tracks or []) if i in by_id]
    artists = {s["artist"] for s in seeds} | {s["aartist"] for s in seeds}
    genres = {s["genre"] for s in seeds if s["genre"]}
    years = [s["year"] for s in seeds if s["year"]]
    if artist:
        a = db.norm(artist)
        artists.add(a)
        genres |= {r["genre"] for r in rows if (r["artist"] == a or r["aartist"] == a) and r["genre"]}
    if genre:
        genres.add(db.norm(genre))
    year0 = sum(years) / len(years) if years else None
    exclude = set(exclude or ()) | {s["id"] for s in seeds}
    now = time.time()
    scored = []
    for r in rows:
        if r["id"] in exclude:
            continue
        s = random.random() * 1.6
        if r["artist"] in artists:
            s += 2.6
        elif r["aartist"] in artists:
            s += 1.6
        if r["genre"] and r["genre"] in genres:
            s += 2.2
        if year0 and r["year"] and abs(r["year"] - year0) <= 6:
            s += 0.8
        if r["liked"]:
            s += 0.9
        s += math.log1p(r["plays"]) * 0.35
        if now - r["last"] < 3 * 3600:  # déjà entendu il y a peu
            s -= 2.5
        scored.append((s, r))
    return _diversify(scored, limit)


MOODS = {
    "chill": ("Chill", "Pour se poser", ("lofi", "lo-fi", "chill", "acoustic", "acoustique", "soul", "r&b", "rnb", "jazz",
                                         "ambient", "folk", "indie", "bossa")),
    "energie": ("Énergie", "Pour se bouger", ("rap", "hip hop", "hip-hop", "trap", "drill", "rock", "metal", "electro",
                                              "edm", "techno", "drum", "punk")),
    "soiree": ("Soirée", "Pour danser", ("dance", "pop", "house", "reggaeton", "afro", "latin", "disco", "funk",
                                        "dancehall", "zouk", "amapiano")),
    "focus": ("Concentration", "Pour bosser", ("ambient", "classique", "classical", "piano", "lofi", "lo-fi",
                                               "instrumental", "soundtrack", "musique de film")),
}


# l'humeur suit l'heure : (heure de début, humeur principale, humeur d'appoint, nom, description)
DAYPARTS = [
    (5, "energie", "soiree", "Mix du matin", "De l'énergie pour démarrer"),
    (11, "soiree", "energie", "Mix de midi", "Pop et sons qui bougent"),
    (14, "focus", "chill", "Mix de l'après-midi", "Pour rester concentré"),
    (18, "soiree", "energie", "Mix de fin de journée", "Ça monte doucement"),
    (21, "chill", "focus", "Mix du soir", "Calme, pour se poser"),
    (0, "chill", "focus", "Mix de la nuit", "Tout en douceur"),
]


def daypart(hour: int | None = None) -> tuple[str, str, str, str]:
    """(humeur, humeur d'appoint, nom, description) selon l'heure (heure locale du serveur)."""
    h = time.localtime().tm_hour if hour is None else hour % 24
    for start, mood, extra, name, desc in DAYPARTS:
        if start == 0 and h < 5:
            return mood, extra, name, desc
    best = max((d for d in DAYPARTS if d[0] <= h and d[0] != 0), key=lambda d: d[0], default=DAYPARTS[-1])
    return best[1], best[2], best[3], best[4]


def _now_pool(rows: list[dict], hour: int | None = None) -> tuple[list[dict], dict[int, float], str, str]:
    """Titres du mix « selon l'heure » et leur poids : l'humeur du moment d'abord, un peu de l'humeur
    d'appoint, tes likes en bonus. Sans genres renseignés : tes titres les plus écoutés à cette heure-là."""
    mood, extra, name, desc = daypart(hour)
    main = {r["id"]: r for r in _mood_pool(rows, mood)}
    side = {r["id"]: r for r in _mood_pool(rows, extra) if r["id"] not in main}
    weights = {i: 3.0 for i in main} | {i: 1.0 for i in side}
    pool = list(main.values()) + list(side.values())
    if len(pool) < 8:  # pas assez de genres : ce que ce profil écoute d'habitude à cette heure-là
        h = time.localtime().tm_hour if hour is None else hour % 24
        hours = [(h + d) % 24 for d in (-1, 0, 1)]
        got = db.q("""SELECT track_id, COUNT(*) n FROM plays WHERE user_id = onde_uid()
                      AND CAST(strftime('%H', played_at, 'unixepoch', 'localtime') AS INT) IN (?,?,?)
                      GROUP BY track_id ORDER BY n DESC LIMIT 200""", *hours)
        by_id = {r["id"]: r for r in rows}
        for g in got:
            r = by_id.get(g["track_id"])
            if r and r["id"] not in weights:
                pool.append(r)
                weights[r["id"]] = 1.0 + math.log1p(g["n"])
    return pool, weights, name, desc


def _mood_pool(rows: list[dict], mood: str) -> list[dict]:
    words = MOODS[mood][2]
    return [r for r in rows if r["genre"] and any(w in r["genre"] for w in words)]


def _user_plays(since: float, until: float | None = None) -> list[tuple[int, int]]:
    rows = db.q("""SELECT track_id, COUNT(*) n FROM plays WHERE user_id = onde_uid() AND played_at >= ? AND played_at < ?
                   GROUP BY track_id ORDER BY n DESC""", since, until or time.time() + 1)
    return [(r["track_id"], r["n"]) for r in rows]


def weekly(limit: int = 50) -> list[int]:
    """« Découvertes de la semaine » : ce qui ressemble à tes likes et à tes écoutes du mois, sans ce que tu as
    déjà entendu ce mois-ci. Le même mix toute la semaine (tiré au sort chaque lundi)."""
    rows = _catalog()
    month = time.time() - 30 * 86400
    seeds = [tid for tid, _ in _user_plays(month)[:15]] + [r["id"] for r in rows if r["liked"]][:15]
    if len(seeds) < 3:
        return []
    heard = {tid for tid, _ in _user_plays(month)}
    week = time.strftime("%G-%V")
    state = random.getstate()
    random.seed(f"{week}:{db.current_uid.get()}")
    try:
        ids = radio(seed_tracks=seeds, exclude=heard | set(seeds), limit=limit)
    finally:
        random.setstate(state)
    return ids


def last_year() -> list[int]:
    """« Il y a un an » : ce que tu écoutais à la même période l'an dernier."""
    now = time.time()
    return [tid for tid, _ in _user_plays(now - 400 * 86400, now - 330 * 86400)][:60]


def top_artists(n: int = 2) -> list[str]:
    rows = db.q("""SELECT t.artist, COUNT(*) c FROM plays p JOIN tracks t ON t.id = p.track_id
                   WHERE p.user_id = onde_uid() AND p.played_at >= ? GROUP BY t.artist COLLATE NOCASE
                   ORDER BY c DESC LIMIT ?""", time.time() - 60 * 86400, n)
    return [r["artist"] for r in rows if r["c"] >= 3]


def mixes(limit: int = 8) -> list[dict]:
    """Mixes affichés sur l'accueil : par genre, découvertes, favoris oubliés, classiques."""
    rows = _catalog()
    out = []
    by_id = {r["id"]: r for r in rows}
    now_pool, _, now_name, now_desc = _now_pool(rows)
    if len(now_pool) >= 8:
        out.append(_mix_card("now", now_name, now_pool, now_desc))
    wk = [by_id[i] for i in weekly() if i in by_id]
    if len(wk) >= 8:
        out.append(_mix_card("weekly", "Découvertes de la semaine", wk, "Rien que pour toi, renouvelé chaque lundi"))
    ly = [by_id[i] for i in last_year() if i in by_id]
    if len(ly) >= 5:
        out.append(_mix_card("lastyear", "Il y a un an", ly, "Ce que tu écoutais à la même période l'an dernier"))
    for a in top_artists():
        na = db.norm(a)
        pool = [r for r in rows if r["artist"] == na or r["aartist"] == na]
        if len(pool) >= 3:
            out.append(_mix_card(f"artist:{a}", f"Mix {a}", pool, f"{a} et des titres qui lui ressemblent"))
    for key, (name, desc, _) in MOODS.items():
        pool = _mood_pool(rows, key)
        if len(pool) >= 8:
            out.append(_mix_card(f"mood:{key}", name, pool, desc))
    limit += len(out)  # (les mix perso s'ajoutent à ceux par genre)
    genres: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    for r in db.q("SELECT genre, COUNT(*) n FROM tracks WHERE genre <> '' GROUP BY genre COLLATE NOCASE"):
        names[db.norm(r["genre"])] = r["genre"]
    for r in rows:
        if r["genre"]:
            genres.setdefault(r["genre"], []).append(r)
    ranked = sorted(genres.items(), key=lambda kv: -(sum(x["plays"] for x in kv[1]) * 2 + len(kv[1])))
    for g, items in ranked:
        if len(items) < 4:
            continue
        out.append(_mix_card(f"genre:{names.get(g, g)}", f"Mix {names.get(g, g)}", items))
        if len(out) >= limit - 3:
            break
    never = [r for r in rows if r["plays"] == 0]
    if len(never) >= 6:
        out.append(_mix_card("discover", "Découvertes", never, "Titres jamais écoutés"))
    month = time.time() - 30 * 86400
    forgotten = [r for r in rows if r["liked"] and r["last"] < month]
    if len(forgotten) >= 5:
        out.append(_mix_card("forgotten", "Favoris oubliés", forgotten, "Tes coups de cœur pas écoutés depuis un moment"))
    classics = [r for r in rows if r["plays"] >= 3]
    if len(classics) >= 8:
        out.append(_mix_card("classics", "Tes classiques", sorted(classics, key=lambda r: -r["plays"]),
                             "Ce que tu écoutes le plus"))
    return out[:limit]


def _mix_card(key: str, name: str, items: list[dict], desc: str | None = None) -> dict:
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    covers: list[str] = []
    for r in items:
        counts[r["artist"]] = counts.get(r["artist"], 0) + 1 + r["plays"]
        labels.setdefault(r["artist"], r["artist_name"])
        if r["cover"] and r["cover"] not in covers and len(covers) < 4:
            covers.append(r["cover"])
    top = [labels[a] for a, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:3]]
    if desc is None:
        desc = ", ".join(top) + (" et plus" if len(counts) > 3 else "")
    return {"key": f"mix:{key}", "name": name, "desc": desc, "covers": covers, "count": len(items),
            "color": M.color_of(covers[0]) if covers else None}


def mix_tracks(kind: str, arg: str = "", limit: int = 60) -> tuple[str, list[int]]:
    rows = _catalog()
    now = time.time()
    if kind == "genre":
        g = db.norm(arg)
        pool = [r for r in rows if r["genre"] == g]
        name = f"Mix {arg}"
        weight = lambda r: 1 + math.log1p(r["plays"]) + (1 if r["liked"] else 0)  # noqa: E731
    elif kind == "discover":
        pool, name = [r for r in rows if r["plays"] == 0], "Découvertes"
        weight = lambda r: 1.0  # noqa: E731
    elif kind == "forgotten":
        pool = [r for r in rows if r["liked"] and r["last"] < now - 30 * 86400]
        name, weight = "Favoris oubliés", (lambda r: 1.0)
    elif kind == "classics":
        pool = [r for r in rows if r["plays"] >= 3]
        name, weight = "Tes classiques", (lambda r: r["plays"])
    elif kind == "weekly":
        return "Découvertes de la semaine", weekly(limit)
    elif kind == "lastyear":
        return "Il y a un an", last_year()[:limit]
    elif kind == "artist":
        own = [r["id"] for r in rows if r["artist"] == db.norm(arg) or r["aartist"] == db.norm(arg)]
        random.shuffle(own)
        similar = radio(artist=arg, exclude=set(own), limit=max(0, limit - min(len(own), limit // 2)))
        mixed, a, b = [], own[: limit // 2], similar
        while a or b:  # un titre de l'artiste, un titre qui lui ressemble…
            if a:
                mixed.append(a.pop(0))
            if b:
                mixed.append(b.pop(0))
        return f"Mix {arg}", mixed[:limit]
    elif kind == "now":
        pool, weights, name, _ = _now_pool(rows, int(arg) if arg.isdigit() else None)
        weight = lambda r: weights.get(r["id"], 1.0) + (1 if r["liked"] else 0)  # noqa: E731
    elif kind == "mood" and arg in MOODS:
        pool, name = _mood_pool(rows, arg), MOODS[arg][0]
        weight = lambda r: 1 + (1 if r["liked"] else 0) + math.log1p(r["plays"]) * 0.5  # noqa: E731
    else:
        return "Mix", []
    # tirage pondéré sans remise (algorithme d'Efraimidis-Spirakis)
    keyed = sorted(pool, key=lambda r: -(random.random() ** (1.0 / max(weight(r), 0.01))))
    return name, _diversify([(len(keyed) - i, r) for i, r in enumerate(keyed)], limit)


def autoplay_ids(queue: list[int], limit: int = 25) -> list[int]:
    """Titres ajoutés automatiquement quand la file d'attente se termine."""
    if not queue:
        return []
    return radio(seed_tracks=queue[-5:], exclude=set(queue[-300:]), limit=limit)

