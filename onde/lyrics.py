"""Paroles, trouvées toutes seules : fichier .lrc à côté du morceau, paroles intégrées aux tags,
fichier .txt voisin, ou LRCLIB (en ligne, gratuit) ; les paroles brutes sont ensuite calées sur la
voix par l'IA (align.py) si elle est installée.

Onde les cherche en arrière-plan pour toute la bibliothèque (le titre qu'on lance d'abord, puis les
plus écoutés), et réessaie régulièrement tant qu'elles ne sont pas trouvées : LRCLIB s'enrichit chaque
jour. Les essais s'espacent (30 min, 2 h, 6 h, 1 jour, 3 jours, puis chaque semaine). Des paroles
brutes sont remplacées par une version synchronisée dès que LRCLIB en a une (vérifié chaque mois).
Sans Internet, rien n'est compté comme un échec : la recherche reprend au retour de la connexion.

Recherche sur LRCLIB (au plus 8 requêtes par titre) : correspondance exacte (titre, artiste, album,
durée), puis recherches par variantes (titre sans « feat. », « Remaster », « (Official Video) »,
numéro de piste ; artiste sans « VEVO » ; premier artiste ; artiste de l'album ; « Artiste - Titre »
dans le titre), puis recherche libre, puis par le titre seul. Un résultat n'est gardé que si le titre,
l'artiste et la durée correspondent ; des paroles synchronisées d'une autre version ne gardent que
leur texte.
"""
from __future__ import annotations

import collections
import difflib
import logging
import re
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path

import mutagen
from mutagen.id3 import ID3
from mutagen.mp4 import MP4Tags

from . import clean, config, db, net, settings

log = logging.getLogger("onde.lyrics")

TS = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
UA = net.UA  # (gardé pour le code qui l'utilisait)
# délai avant le prochain essai, selon le nombre de recherches déjà restées sans résultat
RETRY_DELAYS = (1800, 2 * 3600, 6 * 3600, 86400, 3 * 86400, 7 * 86400)
RETRY_AFTER = RETRY_DELAYS[-1]  # ensuite : une fois par semaine, sans fin
NEVER = 4102444800.0            # (an 2100) paroles effacées à la main : pas de recherche automatique

# recherche automatique en arrière-plan
AUTO_START_DELAY = 45.0   # laisse d'abord Onde démarrer et scanner tranquillement
AUTO_BATCH = 40           # titres par tournée
AUTO_PAUSE = 2.0          # secondes entre deux titres (LRCLIB est un service gratuit : on reste poli)
AUTO_IDLE = 600.0         # rien à faire : on revérifie dans 10 min (ou dès qu'un titre est ajouté)
AUTO_OFFLINE_WAIT = 300.0 # pas d'Internet : on réessaie dans 5 min

_brackets = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_dash_suffix = re.compile(r"\s+[-–—]\s+.*$")
_feat = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|avec|with)\s+.*$", re.I)
_artist_sep = re.compile(r"\s*(?:,|;|/|&|\+|\sx\s|\set\s|\sand\s|\svs\.?\s)\s*", re.I)
_junk = re.compile(
    r"\s*[\(\[][^\)\]]*(feat\.?|ft\.|remaster|version|edit|mono|stereo|bonus|explicit|live at)[^\)\]]*[\)\]]"
    r"|\s+-\s+.*(remaster|version|edit|mono|stereo).*$",
    re.I,
)


def is_synced(text: str | None) -> bool:
    return bool(text and TS.search(text))


def _sylt_to_lrc(frame) -> str | None:
    """SYLT (paroles synchronisées ID3) → LRC. None si les dates ne sont pas en millisecondes."""
    if getattr(frame, "format", 2) != 2:  # 1 = en trames MPEG : inexploitable sans le débit exact
        return None
    lines = []
    for text, ms in frame.text:
        m, s = divmod(ms / 1000, 60)
        lines.append(f"[{int(m):02d}:{s:05.2f}]{str(text).strip()}")
    return "\n".join(lines) if len(lines) >= 2 else None


def _read_side(p: Path) -> str:
    try:
        if p.stat().st_size > 200_000:
            return ""
        return p.read_text(encoding="utf-8", errors="replace").replace("﻿", "").strip()
    except OSError:
        return ""


def _split(text: str, source: str) -> dict | None:
    """Texte trouvé dans un fichier → {synced, plain, source}."""
    if is_synced(text):
        return {"synced": text, "plain": None, "source": source}
    plain = to_plain(text)
    return {"synced": None, "plain": plain, "source": source} if plain else None


def from_file(path: str) -> dict | None:
    """Paroles fournies avec le fichier : .lrc voisin, paroles intégrées aux tags, ou .txt voisin."""
    p = Path(path)
    for cand in (p.with_suffix(".lrc"), p.with_suffix(".LRC"), Path(str(p) + ".lrc")):
        if cand.is_file():
            text = _read_side(cand)
            if text:
                res = _split(text, "fichier .lrc (calé par IA)" if "[re:Onde" in text else "fichier .lrc")
                if res:
                    return res
    res = _from_tags(path)
    if res:
        return res
    for cand in (p.with_suffix(".txt"), p.with_suffix(".TXT")):
        if cand.is_file():
            text = _read_side(cand)
            if text and len([ln for ln in text.splitlines() if ln.strip()]) >= 2 and "\x00" not in text:
                return _split(text, "fichier .txt")
    return None


def _from_tags(path: str) -> dict | None:
    try:
        f = mutagen.File(path)
    except Exception:
        f = None
    tags = getattr(f, "tags", None)
    if tags is None and str(path).lower().endswith((".mp3", ".aiff", ".aif", ".wav")):
        try:  # fichier dont l'audio n'est pas reconnu, mais avec des tags ID3 lisibles
            tags = ID3(path)
        except Exception:
            tags = None
    text, synced = "", None
    if isinstance(tags, ID3):
        for frame in tags.getall("SYLT"):
            synced = _sylt_to_lrc(frame)
            if synced:
                break
        texts = [str(fr.text or "").strip() for fr in tags.getall("USLT")]
        text = max(texts, key=len) if texts else ""
    elif isinstance(tags, MP4Tags):
        text = str((tags.get("\xa9lyr") or [""])[0])
    elif tags is not None:
        for k in ("syncedlyrics", "lyrics", "unsyncedlyrics", "LYRICS", "WM/Lyrics", "Lyrics"):
            try:
                v = tags.get(k)
            except Exception:
                v = None
            if v:
                text = str(v[0] if isinstance(v, list) else v)
                break
    text = (text or "").strip()
    if synced:
        return {"synced": synced, "plain": to_plain(text) or None, "source": "tags"}
    return _split(text, "tags") if text else None


LRCLIB = "https://lrclib.net/api/"
HEADERS = {"Lrclib-Client": f"Onde {config.VERSION} (serveur musical personnel)"}
REQUEST_BUDGET = 8        # requêtes au plus par titre et par essai (LRCLIB est gratuit : on reste poli)
TIME_BUDGET = 30.0        # secondes au plus par titre
GOOD_DIFF = 4.0           # écart de durée (s) pour lequel des paroles synchronisées sont sûres
MAX_DIFF = 12.0           # au-delà : une autre version du morceau, on n'en veut pas

_punct = re.compile(r"[`~!@#$%^&*()_|+\-=?;:\",.<>{}\[\]\\/\x00\n¿¡«»…·–—]")
_tracknum = re.compile(r"^\s*\d{1,3}\s*[-.)–]\s+")
_web_junk = re.compile(
    r"\s*[\(\[][^\)\]]*(official|officiel|lyrics?|paroles|audio|video|vidéo|clip|\bhd\b|\bhq\b|4k|visuali[sz]er)"
    r"[^\)\]]*[\)\]]", re.I)


def key(text: str | None) -> str:
    """Forme de comparaison, la même que LRCLIB : minuscules sans accents, ponctuation → espace,
    apostrophes supprimées (« Don't » = « Dont »)."""
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    s = s.replace("'", "").replace("’", "").replace("‘", "")
    return " ".join(_punct.sub(" ", s).split())


def _akey(artist: str | None) -> str:
    k = key(artist)
    return k[4:] if k.startswith("the ") else k


def _bare(title: str) -> str:
    """Le titre réduit à l'essentiel : sans parenthèses ni « - Remaster… »."""
    t = _web_junk.sub("", _junk.sub("", title or ""))
    return (_dash_suffix.sub("", _brackets.sub("", t)).strip() or t.strip())


def _titles(title: str, artist_keys=()) -> list[str]:
    """Variantes du titre, de la plus fidèle à la plus large : d'abord le nom allégé (sans émojis,
    « [AMV] », « 4K », « Sub. Español », séparateurs…) privé de l'artiste s'il y est écrit, puis le
    titre d'origine nettoyé comme avant."""
    out: list[str] = []

    def add(t: str) -> None:
        t = (t or "").strip(" -–—").strip()
        if t and t not in out:
            out.append(t)

    def is_artist(part: str) -> bool:
        k = _akey(part)
        return bool(artist_keys and k and any(k == a or k in a or a in k for a in artist_keys))

    parts = clean.parse(title).parts
    rest = [p for p in parts if not is_artist(p)] or parts
    for p in rest[:2]:
        p = _junk.sub("", p).strip() or p
        add(p)
        add(_bare(p))
    orig = _web_junk.sub("", _junk.sub("", _tracknum.sub("", title or ""))).strip()
    add(orig)
    add(_bare(orig))
    return out[:4]


def _artists(artist: str, album_artist: str = "") -> list[str]:
    """L'artiste principal (sans « feat. », émojis, « VEVO », « - Topic »), puis le premier seulement
    (« A, B » / « A & B » → « A »), puis l'artiste de l'album."""
    out: list[str] = []
    main = clean.artist_name(artist)
    for a in (main, _artist_sep.split(main, maxsplit=1)[0].strip() if main else "", clean.artist_name(album_artist)):
        if a and a not in out:
            out.append(a)
    return out[:4]


def title_ok(want: str, got: str) -> bool:
    a, b = key(want), key(got)
    if not a or not b:
        return False
    if a == b or key(_bare(want)) == key(_bare(got)):
        return True
    return min(len(a), len(b)) >= 6 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.9


def artist_ok(wants, got: str) -> bool:
    g = _akey(got)
    if not g:
        return False
    parts = {_akey(p) for p in _artist_sep.split(_feat.sub("", got or ""))} | {g}
    for w in wants:
        wk = _akey(w)
        if not wk:
            continue
        if wk in parts:
            return True
        short, long_ = sorted((wk.replace(" ", ""), g.replace(" ", "")), key=len)
        if len(short) >= 4 and short in long_:  # « Coldplay » / « ColdplayVEVO »
            return True
    return False


def _times(text: str) -> list[float]:
    out = []
    for line in (text or "").splitlines():
        m = TS.match(line.strip())
        if m and TS.sub("", line).strip():
            frac = m.group(3) or "0"
            out.append(int(m.group(1)) * 60 + int(m.group(2)) + int(frac) / 10 ** len(frac))
    return out


def valid_synced(text: str | None, duration: float = 0) -> bool:
    """Des paroles synchronisées qui tiennent debout : au moins 2 lignes datées, dans l'ordre,
    et qui ne dépassent pas la fin du morceau."""
    t = _times(text or "")
    if len(t) < 2:
        return False
    backwards = sum(1 for a, b in zip(t, t[1:]) if b < a - 0.5)
    if backwards > max(1, len(t) // 10):
        return False
    return not (duration and max(t) > duration + 5)


_meta_line = re.compile(r"^\s*\[(ar|ti|al|au|by|re|ve|length|offset|la|lr|tool|#)\s*:[^\]]*\]\s*$", re.I)
_word_ts = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")


def to_plain(text: str | None) -> str:
    """Texte seul : sans dates [mm:ss], sans dates mot à mot <mm:ss> ni lignes d'en-tête LRC."""
    lines = []
    for line in (text or "").splitlines():
        if _meta_line.match(line):
            continue
        lines.append(_word_ts.sub("", TS.sub("", line)).strip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class Search:
    """Une recherche sur LRCLIB : compte les requêtes, respecte un budget de requêtes et de temps.
    Les réponses 400/404/422 (paramètre refusé…) valent « rien trouvé » ; une panne réseau, un
    blocage (403, 429) ou un 5xx lèvent net.NetError (on réessaiera plus tard, sans compter d'échec)."""

    def __init__(self, budget: int = REQUEST_BUDGET, seconds: float = TIME_BUDGET):
        self.budget, self.seconds = budget, seconds
        self.requests = 0
        self.start = time.monotonic()
        self.trail: list[str] = []

    @property
    def exhausted(self) -> bool:
        return self.requests >= self.budget or time.monotonic() - self.start > self.seconds

    def get(self, path: str, params: dict):
        if self.exhausted:
            return None
        self.requests += 1
        url = LRCLIB + path + "?" + urllib.parse.urlencode(params)
        try:
            data = net.get_json(url, limit=2_000_000, deadline=10.0, not_found=None, headers=HEADERS)
        except net.NetError as exc:
            if exc.status in (400, 404, 410, 422):  # requête refusée : rien à trouver ainsi
                log.debug("LRCLIB %s : HTTP %s (%s)", path, exc.status, params)
                self.trail.append(f"{path} {params} → HTTP {exc.status}")
                return None
            raise
        n = len(data) if isinstance(data, list) else (1 if data else 0)
        log.debug("LRCLIB %s %s → %d résultat(s)", path, params, n)
        self.trail.append(f"{path} {params} → {n}")
        return data


def _has_lyrics(r: dict | None) -> bool:
    return bool(r and (r.get("syncedLyrics") or r.get("plainLyrics") or r.get("instrumental")))


def _diff(r: dict, duration: float) -> float | None:
    try:
        d = float(r.get("duration") or 0)
    except (TypeError, ValueError):
        d = 0
    return abs(d - duration) if (d and duration) else None


def _usable_synced(r: dict, duration: float) -> str | None:
    text = r.get("syncedLyrics")
    if not text or not valid_synced(text, duration):
        return None
    diff = _diff(r, duration)
    if diff is not None and diff > GOOD_DIFF and duration and max(_times(text)) > duration - 1:
        return None  # autre montage (intro plus longue…) : le texte sert, pas les dates
    return text


def _candidates(results, titles, wants, duration: float, evidence=None) -> list[dict]:
    if isinstance(results, dict):
        results = [results]
    if not isinstance(results, list):
        return []
    out = []
    for r in results:
        if not isinstance(r, dict) or not _has_lyrics(r):
            continue
        if not any(title_ok(t, r.get("trackName") or r.get("name") or "") for t in titles):
            continue
        if wants is not None and not artist_ok(wants, r.get("artistName") or ""):
            continue
        if evidence is not None and not evidence(r):
            continue
        diff = _diff(r, duration)
        if diff is not None and diff > MAX_DIFF:
            continue
        out.append(r)
    return out


def _rank(r: dict, duration: float, album: str) -> tuple:
    diff = _diff(r, duration)
    kind = 0 if _usable_synced(r, duration) else (1 if (r.get("plainLyrics") or r.get("syncedLyrics")) else 2)
    return (kind, 6.0 if diff is None else diff, 0 if album and key(r.get("albumName")) == key(album) else 1)


def _in_hay(name: str, hay: str) -> bool:
    k = _akey(name)
    if len(k) < 3:
        return False
    return f" {k} " in f" {hay} " or (len(k) >= 5 and k.replace(" ", "") in hay.replace(" ", ""))


def from_lrclib(title: str, artist: str, album: str, duration: float, album_artist: str = "",
                path: str = "", search: Search | None = None) -> dict | None:
    """Cherche sur LRCLIB, de la méthode la plus précise à la plus large, et garde le meilleur
    résultat (synchronisé, bonne durée, même album). Rien plutôt que les paroles d'une autre chanson.
    Lève OSError (net.NetError) si LRCLIB est injoignable."""
    s = search or Search()
    artists = _artists(artist or "", album_artist or "")
    parsed = clean.parse(title or "")
    titles = _titles(title or "", {_akey(a) for a in artists})
    # artiste inconnu mais écrit dans le titre (« Titre ~ Artiste », « Artiste - Titre ») : on essaie les deux ordres
    guesses: list[tuple[str, str]] = []  # (artiste, titre)
    if not artists and len(parsed.parts) >= 2:
        a, b = parsed.parts[0], parsed.parts[1]
        guesses = [(a, b), (b, a)] if parsed.artist_first else [(b, a), (a, b)]
        titles = list(dict.fromkeys([g[1] for g in guesses] + titles))[:4]
    if not titles:
        return None
    duration = float(duration or 0)
    album = "" if album == config.SINGLES else (album or "")
    best: list = []  # [rang, résultat]

    def consider(cands) -> None:
        for r in cands:
            rk = _rank(r, duration, album)
            if not best or rk < best[0]:
                best[:] = [rk, r]

    def good() -> bool:
        return bool(best) and best[0][0] == 0 and best[0][1] <= GOOD_DIFF

    def exact(art: str, tit: str, with_album: bool = False) -> None:
        # correspondance exacte (LRCLIB : même titre et artiste, durée à ±2 s, album s'il est donné)
        params = {"track_name": tit, "artist_name": art}
        if 1 <= duration <= 3600:  # LRCLIB refuse les durées au-delà d'une heure
            params["duration"] = round(duration)
        if with_album and album:
            params["album_name"] = album
        consider(_candidates(s.get("get", params), [tit], None, duration))

    if artists:
        if album:
            exact(artists[0], titles[0], with_album=True)
        if not good():
            exact(artists[0], titles[0])
        # 2. recherche par titre et artiste (variantes)
        pairs = [(t, a) for t in titles[:2] for a in artists[:2]]
        pairs = list(dict.fromkeys(pairs + [(titles[0], a) for a in artists[2:]]))[:4]
        for t, a in pairs:
            if good() or s.exhausted:
                break
            consider(_candidates(s.get("search", {"track_name": t, "artist_name": a}), titles, artists, duration))
        # 3. recherche libre « artiste titre » (tags mal remplis)
        if not good() and not s.exhausted:
            consider(_candidates(s.get("search", {"q": f"{artists[0]} {titles[-1]}"}), titles, artists, duration))
    elif guesses:
        for art, tit in guesses:
            if not good():
                exact(art, tit)
        for art, tit in guesses:
            if not good() and not s.exhausted:
                consider(_candidates(s.get("search", {"track_name": tit, "artist_name": art}), [tit], [art], duration))
        if not good() and not s.exhausted:  # recherche libre : l'ordre n'a pas d'importance
            results = s.get("search", {"q": " ".join(parsed.parts[:2])})
            for art, tit in guesses:
                consider(_candidates(results, [tit], [art], duration))
    # 4. par le titre seul : l'artiste trouvé doit apparaître dans nos infos (artiste, album, dossier,
    #    titre), ou, artiste inconnu, être le seul à avoir ce titre avec cette durée exacte
    if not good() and not s.exhausted:
        parts = Path(path).parts[-3:] if path else ()
        hay = key(" ".join([artist or "", album_artist or "", album, title or "", *parts]))
        for tit in titles[: 2 if guesses else 1]:
            if s.exhausted:
                break
            results = s.get("search", {"track_name": tit})
            cands = _candidates(results, titles, None, duration,
                                lambda r: (artists and artist_ok(artists, r.get("artistName") or ""))
                                or _in_hay(r.get("artistName") or "", hay))
            if not cands and not artists and not guesses and duration:
                strict = [r for r in _candidates(results, titles, None, duration)
                          if (_diff(r, duration) if _diff(r, duration) is not None else 99) <= 2]
                if len({_akey(r.get("artistName")) for r in strict}) == 1:
                    cands = strict
            consider(cands)
            if best:
                break
    if not best:
        return None
    r = best[1]
    synced = _usable_synced(r, duration)
    plain = r.get("plainLyrics") or (to_plain(r.get("syncedLyrics")) if r.get("syncedLyrics") else None)
    return {"synced": synced, "plain": plain or None,
            "instrumental": bool(r.get("instrumental")) and not synced and not plain, "source": "LRCLIB",
            "match": f"{r.get('artistName')} — {r.get('trackName')}", "requests": s.requests}


def _found(row) -> bool:
    return bool(row and (row["synced"] or row["plain"] or row["instrumental"]))


def _row_to_dict(row) -> dict:
    d = {"synced": row["synced"], "plain": row["plain"], "source": row["source"],
         "instrumental": bool(row["instrumental"]), "found": _found(row)}
    if not d["found"]:
        next_try = row["next_try"]
        d["next_try"] = None if (next_try or 0) >= NEVER else next_try
        d["tries"] = row["tries"] or 0
    return d


def next_delay(tries: int) -> float:
    """Délai avant le prochain essai après `tries` recherches sans résultat (≥ 1)."""
    return RETRY_DELAYS[min(max(tries, 1), len(RETRY_DELAYS)) - 1]


# appelées quand des paroles sont enregistrées : f(track_id, paroles) — le calage sur la voix (align)
# s'y abonne pour caler aussitôt des paroles brutes
listeners: list = []

# paroles brutes (non synchronisées) : on recherche plus tard une version synchronisée
UPGRADABLE = ("LRCLIB", "tags", "fichier .lrc", "fichier .txt")
UPGRADE_EVERY = 30 * 86400


def _same_text(a: str | None, b: str | None) -> bool:
    return bool(a and b) and " ".join(a.split()) == " ".join(b.split())


def _store(track_id: int, res: dict, tries: int, next_try: float | None) -> dict:
    now = time.time()
    old = db.q1("SELECT * FROM lyrics WHERE track_id=?", track_id)
    align = (None, 0, None, None)
    if old is not None and _same_text(old["plain"], res.get("plain")):
        # mêmes paroles brutes qu'avant (nouvelle recherche) : on garde le calage déjà fait (ou raté)
        align = (old["align_state"], old["align_tries"] or 0, old["align_next"], old["align_error"])
        if not res.get("synced") and old["synced"] and old["align_state"] == "done":
            res = {**res, "synced": old["synced"], "source": old["source"]}
    db.x("""INSERT OR REPLACE INTO lyrics(track_id, synced, plain, source, instrumental, fetched, tries, next_try,
                                          align_state, align_tries, align_next, align_error)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", track_id, res.get("synced"), res.get("plain"), res.get("source"),
         1 if res.get("instrumental") else 0, now, tries, next_try, *align)
    for listener in list(listeners):
        try:
            listener(track_id, res)
        except Exception:
            log.exception("paroles enregistrées : abonné en erreur")
    return _row_to_dict(db.q1("SELECT * FROM lyrics WHERE track_id=?", track_id))


def _plain_next(res: dict) -> float | None:
    """Paroles brutes : quand chercher une version synchronisée (fichier/tags : tout de suite en
    arrière-plan ; LRCLIB : dans un mois)."""
    if res.get("synced") or res.get("instrumental") or not res.get("plain"):
        return None
    if res.get("source") not in UPGRADABLE:
        return None
    return time.time() + (UPGRADE_EVERY if res.get("source") == "LRCLIB" else 0)


# un même titre n'est jamais cherché deux fois en même temps (page ouverte + recherche automatique)
_busy: set[int] = set()
_busy_lock = threading.Condition()

# état de la recherche, pour les Réglages et la console (F7)
_state_lock = threading.Lock()
_current: dict = {}
_recent: collections.deque = collections.deque(maxlen=40)
_session = collections.Counter()
_offline_since: list = [None]
_next_round: list = [None]
_priority: collections.deque = collections.deque(maxlen=100)


def _track(track_id: int):
    return db.q1("""SELECT t.id, t.path, t.title, t.artist, t.duration, a.name AS album, a.artist AS album_artist
                    FROM tracks t JOIN albums a ON a.id=t.album_id WHERE t.id=?""", track_id)


def _lookup(t, why: str) -> dict | None:
    """Recherche en ligne d'un titre, avec journal et état (pour la console)."""
    s = Search()
    short = clean.search_name(t["title"], t["artist"])  # « MOONLIGHT — Kali Uchis » : ce qu'on cherche vraiment
    with _state_lock:
        _current.clear()
        _current.update({"id": t["id"], "title": t["title"], "artist": t["artist"], "query": short, "why": why,
                         "since": time.time()})
    t0 = time.monotonic()
    outcome, detail = "error", ""
    try:
        res = from_lrclib(t["title"], t["artist"], t["album"], t["duration"],
                          album_artist=t["album_artist"] or "", path=t["path"] or "", search=s)
        _offline_since[0] = None
        if res is None:
            outcome = "missing"
        else:
            outcome = "synced" if res.get("synced") else ("plain" if res.get("plain") else "instrumental")
            detail = res.get("match") or ""
        return res
    except Exception as exc:
        outcome, detail = "offline", str(exc)
        if _offline_since[0] is None:
            _offline_since[0] = time.time()
            log.info("LRCLIB injoignable (%s) : la recherche des paroles reprendra plus tard", exc)
        raise
    finally:
        ms = (time.monotonic() - t0) * 1000
        with _state_lock:
            _current.clear()
            _session["searches"] += 1
            _session["requests"] += s.requests
            _session[outcome] += 1
            _recent.appendleft({"ts": time.time(), "id": t["id"], "title": t["title"], "artist": t["artist"],
                                "query": short, "result": outcome, "detail": detail, "requests": s.requests,
                                "ms": round(ms), "why": why})
        if outcome != "offline":
            label = {"synced": "synchronisées", "plain": "non synchronisées", "instrumental": "instrumental",
                     "missing": "introuvables", "error": "erreur"}[outcome]
            log.info("paroles %s : %s%s (%d requête%s, %.1f s, %s)", label, short or t["title"],
                     f" ← {detail}" if detail else "", s.requests, "s" if s.requests > 1 else "", ms / 1000, why)


def get(track_id: int, force: bool = False) -> dict:
    """Paroles d'un titre. Cherche si besoin (fichier, puis LRCLIB quand c'est le moment de réessayer ;
    `force` : tout de suite)."""
    with _busy_lock:
        while track_id in _busy:
            _busy_lock.wait(15)
        _busy.add(track_id)
    try:
        return _get_locked(track_id, force)
    finally:
        with _busy_lock:
            _busy.discard(track_id)
            _busy_lock.notify_all()


def _empty() -> dict:
    return {"found": False, "synced": None, "plain": None, "source": None, "instrumental": False}


def _get_locked(track_id: int, force: bool, why: str = "page") -> dict:
    t = _track(track_id)
    if not t:
        return {"found": False}
    row = db.q1("SELECT * FROM lyrics WHERE track_id=?", track_id)
    if row and not force and (_found(row) or (row["next_try"] or 0) >= NEVER):
        return _row_to_dict(row)  # déjà trouvées (ou effacées à la main)
    res = from_file(t["path"])  # un .lrc a pu être ajouté depuis : on regarde toujours (rapide)
    if res and (res.get("synced") or not force or not settings.get("online_lyrics")):
        if not res.get("synced"):
            wake()  # paroles brutes : version synchronisée cherchée en arrière-plan
        return _store(track_id, res, 0, _plain_next(res))
    if not settings.get("online_lyrics"):
        return _row_to_dict(row) if row else _empty()
    if row and not force and (row["next_try"] or 0) > time.time():
        return _row_to_dict(row)  # pas encore l'heure du prochain essai
    try:
        online = _lookup(t, "à la demande" if force else why)
    except Exception:  # pas d'Internet : ce n'est pas un échec, on réessaiera
        if res:  # (fichier : paroles brutes déjà là)
            return _store(track_id, res, 0, _plain_next(res))
        d = _row_to_dict(row) if row else _empty()
        return {**d, "offline": True}
    if res:  # forcé avec paroles brutes du fichier : on prend la version synchronisée si LRCLIB l'a
        if online and online.get("synced"):
            res = {**online, "plain": online.get("plain") or res.get("plain")}
        return _store(track_id, res, 0, _plain_next(res))
    if online:
        return _store(track_id, online, 0, _plain_next(online))
    tries = (row["tries"] or 0) + 1 if row else 1
    return _store(track_id, {}, tries, time.time() + next_delay(tries))


def upgrade(track_id: int) -> dict:
    """Paroles brutes déjà là : cherche une version synchronisée sur LRCLIB."""
    with _busy_lock:
        if track_id in _busy:
            return {}
        _busy.add(track_id)
    try:
        t = _track(track_id)
        row = db.q1("SELECT * FROM lyrics WHERE track_id=?", track_id)
        if not t or not row or row["synced"] or not row["plain"] or row["source"] not in UPGRADABLE:
            return {}
        try:
            online = _lookup(t, "version synchronisée")
        except Exception:
            return {"offline": True}
        if online and online.get("synced"):
            return _store(track_id, {**online, "plain": online.get("plain") or row["plain"]}, 0, None)
        db.x("UPDATE lyrics SET next_try=? WHERE track_id=?", time.time() + UPGRADE_EVERY, track_id)
        return {}
    finally:
        with _busy_lock:
            _busy.discard(track_id)
            _busy_lock.notify_all()


def save(track_id: int, text: str) -> dict:
    """Paroles saisies à la main (LRC accepté). Texte vide : paroles effacées, et la recherche
    automatique ne les remet pas (« Chercher maintenant » reste possible)."""
    text = (text or "").strip()
    synced = text if is_synced(text) else None
    res = {"synced": synced, "plain": None if synced else (text or None), "source": "manuel" if text else None}
    return _store(track_id, res, 0, None if text else NEVER)


def forget(track_id: int) -> None:
    db.x("DELETE FROM lyrics WHERE track_id=?", track_id)


# ------------------------------------------------------------------ recherche automatique
_stop = threading.Event()
_wake = threading.Event()
_thread: threading.Thread | None = None

_DUE = """(l.track_id IS NULL
            OR (l.synced IS NULL AND l.plain IS NULL AND l.instrumental = 0 AND COALESCE(l.next_try, 0) <= :now)
            OR (l.synced IS NULL AND l.plain IS NOT NULL AND l.next_try IS NOT NULL AND l.next_try <= :now
                AND l.source IN ('LRCLIB', 'tags', 'fichier .lrc', 'fichier .txt')))"""


def due(limit: int = AUTO_BATCH, now: float | None = None) -> list[int]:
    """Titres dont les paroles sont à chercher : jamais cherchés d'abord, puis ceux dont le prochain
    essai est passé, puis les paroles brutes à remplacer par une version synchronisée ; les plus
    écoutés et les plus récents en premier."""
    rows = db.connect().execute(f"""SELECT t.id FROM tracks t LEFT JOIN lyrics l ON l.track_id = t.id WHERE {_DUE}
                    ORDER BY l.track_id IS NOT NULL, l.plain IS NOT NULL, COALESCE(t.last_played, 0) DESC,
                             t.plays DESC, t.id DESC
                    LIMIT :limit""", {"now": time.time() if now is None else now, "limit": limit}).fetchall()
    return [r["id"] for r in rows]


def _is_due(track_id: int) -> bool:
    return db.connect().execute(
        f"SELECT 1 FROM tracks t LEFT JOIN lyrics l ON l.track_id = t.id WHERE t.id = :id AND {_DUE}",
        {"id": track_id, "now": time.time()}).fetchone() is not None


def prioritize(track_id: int) -> None:
    """Titre qu'on va écouter (lecture lancée) : ses paroles sont cherchées avant les autres."""
    try:
        track_id = int(track_id)
    except (TypeError, ValueError):
        return
    if track_id in _priority:
        return
    _priority.append(track_id)
    _wake.set()


def _next_priority() -> int | None:
    while _priority:
        tid = _priority.popleft()
        try:
            if _is_due(tid):
                return tid
        except Exception:
            return None
    return None


def run_once(limit: int = AUTO_BATCH, pause: float = AUTO_PAUSE) -> str:
    """Une tournée de recherche. Renvoie "idle" (rien à chercher), "done", "offline" ou "stopped"."""
    if not settings.get("online_lyrics"):
        _priority.clear()
        return "idle"
    ids = due(limit)
    worked = False
    while True:
        if _stop.is_set():
            return "stopped"
        tid = _next_priority()
        if tid is None:
            if not ids:
                break
            tid = ids.pop(0)
        worked = True
        try:
            row = db.q1("SELECT synced, plain FROM lyrics WHERE track_id=?", tid)
            res = upgrade(tid) if (row and row["plain"] and not row["synced"]) else _get_bg(tid)
        except Exception:
            log.exception("recherche des paroles du titre %s échouée", tid)
            res = {}
        if res.get("offline"):
            return "offline"
        if pause and _stop.wait(pause):
            return "stopped"
    return "done" if worked else "idle"


def _get_bg(track_id: int) -> dict:
    with _busy_lock:
        while track_id in _busy:
            _busy_lock.wait(15)
        _busy.add(track_id)
    try:
        return _get_locked(track_id, False, why="automatique")
    finally:
        with _busy_lock:
            _busy.discard(track_id)
            _busy_lock.notify_all()


def retry_all() -> int:
    """« Tout rechercher maintenant » : les titres introuvables sont recherchés à la prochaine tournée."""
    cur = db.x(f"UPDATE lyrics SET next_try=0 WHERE synced IS NULL AND plain IS NULL AND instrumental=0 "
               f"AND COALESCE(next_try, 0) < {NEVER}")
    n = getattr(cur, "rowcount", 0) or 0
    log.info("paroles : nouvelle recherche demandée pour %d titre(s) introuvable(s)", n)
    wake()
    return n


def status() -> dict:
    """État de la recherche des paroles (Réglages, console F7)."""
    c = db.q1("""SELECT COUNT(*) AS tracks,
                   SUM(l.synced IS NOT NULL) AS synced,
                   SUM(l.synced IS NULL AND l.plain IS NOT NULL) AS plain,
                   SUM(l.synced IS NULL AND l.plain IS NULL AND l.instrumental = 1) AS instrumental,
                   SUM(l.track_id IS NOT NULL AND l.synced IS NULL AND l.plain IS NULL AND l.instrumental = 0
                       AND COALESCE(l.next_try, 0) < ?) AS missing,
                   SUM(l.track_id IS NULL) AS never
                 FROM tracks t LEFT JOIN lyrics l ON l.track_id = t.id""", NEVER)
    counts = {k: int(c[k] or 0) for k in ("tracks", "synced", "plain", "instrumental", "missing", "never")}
    pending = db.connect().execute(
        f"SELECT COUNT(*) AS n FROM tracks t LEFT JOIN lyrics l ON l.track_id = t.id WHERE {_DUE}",
        {"now": time.time()}).fetchone()["n"]
    with _state_lock:
        current = dict(_current) or None
        recent = list(_recent)
        session = dict(_session)
    return {"enabled": bool(settings.get("online_lyrics")), "running": bool(_thread and _thread.is_alive()),
            "current": current, "counts": counts, "pending": int(pending or 0),
            "offlineSince": _offline_since[0], "nextRound": _next_round[0],
            "priority": list(_priority), "recent": recent, "session": session}


def _sleep(seconds: float) -> None:
    _next_round[0] = time.time() + seconds
    _wake.wait(seconds)
    _wake.clear()
    _next_round[0] = None


def _loop() -> None:
    if _stop.wait(AUTO_START_DELAY):
        return
    while not _stop.is_set():
        _wake.clear()  # un réveil demandé avant cette tournée est servi par elle
        try:
            state = run_once()
        except Exception:  # base occupée, etc. : on ne s'arrête jamais pour autant
            log.exception("recherche automatique des paroles")
            state = "idle"
        if state == "offline":
            _sleep(AUTO_OFFLINE_WAIT)
        elif state == "idle":
            _sleep(AUTO_IDLE)
        # "done" : tournée suivante tout de suite (il reste peut-être des titres)


def migrate() -> int:
    """Paroles brutes enregistrées par une version plus ancienne (aucune date de revérification) :
    une version synchronisée sera cherchée une fois, puis chaque mois."""
    cur = db.x("""UPDATE lyrics SET next_try = ? WHERE synced IS NULL AND plain IS NOT NULL AND next_try IS NULL
                  AND source IN ('LRCLIB', 'tags', 'fichier .lrc', 'fichier .txt')""", time.time())
    n = getattr(cur, "rowcount", 0) or 0
    if n:
        log.info("paroles : %d titre(s) avec paroles non synchronisées seront recherchés en version synchronisée", n)
    return n


def start_background() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    try:
        migrate()
    except Exception:
        log.exception("paroles : mise à jour de la base")
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="onde-lyrics", daemon=True)
    _thread.start()


def wake() -> None:
    """Nouveaux titres, ou réglage « Paroles en ligne » activé : la recherche reprend tout de suite."""
    _wake.set()


def stop() -> None:
    _stop.set()
    _wake.set()
