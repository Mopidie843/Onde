"""Écoutes propres à chaque profil : accueil, albums, artistes, mixes, playlists intelligentes, enceinte."""
from __future__ import annotations

import contextlib

import pytest

from onde import api_models as M
from onde import db, discover, library, smart, users
from onde.pi_player import PiPlayer


@contextlib.contextmanager
def as_user(uid):
    token = db.current_uid.set(uid)
    try:
        yield
    finally:
        db.current_uid.reset(token)


@pytest.fixture
def lib(fresh_db):
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'Album A', 'Alice'), (2, 'b', 'Album B', 'Bob')")
    for i, (t, a, alb) in enumerate([("Un", "Alice", 1), ("Deux", "Alice", 1), ("Trois", "Bob", 2)], 1):
        db.x("INSERT INTO tracks(id, path, title, artist, album_id, duration, search) VALUES(?,?,?,?,?,200,?)",
             i, f"/m/{i}.mp3", t, a, alb, t.lower())
    users.create("Pote", "pote-pass", "member")
    return fresh_db


def test_plays_are_per_profile_but_popularity_is_global(lib):
    with as_user(1):
        for _ in range(3):
            library.record_play(1)
    with as_user(2):
        library.record_play(3)
    with as_user(1):
        by = {t["id"]: t for t in M.tracks()}
        assert by[1]["plays"] == 3 and by[3]["plays"] == 0 and by[3]["popularity"] == 1
        assert [t["id"] for t in M.tracks("us.plays > 0", order="us.plays DESC")] == [1]
    with as_user(2):
        assert [t["id"] for t in M.tracks("us.plays > 0", order="us.plays DESC")] == [3]


def test_album_and_artist_plays_per_profile(lib):
    with as_user(1):
        library.record_play(1)
        library.record_play(2)
    with as_user(2):
        library.record_play(3)
    with as_user(1):
        assert [a["id"] for a in M.albums(having="played IS NOT NULL", order="played DESC")] == [1]
        assert {a["name"]: a["plays"] for a in M.artists()} == {"Alice": 2, "Bob": 0}
    with as_user(2):
        assert [a["id"] for a in M.albums(having="played IS NOT NULL", order="played DESC")] == [2]
        assert {a["name"]: a["plays"] for a in M.artists()} == {"Alice": 0, "Bob": 1}


def test_late_offline_play_does_not_move_last_played_back(lib):
    with as_user(1):
        library.record_play(1, at=2000.0)
        library.record_play(1, at=1000.0)
        row = db.q1("SELECT plays, last_played FROM user_track_stats WHERE user_id=1 AND track_id=1")
    assert row["plays"] == 2 and row["last_played"] == 2000.0


def test_discover_catalog_follows_the_profile(lib):
    with as_user(1):
        for _ in range(4):
            library.record_play(1)
        assert next(r for r in discover._catalog() if r["id"] == 1)["plays"] == 4
    with as_user(2):
        assert next(r for r in discover._catalog() if r["id"] == 1)["plays"] == 0


def test_smart_rule_uses_my_plays(lib):
    with as_user(1):
        library.record_play(2)
        library.record_play(2)
    spec = smart.validate({"match": "all", "rules": [{"field": "plays", "op": "gt", "value": 1}], "sort": "plays", "limit": 50})
    with as_user(1):
        assert [t["id"] for t in smart.tracks(spec)] == [2]
    with as_user(2):
        assert smart.tracks(spec) == []


def test_backfill_from_plays_runs_once(lib):
    db.x("INSERT INTO plays(track_id, played_at, seconds, source, user_id) VALUES(1, 10, 5, 'web', 1), (1, 20, 5, 'web', 1), (2, 30, 5, 'web', 2)")
    db.x("DELETE FROM user_track_stats")
    db.x("DELETE FROM meta WHERE k='migr:user_track_stats'")
    db.init()
    db.init()  # idempotent
    rows = {(r["user_id"], r["track_id"]): (r["plays"], r["last_played"]) for r in db.q("SELECT * FROM user_track_stats")}
    assert rows == {(1, 1): (2, 20.0), (2, 2): (1, 30.0)}


def test_delete_profile_purges_stats(lib):
    with as_user(2):
        library.record_play(1)
    users.delete(2)
    assert db.q1("SELECT COUNT(*) n FROM user_track_stats WHERE user_id=2")["n"] == 0


def test_pi_counts_for_owner_after_threshold(lib, monkeypatch):
    p = PiPlayer()
    monkeypatch.setattr(p, "_send", lambda *a, **k: None)
    monkeypatch.setattr(p, "broadcast", lambda: None)
    monkeypatch.setattr(p, "_update_gain", lambda tid: None)
    p.queue, p.owner_uid = [1], 2
    p._load(0)
    p.loaded, p.paused, p.duration = True, False, 200.0
    p.position = 10.0
    p._count_play()
    assert db.q1("SELECT COUNT(*) n FROM plays")["n"] == 0  # trop tôt
    p.position = 31.0
    p._count_play()
    p._count_play()  # une seule fois
    rows = db.q("SELECT user_id, source FROM plays")
    assert [(r["user_id"], r["source"]) for r in rows] == [(2, "pi")]
