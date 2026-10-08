"""Lot 4 : reprise, écoutés récemment, mixes, doublons/qualité, nouveautés, sauvegarde, mise à jour, soirée."""
from __future__ import annotations

import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from onde import backup, config, db, discover, downloader, first_run, follow, jam, library, main, tidy, updater, users


@pytest.fixture
def lib(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(tidy, "TRASH", tmp_path / "corbeille")
    monkeypatch.setattr(config, "MUSIC_DIR", tmp_path / "music")
    (tmp_path / "music").mkdir()
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'Album', 'Artiste')")
    rows = [(1, "Chanson", "Artiste", "Rap", 320000, ".mp3"), (2, "Chanson (Remastered)", "Artiste", "Rap", 128000, ".mp3"),
            (3, "Autre", "Artiste", "Rap", 96000, ".mp3"), (4, "Lofi", "Zen", "Lo-Fi", 0, ".flac")]
    for tid, title, artist, genre, br, ext in rows:
        f = tmp_path / "music" / f"{tid}{ext}"
        f.write_bytes(b"x")
        db.x("""INSERT INTO tracks(id, path, title, artist, album_id, genre, bitrate, ext, duration, search)
                VALUES(?,?,?,?,1,?,?,?,200,?)""", tid, str(f), title, artist, genre, br, ext, db.norm(f"{title} {artist}"))
    discover.invalidate()
    return TestClient(main.app)


def test_resume_roundtrip(lib):
    r = lib.post("/api/resume", json={"trackId": 2, "position": 42.5, "queue": [1, 2, 3], "index": 1,
                                      "device": "PC · abc", "deviceId": "d1", "playing": True})
    assert r.status_code == 200
    s = lib.get("/api/resume").json()["state"]
    assert s["trackId"] == 2 and s["position"] == 42.5 and s["track"]["title"].startswith("Chanson")


def test_recent_unique(lib):
    for tid in (1, 3, 1):
        lib.post(f"/api/tracks/{tid}/played")
        time.sleep(0.01)
    assert [t["id"] for t in lib.get("/api/recent").json()["tracks"]] == [1, 3]


def test_mood_and_artist_mixes(lib):
    for _ in range(3):
        library.record_play(1)
    name, ids = discover.mix_tracks("artist", "Artiste")
    assert name == "Mix Artiste" and set(ids) & {1, 2, 3}
    assert discover.mix_tracks("mood", "chill")[1] == [4]
    assert discover.mix_tracks("mood", "inconnu") == ("Mix", [])
    assert "Artiste" in discover.top_artists()


def test_last_year(lib):
    db.x("INSERT INTO plays(track_id, played_at, seconds, user_id) VALUES(3, ?, 200, 1)", time.time() - 365 * 86400)
    assert discover.last_year() == [3]


def test_duplicates_keep_best_and_merge(lib):
    groups = library.find_near_duplicates()
    assert groups and groups[0]["tracks"][0]["id"] == 1  # 320 kbit/s avant 128
    db.x("INSERT INTO likes(user_id, track_id, liked_at) VALUES(1, 2, 1)")
    db.x("INSERT INTO playlists(id, name, owner_id) VALUES(9, 'p', 1)")
    db.x("INSERT INTO playlist_tracks(playlist_id, track_id, position) VALUES(9, 2, 0)")
    assert lib.post("/api/library/duplicates/merge", json={"keep": 1, "remove": [2]}).json()["removed"] == 1
    assert db.q1("SELECT track_id FROM likes")["track_id"] == 1
    assert db.q1("SELECT track_id FROM playlist_tracks")["track_id"] == 1
    assert not db.q1("SELECT 1 FROM tracks WHERE id=2") and tidy.items()


def test_low_quality_and_replace(lib, monkeypatch):
    assert [t["id"] for t in tidy.low_quality()] == [3, 2]   # pas la FLAC

    def fake(q):
        f = config.MUSIC_DIR / "new.mp3"
        f.write_bytes(b"y")
        db.x("""INSERT INTO tracks(id, path, title, artist, album_id, bitrate, ext, duration, search)
                VALUES(10, ?, 'Autre', 'Artiste', 1, 192000, '.mp3', 201, 'autre')""", str(f))
        return {"status": "added", "track": {"id": 10}}

    monkeypatch.setattr(downloader, "search_download", fake)
    r = lib.post("/api/library/quality/3/replace").json()
    assert r["replaced"] is True and not db.q1("SELECT 1 FROM tracks WHERE id=3")


def test_follow_downloads_new_releases_once(lib, monkeypatch):
    follow.follow("Artiste")
    calls = []
    day = time.strftime("%Y-%m-%d")

    def api(url):
        if "search/artist" in url:
            return {"data": [{"id": 7, "name": "Artiste"}]}
        if "/albums" in url:
            return {"data": [{"id": 70, "title": "Nouveau", "release_date": day}, {"id": 1, "title": "Vieux", "release_date": "2001-01-01"}]}
        return {"data": [{"title": "Tube", "artist": {"name": "Artiste"}}, {"title": "Chanson", "artist": {"name": "Artiste"}}]}

    monkeypatch.setattr(follow.covers, "deezer_api", api)
    monkeypatch.setattr(downloader, "available", lambda: True)
    monkeypatch.setattr(downloader, "search_download", lambda q: calls.append(q) or {"status": "added"})
    monkeypatch.setattr(follow._stop, "wait", lambda s: False)
    follow._stop.clear()
    follow.check_all()
    assert calls == ["Artiste - Tube"]   # « Chanson » est déjà là ; le vieil album est ignoré
    follow.check_all()
    assert calls == ["Artiste - Tube"]   # pas deux fois
    assert follow.following()[0]["releases"][0]["title"] == "Nouveau"


def test_backup_encrypted_roundtrip(lib, tmp_path):
    pytest.importorskip("cryptography")
    backup.configure(directory=str(tmp_path / "bk"), passphrase="phrase-secrete")
    r = backup.run()
    assert r["ok"] and r["file"].endswith(".ondebk")
    f = tmp_path / "bk" / r["file"]
    assert b"SQLite" not in f.read_bytes()
    raw = backup.open_file(f, "phrase-secrete")
    assert raw.startswith(b"SQLite format 3")
    with pytest.raises(ValueError):
        backup.open_file(f, "mauvaise")
    backup.stage_restore(r["file"])
    assert config.DB_PATH.with_name("onde.db.restore").is_file()
    with pytest.raises(ValueError):
        backup.stage_restore("../../etc/passwd")


def test_update_from_zip(tmp_path, monkeypatch):
    root = tmp_path / "install"
    (root / "onde").mkdir(parents=True)
    (root / "onde" / "main.py").write_text("old")
    (root / "onde.env").write_text("ONDE_PORT=9000")
    monkeypatch.setattr(config, "ROOT", root)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("onde/onde/main.py", "new")
        z.writestr("onde/onde/config.py", 'VERSION = "9.9.9"\n')
        z.writestr("onde/onde.env", "ONDE_PORT=1")      # jamais écrasé
        z.writestr("onde/onde/__pycache__/x.pyc", "x")
    r = updater.apply(buf.getvalue())
    assert r["to"] == "9.9.9" and (root / "onde" / "main.py").read_text() == "new"
    assert (root / "onde.env").read_text() == "ONDE_PORT=9000"
    assert not (root / "onde" / "__pycache__").exists()
    assert (tmp_path / "data" / "ancienne-version.zip").is_file()
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("onde/onde/main.py", "x")
        z.writestr("onde/../evil.py", "x")
    with pytest.raises(ValueError):
        updater.apply(bad.getvalue())


def test_jam_guest_without_account(lib, monkeypatch):
    users.update(1, password="admin-pass")
    host = TestClient(main.app)
    host.post("/login", data={"name": config.ADMIN_NAME, "password": "admin-pass"})
    tok = host.post("/api/jam").json()["token"]
    guest = TestClient(main.app)                  # pas connecté
    assert guest.get("/api/tracks").status_code == 401
    assert guest.get(f"/jam/{tok}").status_code == 200
    found = guest.get(f"/api/jam/{tok}/search?q=chanson").json()["tracks"]
    assert found and found[0]["title"].startswith("Chanson")
    assert guest.post(f"/api/jam/{tok}/add", json={"trackId": found[0]["id"], "by": "Léa"}).status_code == 200
    assert guest.post(f"/api/jam/{tok}/add", json={"trackId": 3, "by": "Léa"}).status_code == 400  # trop vite
    items = host.get("/api/jam?after=0").json()["items"]
    assert [i["by"] for i in items] == ["Léa"]
    assert guest.get("/api/jam/faux-jeton").status_code == 404
    host.delete("/api/jam")
    assert guest.get(f"/api/jam/{tok}").status_code == 404
