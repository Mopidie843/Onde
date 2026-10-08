"""Notifications (journal d'événements pour l'appli Android) et protection du mot de passe."""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from onde import users, config, db, main, notify, settings


@pytest.fixture
def n(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.setattr(notify, "_loaded", False)
    monkeypatch.setattr(notify, "_last", 0)
    yield notify
    settings._cache = None


def test_first_connection_gets_no_history(n):
    n.add("import", "Musique ajoutée", "3 titres")
    res = n.feed(None)
    assert res["events"] == [] and res["last"] == 1 and res["reset"]


def test_events_since_and_nothing_new(n):
    n.add("import", "A", "a")
    n.add("lyrics", "B", "b")
    assert [e["title"] for e in n.feed(0)["events"]] == ["A", "B"]
    assert [e["title"] for e in n.feed(1)["events"]] == ["B"]
    assert n.feed(2) == {"last": 2, "events": [], "reset": False}


def test_other_server_numbering_resets(n):
    n.add("import", "A", "a")
    assert n.feed(999)["reset"]  # base remplacée : l'appli repart du dernier numéro


def test_same_kind_is_grouped_into_one_notification(n):
    n.lyrics_synced("Titre 1", "Artiste", 5)
    first = n.feed(0)["events"][0]
    assert first["text"] == "Artiste — Titre 1" and first["link"] == "#/album/5"
    n.lyrics_synced("Titre 2", "Artiste", 5)
    n.lyrics_synced("Titre 3", "Artiste", 6)
    events = n.feed(0)["events"]
    assert len(events) == 1 and events[0]["id"] == 3 and events[0]["n"] == 3
    assert events[0]["text"].startswith("3 titres")
    assert n.feed(1)["events"][0]["id"] == 3  # un téléphone à jour voit la notification mise à jour


def test_music_added_sums_counts(n):
    n.music_added(5)
    n.music_added(7)
    e = n.feed(0)["events"]
    assert len(e) == 1 and e[0]["text"] == "12 titres ajoutés à ta bibliothèque"
    n.music_added(0)
    assert len(n.feed(0)["events"]) == 1


def test_not_grouped_after_window(n, monkeypatch):
    n.music_added(2)
    db.x("UPDATE notifications SET ts = ts - ?", n.GROUP_WINDOW + 5)
    n.music_added(2)
    assert len(n.feed(0)["events"]) == 2


def test_disabled_kind_adds_nothing(n):
    settings.update({"notify_lyrics": False})
    assert n.add("lyrics", "x") is None
    assert n.feed(0)["events"] == []
    assert n.add("import", "y") is not None


def test_journal_is_bounded(n, monkeypatch):
    monkeypatch.setattr(notify, "KEEP", 5)
    for i in range(12):
        n.add("import", f"e{i}")
    ids = [e["id"] for e in n.since(0, 100)]
    assert ids == [8, 9, 10, 11, 12]


# ------------------------------------------------------------------ API
def test_api_long_poll_wakes_up_on_event(n):
    c = TestClient(main.app)
    last = c.get("/api/notifications?after=latest").json()["last"]
    threading.Timer(0.6, lambda: n.add("import", "Musique ajoutée", "1 titre")).start()
    t = time.monotonic()
    r = c.get(f"/api/notifications?after={last}&wait=10").json()
    assert 0.4 < time.monotonic() - t < 5 and [e["title"] for e in r["events"]] == ["Musique ajoutée"]


def test_api_wait_times_out_and_is_capped(n, monkeypatch):
    c = TestClient(main.app)
    monkeypatch.setattr(notify, "MAX_WAIT", 1.0)
    t = time.monotonic()
    r = c.get("/api/notifications?after=0&wait=999").json()
    assert 0.8 < time.monotonic() - t < 4 and r["events"] == []
    assert c.get("/api/notifications?after=abc").json()["reset"]


def test_api_requires_password(n, monkeypatch):
    users.update(1, password="secret123")
    c = TestClient(main.app)
    r = c.get("/api/notifications")
    assert r.status_code == 401 and "Connexion requise" in r.text


# ------------------------------------------------------------------ mot de passe
@pytest.fixture
def pw(fresh_db, monkeypatch):
    users.update(1, password="secret123")
    monkeypatch.setattr(main, "_login_fails", {})
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    return TestClient(main.app)


async def _no_sleep(*a, **k):
    return None


def post(c, pwd, ip=None):
    headers = {"x-forwarded-for": ip} if ip else {}
    return c.post("/login", data={"password": pwd}, headers=headers, follow_redirects=False)


def test_login_locks_after_repeated_failures(pw):
    for _ in range(main.LOGIN_MAX_FAILS):
        assert post(pw, "faux").status_code == 401
    r = post(pw, "secret123")  # même le bon mot de passe : bloqué
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 800


def test_login_success_resets_counter_and_sets_cookie(pw):
    for _ in range(main.LOGIN_MAX_FAILS - 1):
        post(pw, "faux")
    r = post(pw, "secret123")
    assert r.status_code == 303 and "onde_session" in r.headers["set-cookie"]
    for _ in range(main.LOGIN_MAX_FAILS - 1):
        assert post(pw, "faux").status_code == 401  # compteur remis à zéro


def test_lock_expires(pw):
    for _ in range(main.LOGIN_MAX_FAILS):
        post(pw, "faux")
    for rec in main._login_fails.values():
        rec[1] = time.time() - 1
    assert post(pw, "secret123").status_code == 303


def test_lock_is_per_address_behind_a_local_relay(pw):
    """Derrière Tailscale/Cloudflare, tout arrive de 127.0.0.1 : l'adresse du relais (x-forwarded-for) compte."""
    pw = TestClient(main.app, client=("127.0.0.1", 50000))
    for _ in range(main.LOGIN_MAX_FAILS):
        post(pw, "faux", ip="203.0.113.5")
    assert post(pw, "secret123", ip="203.0.113.5").status_code == 429
    assert post(pw, "secret123", ip="198.51.100.7").status_code == 303  # un autre visiteur n'est pas bloqué
    # un « x-forwarded-for » forgé par le visiteur ne sert à rien : on lit l'adresse la plus à droite
    assert post(pw, "faux", ip="1.2.3.4, 203.0.113.5").status_code == 429


def test_cookie_is_secure_over_https_relay(pw):
    r = pw.post("/login", data={"password": "secret123"}, headers={"x-forwarded-proto": "https"},
                follow_redirects=False)
    assert "secure" in r.headers["set-cookie"].lower()


def test_forged_forwarded_header_does_not_bypass_lock(pw):
    """Un visiteur direct (pas le relais local) ne peut pas changer d'adresse en inventant l'en-tête."""
    for i in range(main.LOGIN_MAX_FAILS):
        post(pw, "faux", ip=f"10.9.9.{i}")
    assert post(pw, "secret123", ip="10.1.1.1").status_code == 429
