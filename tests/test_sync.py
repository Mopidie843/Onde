"""Lecture synchronisée : un état par profil, et un vieil état n'est plus rejoué à l'abonnement."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from onde import db, main, routes_sync, users


async def _no_sleep(*a, **k):
    return None


class _Req:
    """Requête minimale pour appeler directement la route SSE (flux infini, illisible par TestClient)."""

    async def is_disconnected(self):
        return True


@pytest.fixture
def hubs(monkeypatch):
    monkeypatch.setattr(routes_sync, "_hubs", {})
    monkeypatch.setattr(routes_sync, "PING_EVERY", 0.01)  # pas d'attente de 15 s : « rien de neuf » tout de suite
    return routes_sync._hubs


def _first_event(user: dict) -> str:
    """Premier message envoyé à un appareil de `user` qui s'abonne (« : ping » s'il n'y a rien à suivre)."""
    async def run():
        with users.use(user):
            resp = await routes_sync.sync_events(_Req())
        it = resp.body_iterator
        try:
            return await it.__anext__()
        finally:
            await it.aclose()
    return asyncio.run(run())


def _data(event: str) -> dict:
    assert event.startswith("data: "), event
    return json.loads(event[6:])


@pytest.fixture
def app(fresh_db, hubs, monkeypatch):
    monkeypatch.setattr(main, "_login_fails", {})
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'Album', 'Artiste')")
    for i in (1, 2):
        db.x("INSERT INTO tracks(id, path, title, artist, album_id, search) VALUES(?,?,?,?,1,?)",
             i, f"/m/{i}.mp3", f"Titre {i}", "Artiste", f"titre {i}")
    users.update(1, password="admin-pass")
    users.create("Pote", "pote-pass", "member")
    users.create("Ecoute", "ecoute-pass", "listener")
    return TestClient(main.app)


def _login(name, pw):
    c = TestClient(main.app)
    r = c.post("/login", data={"name": name, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def _user(name: str) -> dict:
    return next(u for u in users.all_users() if u["name"] == name)


def test_state_is_per_profile(app):
    pote, ecoute = _login("Pote", "pote-pass"), _login("Ecoute", "ecoute-pass")
    assert pote.post("/api/sync/broadcast", json={"trackId": 2, "position": 12.5, "device": "Salon"}).json() == {"ok": True}
    # un autre profil ne voit pas (et ne peut donc pas suivre) ce qu'écoute Pote
    assert _first_event(_user("Ecoute")) == ": ping\n\n"
    assert _first_event(users.admin()) == ": ping\n\n"
    s = _data(_first_event(_user("Pote")))
    assert s["trackId"] == 2 and s["position"] == 12.5 and s["device"] == "Salon" and 0 <= s["age"] < 5
    # ce qu'Ecoute publie ne remplace pas l'état de Pote
    assert ecoute.post("/api/sync/broadcast", json={"trackId": 1, "paused": True}).status_code == 200
    assert _data(_first_event(_user("Pote")))["trackId"] == 2
    assert _data(_first_event(_user("Ecoute")))["trackId"] == 1


def test_stale_state_is_not_replayed(fresh_db, hubs):
    admin = users.admin()
    hub = routes_sync._hub(admin["id"])
    hub.publish({"trackId": 1, "position": 30.0, "paused": False, "device": "Vieux", "ts": time.time() - 3 * 86400})
    assert _first_event(admin) == ": ping\n\n"  # vieux de plusieurs jours : ne remplace pas la file
    hub.publish({"trackId": 1, "position": 30.0, "paused": False, "device": "Récent", "ts": time.time() - 60})
    assert _data(_first_event(admin))["device"] == "Récent"


def test_broadcast_invalid_position(fresh_db, hubs):
    c = TestClient(main.app)
    for raw in ('"abc"', "Infinity", "NaN", "-4", "[1]"):
        r = c.post("/api/sync/broadcast", content=f'{{"trackId": 1, "position": {raw}}}',
                   headers={"Content-Type": "application/json"})
        assert r.status_code == 200, (raw, r.text)
        assert routes_sync._hub(users.admin()["id"]).state["position"] == 0.0
