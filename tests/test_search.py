"""Recherche et page artiste."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from onde import db, main


def _track(tid, title, artist, album_id, album_artist):
    db.x("INSERT OR IGNORE INTO albums(id, key, name, artist, search) VALUES(?,?,?,?,?)",
         album_id, f"k{album_id}", f"Album {album_id}", album_artist, db.norm(f"Album {album_id} {album_artist}"))
    db.x("INSERT INTO tracks(id, path, title, artist, album_id, search) VALUES(?,?,?,?,?,?)",
         tid, f"/m/{tid}.mp3", title, artist, album_id, db.norm(f"{title} {artist} Album {album_id} {album_artist}"))


@pytest.fixture
def lib(fresh_db):
    _track(1, "Halo", "Beyoncé", 1, "Beyoncé")
    _track(2, "Crazy in Love", "BEYONCE", 2, "Beyonce")
    _track(3, "Duo", "beyoncé", 3, "Autre Artiste")
    _track(4, "Rien à voir", "Beyoncé Tribute Band", 4, "Beyoncé Tribute Band")
    return TestClient(main.app)


def test_artist_page_groups_like_the_artist_list(lib):
    """« Beyoncé », « BEYONCE », « beyoncé » : un seul artiste dans la liste, donc tous ses titres sur sa page."""
    r = lib.get("/api/artists/Beyonce").json()
    assert sorted(t["id"] for t in r["tracks"]) == [1, 2, 3]
    assert sorted(a["id"] for a in r["albums"]) == [1, 2]
    assert [a["id"] for a in r["appears"]] == [3]
    names = [a["name"] for a in lib.get("/api/artists").json()]
    assert sum(1 for n in names if db.norm(n) == "beyonce") == 1
    assert lib.get("/api/artists/Personne").status_code == 404
    assert lib.get("/api/artists/%25").status_code == 404  # (caractère spécial de LIKE : rien de trouvé)


def test_search_runs_once_when_text_is_not_shortened(lib, monkeypatch):
    calls = []
    real = main._search
    monkeypatch.setattr(main, "_search", lambda q: calls.append(q) or real(q))
    assert [t["id"] for t in lib.get("/api/search", params={"q": "halo"}).json()["tracks"]] == [1]
    assert calls == ["halo"]
