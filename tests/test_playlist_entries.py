"""Playlists : retirer une entrée puis annuler le retrait la remet à sa place, même après plusieurs retraits."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from onde import db, main


@pytest.fixture
def app(fresh_db):
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'Album', 'Artiste')")
    for i in range(1, 6):
        db.x("INSERT INTO tracks(id, path, title, artist, album_id, search) VALUES(?,?,?,?,1,?)",
             i, f"/m/{i}.mp3", f"Titre {i}", "Artiste", f"titre {i}")
    return TestClient(main.app)


def _order(c, pid) -> list[int]:
    return [t["id"] for t in c.get(f"/api/playlists/{pid}").json()["tracks"]]


def _entry(c, pid, tid) -> int:
    return next(t["entry"] for t in c.get(f"/api/playlists/{pid}").json()["tracks"] if t["id"] == tid)


def _remove(c, pid, tid) -> dict:
    r = c.delete(f"/api/playlists/{pid}/entries/{_entry(c, pid, tid)}")
    assert r.status_code == 200, r.text
    return r.json()


def _undo(c, pid, removed: dict) -> None:
    r = c.post(f"/api/playlists/{pid}/tracks", json={"trackIds": [removed["trackId"]], "position": removed["index"]})
    assert r.status_code == 200 and r.json()["added"] == 1, r.text


def test_undo_after_several_removals_restores_order(app):
    pid = app.post("/api/playlists", json={"name": "P", "trackIds": [1, 2, 3, 4, 5]}).json()["id"]
    assert _order(app, pid) == [1, 2, 3, 4, 5]
    b = _remove(app, pid, 2)
    d = _remove(app, pid, 4)
    assert (b["index"], d["index"]) == (1, 2)  # rangs dans la playlist telle qu'elle était à chaque retrait
    assert _order(app, pid) == [1, 3, 5]
    _undo(app, pid, d)
    assert _order(app, pid) == [1, 3, 4, 5]
    _undo(app, pid, b)
    assert _order(app, pid) == [1, 2, 3, 4, 5]
    # positions contiguës : le rang d'une entrée est aussi sa position
    rows = db.q("SELECT position FROM playlist_tracks WHERE playlist_id=? ORDER BY position", pid)
    assert [r["position"] for r in rows] == [0, 1, 2, 3, 4]


def test_undo_with_gaps_and_out_of_range_position(app):
    pid = app.post("/api/playlists", json={"name": "P", "trackIds": [1, 2, 3]}).json()["id"]
    db.x("UPDATE playlist_tracks SET position = position * 10 WHERE playlist_id=?", pid)  # trous (anciennes données)
    r = _remove(app, pid, 3)
    assert r["index"] == 2
    _undo(app, pid, r)
    assert _order(app, pid) == [1, 2, 3]
    # rang au-delà de la fin : ajouté en dernier, sans trou
    r = _remove(app, pid, 1)
    _undo(app, pid, {**r, "index": 99})
    assert _order(app, pid) == [2, 3, 1]
    rows = db.q("SELECT position FROM playlist_tracks WHERE playlist_id=? ORDER BY position", pid)
    assert [r["position"] for r in rows] == [0, 1, 2]


def test_remove_unknown_entry_is_404(app):
    pid = app.post("/api/playlists", json={"name": "P", "trackIds": [1]}).json()["id"]
    entry = _entry(app, pid, 1)
    assert app.delete(f"/api/playlists/{pid}/entries/{entry}").status_code == 200
    r = app.delete(f"/api/playlists/{pid}/entries/{entry}")  # déjà retirée
    assert r.status_code == 404 and "introuvable" in r.json()["detail"]
    other = app.post("/api/playlists", json={"name": "Q", "trackIds": [2]}).json()["id"]
    r = app.delete(f"/api/playlists/{pid}/entries/{_entry(app, other, 2)}")  # entrée d'une autre playlist
    assert r.status_code == 404 and _order(app, other) == [2]


def test_invalid_position_appends(app):
    pid = app.post("/api/playlists", json={"name": "P", "trackIds": [1, 2]}).json()["id"]
    for i, pos in ((3, "Infinity"), (4, "NaN"), (5, "true")):
        r = app.post(f"/api/playlists/{pid}/tracks", content=f'{{"trackIds": [{i}], "position": {pos}}}',
                     headers={"Content-Type": "application/json"})
        assert r.status_code == 200, r.text
    assert _order(app, pid) == [1, 2, 3, 4, 5]
