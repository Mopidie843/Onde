"""Pochettes : stockage, miniatures WebP, couleur dominante, recherche en ligne."""
from __future__ import annotations

import colorsys
import difflib
import hashlib
import io
import logging
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageOps

from . import config, db, net

log = logging.getLogger("onde.covers")

THUMB_SIZES = (96, 192, 384, 768)
Image.MAX_IMAGE_PIXELS = 80_000_000
UA = net.UA  # (gardé pour le code qui l'utilisait)
IMAGE_LIMIT = 25_000_000  # octets
IMAGE_DEADLINE = 20.0     # s pour télécharger une image complète
DEEZER_QUOTA_WAIT = 1.0   # s : l'API Deezer limite à 50 requêtes par tranche de 5 s


# --------------------------------------------------------------------------- #
# Stockage
# --------------------------------------------------------------------------- #
def _open_image(raw: bytes) -> tuple[Image.Image, bool]:
    """Ouvre l'image ; indique si les octets bruts sont directement réutilisables (JPEG sain)."""
    im = Image.open(io.BytesIO(raw))
    reusable = (im.format == "JPEG" and im.mode == "RGB" and max(im.size) <= 1200
                and im.getexif().get(0x0112, 1) == 1)
    try:
        im.draft("RGB", (1600, 1600))  # décodage JPEG réduit : beaucoup plus rapide
    except Exception:
        pass
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P", "PA"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (18, 18, 18))
        bg.paste(im, mask=im.getchannel("A"))
        return bg, False
    return im.convert("RGB"), reusable


def cover_path(cover_id: str) -> Path:
    return config.COVERS_DIR / f"{cover_id}.jpg"


def store_cover(raw: bytes | None) -> str | None:
    """Enregistre une image (octets bruts) et renvoie son identifiant (hash)."""
    if not raw or len(raw) < 100:
        return None
    cid = hashlib.sha1(raw).hexdigest()[:24]
    if cover_path(cid).exists() and db.q1("SELECT 1 FROM covers WHERE id=?", cid):
        return cid
    try:
        im, reusable = _open_image(raw)
    except Exception as exc:  # image corrompue
        log.debug("image illisible: %s", exc)
        return None
    if min(im.size) < 16:
        return None
    out = cover_path(cid)
    tmp = out.with_name(f"{out.name}.{threading.get_ident()}.tmp")
    if reusable:
        tmp.write_bytes(raw)  # JPEG déjà optimal : copie telle quelle (aucune perte)
    else:
        im.thumbnail((1200, 1200), Image.LANCZOS)
        im.save(tmp, "JPEG", quality=90, optimize=True, progressive=True)
    os.replace(tmp, out)
    db.x(
        "INSERT OR REPLACE INTO covers(id, color, w, h, created) VALUES(?,?,?,?,?)",
        cid, dominant_color(im), im.width, im.height, time.time(),
    )
    return cid


def thumb_file(cover_id: str, size: int, fmt: str = "webp") -> Path | None:
    """Miniature carrée en cache disque (WebP par défaut, JPEG pour l'écran de verrouillage)."""
    if not re.fullmatch(r"[0-9a-f]{8,40}", cover_id or ""):
        return None
    if size >= 1000:
        p = cover_path(cover_id)
        return p if p.exists() else None
    size = min(THUMB_SIZES, key=lambda s: abs(s - size))
    ext = "jpg" if fmt == "jpg" else "webp"
    out = config.THUMBS_DIR / f"{cover_id}_{size}.{ext}"
    if out.exists():
        return out
    src = cover_path(cover_id)
    if not src.exists():
        return None
    with Image.open(src) as im:
        im.draft("RGB", (size * 2, size * 2))
        im = ImageOps.fit(im.convert("RGB"), (size, size), Image.LANCZOS)
    tmp = out.with_name(f"{out.name}.{threading.get_ident()}.tmp")
    if ext == "jpg":
        im.save(tmp, "JPEG", quality=86, optimize=True, progressive=True)
    else:
        im.save(tmp, "WEBP", quality=82, method=4)
    os.replace(tmp, out)
    return out


def embed_bytes(cover_id: str, max_size: int = 800) -> bytes | None:
    """JPEG compact (800 px) destiné à être écrit dans les fichiers audio."""
    src = cover_path(cover_id)
    if not src.exists():
        return None
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((max_size, max_size), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()


def dominant_color(im: Image.Image) -> str:
    """Couleur « d'ambiance » de la pochette, assombrie pour servir de fond."""
    small = im.convert("RGB").resize((48, 48), Image.BILINEAR)
    pal_img = small.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    palette = pal_img.getpalette() or []
    best, best_score = (60, 60, 70), -1.0
    for count, idx in pal_img.getcolors() or []:
        r, g, b = palette[idx * 3: idx * 3 + 3]
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        weight = 1.0 if 0.12 < l < 0.88 else 0.25
        score = count * (0.35 + s) * weight
        if score > best_score:
            best, best_score = (r, g, b), score
    h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in best))
    l = min(max(l, 0.30), 0.46)
    s = min(s, 0.72)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


# --------------------------------------------------------------------------- #
# Recherche en ligne (Apple Music / iTunes + Deezer, sans clé d'API)
#
# Toutes les requêtes passent par onde.net (client httpx partagé : délais stricts, taille plafonnée,
# nouvel essai sur les erreurs passagères). Ces fonctions sont bloquantes et tournent dans des
# threads (routes synchrones, tâches de fond) ; net refuse de les exécuter sur la boucle asyncio.
# --------------------------------------------------------------------------- #
def _get_json(url: str) -> dict:
    return net.get_json(url)


def deezer_api(url: str) -> dict:
    """GET sur l'API Deezer. Deezer signale ses erreurs dans un JSON (HTTP 200) ; le dépassement de
    quota (code 4) arrive vite pendant la recherche automatique des pochettes d'une grosse
    bibliothèque : on patiente et on réessaie, au lieu de conclure à tort qu'il n'y a « aucun
    résultat » (l'album serait alors marqué comme sans pochette). Les autres erreurs restent dans
    la réponse, à l'appelant de les traiter."""
    data: dict = {}
    for attempt in range(3):
        data = net.get_json(url)
        err = data.get("error") if isinstance(data, dict) else None
        if not (isinstance(err, dict) and err.get("code") == 4) or attempt == 2:
            return data
        log.debug("quota Deezer atteint, nouvel essai dans %.1f s", DEEZER_QUOTA_WAIT * (attempt + 1))
        time.sleep(DEEZER_QUOTA_WAIT * (attempt + 1))
    return data


def _deezer_search(path: str, query: str, limit: int) -> list[dict]:
    params = urllib.parse.urlencode({"q": query, "limit": limit})
    data = deezer_api(f"https://api.deezer.com/search/{path}?{params}")
    err = data.get("error") if isinstance(data, dict) else None
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise net.NetError(f"Deezer : {msg or 'erreur'}")
    return data.get("data") or []


def fetch_image(url: str) -> bytes:
    """Télécharge une image (25 Mo et 20 s au plus) et vérifie que c'en est bien une."""
    raw = net.get_bytes(url, limit=IMAGE_LIMIT, deadline=IMAGE_DEADLINE,
                        headers={"Accept": "image/avif,image/webp,image/*,*/*;q=0.5"})
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.verify()
    except Exception as exc:
        raise ValueError("ce lien ne pointe pas vers une image") from exc
    return raw


def _itunes(query: str, kind: str, limit: int) -> list[dict]:
    entity = "album" if kind == "album" else "song"
    params = urllib.parse.urlencode(
        {"term": query, "entity": entity, "media": "music", "limit": limit}
    )
    data = _get_json(f"https://itunes.apple.com/search?{params}")
    out = []
    for r in data.get("results", []):
        art = r.get("artworkUrl100")
        if not art:
            continue
        out.append({
            "source": "Apple Music",
            "title": r.get("collectionName") if kind == "album" else r.get("trackName"),
            "album": r.get("collectionName") or "",
            "artist": r.get("artistName") or "",
            "year": (r.get("releaseDate") or "")[:4],
            "thumb": art.replace("100x100bb", "300x300bb"),
            "url": art.replace("100x100bb", "1200x1200bb"),
        })
    return out


def _deezer(query: str, kind: str, limit: int) -> list[dict]:
    out = []
    for r in _deezer_search("album" if kind == "album" else "track", query, limit):
        alb = r if kind == "album" else (r.get("album") or {})
        if not alb.get("cover_xl"):
            continue
        out.append({
            "source": "Deezer",
            "title": r.get("title") or "",
            "album": alb.get("title") or "",
            "artist": (r.get("artist") or {}).get("name", ""),
            "year": "",
            "thumb": alb.get("cover_medium") or alb.get("cover_big"),
            "url": alb.get("cover_xl"),
        })
    return out


def _deezer_artists(query: str, limit: int) -> list[dict]:
    out = []
    for r in _deezer_search("artist", query, limit):
        url = r.get("picture_xl") or ""
        if not url or "/artist//" in url:  # image par défaut de Deezer (silhouette)
            continue
        out.append({
            "source": "Deezer", "title": r.get("name") or "", "album": "", "artist": r.get("name") or "",
            "year": "", "fans": r.get("nb_fan") or 0,
            "thumb": r.get("picture_medium") or r.get("picture_big") or url, "url": url,
        })
    return out


def find_artist_image(name: str) -> str | None:
    """URL de la photo Deezer de l'artiste si le nom correspond vraiment (sinon None)."""
    target = db.norm(name)
    if not target:
        return None
    best, best_score = None, 0.0
    for r in _deezer_artists(name, 6):
        n = db.norm(r["artist"])
        score = 1.0 if n == target else difflib.SequenceMatcher(None, n, target).ratio()
        score += min(r["fans"], 5_000_000) / 50_000_000  # départage les homonymes
        if score > best_score:
            best, best_score = r, score
    return best["url"] if best and best_score >= 0.9 else None


def search_online(query: str, kind: str = "album", limit: int = 12) -> dict:
    query = (query or "").strip()
    if not query:
        return {"results": [], "errors": []}
    if kind == "artist":
        try:
            return {"results": _deezer_artists(query, limit), "errors": []}
        except Exception as exc:
            return {"results": [], "errors": [f"Deezer : {exc}"]}
    errors: list[str] = []
    lists: list[list[dict]] = []
    with ThreadPoolExecutor(2) as ex:
        futures = {ex.submit(fn, query, kind, limit): name
                   for fn, name in ((_itunes, "Apple Music"), (_deezer, "Deezer"))}
        for fut, name in futures.items():
            try:
                lists.append(fut.result())
            except Exception as exc:
                errors.append(f"{name} : {exc}")
                log.info("recherche pochette %s échouée: %s", name, exc)
    # entrelace les sources pour varier les propositions
    results, seen = [], set()
    for i in range(max((len(l) for l in lists), default=0)):
        for lst in lists:
            if i < len(lst) and lst[i]["url"] not in seen:
                seen.add(lst[i]["url"])
                results.append(lst[i])
    return {"results": results, "errors": errors}


def _itunes_meta(query: str, limit: int) -> list[dict]:
    params = urllib.parse.urlencode({"term": query, "entity": "song", "media": "music", "limit": limit,
                                     "country": "FR"})
    data = _get_json(f"https://itunes.apple.com/search?{params}")
    out = []
    for r in data.get("results", []):
        art = r.get("artworkUrl100") or ""
        out.append({
            "source": "Apple Music", "title": r.get("trackName") or "", "artist": r.get("artistName") or "",
            "album": r.get("collectionName") or "",
            "albumArtist": r.get("collectionArtistName") or r.get("artistName") or "",
            "year": (r.get("releaseDate") or "")[:4], "genre": r.get("primaryGenreName") or "",
            "trackNo": r.get("trackNumber"), "discNo": r.get("discNumber"),
            "duration": round((r.get("trackTimeMillis") or 0) / 1000),
            "thumb": art.replace("100x100bb", "300x300bb") if art else "",
            "cover": art.replace("100x100bb", "1200x1200bb") if art else "",
        })
    return out


def _deezer_meta(query: str, limit: int) -> list[dict]:
    out = []
    for r in _deezer_search("track", query, limit):
        alb = r.get("album") or {}
        out.append({
            "source": "Deezer", "title": r.get("title") or "", "artist": (r.get("artist") or {}).get("name", ""),
            "album": alb.get("title") or "", "albumArtist": (r.get("artist") or {}).get("name", ""),
            "year": "", "genre": "", "trackNo": None, "discNo": None, "duration": r.get("duration") or 0,
            "thumb": alb.get("cover_medium") or "", "cover": alb.get("cover_xl") or "",
        })
    return out


def search_meta(query: str, limit: int = 10) -> dict:
    """Infos complètes d'un titre (album, année, genre, n° de piste, pochette) pour compléter les tags."""
    query = (query or "").strip()
    if not query:
        return {"results": [], "errors": []}
    results, errors, seen = [], [], set()
    with ThreadPoolExecutor(2) as ex:
        futures = [(ex.submit(_itunes_meta, query, limit), "Apple Music"), (ex.submit(_deezer_meta, query, limit), "Deezer")]
        for fut, name in futures:
            try:
                for r in fut.result():
                    key = (db.norm(r["title"]), db.norm(r["artist"]), db.norm(r["album"]))
                    if key not in seen:
                        seen.add(key)
                        results.append(r)
            except Exception as exc:
                errors.append(f"{name} : {exc}")
    return {"results": results[:16], "errors": errors}


_junk = re.compile(
    r"[\(\[][^\)\]]*(deluxe|remaster|edition|édition|version|expanded|bonus|anniversary|single|ep)[^\)\]]*[\)\]]"
    r"|\s-\s(single|ep)$",
    re.I,
)


def _clean(s: str) -> str:
    return db.norm(_junk.sub("", s or ""))


def _sim(a: str, b: str) -> float:
    a, b = _clean(a), _clean(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        ratio = max(ratio, 0.85)
    return ratio


def best_match(results: list[dict], title: str, artist: str, kind: str) -> dict | None:
    unknown_artist = not artist or artist == config.UNKNOWN_ARTIST
    best, best_score = None, 0.0
    for r in results:
        t_score = _sim(r["title"] if kind == "song" else r["album"], title)
        a_score = 1.0 if unknown_artist else _sim(r["artist"], artist)
        if t_score < 0.72 or (not unknown_artist and a_score < 0.5):
            continue
        if unknown_artist and t_score < 0.95:
            continue
        score = t_score * 0.6 + a_score * 0.4
        if score > best_score:
            best, best_score = r, score
    return best
