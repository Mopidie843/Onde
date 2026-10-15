"""Demandes de titres : reprise des téléchargements interrompus, profil supprimé, faux positifs de la
mini IA sur les « versions retouchées »."""
from __future__ import annotations

import time

import pytest

from onde import asks, clean, db, downloader, quota, users


class _SyncThread:
    def __init__(self, target=None, args=(), **k):
        self.t, self.a = target, args

    def start(self):
        self.t(*self.a)

    def join(self, *a):
        pass


@pytest.fixture
def stuck(fresh_db, monkeypatch, tmp_path):
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'Album', 'Jul')")
    db.x("INSERT INTO tracks(id, path, title, artist, album_id, search, size, ext) VALUES(1, ?, 'Titre', 'Jul', 1, 'titre jul', ?, '.mp3')",
         str(tmp_path / "1.mp3"), quota.MB)
    users.create("Ecoute", "ecoute-pass", "listener")
    got: list[str] = []
    monkeypatch.setattr(asks.threading, "Thread", _SyncThread)
    monkeypatch.setattr(downloader, "search_download", lambda q: got.append(q) or {"status": "added", "track": {"id": 1}})
    monkeypatch.setattr(asks, "_running", set())
    return got


def add_ask(user_id: int, query: str, decided: float) -> int:
    return db.x("""INSERT INTO asks(user_id, kind, query, status, created, decided, decided_by, reason)
                   VALUES(?, 'track', ?, 'downloading', ?, ?, 'admin', 'Accepté : téléchargement en cours')""",
                user_id, query, decided - 10, decided).lastrowid


def test_stuck_download_is_resumed(stuck):
    me = db.q1("SELECT id FROM users WHERE name='Ecoute'")["id"]
    old = add_ask(me, "Jul - Bande organisée", time.time() - 3600)   # Onde redémarré pendant le téléchargement
    recent = add_ask(me, "Jul - Tchikita", time.time() - 60)          # peut-être encore en cours ailleurs
    assert asks.resume_stuck() == 1
    assert stuck == ["Jul - Bande organisée"]
    assert asks.get(old)["status"] == "done" and asks.get(old)["trackId"] == 1
    assert asks.get(recent)["status"] == "downloading"
    assert asks.resume_stuck(time.time() + asks.STUCK_AFTER) == 1  # 15 min plus tard, elle aussi
    assert asks.get(recent)["status"] == "done" and not asks._running


def test_running_download_is_not_launched_twice(stuck):
    me = db.q1("SELECT id FROM users WHERE name='Ecoute'")["id"]
    a = add_ask(me, "Jul - Long", time.time() - 3600)
    asks._running.add(a)  # téléchargement très long, toujours en cours dans ce processus
    assert asks.resume_stuck() == 0 and stuck == []


def test_deleted_profile_fails_instead_of_hanging(stuck):
    a = add_ask(999, "Jul - Orphelin", time.time() - 3600)
    asks.resume_stuck()
    r = asks.get(a)
    assert r["status"] == "failed" and r["reason"] == "Profil supprimé" and stuck == []
    assert asks.resume_stuck() == 0


@pytest.mark.parametrize("query", ["Tiakola - 24h", "PNL - 2h du mat", "Disiz - Loop", "Booba - 92i"])
def test_real_songs_are_not_junk(query):
    assert not asks.JUNK.search(clean.fold(query))


@pytest.mark.parametrize("query", ["Jul - Tchikita 1 hour", "Pluie 10 hours", "Rain sounds 8 heures",
                                   "Tiakola - Meuda 1h loop", "Ninho - Lettre loop version", "Jul (nightcore)"])
def test_edited_versions_are_junk(query):
    assert asks.JUNK.search(clean.fold(query))


def test_ai_accepts_title_with_hours(stuck):
    me = db.q1("SELECT id FROM users WHERE name='Ecoute'")["id"]
    v = asks.evaluate("Jul - 24h", me, online=False)
    assert not any("retouchée" in r for r in v["reasons"]) and v["ok"]  # (Jul est déjà dans la bibliothèque)
