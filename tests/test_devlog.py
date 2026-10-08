"""Console développeur (F7) : journal en mémoire, requêtes notées, erreurs des pages, infos système."""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from onde import devlog, main


@pytest.fixture
def client(fresh_db):
    with TestClient(main.app) as c:
        yield c


def test_logs_are_captured_and_paged(client):
    logging.getLogger("onde.lyrics").info("bonjour la console")
    r = client.get("/api/dev/logs").json()
    assert any(i["message"] == "bonjour la console" and i["source"] == "onde.lyrics" for i in r["items"])
    last = r["last"]
    assert client.get(f"/api/dev/logs?after={last}").json()["items"] == []
    logging.getLogger("onde").warning("attention")
    new = client.get(f"/api/dev/logs?after={last}&wait=2").json()["items"]
    assert [i["message"] for i in new] == ["attention"] and new[0]["level"] == "WARNING"


def test_exceptions_have_traceback(client):
    try:
        1 / 0
    except ZeroDivisionError:
        logging.getLogger("onde").exception("échec")
    item = client.get("/api/dev/logs").json()["items"][-1]
    assert "ZeroDivisionError" in item["message"]


def test_restart_resets(client):
    r = client.get("/api/dev/logs?after=999999999").json()
    assert r["reset"] and r["items"]


def test_http_requests_noted(client):
    client.get("/api/info")
    client.get("/api/tracks/99999")
    items = client.get("/api/dev/http").json()["items"]
    paths = [(i["path"], i["status"]) for i in items]
    assert ("/api/info", 200) in paths and ("/api/tracks/99999", 404) in paths
    assert not any(p.startswith("/api/dev/") for p, _ in paths)


def test_client_errors_rate_limited(client, monkeypatch):
    monkeypatch.setattr(devlog, "CLIENT_PER_MINUTE", 3)
    devlog._client_counts.clear()
    r = client.post("/api/dev/client", json={"device": "Appli Android 1.1",
                                             "items": [{"level": "error", "message": f"oups {i}", "page": "#/"} for i in range(5)]})
    assert r.json()["kept"] == 3
    msgs = [i["message"] for i in client.get("/api/dev/logs").json()["items"] if i["source"] == "page"]
    assert msgs[-1].startswith("[Appli Android 1.1 · ") and msgs[-1].endswith("oups 2")


def test_info_and_debug(client, monkeypatch):
    monkeypatch.setenv("ONDE_PASSWORD", "secret123")
    d = client.get("/api/dev/info").json()
    assert d["version"] and d["memory"] is not None and "settings" in d and d["paths"]["music"]
    assert d["env"]["ONDE_PASSWORD"] == "••••••" and "secret123" not in str(d)
    assert client.put("/api/dev/debug", json={"on": True}).json()["debug"] is True
    logging.getLogger("onde.net").debug("détail")
    assert any(i["message"] == "détail" for i in client.get("/api/dev/logs").json()["items"])
    assert client.put("/api/dev/debug", json={"on": False}).json()["debug"] is False


def test_lyrics_status_api(client):
    st = client.get("/api/lyrics/status").json()
    assert "counts" in st and "recent" in st
    assert client.post("/api/lyrics/retry").json() == {"count": 0}
