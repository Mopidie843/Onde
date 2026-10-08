"""Base SQLite : création du schéma, migrations, fonction SQL onde_norm, transactions."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from onde import db


def tables() -> set[str]:
    return {r["name"] for r in db.q("SELECT name FROM sqlite_master WHERE type='table'")}


def columns(table: str) -> set[str]:
    return {r["name"] for r in db.q(f"PRAGMA table_info({table})")}


def test_init_creates_schema(fresh_db):
    assert {"albums", "tracks", "playlists", "playlist_tracks", "plays", "lyrics", "covers",
            "smart_playlists", "artists"} <= tables()
    for table, col, _decl in db.MIGRATIONS:
        assert col in columns(table)


def test_connection_settings(fresh_db):
    assert db.q1("PRAGMA journal_mode")[0] == "wal"         # lectures pendant les écritures
    assert db.q1("PRAGMA foreign_keys")[0] == 1             # suppressions en cascade
    assert db.q1("PRAGMA busy_timeout")[0] == 15000


def test_init_is_idempotent(fresh_db):
    db.x("INSERT INTO albums(key, name, artist) VALUES('k', 'Album', 'Artiste')")
    db.init()
    db.init()
    assert db.q1("SELECT COUNT(*) FROM albums")[0] == 1      # rien d'effacé au redémarrage


@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 35), reason="DROP COLUMN : SQLite ≥ 3.35")
def test_migration_adds_missing_columns(fresh_db):
    """Base créée par une ancienne version d'Onde : les colonnes ajoutées depuis sont créées."""
    db.x("ALTER TABLE lyrics DROP COLUMN next_try")
    db.x("DROP INDEX ix_tracks_lufs")
    db.x("ALTER TABLE tracks DROP COLUMN lufs")
    assert "next_try" not in columns("lyrics")
    db.init()
    assert "next_try" in columns("lyrics") and "lufs" in columns("tracks")
    assert db.q1("SELECT 1 FROM sqlite_master WHERE name='ix_tracks_lufs'")  # index recréé


@pytest.mark.parametrize("raw, expected", [
    ("Beyoncé", "beyonce"),
    ("  ÉTÉ   à  Paris ", "ete a paris"),
    ("Straße", "strasse"),            # casefold, pas seulement lower()
    ("Ｆｕｌｌ　Ｗｉｄｔｈ", "full width"),  # NFKD : caractères pleine chasse
    ("", ""),
    (None, ""),
])
def test_norm(raw, expected):
    assert db.norm(raw) == expected


def test_onde_norm_sql_function(fresh_db):
    assert db.q1("SELECT onde_norm(?)", "Sigur Rós")[0] == "sigur ros"
    assert db.q1("SELECT onde_norm(NULL)")[0] == ""
    db.x("INSERT INTO albums(key, name, artist) VALUES('a', 'Ágætis byrjun', 'Sigur Rós')")
    row = db.q1("SELECT name FROM albums WHERE onde_norm(artist) = onde_norm(?)", "SIGUR ROS")
    assert row["name"] == "Ágætis byrjun"


def test_like_terms_escapes_wildcards(fresh_db):
    assert db.like_terms("100% Pure_Love") == ["%100\\%%", "%pure\\_love%"]
    db.x("INSERT INTO albums(key, name, artist, search) VALUES('1', 'x', 'y', '100% pure_love')")
    db.x("INSERT INTO albums(key, name, artist, search) VALUES('2', 'x', 'y', '1000 purexlove')")
    sql = "SELECT key FROM albums WHERE " + " AND ".join(["search LIKE ? ESCAPE '\\'"] * 2)
    assert [r["key"] for r in db.q(sql, *db.like_terms("100% pure_love"))] == ["1"]


def test_tx_rolls_back_on_error(fresh_db):
    with pytest.raises(RuntimeError):
        with db.tx():
            db.x("INSERT INTO albums(key, name, artist) VALUES('k', 'Album', 'Artiste')")
            raise RuntimeError("boum")
    assert db.q1("SELECT COUNT(*) FROM albums")[0] == 0
    with db.tx():
        with db.tx():  # réentrant
            db.x("INSERT INTO albums(key, name, artist) VALUES('k', 'Album', 'Artiste')")
    assert db.q1("SELECT COUNT(*) FROM albums")[0] == 1


def test_one_connection_per_thread(fresh_db):
    main = db.connect()
    assert db.connect() is main
    seen = []
    t = threading.Thread(target=lambda: seen.append(db.connect()))
    t.start()
    t.join()
    assert seen[0] is not main
    seen[0].close()


def test_cascade_delete(fresh_db):
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'k', 'Album', 'Artiste')")
    db.x("INSERT INTO tracks(id, path, title, artist, album_id) VALUES(1, '/m/1.mp3', 'T', 'A', 1)")
    db.x("INSERT INTO lyrics(track_id, plain) VALUES(1, 'la la')")
    db.x("DELETE FROM tracks WHERE id=1")
    assert db.q1("SELECT COUNT(*) FROM lyrics")[0] == 0
