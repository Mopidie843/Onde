"""Nouveautés des artistes suivis : une sortie ratée (pas d'Internet, arrêt d'Onde) est retentée."""
from __future__ import annotations

import time

import pytest

from onde import db, downloader, follow


@pytest.fixture
def releases(fresh_db, monkeypatch):
    """Un artiste suivi qui vient de sortir un single de deux titres. `dl.fail` : téléchargement en panne."""
    follow.follow("Artiste")
    day = time.strftime("%Y-%m-%d")

    def api(url):
        if "search/artist" in url:
            return {"data": [{"id": 7, "name": "Artiste"}]}
        if "/albums" in url:
            return {"data": [{"id": 70, "title": "Nouveau", "release_date": day}]}
        return {"data": [{"title": "Tube", "artist": {"name": "Artiste"}}, {"title": "Face B", "artist": {"name": "Artiste"}}]}

    class Dl:
        fail = True
        calls: list[str] = []

        def __call__(self, q):
            self.calls.append(q)
            if self.fail:
                raise RuntimeError("pas d'Internet")
            return {"status": "added"}

    dl = Dl()
    dl.calls = []
    monkeypatch.setattr(follow.covers, "deezer_api", api)
    monkeypatch.setattr(downloader, "available", lambda: True)
    monkeypatch.setattr(downloader, "search_download", dl)
    monkeypatch.setattr(follow._stop, "wait", lambda s: False)
    follow._stop.clear()
    return dl


def release():
    return db.q1("SELECT downloaded, tries FROM releases WHERE id='dz:70'")


def test_failed_release_is_retried(releases):
    follow.check_all()
    assert release()["downloaded"] == 0 and release()["tries"] == 1 and len(releases.calls) == 2
    releases.fail = False  # Internet revenu
    follow.check_all()
    assert release()["downloaded"] == 2 and len(releases.calls) == 4
    follow.check_all()
    assert len(releases.calls) == 4  # téléchargée : plus jamais retentée


def test_release_given_up_after_max_tries(releases):
    for _ in range(follow.MAX_TRIES + 2):
        follow.check_all()
    assert release()["tries"] == follow.MAX_TRIES and len(releases.calls) == 2 * follow.MAX_TRIES


def test_stop_during_download_leaves_release_to_retry(releases, monkeypatch):
    releases.fail = False

    def stop_after(q):
        releases.calls.append(q)
        follow._stop.set()  # Onde s'arrête pendant le téléchargement
        return {"status": "added"}

    monkeypatch.setattr(downloader, "search_download", stop_after)
    follow.check_all()
    assert release()["downloaded"] == 0 and release()["tries"] == 1
    follow._stop.clear()
    monkeypatch.setattr(downloader, "search_download", releases)
    follow.check_all()
    assert release()["downloaded"] == 2


def test_ensure_keeps_callers_transaction(fresh_db):
    class Abort(Exception):
        pass

    with pytest.raises(Abort):
        with db.tx():
            db.x("INSERT INTO meta(k, v) VALUES('essai', '1')")
            follow.ensure()  # (executescript validait ici la transaction en cours)
            raise Abort
    assert not db.q1("SELECT 1 FROM meta WHERE k='essai'")
    follow.follow("Artiste")  # tables bien là après l'annulation
    assert follow.is_following("artiste")
