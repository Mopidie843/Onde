"""Téléchargement d'un titre à partir de son nom ou d'un lien (YouTube, SoundCloud, Bandcamp…).

- Nom tapé (« Artiste - Titre » ou juste le titre) : cherche d'abord les vraies infos du
  morceau (iTunes/Deezer : titre, artiste, album, durée, pochette), compare plusieurs
  résultats audio pour choisir le meilleur, puis télécharge.
- Lien collé qui n'est pas un fichier audio direct (page YouTube, SoundCloud…) : téléchargé
  directement avec le même moteur.
Dans les deux cas : MP3, tags et pochette écrits automatiquement, puis rangé dans la
bibliothèque comme n'importe quel autre import.
"""
from __future__ import annotations

import base64
import difflib
import html as htmllib
import json
import logging
import re
import urllib.parse
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import clean, config, covers, library, net

log = logging.getLogger("onde.download")

_ytdlp_ok: bool | None = None


def available() -> bool:
    global _ytdlp_ok
    if _ytdlp_ok is None:
        try:
            import yt_dlp  # noqa: F401
            _ytdlp_ok = True
        except Exception:
            _ytdlp_ok = False
    return _ytdlp_ok


def _need_ytdlp() -> None:
    if not available():
        raise RuntimeError(
            "Le téléchargement en ligne nécessite yt-dlp sur le serveur. Dans le dossier d'Onde : "
            "« .venv/bin/pip install -U yt-dlp » sur le Pi (« .venv\\Scripts\\pip install -U yt-dlp » "
            "sous Windows), puis relance Onde."
        )
    if not config.FFMPEG:
        # sans ffmpeg, yt-dlp récupère bien l'audio mais ne peut pas le convertir en MP3 :
        # le fichier brut (webm/opus…) arriverait illisible par Onde. On arrête tout de suite
        # avec un message clair plutôt que de laisser échouer plus loin, silencieusement.
        raise RuntimeError(
            "ffmpeg est introuvable sur ce serveur : nécessaire pour convertir l'audio téléchargé en MP3. "
            "Installe-le puis relance Onde — Windows : winget install ffmpeg (ou télécharge-le sur "
            "ffmpeg.org et ajoute-le au PATH) ; macOS : brew install ffmpeg ; "
            "Linux/Raspberry Pi : sudo apt install ffmpeg."
        )


def _base_opts(**extra) -> dict:
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "ignoreerrors": "only_download",
        "socket_timeout": 20,
        "retries": 3,
        # client "android" : contourne souvent la vérification anti-robot de YouTube
        # (page web bloquée sans connexion) sans avoir besoin de cookies de connexion.
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        **extra,
    }
    if config.FFMPEG:
        opts["ffmpeg_location"] = config.FFMPEG
    return opts


_junk = re.compile(r"\s*[\(\[][^\)\]]*(official|officiel|lyrics?|paroles|audio|video|vidéo|clip|"
                    r"hd|hq|4k|visuali[sz]er|topic|remaster\w*)[^\)\]]*[\)\]]\s*", re.I)
_bad_words = re.compile(r"\b(live|cover|reaction|karaok[ei]|instrumental|8d audio|sped up|nightcore|"
                         r"slowed|remix|mashup|tutorial|lesson|reagi[oó]n)\b", re.I)
_topic = re.compile(r"\btopic\b|vevo", re.I)
_fold_re = re.compile(r"[^\w]+", re.U)

# parenthèse finale à garder seulement si elle ressemble à une info musicale utile (feat., remix…) —
# sinon (texte promo, hashtags, phrase dans une autre langue…) elle est retirée du titre.
_keep_paren = re.compile(
    r"\b(feat\.?|ft\.?|with|remix|live|cover|acoustic|instrumental|extended|edit|version|mix|"
    r"deluxe|bonus|mono|stereo|demo|session|take|remaster\w*|original|radio|club|dub|vip|reprise)\b",
    re.I,
)
_trailing_paren = re.compile(r"\s*[\(\[]([^()\[\]]*)[\)\]]\s*$")


def _strip_unknown_paren(s: str) -> str:
    """Retire les parenthèses/crochets finaux qui ne portent pas d'info musicale reconnue."""
    prev = None
    while prev != s:
        prev = s
        m = _trailing_paren.search(s)
        if m and not _keep_paren.search(m.group(1)):
            s = s[: m.start()].strip()
    return s


def _clean_title(s: str) -> str:
    """Titre YouTube sans décorations (émojis, [AMV], 4K, Sub. Español, séparateurs…). « Artiste - Titre »
    garde son tiret (le code plus bas en tire l'artiste) ; les autres formes (« Titre ~ Artiste ») sont jointes par
    un tiret long, pour ne jamais prendre le titre pour l'artiste."""
    p = clean.parse(s)
    s = (" - " if p.artist_first else " — ").join(p.parts) if p.parts else (s or "")
    s = _junk.sub(" ", s).strip(" -–|").strip()
    return _strip_unknown_paren(s)


_LINKS = {"et", "and", "y", "e", "und", "feat", "ft", "featuring", "with", "avec", "x"}


def _fold(s: str) -> str:
    """Minuscules, sans accents ni ponctuation, sans les mots de liaison entre artistes :
    « Bigflo et Oli », « Bigflo & Oli », « BIGFLO AND OLI » → « bigflo oli »."""
    s = unicodedata.normalize("NFKD", (s or "").replace("'", "").replace("’", ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(w for w in _fold_re.sub(" ", s).split() if w not in _LINKS)


def _similarity(a: str, b: str) -> float:
    a, b = _fold(a), _fold(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _friendly_error(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "sign in" in low or "bot" in low or "confirm you" in low:
        return ("YouTube demande une vérification anti-robot sur ce serveur (fréquent avec les adresses IP de "
                "certains fournisseurs). Réessaie dans quelques minutes, ou colle un lien direct vers un MP3.")
    if "429" in low or "too many requests" in low:
        return "Trop de demandes envoyées d'un coup — réessaie dans une minute."
    if "unsupported url" in low:
        return "Ce lien n'est pas reconnu (site non pris en charge)."
    return msg[:220]


def _search_candidates(prefix: str, query: str, n: int) -> list[dict]:
    """Renvoie jusqu'à `n` résultats (titre/durée/chaîne) pour `query`, sans télécharger.

    Extraction « à plat » : lit juste la page de résultats, sans ouvrir chaque vidéo — beaucoup
    plus rapide et bien moins susceptible de déclencher une vérification anti-robot.
    """
    import yt_dlp
    opts = _base_opts(extract_flat="in_playlist")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"{prefix}{n}:{query}", download=False)
    except Exception as exc:
        log.info("recherche %s%r échouée : %s", prefix, query, exc)
        return []
    entries = [e for e in (info or {}).get("entries", []) if e]
    return entries


def _score(entry: dict, title: str, artist: str, want_bad: bool, ref_duration: float | None) -> float:
    etitle = entry.get("title") or ""
    uploader = entry.get("uploader") or entry.get("channel") or ""
    short = _clean_title(etitle) or etitle
    score = 0.6 * max(_similarity(short, f"{artist} {title}"), _similarity(short, f"{title} {artist}")) \
        + 0.4 * max(_similarity(short, title), _similarity(short.split(" - ")[-1], title))
    fa = _fold(artist)
    if fa and (fa in _fold(uploader) or _fold(uploader) in fa and len(_fold(uploader)) >= 4):
        score += 0.25
    elif fa and fa in _fold(etitle):
        score += 0.1
    if title and _fold(re.split(r"[(\[]", title)[0]) not in _fold(etitle):
        score -= 0.25  # le titre cherché n'apparaît même pas : sans doute une autre chanson
    if _topic.search(uploader):
        score += 0.2
    if not want_bad and _bad_words.search(etitle):
        score -= 0.5
    dur = entry.get("duration")
    if ref_duration and dur:
        diff = abs(dur - ref_duration)
        score += 0.3 if diff <= 3 else 0.15 if diff <= 8 else -0.1 if diff <= 20 else -0.4
    elif dur and dur < 40:
        score -= 0.3  # trop court pour être le morceau entier (extrait, teaser…)
    if entry.get("availability") in ("private", "needs_auth"):
        score -= 1.0
    if entry.get("age_limit"):
        score -= 0.2
    return score


SOURCES = (("ytsearch", 8), ("scsearch", 6))  # YouTube et SoundCloud


def _pick_best(probe: str, title: str, artist: str, ref_duration: float | None) -> tuple[dict, str] | None:
    """Cherche sur YouTube ET SoundCloud en même temps et garde le meilleur résultat des deux."""
    want_bad = bool(_bad_words.search(probe))
    with ThreadPoolExecutor(len(SOURCES)) as ex:
        found = list(ex.map(lambda src: (src[0], _search_candidates(src[0], probe, src[1])), SOURCES))
    best, best_score, best_src = None, -9.0, None
    for prefix, candidates in found:
        for e in candidates:
            sc = _score(e, title or probe, artist, want_bad, ref_duration)
            if prefix == "scsearch" and (e.get("duration") or 0) and (e.get("duration") or 0) <= 31:
                sc -= 1.0  # extrait de 30 s (titre SoundCloud Go+)
            if sc > best_score:
                best, best_score, best_src = e, sc, prefix
    if best is not None and best_score > -0.3:
        log.info("meilleur résultat pour %r : %s — %s (%s, score %.2f)", probe, best.get("title"),
                 best.get("uploader") or best.get("channel"), "SoundCloud" if best_src == "scsearch" else "YouTube", best_score)
        return best, best_src
    return None


def _download(url_or_search: str, tmp_base: Path) -> Path:
    import yt_dlp
    opts = _base_opts(
        outtmpl=str(tmp_base) + ".%(ext)s",
        postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        postprocessor_args={"ffmpeg": ["-metadata", "comment="]},
        noplaylist=True,
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url_or_search, download=True)
        if info and "entries" in info:
            entries = [e for e in info["entries"] if e]
            info = entries[0] if entries else {}
    mp3 = tmp_base.with_suffix(".mp3")
    if not mp3.exists():
        found = sorted(tmp_base.parent.glob(tmp_base.name + ".*"))
        if not found:
            raise RuntimeError("Le téléchargement a échoué (aucun fichier produit)")
        mp3 = found[0]
    return mp3, (info or {})


def _finish(mp3: Path, entry: dict, meta: dict | None, fallback_query: str) -> dict:
    """Complète les tags/la pochette à partir de `meta` (iTunes/Deezer) et `entry` (yt-dlp), puis range le fichier."""
    yt_title = _clean_title(entry.get("track") or entry.get("title") or fallback_query)
    yt_artist = _clean_title(entry.get("artist") or entry.get("uploader") or entry.get("channel") or "")
    if not yt_artist and " - " in yt_title:
        yt_artist, yt_title = (p.strip() for p in yt_title.split(" - ", 1))

    title = (meta and meta.get("title")) or yt_title or fallback_query
    artist = (meta and meta.get("artist")) or yt_artist or config.UNKNOWN_ARTIST
    fields = {"title": title, "artist": artist}
    if meta:
        fields.update(
            album=meta.get("album") or "",
            album_artist=meta.get("albumArtist") or artist,
            year=meta.get("year") or "",
            genre=meta.get("genre") or "",
            track_no=meta.get("trackNo") or "",
        )
    try:
        library.write_tags(str(mp3), fields)
    except Exception as exc:
        log.info("tags non écrits pour %s : %s", mp3, exc)

    cover_url = (meta and (meta.get("cover") or meta.get("thumb"))) or (entry.get("thumbnail") or "")
    if cover_url:
        try:
            raw = covers.fetch_image(cover_url)
            cid = covers.store_cover(raw)
            jpeg = covers.embed_bytes(cid) if cid else None
            if jpeg:
                library.embed_cover(str(mp3), jpeg)
        except Exception as exc:
            log.info("pochette non intégrée pour %s : %s", mp3, exc)

    name = f"{artist} - {title}.mp3" if artist and artist != config.UNKNOWN_ARTIST else f"{title}.mp3"
    return library.ingest_any(mp3, name)


def _meta_score(r: dict, parts: list[str]) -> float:
    """À quel point ce résultat (Apple Music / Deezer) correspond à ce qui a été tapé (0 à 1 ; 0 = pas lui)."""
    rt, ra = r.get("title") or "", r.get("artist") or ""
    if len(parts) >= 2:
        best = 0.0
        for a, t in ((parts[0], " ".join(parts[1:])), (parts[-1], " ".join(parts[:-1]))):  # « Artiste - Titre » ou l'inverse
            ts = max(_similarity(rt, t), _similarity(re.split(r"[(\[]", rt)[0], t))
            as_ = max(_similarity(ra, a), 1.0 if _fold(a) and (_fold(a) in _fold(ra) or _fold(ra) in _fold(a)) else 0.0)
            if ts >= 0.75 and as_ >= 0.6:
                best = max(best, 0.6 * ts + 0.4 * as_)
        return best
    want = set(_fold(" ".join(parts)).split())
    have = set(_fold(f"{ra} {rt}").split())
    if not want:
        return 0.0
    recall = len(want & have) / len(want)
    return recall * _similarity(f"{ra} {rt}", " ".join(parts)) ** 0.5 if recall >= 0.75 else 0.0


def _lookup_meta(query: str, parts: list[str] | None = None) -> dict | None:
    """Les vraies infos du morceau (titre, artiste, album, durée, pochette), seulement si c'est bien lui :
    un résultat qui ne correspond pas à ce qui a été tapé n'est jamais pris (sinon on téléchargerait
    une autre chanson). Plusieurs formulations sont essayées (« et » / « & », titre seul + artiste…)."""
    parts = parts or [query]
    tries = [query]
    plain = " ".join(_fold(p) for p in parts)
    if plain and plain != " ".join(query.lower().split()):
        tries.append(plain)  # « bigflo et oli alors alors » → « bigflo oli alors alors »
    if len(parts) >= 2:
        tries.append(f"{' '.join(parts[1:])} {parts[0]}")  # titre d'abord
    seen = set()
    for q in tries:
        if q.lower() in seen:
            continue
        seen.add(q.lower())
        try:
            found = covers.search_meta(q, limit=8)
        except Exception as exc:
            log.info("recherche de métadonnées échouée pour %r : %s", q, exc)
            continue
        scored = [(_meta_score(r, parts), r) for r in found.get("results") or []]
        scored = [x for x in scored if x[0] > 0]
        if scored:
            return max(scored, key=lambda x: x[0])[1]
    return None


def search_download(query: str) -> dict:
    """Cherche `query` en ligne (métadonnées + audio) et importe le meilleur résultat trouvé."""
    query = (query or "").strip()
    if not query:
        raise ValueError("Recherche vide")
    _need_ytdlp()

    # 1) métadonnées propres d'abord (titre, artiste, album, durée, pochette) : elles guident
    #    le choix du meilleur résultat audio et évitent les faux titres YouTube.
    parsed = clean.parse(query)  # « MOONLIGHT🌙~ Kali Uchis || [AMV] 4K » → « MOONLIGHT Kali Uchis »
    short = " ".join(parsed.parts) or query
    meta = _lookup_meta(short, parsed.parts)
    ref_title = (meta and meta.get("title")) or ""
    ref_artist = (meta and meta.get("artist")) or ""
    ref_duration = (meta and meta.get("duration")) or None
    if not ref_title and len(parsed.parts) >= 2:
        if parsed.artist_first:  # « Artiste - Titre »
            a, t = parsed.parts[0], " ".join(parsed.parts[1:])
        else:                    # « Titre ~ Artiste », « Titre — Artiste »
            t, a = parsed.parts[0], " ".join(parsed.parts[1:])
        ref_artist, ref_title = a.strip(), t.strip()
    probe = f"{ref_artist} {ref_title}".strip() or short

    config.INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    tmp_base = config.INCOMING_DIR / uuid.uuid4().hex

    # 2) plusieurs candidats (YouTube et SoundCloud), notés et triés
    picked = None
    try:
        picked = _pick_best(probe, ref_title, ref_artist, ref_duration)
    except Exception as exc:
        log.info("comparaison des résultats échouée, repli sur une recherche simple : %s", exc)

    try:
        if picked:
            entry_hint, source = picked
            url = entry_hint.get("url") or entry_hint.get("webpage_url")
            mp3, entry = _download(url or f"ytsearch1:{probe}", tmp_base)
        else:
            # aucun résultat assez proche : on prend quand même le 1er résultat YouTube
            mp3, entry = _download(f"ytsearch1:{probe}", tmp_base)
    except Exception as exc:
        for p in tmp_base.parent.glob(tmp_base.name + "*"):
            p.unlink(missing_ok=True)
        raise RuntimeError(_friendly_error(exc)) from exc

    return _finish(mp3, entry, meta, short)


_yt_playlist = re.compile(r"(?:music\.)?youtube\.com/(?:playlist\?|watch\?.*[&?]list=)", re.I)
_deezer_playlist = re.compile(r"deezer\.com/(?:\w\w/)?playlist/(\d+)", re.I)
_soundcloud_playlist = re.compile(
    r"^https?://(?:(?:www|m)\.)?soundcloud\.com/(?!discover|search|charts|upload|you|stream|pages|terms)[\w-]+"
    r"(?:/(?:sets/[\w-]+|likes|tracks|albums|reposts|popular-tracks|toptracks))?/?(?:[?#].*)?$", re.I)
_spotify_playlist = re.compile(
    r"open\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?playlist/([A-Za-z0-9]+)|spotify:playlist:([A-Za-z0-9]+)", re.I)


def playlist_kind(url: str) -> str | None:
    """Devine le service d'une URL de playlist collée, ou None si ce n'en est pas une reconnue."""
    if _yt_playlist.search(url or ""):
        return "youtube"
    if _deezer_playlist.search(url or ""):
        return "deezer"
    if _spotify_playlist.search(url or ""):
        return "spotify"
    if _soundcloud_playlist.match((url or "").strip()):
        return "soundcloud"  # playlist (sets), likes, ou tous les titres d'un artiste
    return None


def _resolve_soundcloud_playlist(url: str) -> tuple[str, list[str]]:
    """Les liens des titres d'une playlist SoundCloud : chacun est ensuite téléchargé directement
    depuis SoundCloud (pas recherché ailleurs)."""
    import yt_dlp
    opts = _base_opts(extract_flat="in_playlist", playlistend=500, noplaylist=False)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise RuntimeError(_friendly_error(exc)) from exc
    links = []
    for e in (info or {}).get("entries") or []:
        link = e and (e.get("webpage_url") or e.get("url"))
        if link and link.startswith("http") and link not in links:
            links.append(link)
    if not links:
        raise RuntimeError("Playlist SoundCloud introuvable ou vide")
    return (info or {}).get("title") or "Playlist SoundCloud", links


def _resolve_youtube_playlist(url: str) -> tuple[str, list[str]]:
    import yt_dlp
    opts = _base_opts(extract_flat="in_playlist", playlistend=500, noplaylist=False)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise RuntimeError(_friendly_error(exc)) from exc
    entries = [e for e in (info or {}).get("entries", []) if e and e.get("title")]
    if not entries:
        raise RuntimeError("Playlist YouTube introuvable ou vide")
    return (info or {}).get("title") or "Playlist YouTube", [e["title"] for e in entries]


def _resolve_deezer_playlist(url: str) -> tuple[str, list[str]]:
    m = _deezer_playlist.search(url)
    if not m:
        raise RuntimeError("Lien de playlist Deezer non reconnu")
    try:
        data = covers.deezer_api(f"https://api.deezer.com/playlist/{m.group(1)}")
    except (net.NetError, ValueError) as exc:
        raise RuntimeError(f"Deezer injoignable (vérifie la connexion Internet du serveur) : {exc}") from exc
    if data.get("error"):
        raise RuntimeError("Playlist Deezer introuvable (privée ou supprimée ?)")
    tracks = (data.get("tracks") or {}).get("data") or []
    if not tracks:
        raise RuntimeError("Cette playlist Deezer est vide")
    queries = [f"{t['artist']['name']} - {t['title']}" for t in tracks if t.get("title") and t.get("artist")]
    return data.get("title") or "Playlist Deezer", queries


# --------------------------------------------------------------------------- #
# Spotify (sans compte ni clé d'API) : on lit les pages publiques. Leur structure change de temps en
# temps, d'où plusieurs méthodes, de la plus fiable à la plus approximative :
#   1. page « embed » : JSON __NEXT_DATA__ (méthode principale) ;
#   2. autres JSON de la page : application/json, ld+json, « resource » (ancien embed, encodé
#      en URL), « initial-state » (page web, en base64) ;
#   3. données « flight » de Next.js (self.__next_f.push) si Spotify passe à l'App Router ;
#   4. paires "title"/"subtitle" trouvées n'importe où dans le texte ;
#   5. liens /track/… et /artist/… du HTML rendu côté serveur ;
# d'abord sur la page embed, puis sur la page publique de la playlist. Le nom de la playlist vient
# du JSON, sinon des balises og:title / twitter:title / <title>, sinon de l'API oEmbed officielle.
# --------------------------------------------------------------------------- #
SPOTIFY_MAX_TRACKS = 1000
_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)
_ATTR = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_META = re.compile(r"<meta\b[^>]*>", re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.S | re.I)
_FLIGHT = re.compile(r'self\.__next_f\.push\(\[\s*\d+\s*,\s*"((?:[^"\\]|\\.)*)"\s*\]\)', re.S)
_FLIGHT_ROW = re.compile(r"^[0-9a-zA-Z]+:([\[{].*)$")
_PAIR = re.compile(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"subtitle"\s*:\s*"((?:[^"\\]|\\.)*)"')
_LINK = re.compile(
    r"""<a\b[^>]*?\bhref=["'](?:https?://open\.spotify\.com)?/(?:intl-[\w-]+/)?(track|artist)/([A-Za-z0-9]+)[^"']*["'][^>]*>(.*?)</a\s*>""",
    re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_WRAPPERS = ("track", "itemV2", "item", "data")  # enveloppes fréquentes autour d'un morceau


def _clean_text(s: str | None) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(s or "")).strip()


def _attrs(tag_inside: str) -> dict[str, str]:
    return {k.lower(): (a or b or c) for k, a, b, c in _ATTR.findall(tag_inside)}


def _loads(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _loads_b64(text: str):
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", text) + "==", validate=False)
        return json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def _artist_names(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [n for a in v for n in _artist_names(a)]
    if isinstance(v, dict):
        if isinstance(v.get("items"), list):
            return _artist_names(v["items"])
        if isinstance(v.get("name"), str):
            return [v["name"]]
        if isinstance(v.get("profile"), dict) and isinstance(v["profile"].get("name"), str):
            return [v["profile"]["name"]]
    return []


def _track_tuple(x) -> tuple[str, str] | None:
    """(titre, artistes) si l'objet JSON ressemble à un morceau, sinon None. Formes reconnues :
    embed {title, subtitle} ; API web {name, artists:[{name}]} ; page web récente
    {name, artists:{items:[{profile:{name}}]}} ; schema.org {name, byArtist} ; et leurs enveloppes
    ({"track": …}, {"itemV2": {"data": …}}…)."""
    for _ in range(len(_WRAPPERS) + 1):
        if not isinstance(x, dict):
            return None
        title = x.get("title")
        if isinstance(title, str) and title.strip() and isinstance(x.get("subtitle"), str):
            return title, x["subtitle"]
        name = x.get("name")
        if isinstance(name, str) and name.strip():
            for key in ("artists", "byArtist", "artist"):
                if key in x:
                    names = [n.strip() for n in _artist_names(x[key]) if n and n.strip()]
                    if names:
                        return name, ", ".join(dict.fromkeys(names))
        x = next((x[k] for k in _WRAPPERS if isinstance(x.get(k), dict)), None)
    return None


def _find_track_lists(node, out: list[list[tuple[str, str]]], depth: int = 0) -> None:
    """Cherche récursivement des listes d'objets ressemblant à des morceaux : la structure exacte des
    pages Spotify change de temps en temps, donc on reconnaît la « forme » des données plutôt qu'un
    chemin fixe."""
    if depth > 16:  # garde-fou : structure trop profonde
        return
    if isinstance(node, list):
        tracks = [t for t in (_track_tuple(x) for x in node[:5000] if isinstance(x, dict)) if t]
        if tracks and len(tracks) >= 0.6 * len(node):
            out.append(tracks)
            return
        for x in node:
            _find_track_lists(x, out, depth + 1)
    elif isinstance(node, dict):
        for v in node.values():
            _find_track_lists(v, out, depth + 1)


def _json_blobs(html: str) -> list:
    """Les blocs JSON de la page, __NEXT_DATA__ en premier."""
    first, others = [], []
    for m in _SCRIPT.finditer(html):
        attrs = _attrs(m.group(1))
        body = m.group(2).strip()
        if not body or attrs.get("src"):
            continue
        sid, stype = attrs.get("id", ""), attrs.get("type", "").lower()
        data = None
        if sid == "__NEXT_DATA__" or stype in ("application/json", "application/ld+json"):
            data = _loads(body)
            if data is None and sid == "resource":  # ancien embed : JSON encodé en URL
                data = _loads(urllib.parse.unquote(body))
        elif sid == "initial-state" or stype == "text/plain":  # page web : JSON en base64
            data = _loads_b64(body)
        if data is not None:
            (first if sid == "__NEXT_DATA__" else others).append(data)
    return first + others


def _flight_text(html: str) -> str:
    parts = []
    for m in _FLIGHT.finditer(html):
        text = _loads(f'"{m.group(1)}"')
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _flight_blobs(flight: str) -> list:
    out = []
    for line in flight.splitlines():
        m = _FLIGHT_ROW.match(line.strip())
        if m:
            data = _loads(m.group(1))
            if data is not None:
                out.append(data)
    return out


def _pairs(text: str) -> list[tuple[str, str]]:
    out = []
    for m in _PAIR.finditer(text or ""):
        title, sub = _loads(f'"{m.group(1)}"'), _loads(f'"{m.group(2)}"')
        if isinstance(title, str) and isinstance(sub, str) and title.strip():
            out.append((title, sub))
    return out


def _tracks_from_links(html: str) -> list[tuple[str, str]]:
    """Lignes du HTML rendu côté serveur : un lien /track/… suivi des liens /artist/… du morceau."""
    rows: list[list] = []  # [id, titre, [artistes]]
    for kind, ident, inner in _LINK.findall(html):
        text = _clean_text(_TAGS.sub(" ", inner))
        if kind.lower() == "track":
            if rows and rows[-1][0] == ident:  # même morceau (lien sur l'image puis sur le titre)
                rows[-1][1] = rows[-1][1] or text
                continue
            rows.append([ident, text, []])
        elif rows and text and text not in rows[-1][2]:
            rows[-1][2].append(text)
    return [(t, ", ".join(a)) for _, t, a in rows if t]


def _meta_content(html: str, *keys: str) -> str | None:
    for tag in _META.findall(html):
        attrs = _attrs(tag[5:])
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in keys and attrs.get("content"):
            return _clean_text(attrs["content"])
    return None


def _usable_name(name: str | None) -> str | None:
    name = _clean_text(name)
    if not name or name.lower().startswith("spotify"):  # page d'erreur ou titre générique
        return None
    return name


def _html_name(html: str) -> str | None:
    name = _usable_name(_meta_content(html, "og:title", "twitter:title"))
    if name:
        return name
    m = _TITLE.search(html)
    if not m:
        return None
    title = re.sub(r"\s*[|·–-]\s*Spotify\s*$", "", _clean_text(m.group(1)))
    title = re.sub(r"\s+[-–]\s+(?:playlist|Playlist)\b.*$", "", title)  # « X - playlist by Y »
    return _usable_name(title)


def _entity_name(data) -> str | None:
    try:
        name = data["props"]["pageProps"]["state"]["data"]["entity"]["name"]
    except (KeyError, TypeError):
        return None
    return _usable_name(name) if isinstance(name, str) else None


def _spotify_parse(html: str) -> tuple[str | None, list[tuple[str, str]]]:
    """(nom, [(titre, artistes)]) d'une page Spotify, méthode par méthode (voir plus haut)."""
    name = None
    flight = _flight_text(html)
    for data in _json_blobs(html) + _flight_blobs(flight):
        name = name or _entity_name(data)
        lists: list[list[tuple[str, str]]] = []
        _find_track_lists(data, lists)
        if lists:
            return name or _html_name(html), max(lists, key=len)
    name = name or _html_name(html)
    pairs = [p for p in (_pairs(flight) or _pairs(html)) if p[0] != name]
    if pairs:
        return name, pairs
    return name, _tracks_from_links(html)


def _spotify_oembed_name(page_url: str) -> str | None:
    """Nom de la playlist via l'API oEmbed publique de Spotify (stable, mais sans les titres)."""
    try:
        data = net.get_json("https://open.spotify.com/oembed?" + urllib.parse.urlencode({"url": page_url}),
                            deadline=8.0, retries=0)
    except (net.NetError, ValueError):
        return None
    title = data.get("title") if isinstance(data, dict) else None
    return _usable_name(title) if isinstance(title, str) else None


def _resolve_spotify_playlist(url: str) -> tuple[str, list[str]]:
    m = _spotify_playlist.search(url or "")
    if not m:
        raise RuntimeError("Lien de playlist Spotify non reconnu")
    pid = m.group(1) or m.group(2)
    pages = (f"https://open.spotify.com/embed/playlist/{pid}", f"https://open.spotify.com/playlist/{pid}")
    headers = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6"}
    name, missing = None, 0
    for i, page in enumerate(pages):
        try:
            html = net.get_text(page, limit=6_000_000, deadline=12.0, headers=headers)
        except net.NetError as exc:
            if exc.status == 404:
                missing += 1
                continue
            if exc.status == 429:
                raise RuntimeError("Spotify reçoit trop de demandes de ce serveur : réessaie dans une minute.") from exc
            if i == 0 and exc.status is None:  # réseau coupé : inutile d'insister
                raise RuntimeError(f"Spotify injoignable (vérifie la connexion Internet du serveur) : {exc}") from exc
            log.info("page Spotify %s illisible : %s", page, exc)
            continue
        found, tracks = _spotify_parse(html)
        name = name or found
        if tracks:
            queries = [f"{a} - {t}" if a.strip() else t for t, a in tracks][:SPOTIFY_MAX_TRACKS]
            if i or not found:
                log.info("playlist Spotify lue par une méthode de secours (%s, %d titres)", page, len(queries))
            return name or "Playlist Spotify", queries
    if missing == len(pages):
        raise RuntimeError("Playlist Spotify introuvable (privée ou supprimée ?)")
    name = name or _spotify_oembed_name(pages[1])
    if name:
        raise RuntimeError(
            f"La playlist Spotify « {name} » a bien été trouvée, mais Spotify ne laisse pas lire la liste de ses "
            "titres pour l'instant (son site a changé). Copie plutôt les titres dans la liste à importer.")
    raise RuntimeError(
        "Impossible de lire cette playlist Spotify (Spotify a changé son site, ou la playlist est privée). "
        "Copie plutôt les titres à la main dans la liste.")


def resolve_playlist(url: str) -> dict:
    """Résout un lien de playlist (YouTube, Deezer, Spotify) en une liste ordonnée de requêtes
    « Artiste - Titre », prête à passer dans le téléchargement par liste."""
    kind = playlist_kind(url)
    if kind == "youtube":
        _need_ytdlp()
        name, queries = _resolve_youtube_playlist(url)
    elif kind == "deezer":
        name, queries = _resolve_deezer_playlist(url)
    elif kind == "spotify":
        name, queries = _resolve_spotify_playlist(url)
    elif kind == "soundcloud":
        _need_ytdlp()
        name, queries = _resolve_soundcloud_playlist(url)
    else:
        raise ValueError(
            "Lien de playlist non reconnu (YouTube, SoundCloud, Deezer et Spotify sont pris en charge)."
        )
    return {"name": name, "kind": kind, "queries": queries}


def download_from_url(url: str) -> dict:
    """Télécharge directement depuis un lien de page (YouTube, SoundCloud, Bandcamp…)."""
    url = (url or "").strip()
    if not url:
        raise ValueError("Lien vide")
    _need_ytdlp()

    config.INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    tmp_base = config.INCOMING_DIR / uuid.uuid4().hex
    try:
        mp3, entry = _download(url, tmp_base)
    except Exception as exc:
        for p in tmp_base.parent.glob(tmp_base.name + "*"):
            p.unlink(missing_ok=True)
        raise RuntimeError(_friendly_error(exc)) from exc

    query = entry.get("title") or url
    t = _clean_title(entry.get("track") or entry.get("title") or "")
    a = _clean_title(entry.get("artist") or entry.get("uploader") or entry.get("channel") or "")
    if " - " in t:
        a, t = (x.strip() for x in t.split(" - ", 1))
    meta = _lookup_meta(f"{a} {t}".strip(), [a, t] if a and t else None) if (a or t) else None
    return _finish(mp3, entry, meta, query)
