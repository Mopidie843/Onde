"""Partage de playlist : un seul lien par playlist, même avec deux créations simultanées."""
from __future__ import annotations

import sqlite3

import pytest

from onde import db, share


@pytest.fixture
def playlist(fresh_db):
    db.x("INSERT INTO playlists(id, name, owner_id) VALUES(1, 'Été', 1)")
    return 1


def test_old_duplicates_keep_the_oldest(playlist):
    db.x("INSERT INTO shares(token, playlist_id, owner_id, created) VALUES('recent', 1, 1, 20)")
    db.x("INSERT INTO shares(token, playlist_id, owner_id, created) VALUES('ancien', 1, 1, 10)")
    assert share.create(playlist)["token"] == "ancien"
    assert [r["token"] for r in db.q("SELECT token FROM shares")] == ["ancien"]
    with pytest.raises(sqlite3.IntegrityError):  # la base refuse désormais un 2e lien
        db.x("INSERT INTO shares(token, playlist_id, owner_id, created) VALUES('autre', 1, 1, 30)")


def test_simultaneous_creations_give_one_token(playlist, monkeypatch):
    first = share.create(playlist)["token"]
    real = share.of_playlist
    calls: list[int] = []

    def racy(pid):  # la 2e requête n'a pas encore vu le lien créé par la 1re
        calls.append(pid)
        return None if len(calls) == 1 else real(pid)

    monkeypatch.setattr(share, "of_playlist", racy)
    assert share.create(playlist)["token"] == first and len(calls) == 2
    assert db.q1("SELECT COUNT(*) n FROM shares")["n"] == 1
    assert db.q1("SELECT COUNT(*) n FROM activity WHERE action='share'")["n"] == 1
