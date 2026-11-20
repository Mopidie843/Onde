"""Scrobbling ListenBrainz par profil : jeton vérifié, écoutes envoyées, file si le serveur est en panne."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from onde import db, library, main, scrobble, users

LB = "https://api.listenbrainz.org"
TOKEN = "abcd-1234-efgh-5678"


@pytest.fixture
def lib(fresh_db, http):
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'Album A', 'Alice')")
    db.x("INSERT INTO tracks(id, path, title, artist, album_id, duration, search) VALUES(1, '/m/1.mp3', 'Un', 'Alice', 1, 200, 'un')")
    http.routes[f"{LB}/1/validate-token"] = httpx.Response(200, json={"valid": True, "user_name": "alice_lb"})
    return http


def test_save_validates_and_never_leaks_token(lib):
    st = scrobble.save(1, TOKEN)
    assert st["enabled"] and st["user"] == "alice_lb" and "token" not in json.dumps(st)


def test_invalid_token_refused(lib):
    lib.routes[f"{LB}/1/validate-token"] = httpx.Response(200, json={"valid": False})
    with pytest.raises(ValueError):
        scrobble.save(1, TOKEN)
    assert not scrobble.status(1)["enabled"]


def test_bad_token_format_and_url(lib):
    with pytest.raises(ValueError):
        scrobble.save(1, "x y")
    with pytest.raises(ValueError):
        scrobble.save(1, TOKEN, "http://pas-https.example")


def test_play_is_submitted_with_metadata(lib):
    sent = []
    lib.routes[f"{LB}/1/submit-listens"] = lambda req: sent.append(json.loads(req.content)) or httpx.Response(200, json={"status": "ok"})
    scrobble.save(1, TOKEN)
    token = db.current_uid.set(1)
    try:
        scrobble.enqueue(1, 1, 1700000000)
    finally:
        db.current_uid.reset(token)
    assert scrobble.flush(1) == 1
    listen = sent[0]["payload"][0]
    assert sent[0]["listen_type"] == "single" and listen["listened_at"] == 1700000000
    assert listen["track_metadata"]["artist_name"] == "Alice" and listen["track_metadata"]["track_name"] == "Un"
    assert listen["track_metadata"]["release_name"] == "Album A"
    assert db.q1("SELECT COUNT(*) n FROM scrobble_queue")["n"] == 0


def test_server_down_keeps_then_gives_up(lib):
    lib.routes[f"{LB}/1/submit-listens"] = httpx.Response(503)
    scrobble.save(1, TOKEN)
    scrobble.enqueue(1, 1, 1700000000)
    for expected in (1, 1, 0):  # 3 essais au plus
        assert scrobble.flush(1) == 0
        assert db.q1("SELECT COUNT(*) n FROM scrobble_queue")["n"] == expected


def test_rejected_token_disables(lib):
    lib.routes[f"{LB}/1/submit-listens"] = httpx.Response(401)
    scrobble.save(1, TOKEN)
    scrobble.enqueue(1, 1, 1700000000)
    scrobble.flush(1)
    assert not scrobble.status(1)["enabled"] and db.q1("SELECT COUNT(*) n FROM scrobble_queue")["n"] == 0


def test_no_token_nothing_queued(lib):
    assert scrobble.enqueue(1, 1, 1700000000) is False


def test_record_play_scrobbles_for_the_right_profile(lib, monkeypatch):
    users.create("Pote", "pote-pass", "member")
    scrobble.save(1, TOKEN)
    calls = []
    monkeypatch.setattr(library.bg, "submit", lambda fn, *a: calls.append(a))
    t = db.current_uid.set(2)  # le pote n'a pas de jeton
    try:
        library.record_play(1)
    finally:
        db.current_uid.reset(t)
    assert calls == [] and db.q1("SELECT COUNT(*) n FROM scrobble_queue")["n"] == 0
    library.record_play(1)  # admin : jeton
    assert calls == [(1,)] and db.q1("SELECT COUNT(*) n FROM scrobble_queue WHERE user_id=1")["n"] == 1


def test_routes(lib):
    client = TestClient(main.app)
    assert client.get("/api/me/scrobble").json()["enabled"] is False
    r = client.put("/api/me/scrobble", json={"token": TOKEN})
    assert r.status_code == 200 and r.json()["enabled"] and TOKEN not in r.text
    assert client.put("/api/me/scrobble", json={"token": "x"}).status_code == 400
    assert client.delete("/api/me/scrobble").json() == {"ok": True}
    assert client.get("/api/me/scrobble").json()["enabled"] is False
