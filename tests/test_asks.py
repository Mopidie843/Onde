"""Espace par profil, demandes à l'admin (+ mini IA), partage de playlist, tableau de bord, mix selon l'heure."""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from onde import asks, config, dashboard, db, discover, downloader, main, quota, share, users


async def _no_sleep(*a, **k):
    return None


@pytest.fixture
def app(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_login_fails", {})
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'Album', 'Jul')")
    for i in (1, 2, 3):
        f = tmp_path / f"{i}.mp3"
        f.write_bytes(b"ID3" + b"\0" * 100)
        db.x("INSERT INTO tracks(id, path, title, artist, album_id, search, size, ext, genre) VALUES(?,?,?,?,1,?,?,?,?)",
             i, str(f), f"Titre {i}", "Jul", f"titre {i} jul", 10 * quota.MB, ".mp3", "rap")
    users.update(1, password="admin-pass")
    users.create("Pote", "pote-pass", "member")
    users.create("Ecoute", "ecoute-pass", "listener")
    return TestClient(main.app)


def login(name, pw):
    c = TestClient(main.app)
    r = c.post("/login", data={"name": name, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def uid(name):
    return db.q1("SELECT id FROM users WHERE name=?", name)["id"]


# ------------------------------------------------------------------ espace
def test_quota_counts_and_blocks(app):
    pote = uid("Pote")
    db.x("UPDATE tracks SET added_by=? WHERE id IN (1,2)", pote)
    info = quota.info(pote)
    assert info["used"] == 20 * quota.MB and info["limitMb"] == config.QUOTA_MEMBER_MB and not info["full"]
    assert quota.info(1)["limit"] is None  # l'admin n'a pas de limite
    quota.set_limit(pote, 15)
    assert quota.info(pote)["full"]
    c = login("Pote", "pote-pass")
    assert c.get("/api/me/quota").json()["full"] is True
    r = c.post("/api/import-url", json={"url": "jul - bande organisée"})
    assert r.status_code == 413 and "plein" in r.json()["detail"]
    r = c.post("/api/upload", content=b"x" * 10, headers={"x-filename": "a.mp3"})
    assert r.status_code == 413 and r.json()["quota"] is True
    # demande d'espace → l'admin accepte → limite augmentée
    a = c.post("/api/me/quota-request", json={"amountMb": 500}).json()
    assert a["kind"] == "quota" and a["status"] == "pending"
    assert c.post("/api/me/quota-request", json={"amountMb": 500}).status_code == 400  # (déjà envoyée)
    assert c.post(f"/api/asks/{a['id']}/approve").status_code == 403  # pas l'admin
    admin = login(config.ADMIN_NAME, "admin-pass")
    assert admin.post(f"/api/asks/{a['id']}/approve").json()["status"] == "done"
    assert quota.limit_mb(pote) == 515 and not quota.info(pote)["full"]


def test_import_marks_owner(app, monkeypatch):
    monkeypatch.setattr(downloader, "search_download",
                        lambda q: {"status": "added", "track": {"id": 3, "title": "Titre 3", "artist": "Jul"}})
    c = login("Pote", "pote-pass")
    assert c.post("/api/import-url", json={"url": "jul - titre 3"}).status_code == 200
    assert db.q1("SELECT added_by FROM tracks WHERE id=3")["added_by"] == uid("Pote")


def test_admin_sets_quota(app):
    admin = login(config.ADMIN_NAME, "admin-pass")
    assert admin.patch(f"/api/users/{uid('Pote')}", json={"quotaMb": 1234}).status_code == 200
    assert quota.limit_mb(uid("Pote")) == 1234
    admin.patch(f"/api/users/{uid('Pote')}", json={"quotaMb": None})
    assert quota.limit_mb(uid("Pote")) == config.QUOTA_MEMBER_MB


def test_import_error_logged(app, monkeypatch):
    def boom(q):
        raise RuntimeError("introuvable sur YouTube")
    monkeypatch.setattr(downloader, "search_download", boom)
    c = login("Pote", "pote-pass")
    assert c.post("/api/import-url", json={"url": "un truc"}).status_code == 400
    d = dashboard.compute()
    assert d["errors"] and "introuvable" in d["errors"][0]["detail"] and d["errors"][0]["name"] == "Pote"


# ------------------------------------------------------------------ demandes de titres
def test_listener_asks_admin_approves(app, monkeypatch):
    got = []
    monkeypatch.setattr(downloader, "search_download",
                        lambda q: got.append(q) or {"status": "added", "track": {"id": 2}})
    monkeypatch.setattr(asks.threading, "Thread", _SyncThread)
    ecoute = login("Ecoute", "ecoute-pass")
    assert ecoute.post("/api/import-url", json={"url": "x"}).status_code == 403  # écoute seule : pas d'import
    a = ecoute.post("/api/asks", json={"query": "Jul - Bande organisée"}).json()
    assert a["status"] == "pending" and a["autoAt"] > time.time()
    assert ecoute.post("/api/asks", json={"query": "jul - bande organisée"}).status_code == 400  # doublon
    assert [x["id"] for x in ecoute.get("/api/asks").json()["asks"]] == [a["id"]]
    assert ecoute.get("/api/dashboard").status_code == 403
    admin = login(config.ADMIN_NAME, "admin-pass")
    assert admin.get("/api/dashboard").json()["pendingAsks"] == 1
    r = admin.post(f"/api/asks/{a['id']}/approve").json()
    assert got == ["Jul - Bande organisée"]
    a2 = asks.get(a["id"])
    assert a2["status"] == "done" and a2["trackId"] == 2 and a2["decidedBy"] == "admin"
    assert admin.post(f"/api/asks/{a['id']}/refuse").status_code == 409  # déjà répondu
    assert r["id"] == a["id"]


class _SyncThread:
    def __init__(self, target=None, args=(), **k):
        self.t, self.a = target, args

    def start(self):
        self.t(*self.a)

    def join(self, *a):
        pass


def test_auto_decision_after_delay(app, monkeypatch):
    monkeypatch.setattr(asks.threading, "Thread", _SyncThread)
    monkeypatch.setattr(downloader, "search_download", lambda q: {"status": "added", "track": {"id": 1}})
    me = users.get(uid("Ecoute"))
    with users.use(me):
        known = asks.ask_track("Jul - Nouveau son")          # artiste déjà là → oui
        slowed = asks.ask_track("Jul - Son (slowed + reverb)")  # version retouchée → non
        dup = asks.ask_track("Jul - Titre 2")                  # déjà dans la bibliothèque → non
        vague = asks.ask_track("zzqx wkpv")                    # rien de connu, rien en ligne → non
    assert asks.tick(time.time(), online=False) == 0  # pas encore 5 minutes
    assert asks.tick(time.time() + config.ASK_AUTO_DELAY + 1, online=False) == 4
    k, s, d, v = (asks.get(x["id"]) for x in (known, slowed, dup, vague))
    assert k["status"] == "done" and k["decidedBy"] == "auto" and "Jul a déjà 3 titres" in k["reason"]
    assert s["status"] == "refused" and "retouchée" in s["reason"]
    assert d["status"] == "refused" and "déjà dans la bibliothèque" in d["reason"]
    assert v["status"] == "refused" and v["decidedBy"] == "auto"


def test_ai_uses_lyrics_and_meta(app, monkeypatch):
    from onde import lyrics
    monkeypatch.setattr(downloader, "_lookup_meta", lambda q, p=None: {"artist": "Bigflo & Oli", "title": "Alors alors", "duration": 200})
    monkeypatch.setattr(lyrics, "from_lrclib", lambda *a, **k: {"plain": "alors alors", "synced": None, "instrumental": False})
    v = asks.evaluate("bigflo et oli - alors alors", uid("Ecoute"))
    assert v["ok"] and v["score"] == 2 and any("paroles trouvées" in r for r in v["reasons"])
    monkeypatch.setattr(lyrics, "from_lrclib", lambda *a, **k: {"plain": "sieg heil ...", "synced": None})
    v = asks.evaluate("groupe - chanson", uid("Ecoute"))
    assert not v["ok"] and "paroles haineuses" in v["reasons"]


def test_ask_limits(app):
    with users.use(users.get(uid("Ecoute"))):
        for i in range(asks.MAX_PENDING):
            asks.ask_track(f"Artiste - Titre {i}")
        with pytest.raises(ValueError):
            asks.ask_track("Artiste - Encore un")
        with pytest.raises(ValueError):
            asks.ask_track("ab")


def test_admin_answer_beats_ai(app):
    with users.use(users.get(uid("Ecoute"))):
        a = asks.ask_track("Jul - Truc")
    asks.refuse(a["id"], "non merci")
    asks.tick(time.time() + 9999, online=False)
    assert asks.get(a["id"])["decidedBy"] == "admin"


# ------------------------------------------------------------------ partage de playlist
def test_share_playlist_readonly(app):
    pote = login("Pote", "pote-pass")
    pid = pote.post("/api/playlists", json={"name": "Été"}).json()["id"]
    pote.post(f"/api/playlists/{pid}/tracks", json={"trackIds": [1, 2]})
    sh = pote.post(f"/api/playlists/{pid}/share").json()["share"]
    tok = sh["token"]
    assert pote.post(f"/api/playlists/{pid}/share").json()["share"]["token"] == tok  # même lien
    ecoute = login("Ecoute", "ecoute-pass")
    assert ecoute.get(f"/api/playlists/{pid}/share").json()["share"] is None   # pas sa playlist
    assert ecoute.post(f"/api/playlists/{pid}/share").status_code == 403
    anon = TestClient(main.app)
    assert anon.get("/api/info").status_code == 401
    v = anon.get(f"/api/share/{tok}").json()
    assert v["name"] == "Été" and [t["id"] for t in v["tracks"]] == [1, 2] and v["owner"] == "Pote"
    assert anon.get(f"/api/share/{tok}/stream/1").status_code == 200
    assert anon.get(f"/api/share/{tok}/stream/3").status_code == 404   # pas dans la playlist
    assert anon.get(f"/api/tracks/3/stream").status_code == 401
    assert anon.get(f"/share/{tok}").status_code == 200
    assert anon.get("/share/inconnu").status_code == 410
    assert anon.post(f"/api/share/{tok}/stream/1").status_code in (401, 405)
    pote.delete(f"/api/playlists/{pid}/share")
    assert anon.get(f"/api/share/{tok}").status_code == 404


# ------------------------------------------------------------------ tableau de bord
def test_dashboard(app):
    pote = login("Pote", "pote-pass")
    pote.post("/api/resume", json={"trackId": 1, "position": 12, "queue": [1, 2], "playing": True, "device": "Téléphone"})
    d = login(config.ADMIN_NAME, "admin-pass").get("/api/dashboard").json()
    names = {o["name"] for o in d["online"]}
    assert {"Pote", config.ADMIN_NAME} <= names
    assert d["playing"][0]["name"] == "Pote" and d["playing"][0]["track"]["id"] == 1
    assert d["library"]["tracks"] == 3 and d["library"]["size"] == 30 * quota.MB
    assert d["disk"]["free"] > 0
    assert {u["name"] for u in d["users"]} == {config.ADMIN_NAME, "Pote", "Ecoute"}


# ------------------------------------------------------------------ mix selon l'heure
def test_daypart_and_now_mix(app):
    assert discover.daypart(8)[0] == "energie" and discover.daypart(22)[0] == "chill"
    assert discover.daypart(3)[2] == "Mix de la nuit" and discover.daypart(15)[0] == "focus"
    for i in range(4, 14):
        db.x("INSERT INTO tracks(id, path, title, artist, album_id, search, genre) VALUES(?,?,?,?,1,?,?)",
             i, f"/m/{i}.mp3", f"T{i}", f"A{i}", f"t{i}", "rap" if i % 2 else "lofi")
    discover.invalidate()
    with users.use(users.get(1)):
        name, ids = discover.mix_tracks("now", "8")
        assert name == "Mix du matin" and ids
        rap = {r["id"] for r in db.q("SELECT id FROM tracks WHERE genre='rap'")}
        assert sum(1 for i in ids[:5] if i in rap) >= 3  # surtout de l'énergie le matin
        name, ids = discover.mix_tracks("now", "22")
        assert name == "Mix du soir"
    morning = discover.daypart(8)
    import pytest as _p
    mp = _p.MonkeyPatch()
    mp.setattr(discover, "daypart", lambda hour=None: morning)
    try:
        with users.use(users.get(1)):
            m = discover.mixes()
        assert m[0]["key"] == "mix:now" and m[0]["name"] == "Mix du matin"
    finally:
        mp.undo()
