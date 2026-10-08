"""Rangement automatique : titres raccourcis, « Slowed » / « Reverb » à la corbeille."""
from __future__ import annotations

import pytest

from onde import config, db, settings, tidy


@pytest.fixture
def lib(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(tidy, "TRASH", tmp_path / "corbeille")
    monkeypatch.setattr(config, "MUSIC_DIR", tmp_path / "music")
    (tmp_path / "music").mkdir()
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'Album', 'Artiste')")
    rows = [("Bonne chanson", "bonne.mp3"), ("Song (Slowed + Reverb)", "s1.mp3"), ("Calm", "calm - reverb.mp3"),
            ("Mr. Brightside", "mrb.mp3"), ("MOONLIGHT🌙~ Kali Uchis || [AMV] 4K", "moon.mp3"),
            ("Reverberation", "rev.mp3")]
    for i, (title, fn) in enumerate(rows, 1):
        f = tmp_path / "music" / fn
        f.write_bytes(b"x")
        db.x("INSERT INTO tracks(id, path, title, artist, album_id, search) VALUES(?,?,?,?,1,?)",
             i, str(f), title, "Artiste", db.norm(title))
    return tmp_path


def test_unwanted_words():
    assert tidy.unwanted("Song (Slowed + Reverb)", "/x/a.mp3") == "slowed"
    assert tidy.unwanted("Calm", "/x/calm - REVERB.mp3") == "reverb"
    assert tidy.unwanted("Reverberation", "/x/r.mp3") is None  # mot entier seulement
    assert tidy.unwanted("Bonne chanson", "/x/b.mp3") is None


def test_run_shortens_and_trashes(lib):
    out = tidy.run()
    titles = {r["id"]: r["title"] for r in db.q("SELECT id, title FROM tracks")}
    assert out == {"shortened": 1, "removed": 2}
    assert set(titles) == {1, 4, 5, 6}
    assert titles[5] == "MOONLIGHT — Kali Uchis" and titles[4] == "Mr. Brightside"
    assert db.q1("SELECT id FROM tracks WHERE search LIKE '%moonlight%'")
    assert not (lib / "music" / "s1.mp3").exists()
    assert tidy.run() == {"shortened": 0, "removed": 0}  # idempotent


def test_restore_and_empty(lib):
    tidy.run()
    items = tidy.items()
    assert len(items) == 2 and items[0]["word"] in ("slowed", "reverb")
    tidy.restore(items[0]["id"])
    assert len(tidy.items()) == 1
    # (le scan réindexe le fichier récupéré, comme le fait l'API)
    path = next(p for p in (lib / "music").iterdir() if p.name in ("s1.mp3", "calm - reverb.mp3"))
    db.x("INSERT INTO tracks(path, title, artist, album_id, search) VALUES(?,?,?,1,?)", str(path), "Back", "A", "back")
    assert tidy.run()["removed"] == 0  # récupéré : il n'est pas remis à la corbeille
    assert tidy.empty() == 1 and tidy.items() == []
    with pytest.raises(KeyError):
        tidy.restore("../../etc/passwd")


def test_safety_limit(lib):
    settings.update({"unwanted_words": "a, e, o, u, i"})  # presque tout correspondrait
    assert tidy.sweep()["removed"] == 0
    assert db.q1("SELECT COUNT(*) n FROM tracks")["n"] == 6


def test_disabled(lib):
    settings.update({"shorten_titles": False, "remove_unwanted": False})
    assert tidy.run() == {"shortened": 0, "removed": 0}
    assert db.q1("SELECT COUNT(*) n FROM tracks")["n"] == 6
