"""Sauvegarde complète (/api/library/export) : aucun fichier temporaire ne traîne, même si le client part."""
from __future__ import annotations

import io
import json
import tempfile
import zipfile

import pytest
from fastapi.testclient import TestClient

from onde import db, main, routes_maintenance


@pytest.fixture
def tmpdir_(fresh_db, tmp_path, monkeypatch):
    """Dossier temporaire du système détourné vers un dossier vide propre au test."""
    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    song = tmp_path / "song.mp3"
    song.write_bytes(b"ID3" + b"\0" * 64)
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'Album', 'Artiste')")
    db.x("INSERT INTO tracks(id, path, title, artist, album_id, search, track_no, ext) VALUES(1,?,?,?,1,?,1,'.mp3')",
         str(song), "Titre", "Artiste", "titre")
    return d


def test_export_zip_contains_manifest_and_cleans_up(tmpdir_):
    r = TestClient(main.app).get("/api/library/export")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = zf.namelist()
        assert names[0] == "onde-playlists-et-likes.json" and "Artiste/Album/01 - Titre.mp3" in names
        assert json.loads(zf.read(names[0]))["app"] == "Onde"
    assert list(tmpdir_.iterdir()) == []


def test_export_abandoned_before_streaming_leaves_no_temp_file(tmpdir_):
    resp = routes_maintenance.library_export()  # client parti avant le début de la diffusion
    assert resp.media_type == "application/zip"
    del resp
    assert list(tmpdir_.iterdir()) == []
