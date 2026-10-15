"""Robustesse de l'API : une entrée invalide donne une erreur 4xx lisible, jamais une erreur 500.

Chaque route POST / PUT / PATCH de /api reçoit des corps piégés (tableau au lieu d'un objet, texte à la
place d'un nombre, liste de numéros qui n'en sont pas, Infinity…). Les routes qui lancent une action
lourde (redémarrage, installation, téléchargement, réseau) sont écartées ; celles qui répondent encore
500 sont listées dans KNOWN_500 avec le fichier à corriger (chantier d'un autre lot : à retirer de la
liste dès que ce fichier utilise les outils de web.py : _int, _ints, _num, _text, _obj).
"""
from __future__ import annotations

import json
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from onde import jam, main, settings, users, web

SKIP = {  # action lourde ou effet de bord hors de la base de test
    ("POST", "/api/settings/https"), ("POST", "/api/update"), ("POST", "/api/update/check"),
    ("POST", "/api/align/install"), ("POST", "/api/backup/run"), ("POST", "/api/backup/restore"),
    ("POST", "/api/follow-check"), ("POST", "/api/tidy/run"), ("POST", "/api/library/scan"),
    ("POST", "/api/library/covers"), ("POST", "/api/library/artists"), ("POST", "/api/upload"),
    ("POST", "/api/import-url"), ("POST", "/api/import-playlist"),  # (testées dans test_security.py)
    ("POST", "/api/library/quality/{tid}/replace"), ("POST", "/api/auth/logout"),
}

KNOWN_500 = {  # (méthode, route) : fichier où la correction reste à faire
    ("POST", "/api/smart"): "routes_playlists.py (name non textuel)",
    ("PATCH", "/api/smart/{sid}"): "routes_playlists.py (name non textuel)",
    ("POST", "/api/playlists/{pid}/tracks"): "routes_playlists.py (trackIds : _ints)",
    ("POST", "/api/library/duplicates/merge"): "routes_maintenance.py (keep : _int)",
    ("POST", "/api/tracks/batch-edit"): "routes_maintenance.py (ids : _ints, fields : objet)",
    ("POST", "/api/tracks/batch-fix"): "routes_maintenance.py (ids : _ints)",
    ("POST", "/api/sync/broadcast"): "routes_sync.py (position : _num)",
}

TRAPS = [
    [],
    "texte",
    {"trackIds": ["x", {}], "ids": "x", "queue": "12", "tracks": [None]},
    {"trackId": "abc", "position": "abc", "index": "x", "keep": "x", "order": {"a": 1}},
    {"name": 123, "description": ["x"], "email": {}, "role": [], "password": 5, "note": {"a": 1}},
    {"url": {"a": 1}, "days": "inf", "uses": 1e300, "amountMb": "nan", "quotaMb": "abc", "fields": "x"},
    {"name": {"a": 1}, "rules": "x", "text": ["x"], "query": {}, "reason": [1], "dir": 5, "passphrase": []},
]
RAW_TRAPS = [  # JSON accepté par Python mais pas par les navigateurs (Infinity, NaN)
    b'{"trackId": 1, "position": Infinity, "queue": [1], "index": NaN, "days": Infinity, "uses": -Infinity,'
    b' "amountMb": Infinity, "quotaMb": Infinity, "trackIds": [Infinity]}',
]


def _routes(routes):
    """Toutes les routes, y compris celles des routeurs inclus (routes_*.py)."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        inner = getattr(getattr(r, "original_router", None), "routes", None)
        if inner:
            yield from _routes(inner)


async def _no_sleep(*a, **k):
    return None


@pytest.fixture
def admin(fresh_db, http, monkeypatch):
    monkeypatch.setattr(main, "_login_fails", {})
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.setattr(jam, "_jams", {})  # (soirées en mémoire : rien ne reste pour les autres tests)
    monkeypatch.setattr(jam, "_by_host", {})
    fresh_db.x("INSERT INTO albums(id, key, name, artist, search) VALUES(1, 'a', 'Album', 'Artiste', 'album artiste')")
    fresh_db.x("INSERT INTO tracks(id, path, title, artist, album_id, search) VALUES(1, '/m/1.mp3', 'Titre', 'Artiste', 1, 'titre')")
    users.update(1, password="admin-pass")
    users.create("Pote", "pote-pass", "member")
    c = TestClient(main.app, raise_server_exceptions=False)
    assert c.post("/login", data={"name": "", "password": "admin-pass"}, follow_redirects=False).status_code == 303
    yield c
    settings._cache = None


def _path(template: str, values: dict) -> str:
    return re.sub(r"\{(\w+)(:\w+)?\}", lambda m: str(values.get(m.group(1), 1)), template)


def test_no_server_error_on_invalid_input(admin):
    pid = admin.post("/api/playlists", json={"name": "Essai"}).json()["id"]
    values = {"tid": 1, "aid": 1, "pid": pid, "name": "Artiste", "key": "x", "ask_id": 1, "user_id": 2, "sid": 1,
              "token": jam.start(1, "Admin")["token"]}
    failures, tried = [], 0
    for route in _routes(main.app.routes):
        if not route.path.startswith("/api/"):
            continue
        for method in sorted(route.methods & {"POST", "PUT", "PATCH"}):
            if (method, route.path) in SKIP or (method, route.path) in KNOWN_500:
                continue
            url = _path(route.path, values)
            bodies = [json.dumps(t).encode() for t in TRAPS] + RAW_TRAPS
            for body in bodies:
                tried += 1
                r = admin.request(method, url, content=body, headers={"content-type": "application/json"})
                if r.status_code >= 500 and r.status_code != 503:  # (503 voulu : lecteur du Pi absent)
                    failures.append(f"{method} {route.path} {body[:60]!r} → {r.status_code}")
    assert tried > 100
    assert failures == [], "\n".join(failures)
    assert admin.get("/api/me").json()["role"] == "admin"  # (toujours connecté : aucun piège n'a cassé la session)


def test_json_never_contains_infinity():
    assert web.J({"a": float("inf"), "b": [float("nan"), 1.5], "c": "x"}).body == b'{"a":null,"b":[null,1.5],"c":"x"}'
    assert web.J({"a": 1}).body == b'{"a":1}'


def test_input_helpers():
    from fastapi import HTTPException
    assert web._int("12", 0, 10) == 10 and web._num(-3, 0, 5) == 0 and web._num("2.5", 0, 5) == 2.5
    assert web._ints(None, 5) == [] and web._ints([1, "2", 3.0], 2) == [1, 2]
    assert web._text("  salut  ", 3) == "sal" and web._text(None, 5) == "" and web._text(42, 5) == "42"
    assert web._obj(b"") == {} and web._obj(b'{"a": 1}') == {"a": 1}
    for bad in (lambda: web._num(float("inf"), 0, 1), lambda: web._num("nan", 0, 1), lambda: web._int([], 0, 1),
                lambda: web._ints("12", 5), lambda: web._ints([{}], 5), lambda: web._text({"a": 1}, 5),
                lambda: web._text(True, 5), lambda: web._obj(b"[1]"), lambda: web._obj(b"{x")):
        with pytest.raises(HTTPException) as exc:
            bad()
        assert exc.value.status_code == 400 and exc.value.detail.startswith("Requête invalide")


def test_resume_refuses_infinite_position(admin):
    r = admin.post("/api/resume", content=b'{"trackId": 1, "position": Infinity}',
                   headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert admin.post("/api/resume", json={"trackId": 1, "position": 12.5, "queue": [1]}).status_code == 200
    assert admin.get("/api/resume").json()["state"]["position"] == 12.5
