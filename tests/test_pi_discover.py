"""Enceinte : titre disparu sans boucle infinie ; mix de la semaine sans toucher au hasard global."""
from __future__ import annotations

import random

import pytest

from onde import db, discover
from onde.pi_player import PiPlayer


@pytest.fixture
def pi(fresh_db, monkeypatch):
    p = PiPlayer()
    monkeypatch.setattr(p, "_send", lambda *a, **k: None)
    monkeypatch.setattr(p, "broadcast", lambda: None)
    monkeypatch.setattr(p, "_update_gain", lambda tid: None)
    return p


@pytest.mark.parametrize("repeat", ["off", "one", "all"])
def test_missing_tracks_do_not_recurse_forever(pi, repeat):
    pi.queue = [901, 902, 903]  # aucun n'existe en base
    pi.repeat = repeat
    pi._load(0)  # ne doit ni boucler ni lever RecursionError
    assert pi.paused and not pi.loaded


def test_missing_track_skips_to_next_existing(pi):
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'A', 'X')")
    db.x("INSERT INTO tracks(id, path, title, artist, album_id, duration) VALUES(7, '/m/7.mp3', 'T', 'X', 1, 100)")
    pi.queue = [901, 7]
    pi._load(0)
    assert pi.index == 1


def test_weekly_leaves_global_random_alone(fresh_db):
    random.seed(1234)
    before = random.getstate()
    discover.weekly()
    assert random.getstate() == before


def test_radio_with_rng_is_reproducible(fresh_db):
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'A', 'X')")
    for i in range(1, 30):
        db.x("INSERT INTO tracks(id, path, title, artist, album_id, genre, search) VALUES(?,?,?,?,1,'Pop',?)", i, f"/m/{i}.mp3", f"T{i}", "X", f"t{i}")
    discover.invalidate() if hasattr(discover, "invalidate") else None
    a = discover.radio(seed_tracks=[1], limit=10, rng=random.Random("s"))
    b = discover.radio(seed_tracks=[1], limit=10, rng=random.Random("s"))
    assert a == b


def test_smart_random_order_changes_between_weeks(fresh_db, monkeypatch):
    import datetime

    from onde import smart

    spec = smart.validate({"match": "all", "rules": [], "sort": "random", "limit": 50})

    def order_for(day):
        class D(datetime.date):
            @classmethod
            def today(cls):
                return day
        monkeypatch.setattr(smart.datetime, "date", D)
        sql = smart.build(spec)[2]
        return [r[0] for r in db.q(f"WITH t(id) AS (VALUES {','.join(f'({i})' for i in range(1, 31))}) SELECT id FROM t ORDER BY {sql.replace('t.id', 'id')}")]

    w1, w2 = order_for(datetime.date(2026, 3, 2)), order_for(datetime.date(2026, 3, 9))
    assert w1 != w2 and w1 == order_for(datetime.date(2026, 3, 3))  # change chaque semaine, stable dans la semaine
    assert w2[1:] + w2[:1] != w1 and w1[1:] + w1[:1] != w2  # (pas un simple décalage circulaire)
