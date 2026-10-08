"""Playlist de départ : en arrière-plan, une seule fois, titres déjà présents sautés."""
from __future__ import annotations

import pytest

from onde import db, downloader, first_run, library, settings


@pytest.fixture
def starter(fresh_db, tmp_path, monkeypatch):
    first_run._stop.clear()  # (un autre test a pu arrêter le serveur)
    monkeypatch.setattr(first_run, "STATE", tmp_path / "starter.json")
    monkeypatch.setattr(first_run, "TRACKS", [("Mastu", "Quotidien"), ("JOYCA", "Millionaire"), ("Black M", "Mme Pavoshko")])
    monkeypatch.setattr(downloader, "available", lambda: True)
    monkeypatch.setitem(library.status, "last_scan", 1.0)
    settings.update({"starter_pack": True})
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'Album', 'JOYCA')")
    db.x("INSERT INTO tracks(path, title, artist, album_id, search) VALUES('/m/x.mp3', 'Millionaire', 'JOYCA', 1, ?)",
         db.norm("Millionaire JOYCA Album"))
    calls = []

    def fake(q):
        calls.append(q)
        if "Pavoshko" in q:
            raise RuntimeError("pas d'Internet")
        return {"track": {"title": q}}

    monkeypatch.setattr(downloader, "search_download", fake)
    monkeypatch.setattr(first_run._stop, "wait", lambda s: False)
    return calls


def test_downloads_missing_once(starter):
    first_run._worker()
    assert starter == ["Mastu - Quotidien", "Black M - Mme Pavoshko"]  # JOYCA déjà là
    assert first_run.pending() == [("Black M", "Mme Pavoshko")]   # échec : retenté plus tard
    starter.clear()
    first_run._worker()
    assert starter == ["Black M - Mme Pavoshko"]
    first_run._worker()
    assert first_run.pending() == []  # 3 essais au plus
    first_run._worker()
    assert starter == ["Black M - Mme Pavoshko"] * 2


def test_disabled(starter):
    settings.update({"starter_pack": False})
    first_run._worker()
    assert starter == []


def test_in_library(fresh_db):
    assert not first_run.in_library("Personne", "Rien")
