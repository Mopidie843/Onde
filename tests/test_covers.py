"""Pochettes : téléchargement d'image (réseau simulé), enregistrement, miniatures, et couleur
dominante utilisée comme fond de l'interface."""
from __future__ import annotations

import asyncio
import colorsys
import io
import re

import httpx
import pytest
from PIL import Image

from onde import config, covers, db, net

IMG_URL = "https://img.example.com/cover.jpg"


def jpeg(color=(200, 30, 30), size=(300, 300), fmt="JPEG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


def split(colors: list[tuple[tuple[int, int, int], float]], size=100) -> Image.Image:
    """Image en bandes verticales : [(couleur, part de la largeur)]."""
    im = Image.new("RGB", (size, size))
    x = 0
    for color, share in colors:
        w = round(size * share)
        im.paste(color, (x, 0, min(size, x + w), size))
        x += w
    return im


def hls(hex_color: str) -> tuple[float, float, float]:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(r, g, b)


@pytest.fixture
def covers_dirs(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COVERS_DIR", tmp_path / "covers")
    monkeypatch.setattr(config, "THUMBS_DIR", tmp_path / "thumbs")
    config.COVERS_DIR.mkdir()
    config.THUMBS_DIR.mkdir()
    return tmp_path


# --------------------------------------------------------------------------- téléchargement simulé
def test_fetch_image(http):
    raw = jpeg()
    http.routes[IMG_URL] = httpx.Response(200, headers={"content-type": "image/jpeg"}, content=raw)
    assert covers.fetch_image(IMG_URL) == raw
    req = http.requests[0]
    assert req.headers["user-agent"] == net.UA and "image/" in req.headers["accept"]


def test_fetch_image_rejects_non_images(http):
    http.routes[IMG_URL] = httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>pub</html>")
    with pytest.raises(ValueError, match="pas vers une image"):
        covers.fetch_image(IMG_URL)


def test_fetch_image_size_limit(http, monkeypatch):
    monkeypatch.setattr(covers, "IMAGE_LIMIT", 1000)
    http.routes[IMG_URL] = httpx.Response(200, content=jpeg(size=(800, 800)))
    with pytest.raises(net.TooLarge):
        covers.fetch_image(IMG_URL)


def test_fetch_image_retries_transient_errors(http):
    answers = iter([httpx.Response(503), httpx.Response(200, content=jpeg())])
    http.routes[IMG_URL] = lambda request: next(answers)
    assert covers.fetch_image(IMG_URL)[:2] == b"\xff\xd8"
    assert len(http.requests) == 2


def test_fetch_image_never_blocks_the_event_loop(http):
    """Appel bloquant refusé dans la boucle asyncio ; la version asynchrone passe par un thread."""
    raw = jpeg()
    http.routes[IMG_URL] = httpx.Response(200, content=raw)

    async def scenario():
        with pytest.raises(RuntimeError):
            covers.fetch_image(IMG_URL)
        return await net.aget_bytes(IMG_URL)

    assert asyncio.run(scenario()) == raw


# --------------------------------------------------------------------------- enregistrement
def test_store_cover_records_color_and_thumbnails(covers_dirs, http):
    http.routes[IMG_URL] = httpx.Response(200, content=jpeg((200, 30, 30)))
    cid = covers.store_cover(covers.fetch_image(IMG_URL))
    assert re.fullmatch(r"[0-9a-f]{24}", cid) and covers.cover_path(cid).is_file()
    row = db.q1("SELECT color, w, h FROM covers WHERE id=?", cid)
    assert (row["w"], row["h"]) == (300, 300) and re.fullmatch(r"#[0-9a-f]{6}", row["color"])
    assert covers.store_cover(jpeg((200, 30, 30))) == cid  # même image : même identifiant
    thumb = covers.thumb_file(cid, 192)
    with Image.open(thumb) as im:
        assert im.size == (192, 192) and im.format == "WEBP"


def test_store_cover_ignores_garbage(covers_dirs):
    assert covers.store_cover(None) is None
    assert covers.store_cover(b"x" * 500) is None
    assert covers.store_cover(jpeg(size=(8, 8))) is None  # trop petite pour une pochette


def test_store_cover_png_with_transparency(covers_dirs):
    buf = io.BytesIO()
    Image.new("RGBA", (64, 64), (0, 0, 255, 0)).save(buf, "PNG")
    cid = covers.store_cover(buf.getvalue())
    with Image.open(covers.cover_path(cid)) as im:
        assert im.format == "JPEG" and im.mode == "RGB"


# --------------------------------------------------------------------------- couleur dominante
def test_dominant_color_format_and_clamping():
    color = covers.dominant_color(Image.new("RGB", (64, 64), (255, 0, 0)))
    assert re.fullmatch(r"#[0-9a-f]{6}", color)
    h, l, s = hls(color)
    assert h < 0.02 or h > 0.98                 # reste rouge
    assert 0.29 <= l <= 0.47 and s <= 0.73      # assombrie et adoucie : lisible sous du texte blanc


def test_dominant_color_prefers_vivid_over_grey():
    color = covers.dominant_color(split([((40, 90, 220), 0.3), ((128, 128, 128), 0.7)]))
    h, _l, s = hls(color)
    assert 0.55 < h < 0.7 and s > 0.4           # le bleu l'emporte sur le gris pourtant majoritaire


def test_dominant_color_ignores_black_borders():
    color = covers.dominant_color(split([((240, 140, 20), 0.2), ((0, 0, 0), 0.8)]))
    h, _l, _s = hls(color)
    assert 0.05 < h < 0.12                      # orange, pas noir


def test_dominant_color_black_cover_stays_usable():
    _h, l, s = hls(covers.dominant_color(Image.new("RGB", (64, 64), (0, 0, 0))))
    assert l >= 0.29 and s < 0.05               # gris foncé, jamais du noir pur


# --------------------------------------------------------------------------- recherche en ligne
def test_search_online_survives_one_source_down(http):
    http.routes["https://itunes.apple.com/search"] = httpx.Response(200, json={"results": [
        {"collectionName": "Album", "artistName": "Artiste",
         "artworkUrl100": "https://is1.mzstatic.com/a/100x100bb.jpg"}]})
    http.routes["https://api.deezer.com/search/album"] = httpx.Response(500)
    out = covers.search_online("Artiste Album")
    assert len(out["results"]) == 1
    assert out["errors"] and out["errors"][0].startswith("Deezer")


def test_deezer_quota_is_retried(http, monkeypatch):
    monkeypatch.setattr(covers, "DEEZER_QUOTA_WAIT", 0.0)
    answers = iter([httpx.Response(200, json={"error": {"code": 4, "message": "Quota limit exceeded"}}),
                    httpx.Response(200, json={"data": [{"id": 1}]})])
    http.routes["https://api.deezer.com/search/album"] = lambda request: next(answers)
    assert covers.deezer_api("https://api.deezer.com/search/album?q=x") == {"data": [{"id": 1}]}
