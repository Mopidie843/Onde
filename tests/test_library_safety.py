"""Bibliothèque : garde-fous du scan (disque démonté), recherches en ligne retentées après une panne de
réseau (photos d'artistes, pochettes), et tags des fichiers WAV / AIFF."""
from __future__ import annotations

import io
import math
import struct
import threading
import time
import wave

import httpx
import pytest
from PIL import Image

from onde import config, covers, db, library, net, settings, tidy

IMG = "https://img.example.com/photo.jpg"


def jpeg(color=(30, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), color).save(buf, "JPEG")
    return buf.getvalue()


def no_network(request):
    raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution", request=request)


@pytest.fixture
def music(fresh_db, tmp_path, monkeypatch):
    """Dossier de musique vide, sans tâches de fond ni rangement automatique après le scan."""
    root = tmp_path / "music"
    root.mkdir()
    monkeypatch.setattr(config, "MUSIC_DIR", root)
    monkeypatch.setattr(config, "EXTRA_DIRS", [])
    monkeypatch.setattr(config, "COVERS_DIR", tmp_path / "covers")
    monkeypatch.setattr(config, "THUMBS_DIR", tmp_path / "thumbs")
    config.COVERS_DIR.mkdir()
    config.THUMBS_DIR.mkdir()
    monkeypatch.setattr(library, "schedule_background", lambda cleanup=False: None)
    monkeypatch.setattr(tidy, "run", lambda: None)
    monkeypatch.setattr(library, "status", dict(library.status, offlineRoots=[]))
    monkeypatch.setattr(library, "_stop", threading.Event())  # (un autre test a pu arrêter Onde)
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'Album', 'Artiste')")
    return root


def add_tracks(root, n: int, on_disk: bool = True) -> list[int]:
    """n titres indexés ; sur le disque (taille et date identiques : le scan ne les relit pas) ou pas."""
    ids = []
    for i in range(n):
        p = root / "Artiste" / "Album" / f"{i:02d}.mp3"
        size = mtime = 0
        if on_disk:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
            size, mtime = p.stat().st_size, p.stat().st_mtime
        cur = db.x("INSERT INTO tracks(path, fp, title, artist, album_id, size, mtime, search) VALUES(?,?,?,?,1,?,?,?)",
                   str(p), f"fp{i}", f"Titre {i}", "Artiste", size, mtime, f"titre {i}")
        ids.append(cur.lastrowid)
    return ids


def count(sql: str) -> int:
    return db.q1(sql)["n"]


# --------------------------------------------------------------------------- scan : disque démonté
def test_empty_music_dir_keeps_everything(music):
    ids = add_tracks(music, 3, on_disk=False)  # disque démonté : le point de montage est vide
    db.x("INSERT INTO likes(user_id, track_id, liked_at) VALUES(1, ?, 1)", ids[0])
    library.record_play(ids[1])
    library.scan()
    assert count("SELECT COUNT(*) n FROM tracks") == 3
    assert count("SELECT COUNT(*) n FROM likes") == 1 and count("SELECT COUNT(*) n FROM plays") == 1
    assert library.status["removed"] == 0 and library.status["offlineRoots"] == [str(music)]
    notes = db.q("SELECT * FROM notifications WHERE kind='storage'")
    assert len(notes) == 1 and "rien n'a été supprimé" in notes[0]["title"]
    library.scan()  # toujours absent : pas de nouvelle notification à chaque scan
    assert count("SELECT COUNT(*) n FROM notifications WHERE kind='storage'") == 1
    # disque rebranché : plus signalé
    for r in db.q("SELECT path FROM tracks"):
        (music / "Artiste" / "Album").mkdir(parents=True, exist_ok=True)
        open(r["path"], "wb").write(b"x")
    library.scan()
    assert library.status["offlineRoots"] == [] and count("SELECT COUNT(*) n FROM tracks") == 3


def test_missing_music_dir_keeps_everything(music):
    add_tracks(music, 2, on_disk=False)
    music.rmdir()
    library.scan()
    assert count("SELECT COUNT(*) n FROM tracks") == 2 and library.status["offlineRoots"] == [str(music)]


def test_fresh_install_is_not_reported(music):
    library.scan()  # dossier vide, aucun titre connu : rien à signaler
    assert library.status["offlineRoots"] == [] and not db.q("SELECT 1 FROM notifications")


def test_half_vanished_folder_is_kept_until_full_scan(music):
    ids = add_tracks(music, 25)
    for i in range(13):  # plus de la moitié disparaît d'un coup (disque mal monté ?)
        (music / "Artiste" / "Album" / f"{i:02d}.mp3").unlink()
    library.scan()
    assert count("SELECT COUNT(*) n FROM tracks") == 25 and library.status["offlineRoots"] == [str(music)]
    library.scan(full=True)  # « Scan complet » : suppression voulue
    assert count("SELECT COUNT(*) n FROM tracks") == 12 and library.status["removed"] == 13
    assert not db.q1("SELECT 1 FROM tracks WHERE id=?", ids[0])


def test_few_missing_files_are_removed(music):
    add_tracks(music, 25)
    for i in range(3):
        (music / "Artiste" / "Album" / f"{i:02d}.mp3").unlink()
    library.scan()
    assert count("SELECT COUNT(*) n FROM tracks") == 22 and library.status["removed"] == 3
    assert library.status["offlineRoots"] == []


def test_small_folder_has_no_ratio_guard(music):
    add_tracks(music, 5)
    for i in range(4):
        (music / "Artiste" / "Album" / f"{i:02d}.mp3").unlink()
    library.scan()
    assert count("SELECT COUNT(*) n FROM tracks") == 1


# --------------------------------------------------------------------------- réseau absent
def test_is_offline():
    req = httpx.Request("GET", "https://x.example")
    try:
        try:
            raise httpx.ConnectError("refusé", request=req)
        except httpx.ConnectError as exc:
            raise net.NetError("connexion impossible") from exc
    except net.NetError as wrapped:
        assert net.is_offline(wrapped)
    assert net.is_offline(net.NetError("délai dépassé", offline=True))
    assert not net.is_offline(net.NetError("HTTP 404", status=404))
    assert not net.is_offline(net.NetError("Deezer : no data"))
    assert not net.is_offline(ValueError("pas une image")) and not net.is_offline(None)


def test_artist_photos_wait_for_network(music, http, monkeypatch):
    monkeypatch.setattr(settings, "_cache", {**settings.all_settings(), "artist_images": True})
    add_tracks(music, 1)
    http.routes["https://api.deezer.com/search/artist"] = no_network
    assert library.fetch_artist_images() == 0
    assert not db.q("SELECT 1 FROM artists")  # rien n'est marqué « essayé »
    http.routes["https://api.deezer.com/search/artist"] = httpx.Response(200, json={"data": [
        {"name": "Artiste", "picture_xl": IMG, "picture_medium": IMG, "nb_fan": 10}]})
    http.routes[IMG] = httpx.Response(200, headers={"content-type": "image/jpeg"}, content=jpeg())
    assert library.fetch_artist_images() == 1
    assert db.q1("SELECT image_id FROM artists")["image_id"]


def test_artist_without_photo_retried_after_a_week(music, http, monkeypatch):
    monkeypatch.setattr(settings, "_cache", {**settings.all_settings(), "artist_images": True})
    add_tracks(music, 1)
    http.routes["https://api.deezer.com/search/artist"] = httpx.Response(200, json={"data": []})
    db.x("INSERT INTO artists(key, name, image_id, locked, tried) VALUES('artiste', 'Artiste', NULL, 0, ?)",
         time.time() - 86400)
    library.fetch_artist_images()
    assert not http.requests  # essayé hier : pas de nouvel essai
    db.x("UPDATE artists SET tried=?", time.time() - 8 * 86400)
    library.fetch_artist_images()
    assert len(http.requests) == 1 and db.q1("SELECT tried FROM artists")["tried"] > time.time() - 60


def test_album_cover_retried_after_offline_import(music, http):
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(2, 'b', 'Disque', 'Groupe')")
    http.routes["https://itunes.apple.com/search"] = no_network
    http.routes["https://api.deezer.com/search/album"] = no_network
    assert covers.search_online("Groupe Disque")["offline"] is True
    assert library.auto_cover_album(2, False) is False
    assert db.q1("SELECT cover_tried FROM albums WHERE id=2")["cover_tried"] == 0  # sera retentée
    # Internet revenu, mais aucune pochette : là, l'album est marqué
    http.routes["https://itunes.apple.com/search"] = httpx.Response(200, json={"results": []})
    http.routes["https://api.deezer.com/search/album"] = httpx.Response(200, json={"data": []})
    assert covers.search_online("Groupe Disque")["offline"] is False
    assert library.auto_cover_album(2, False) is False
    assert db.q1("SELECT cover_tried FROM albums WHERE id=2")["cover_tried"] == 1


def test_search_online_one_source_down_is_not_offline(http):
    http.routes["https://itunes.apple.com/search"] = no_network
    http.routes["https://api.deezer.com/search/album"] = httpx.Response(500)
    assert covers.search_online("Groupe Disque")["offline"] is False  # Deezer a répondu (mal)


# --------------------------------------------------------------------------- tags WAV / AIFF
def make_wav(path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 8000)


def make_aiff(path, frames: int = 8000) -> None:
    exp = int(math.floor(math.log2(8000)))
    rate = struct.pack(">HQ", 16383 + exp, int(8000 * (1 << (63 - exp))))  # flottant 80 bits
    comm = struct.pack(">hIh", 1, frames, 16) + rate
    ssnd = struct.pack(">II", 0, 0) + b"\0\0" * frames
    body = b"AIFF" + b"COMM" + struct.pack(">I", len(comm)) + comm + b"SSND" + struct.pack(">I", len(ssnd)) + ssnd
    path.write_bytes(b"FORM" + struct.pack(">I", len(body)) + body)


@pytest.mark.parametrize("ext", [".wav", ".aiff"])
def test_write_tags_wav_aiff(fresh_db, tmp_path, ext):
    p = tmp_path / f"son{ext}"
    (make_wav if ext == ".wav" else make_aiff)(p)
    library.write_tags(str(p), {"title": "Titre é", "artist": "Artiste", "album": "Album", "album_artist": "Groupe",
                                "track_no": 3, "disc_no": 2, "year": "2020", "genre": "Rap"})
    m = library.read_meta(p)
    assert (m["title"], m["artist"], m["album"], m["album_artist"]) == ("Titre é", "Artiste", "Album", "Groupe")
    assert (m["track_no"], m["disc_no"], m["year"], m["genre"]) == (3, 2, 2020, "Rap")
    library.write_tags(str(p), {"genre": "", "title": "Autre"})  # vide : le tag est retiré
    m = library.read_meta(p)
    assert m["genre"] == "" and m["title"] == "Autre" and m["artist"] == "Artiste"
    library.embed_cover(str(p), jpeg())
    assert library.read_meta(p)["cover"]
