"""Profils : connexion, droits, données propres à chacun, invitations, migration."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from onde import config, db, main, users


async def _no_sleep(*a, **k):
    return None


@pytest.fixture
def app(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "_login_fails", {})
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    db.x("INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'Album', 'Artiste')")
    for i in (1, 2, 3):
        db.x("INSERT INTO tracks(id, path, title, artist, album_id, search) VALUES(?,?,?,?,1,?)",
             i, f"/m/{i}.mp3", f"Titre {i}", "Artiste", f"titre {i}")
    users.update(1, password="admin-pass")
    users.create("Pote", "pote-pass", "member")
    users.create("Ecoute", "ecoute-pass", "listener")
    return TestClient(main.app)


def login(app, name, pw):
    c = TestClient(main.app)
    r = c.post("/login", data={"name": name, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def test_open_access_without_password(fresh_db):
    c = TestClient(main.app)
    me = c.get("/api/me").json()
    assert me["role"] == "admin" and me["name"] == config.ADMIN_NAME and me["accounts"] is False


def test_login_required_and_wrong_password(app):
    assert app.get("/api/info").status_code == 401
    assert app.get("/", follow_redirects=False).headers["location"] == "/login"
    r = app.post("/login", data={"name": "Pote", "password": "faux"}, follow_redirects=False)
    assert r.status_code == 401
    r = app.post("/login", data={"name": "Personne", "password": "x"}, follow_redirects=False)
    assert r.status_code == 401


def test_legacy_password_only_logs_admin(app):
    c = login(app, "", "admin-pass")
    assert c.get("/api/me").json()["role"] == "admin"


def test_rights(app):
    pote = login(app, "pote", "pote-pass")       # (pseudo sans majuscule : accepté)
    ecoute = login(app, "Ecoute", "ecoute-pass")
    admin = login(app, config.ADMIN_NAME, "admin-pass")
    # tout le monde écoute, like, crée ses playlists
    for c in (pote, ecoute, admin):
        assert c.get("/api/tracks").status_code == 200
        assert c.put("/api/liked/1").status_code == 200
        assert c.post("/api/playlists", json={"name": "x"}).status_code == 200
    # suppression, réglages, profils : administrateur seulement
    for c in (pote, ecoute):
        assert c.delete("/api/tracks/2").status_code == 403
        assert c.put("/api/settings", json={"autoplay": False}).status_code == 403
        assert c.get("/api/users").status_code == 403
        assert c.post("/api/invites", json={"role": "member"}).status_code == 403
        assert c.get("/api/dev/logs").status_code == 403
        assert c.get("/api/library/export").status_code == 403
    # import : « écoute + import » oui, « écoute seule » non
    assert ecoute.post("/api/import-url", json={"url": "x"}).status_code == 403
    assert pote.post("/api/import-url", json={"url": ""}).status_code != 403
    assert admin.get("/api/users").status_code == 200


def test_likes_and_playlists_are_per_profile(app):
    pote = login(app, "Pote", "pote-pass")
    admin = login(app, config.ADMIN_NAME, "admin-pass")
    pote.put("/api/liked/1")
    admin.put("/api/liked/2")
    assert [t["id"] for t in pote.get("/api/liked").json()] == [1]
    assert [t["id"] for t in admin.get("/api/liked").json()] == [2]
    assert pote.get("/api/tracks/2").json()["liked"] is False
    pid = pote.post("/api/playlists", json={"name": "À moi"}).json()["id"]
    assert [p["name"] for p in admin.get("/api/playlists").json()] == []
    assert admin.get(f"/api/playlists/{pid}").status_code == 404
    assert admin.delete(f"/api/playlists/{pid}").status_code == 404   # pas celle des autres
    assert pote.get(f"/api/playlists/{pid}").status_code == 200
    # historique et stats
    pote.post("/api/tracks/3/played")
    assert [t["id"] for t in pote.get("/api/history").json()] == [3]
    assert admin.get("/api/history").json() == []


def test_invite_flow(app):
    admin = login(app, config.ADMIN_NAME, "admin-pass")
    link = admin.post("/api/invites", json={"role": "member", "days": 1}).json()["path"]
    guest = TestClient(main.app)
    assert "Crée ton profil" in guest.get(link).text
    r = guest.post(link, data={"name": "Ami", "password": "court"}, follow_redirects=False)
    assert r.status_code == 400  # mot de passe trop court
    r = guest.post(link, data={"name": "Ami", "password": "ami-pass-1", "email": "ami@example.com"},
                   follow_redirects=False)
    assert r.status_code == 400 and "conditions" in r.text  # case des conditions pas cochée
    assert "/conditions" in guest.get(link).text
    r = guest.post(link, data={"name": "Ami", "password": "ami-pass-1", "email": "ami@example.com", "terms": "1"},
                   follow_redirects=False)
    assert r.status_code == 303
    me = guest.get("/api/me").json()
    assert me["name"] == "Ami" and me["role"] == "member" and me["terms"] is True
    assert "Usage strictement privé" in TestClient(main.app).get("/conditions").text
    other = TestClient(main.app)
    assert other.post(link, data={"name": "Ami2", "password": "ami-pass-2"}, follow_redirects=False).status_code == 410 \
        or "expiré" in other.post(link, data={"name": "Ami2", "password": "ami-pass-2"}).text  # lien à usage unique
    assert any(a["action"] == "signup" for a in admin.get("/api/activity").json())


def test_admin_manages_profiles(app):
    admin = login(app, config.ADMIN_NAME, "admin-pass")
    lst = admin.get("/api/users").json()["users"]
    pote = next(u for u in lst if u["name"] == "Pote")
    assert admin.patch(f"/api/users/{pote['id']}", json={"role": "listener"}).json()["role"] == "listener"
    assert admin.patch("/api/users/1", json={"role": "listener"}).status_code == 400  # Mopidie reste admin
    assert admin.patch(f"/api/users/{pote['id']}", json={"password": "nouveau-pass"}).status_code == 200
    login(app, "Pote", "nouveau-pass")
    assert admin.delete("/api/users/1").status_code == 400
    assert admin.delete(f"/api/users/{pote['id']}").status_code == 200


def test_change_own_password_keeps_this_device(app):
    pote = login(app, "Pote", "pote-pass")
    r = pote.post("/api/settings/password", json={"current": "faux", "password": "x-new-pass"})
    assert r.status_code == 403
    r = pote.post("/api/settings/password", json={"current": "pote-pass", "password": "x-new-pass"})
    assert r.status_code == 200
    assert pote.get("/api/me").json()["name"] == "Pote"   # toujours connecté
    login(app, "Pote", "x-new-pass")


def test_logout(app):
    c = login(app, "Pote", "pote-pass")
    c.get("/logout", follow_redirects=False)
    assert c.get("/api/me").status_code == 401


def test_password_hash_is_not_plain(app):
    row = db.q1("SELECT pw FROM users WHERE name='Pote'")
    assert "pote-pass" not in row["pw"] and row["pw"].startswith("scrypt$")
    assert users.check_password("pote-pass", row["pw"]) and not users.check_password("x", row["pw"])


def test_old_likes_table_is_migrated(tmp_path, monkeypatch):
    main_db = tmp_path / "old.db"
    con = sqlite3.connect(main_db)
    old_schema = db.SCHEMA.split("CREATE TABLE IF NOT EXISTS likes(")[0] + \
        "CREATE TABLE likes(track_id INTEGER PRIMARY KEY, liked_at REAL);\n"
    con.executescript(old_schema)
    con.executescript("""INSERT INTO albums(id, key, name, artist) VALUES(1, 'a', 'A', 'B');
                         INSERT INTO tracks(id, path, title, artist, album_id) VALUES(5, '/5', 't', 'a', 1), (7, '/7', 't', 'a', 1);
                         INSERT INTO likes VALUES(5, 1.0), (7, 2.0), (9, 3.0);""")
    con.commit()
    con.close()
    from tests.conftest import _close_connection
    _close_connection()
    monkeypatch.setattr(config, "DB_PATH", main_db)
    db.init()
    rows = db.q("SELECT user_id, track_id FROM likes ORDER BY track_id")
    assert [(r["user_id"], r["track_id"]) for r in rows] == [(1, 5), (1, 7)]
    _close_connection()


def test_legacy_password_migrates_to_admin(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "PASSWORD", "ancien-mdp")
    db.x("DELETE FROM meta WHERE k='users:legacy_pw'")
    db.x("UPDATE users SET pw=NULL")
    users.ensure_admin()
    assert users.authenticate("", "ancien-mdp")["role"] == "admin"
    assert users.accounts_enabled()


def test_app_json_auth(app):
    c = TestClient(main.app)
    st = c.get("/api/auth/state").json()
    assert st["accounts"] is True and st["me"] is None
    r = c.post("/api/auth/login", json={"name": "Pote", "password": "faux"})
    assert r.status_code == 401 and "incorrect" in r.json()["detail"]
    r = c.post("/api/auth/login", json={"name": "Pote", "password": "pote-pass"})
    assert r.status_code == 200 and users.COOKIE in r.headers["set-cookie"]
    assert c.get("/api/auth/state").json()["me"]["name"] == "Pote"
    assert c.get("/api/me").json()["role"] == "member"
    c.post("/api/auth/logout")
    assert c.get("/api/me").status_code == 401


def test_app_signup_with_pasted_link(app):
    admin = login(app, config.ADMIN_NAME, "admin-pass")
    path = admin.post("/api/invites", json={"role": "listener"}).json()["path"]
    tok = path.rsplit("/", 1)[1]
    c = TestClient(main.app)
    assert c.get(f"/api/auth/invite/{tok}").json() == {"valid": True, "role": "listener", "roleLabel": "Écoute seule"}
    r = c.post("/api/auth/signup", json={"token": f"https://onde.example.ts.net{path}", "name": "Appli", "password": "appli-pass"})
    assert r.status_code == 400 and "conditions" in r.json()["detail"]
    r = c.post("/api/auth/signup", json={"token": f"https://onde.example.ts.net{path}", "name": "Appli", "password": "appli-pass", "terms": True})
    assert r.status_code == 200 and r.json()["user"]["role"] == "listener"
    assert c.get("/api/me").json()["name"] == "Appli"
    r = TestClient(main.app).post("/api/auth/signup", json={"token": tok, "name": "Autre", "password": "autre-pass", "terms": True})
    assert r.status_code == 400   # invitation à usage unique
    fresh = admin.post("/api/invites", json={"role": "member"}).json()["path"]
    page = TestClient(main.app).get(fresh).text
    assert "onde://join?server=" in page and "token=" in page


def test_admin_reset_link(app):
    """Sans serveur de mail : l'admin crée un lien, le pote choisit un nouveau mot de passe, une seule fois."""
    admin = login(app, config.ADMIN_NAME, "admin-pass")
    pote_old = login(app, "Pote", "pote-pass")
    pid = next(u["id"] for u in admin.get("/api/users").json()["users"] if u["name"] == "Pote")
    # seul l'admin peut créer un lien
    assert pote_old.post(f"/api/users/{pid}/reset-link").status_code == 403
    r = admin.post(f"/api/users/{pid}/reset-link")
    assert r.status_code == 200
    path = r.json()["path"]
    assert path.startswith("/reset/") and r.json()["expires"] > __import__("time").time() + 23 * 3600
    assert admin.post("/api/users/9999/reset-link").status_code == 404
    # un nouveau lien annule le précédent
    path2 = admin.post(f"/api/users/{pid}/reset-link").json()["path"]
    anon = TestClient(main.app)
    assert anon.get(path).status_code == 410
    assert anon.get(path2).status_code == 200
    # mot de passe trop court : refusé, le lien reste valable
    assert anon.post(path2, data={"password": "x"}, follow_redirects=False).status_code == 400
    ok = anon.post(path2, data={"password": "nouveau-pass-123"}, follow_redirects=False)
    assert ok.status_code == 303
    # ancien appareil déconnecté, ancien mot de passe refusé, nouveau accepté, lien déjà utilisé
    assert pote_old.get("/api/me").status_code == 401
    assert TestClient(main.app).post("/login", data={"name": "Pote", "password": "pote-pass"}, follow_redirects=False).status_code != 303
    login(app, "Pote", "nouveau-pass-123")
    assert TestClient(main.app).get(path2).status_code == 410
    assert TestClient(main.app).post(path2, data={"password": "autre-pass-456"}, follow_redirects=False).status_code == 400


def test_reset_link_expires(app):
    admin = login(app, config.ADMIN_NAME, "admin-pass")
    pid = next(u["id"] for u in admin.get("/api/users").json()["users"] if u["name"] == "Pote")
    path = admin.post(f"/api/users/{pid}/reset-link").json()["path"]
    db.x("UPDATE resets SET expires=?", 1.0)
    anon = TestClient(main.app)
    assert anon.get(path).status_code == 410
    assert anon.post(path, data={"password": "nouveau-pass-123"}, follow_redirects=False).status_code == 400


def test_forgot_without_mail_warns_admin(app, monkeypatch):
    """Sans serveur de mail : « Mot de passe oublié » prévient l'admin (notification, tableau de bord), sans rien révéler."""
    monkeypatch.setattr(users, "mail_available", lambda: False)
    anon = TestClient(main.app)
    assert 'name="name"' in anon.get("/forgot").text           # le formulaire demande le pseudo
    assert "/forgot" in anon.get("/login").text                # et le lien est toujours sur la page de connexion
    admin = login(app, config.ADMIN_NAME, "admin-pass")
    start = admin.get("/api/notifications").json()["last"]
    ok = anon.post("/forgot", data={"name": "Pote"})
    unknown = anon.post("/forgot", data={"name": "Personne"})
    assert ok.status_code == unknown.status_code == 200
    assert ok.text == unknown.text                              # même réponse : on ne devine pas les pseudos
    reqs = admin.get("/api/pwreqs").json()["requests"]
    assert [r["name"] for r in reqs] == ["Pote"]
    # notification pour l'admin seulement
    ev = admin.get(f"/api/notifications?after={start}").json()["events"]
    assert [e["kind"] for e in ev] == ["password"] and "Pote" in ev[0]["title"] and ev[0]["link"] == "#/dashboard"
    pote = login(app, "Pote", "pote-pass")
    assert pote.get(f"/api/notifications?after={start}").json()["events"] == []
    assert pote.get("/api/pwreqs").status_code == 403 and pote.delete(f"/api/pwreqs/{reqs[0]['id']}").status_code == 403
    # anti-spam : pas de seconde notification tout de suite
    anon.post("/forgot", data={"name": "pote"})
    assert len(admin.get("/api/pwreqs").json()["requests"]) == 1
    assert len(admin.get(f"/api/notifications?after={start}").json()["events"]) == 1
    # l'admin ne peut pas être « prévenu » pour lui-même, pseudo vide ignoré
    anon.post("/forgot", data={"name": config.ADMIN_NAME}); anon.post("/forgot", data={"name": ""})
    assert len(admin.get("/api/pwreqs").json()["requests"]) == 1
    assert admin.get("/api/dashboard").json()["pwreqs"][0]["name"] == "Pote"
    # créer le lien traite la demande
    r = admin.post(f"/api/users/{reqs[0]['userId']}/reset-link")
    assert r.status_code == 200 and admin.get("/api/pwreqs").json()["requests"] == []


def test_pwreq_dismiss(app, monkeypatch):
    monkeypatch.setattr(users, "mail_available", lambda: False)
    TestClient(main.app).post("/forgot", data={"name": "Ecoute"})
    admin = login(app, config.ADMIN_NAME, "admin-pass")
    rid = admin.get("/api/pwreqs").json()["requests"][0]["id"]
    assert admin.delete(f"/api/pwreqs/{rid}").status_code == 200
    assert admin.get("/api/pwreqs").json()["requests"] == []
    # les notifications d'« ask » sont aussi cachées aux amis
    from onde import notify
    notify.add("ask", "Demande de X", "…")
    pote = login(app, "Pote", "pote-pass")
    assert all(e["kind"] != "ask" for e in pote.get("/api/notifications?after=0").json().get("events", []))


def test_app_forgot_without_mail(app, monkeypatch):
    """L'appli Android (API /api/auth/forgot) : sans serveur de mail, le pseudo ou le mail prévient l'admin."""
    monkeypatch.setattr(users, "mail_available", lambda: False)
    admin = login(app, config.ADMIN_NAME, "admin-pass")
    users.update(users.get_by_name("Pote")["id"] if hasattr(users, "get_by_name") else next(u["id"] for u in users.all_users() if u["name"] == "Pote"), email="pote@exemple.fr")
    anon = TestClient(main.app)
    r = anon.post("/api/auth/forgot", json={"name": "Pote", "email": "Pote"})
    assert r.status_code == 200 and r.json()["ok"]
    assert [x["name"] for x in admin.get("/api/pwreqs").json()["requests"]] == ["Pote"]
    admin.delete(f"/api/pwreqs/{admin.get('/api/pwreqs').json()['requests'][0]['id']}")
    assert anon.post("/api/auth/forgot", json={"email": "pote@exemple.fr"}).status_code == 200   # par le mail aussi
    assert [x["name"] for x in admin.get("/api/pwreqs").json()["requests"]] == ["Pote"]
    assert anon.post("/api/auth/forgot", json={"name": "Inconnu"}).json()["ok"]                  # même réponse
