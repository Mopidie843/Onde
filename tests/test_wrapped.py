"""Bilan de l'année (« Wrapped ») : calcul des tops, des séries de jours et de la route."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from onde import db, main, stats


def ts(year, month, day, hour=12):
    return time.mktime((year, month, day, hour, 0, 0, 0, 0, -1))


@pytest.fixture
def lib(fresh_db):
    db.x("INSERT INTO albums(id, key, name, artist, cover_id) VALUES(1, 'a', 'Album A', 'Alice', 'aaaaaaaa'), (2, 'b', 'Album B', 'Bob', NULL)")
    rows = [(1, "Un", "Alice", 1, "Pop"), (2, "Deux", "Alice", 1, "Pop"), (3, "Trois", "Bob", 2, "Rock")]
    for i, title, artist, alb, genre in rows:
        db.x("INSERT INTO tracks(id, path, title, artist, album_id, genre, search) VALUES(?,?,?,?,?,?,?)",
             i, f"/m/{i}.mp3", title, artist, alb, genre, title.lower())
    return fresh_db


def play(track, when, seconds=200):
    db.x("INSERT INTO plays(track_id, played_at, seconds, source, user_id) VALUES(?,?,?,?,1)", track, when, seconds, "web")


def test_empty_library_has_no_plays(lib):
    out = stats.wrapped()
    assert out["plays"] == 0 and out["years"] == []


def test_totals_tops_and_year_filter(lib):
    for _ in range(3):
        play(1, ts(2025, 3, 10))
    play(2, ts(2025, 3, 10))
    play(3, ts(2025, 7, 1))
    play(3, ts(2024, 12, 31))  # autre année : ignoré
    out = stats.wrapped(2025)
    assert out["year"] == 2025 and out["years"] == [2025, 2024]
    assert out["plays"] == 5 and out["tracks"] == 3 and out["artists"] == 2
    assert [t["id"] for t in out["topTracks"]][0] == 1 and out["topTracks"][0]["count"] == 3
    assert out["topArtists"][0]["name"] == "Alice" and out["topArtists"][0]["count"] == 4
    assert out["topAlbum"]["id"] == 1 and out["topAlbum"]["plays"] == 4
    assert out["topGenres"][0] == {"name": "Pop", "count": 4}
    assert out["busiestMonth"]["month"] == 3
    assert out["busiestDay"]["date"] == "2025-03-10"
    assert out["months"][2] == round(4 * 200 / 60)


def test_unknown_year_falls_back_to_latest(lib):
    play(1, ts(2023, 5, 5))
    assert stats.wrapped(1999)["year"] == 2023


def test_best_streak_counts_consecutive_days(lib):
    for d in (1, 2, 3, 10, 11):
        play(1, ts(2025, 6, d))
    assert stats.wrapped(2025)["bestStreak"] == 3


def test_new_artists_only_counts_first_ever_play_in_year(lib):
    play(1, ts(2024, 5, 1))  # Alice connue depuis 2024
    play(2, ts(2025, 5, 1))
    play(3, ts(2025, 5, 2))  # Bob découvert en 2025
    assert stats.wrapped(2025)["newArtists"] == 1


def test_daypart(lib):
    play(1, ts(2025, 1, 1, 22))
    play(1, ts(2025, 1, 2, 21))
    play(1, ts(2025, 1, 3, 8))
    assert stats.wrapped(2025)["daypart"] == "soir"


def test_stats_are_per_user(lib):
    db.x("INSERT INTO plays(track_id, played_at, seconds, source, user_id) VALUES(1, ?, 100, 'web', 99)", ts(2025, 1, 1))
    assert stats.wrapped(2025)["plays"] == 0


def test_route(lib):
    play(1, ts(2025, 2, 2))
    client = TestClient(main.app)
    r = client.get("/api/wrapped?year=2025")
    assert r.status_code == 200 and r.json()["plays"] == 1
    assert client.get("/api/wrapped").json()["year"] == 2025
