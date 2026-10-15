"""Sécurité du serveur : vrai visiteur derrière la porte d'entrée HTTPS, liens de mot de passe,
visiteurs anonymes des pages publiques, liens vers le réseau local, adresse à distance."""
from __future__ import annotations

import io
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from onde import config, db, gateway, jam, main, remote, settings, users, web


async def _no_sleep(*a, **k):
    return None


@pytest.fixture
def app(fresh_db, monkeypatch):
    monkeypatch.setattr(main, "_login_fails", {})
    monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(settings, "_cache", None)
    monkeypatch.setattr(jam, "_jams", {})  # (soirées en mémoire : rien ne reste pour les autres tests)
    monkeypatch.setattr(jam, "_by_host", {})
    monkeypatch.setattr(remote, "status", lambda force=False: {"mode": "public", "url": "https://onde.tail1234.ts.net",
                                                               "ip": "100.64.0.7", "dns": "onde.tail1234.ts.net"})
    monkeypatch.setattr(main, "lan_urls", lambda: ["http://192.168.1.20:8000", "http://onde.local:8000"])
    fresh_db.x("INSERT INTO albums(id, key, name, artist, search) VALUES(1, 'a', 'Album', 'Artiste', 'album artiste')")
    fresh_db.x("INSERT INTO tracks(id, path, title, artist, album_id, search) VALUES(1, '/m/1.mp3', 'Titre', 'Artiste', 1, 'titre artiste')")
    users.update(1, password="admin-pass")
    users.create("Pote", "pote-pass", "member", "pote@exemple.fr")
    users.create("Ecoute", "ecoute-pass", "listener")
    yield TestClient(main.app)
    settings._cache = None


def login(name, pw, **kw):
    c = TestClient(main.app, **kw)
    r = c.post("/login", data={"name": name, "password": pw}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


# ------------------------------------------------------------------ FX02 / FX04 : porte d'entrée HTTPS
def _fail(c, ip=None):
    headers = {"x-forwarded-for": ip} if ip else {}
    return c.post("/login", data={"name": "Pote", "password": "faux"}, headers=headers, follow_redirects=False)


def test_visitor_behind_gateway_is_the_real_one(app, monkeypatch):
    """Derrière la porte d'entrée, tout arrive de 127.0.0.1 : le blocage vise le vrai visiteur, et un
    x-forwarded-for inventé par lui ne change rien."""
    monkeypatch.setattr(gateway, "PEERS", {41001: ("203.0.113.9", True), 41002: ("198.51.100.4", True)})
    attacker = TestClient(main.app, client=("127.0.0.1", 41001))
    for i in range(main.LOGIN_MAX_FAILS):
        assert _fail(attacker, f"10.0.0.{i}").status_code == 401  # (chaque fois une « autre » adresse inventée)
    assert set(main._login_fails) == {"203.0.113.9"}
    r = attacker.post("/login", data={"name": "Pote", "password": "pote-pass"}, follow_redirects=False)
    assert r.status_code == 429
    # un autre visiteur, relayé par la même porte, n'est pas bloqué pour autant
    other = TestClient(main.app, client=("127.0.0.1", 41002))
    r = other.post("/login", data={"name": "Pote", "password": "pote-pass"}, follow_redirects=False)
    assert r.status_code == 303


def test_relay_on_the_machine_itself_still_reads_forwarded_header(app, monkeypatch):
    """Tailscale Serve / cloudflared arrivent sur la porte depuis la machine elle-même : leur en-tête compte."""
    monkeypatch.setattr(gateway, "PEERS", {41003: ("127.0.0.1", False)})
    c = TestClient(main.app, client=("127.0.0.1", 41003))
    _fail(c, "1.2.3.4, 203.0.113.50")
    assert set(main._login_fails) == {"203.0.113.50"}


def test_session_cookie_is_secure_behind_gateway(app, monkeypatch):
    monkeypatch.setattr(gateway, "PEERS", {41004: ("203.0.113.9", True), 41005: ("192.168.1.30", False)})
    r = TestClient(main.app, client=("127.0.0.1", 41004)).post(
        "/login", data={"name": "Pote", "password": "pote-pass"}, follow_redirects=False)
    assert "secure" in r.headers["set-cookie"].lower()
    r = TestClient(main.app, client=("127.0.0.1", 41005)).post(
        "/login", data={"name": "Pote", "password": "pote-pass"}, follow_redirects=False)
    assert "secure" not in r.headers["set-cookie"].lower()  # HTTP simple sur le réseau local


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_gateway_notes_the_visitor_while_relaying():
    """La vraie porte d'entrée : pendant le relais, Onde retrouve l'origine de la connexion ; après, plus rien."""
    seen: list = []
    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen(1)

    def serve():
        conn, addr = upstream.accept()
        with conn:
            conn.recv(4096)
            seen.append(gateway.origin(addr[1]))
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")

    threading.Thread(target=serve, daemon=True).start()
    gw = gateway.Gateway("127.0.0.1", _free_port(), upstream.getsockname()[1], "http", None, Path("absent.crt"))
    gw.start()
    try:
        with socket.create_connection(("127.0.0.1", gw.port), timeout=5) as c:
            c.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            data = b""
            while chunk := c.recv(4096):
                data += chunk
        assert data.endswith(b"ok")
        assert seen == [("127.0.0.1", False)]
        for _ in range(50):
            if not gateway.PEERS:
                break
            time.sleep(0.05)
        assert gateway.PEERS == {}
    finally:
        gw.stop()
        upstream.close()


# ------------------------------------------------------------------ FX03 : lien de mot de passe par mail
@pytest.fixture
def mails(app, monkeypatch):
    sent: list = []
    monkeypatch.setattr(users, "mail_available", lambda: True)
    monkeypatch.setattr(users, "send_mail", lambda to, subject, text: sent.append((to, text)))
    return sent


def test_reset_mail_ignores_forged_host(app, mails):
    anon = TestClient(main.app)
    r = anon.post("/api/auth/forgot", json={"email": "pote@exemple.fr"}, headers={"host": "piege.example"})
    assert r.status_code == 200 and r.json()["admin"] is True
    assert mails == []  # pas de lien vers l'adresse inventée…
    admin = login(config.ADMIN_NAME, "admin-pass")
    assert [x["name"] for x in admin.get("/api/pwreqs").json()["requests"]] == ["Pote"]  # … l'admin est prévenu
    r = anon.post("/forgot", data={"email": "pote@exemple.fr"}, headers={"host": "piege.example"})
    assert r.status_code == 200 and "administrateur" in r.text and mails == []


def test_reset_mail_uses_known_addresses(app, mails):
    anon = TestClient(main.app)
    anon.post("/api/auth/forgot", json={"email": "pote@exemple.fr"}, headers={"host": "192.168.1.20:8000"})
    assert len(mails) == 1 and "http://192.168.1.20:8000/reset/" in mails[0][1]
    anon.post("/forgot", data={"email": "pote@exemple.fr"}, headers={"host": "onde.tail1234.ts.net"})
    assert len(mails) == 2 and "https://onde.tail1234.ts.net/reset/" in mails[1][1]
    settings.update({"public_url": "https://musique.exemple.fr/"})
    anon.post("/api/auth/forgot", json={"email": "pote@exemple.fr"}, headers={"host": "piege.example"})
    assert len(mails) == 3 and "https://musique.exemple.fr/reset/" in mails[2][1]
    assert db.q1("SELECT COUNT(*) n FROM resets")["n"] == 1  # un seul lien actif à la fois


def test_reset_with_empty_password_is_refused(app):
    """Un lien de réinitialisation ne peut pas enlever le mot de passe de l'admin (retour à l'accès libre)."""
    token, _ = users.admin_reset_link(1)
    with pytest.raises(ValueError):
        users.finish_reset(token, "")
    anon = TestClient(main.app)
    r = anon.post(f"/reset/{token}", data={"password": ""}, follow_redirects=False)
    assert r.status_code == 400 and "6 caractères" in r.text
    assert users.accounts_enabled() and users.authenticate(config.ADMIN_NAME, "admin-pass")
    assert users.reset_valid(token)  # (le lien sert encore pour un vrai mot de passe)


# ------------------------------------------------------------------ FX09 : visiteur anonyme
def test_anonymous_visitor_is_not_the_admin(app):
    anon = TestClient(main.app)
    r = anon.get("/api/terms").json()
    assert r["accepted"] is None and r["version"]
    pote = login("Pote", "pote-pass")
    assert pote.get("/api/terms").json()["accepted"] is False
    assert not users.allowed("guest", "GET", "/api/tracks")
    # soirée : un invité sans compte voit la soirée et ajoute un titre comme avant
    token = login(config.ADMIN_NAME, "admin-pass").post("/api/jam").json()["token"]
    assert anon.get(f"/api/jam/{token}").status_code == 200
    assert anon.get(f"/api/jam/{token}/search", params={"q": "titre"}).json()["tracks"][0]["id"] == 1
    assert anon.post(f"/api/jam/{token}/add", json={"trackId": 1, "by": "Léa"}).json()["ok"]
    assert anon.post(f"/api/jam/{token}/add", json={"trackId": "abc"}).status_code == 400


# ------------------------------------------------------------------ FX10 : liens vers le réseau local
def _png() -> bytes:
    buf = io.BytesIO()
    Image.effect_noise((64, 64), 40).convert("RGB").save(buf, "PNG")  # (16 px et 100 octets au moins)
    return buf.getvalue()


def test_cover_by_url_refuses_local_network(app, http, monkeypatch):
    ecoute = login("Ecoute", "ecoute-pass")
    pid = ecoute.post("/api/playlists", json={"name": "À moi"}).json()["id"]
    for url in ("http://127.0.0.1:22/x.png", "http://192.168.1.1/x.png", "http://[::1]/x.png", "http://169.254.169.254/",
                "http://100.101.102.103/x.png", "http://[::ffff:10.0.0.1]/x.png", "http://0.0.0.0:8000/"):
        r = ecoute.post(f"/api/playlists/{pid}/cover", json={"url": url})
        assert r.status_code == 400 and "Adresse locale refusée" in r.json()["detail"], url
    assert http.requests == []  # aucune requête partie
    # un nom qui pointe vers le réseau local est refusé aussi
    monkeypatch.setattr(web.socket, "getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", ("10.0.0.8", 0))])
    assert "Adresse locale" in ecoute.post(f"/api/playlists/{pid}/cover", json={"url": "http://nas.maison/x.png"}).json()["detail"]
    # une adresse publique passe ; une erreur ne dit rien de la cible (« Image inaccessible »)
    monkeypatch.setattr(web.socket, "getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    http.routes["https://img.example/ok.png"] = httpx.Response(200, content=_png())
    assert ecoute.post(f"/api/playlists/{pid}/cover", json={"url": "https://img.example/ok.png"}).status_code == 200
    r = ecoute.post(f"/api/playlists/{pid}/cover", json={"url": "https://img.example/absente.png"})
    assert r.status_code == 400 and r.json()["detail"] == "Image inaccessible depuis Onde."
    # corps invalides : 400, jamais 500
    assert ecoute.post(f"/api/playlists/{pid}/cover", json=["x"]).status_code == 400
    assert ecoute.post(f"/api/playlists/{pid}/cover", json={"url": {"a": 1}}).status_code == 400


def test_import_url_refuses_local_network_for_friends(app, http):
    pote = login("Pote", "pote-pass")
    r = pote.post("/api/import-url", json={"url": "http://10.0.0.5:8080/chanson.mp3"})
    assert r.status_code == 400 and "Adresse locale refusée" in r.json()["detail"]
    assert http.requests == []
    assert pote.post("/api/import-url", json={"url": ["x"]}).status_code == 400


# ------------------------------------------------------------------ FX52 : adresse à distance pour tous
def test_remote_url_for_every_profile(app):
    ecoute = login("Ecoute", "ecoute-pass")
    assert ecoute.get("/api/remote").status_code == 403  # l'état détaillé reste pour l'admin
    assert ecoute.get("/api/remote/url").json() == {"mode": "public", "url": "https://onde.tail1234.ts.net"}
    assert not users.allowed("listener", "GET", "/api/remote/urlx")


# ------------------------------------------------------------------ FX65 : pas de gzip sur les gros fichiers
def test_no_gzip_on_streams_and_archives():
    for path in ("/api/share/abc/stream/12", "/api/library/export", "/api/app/apk", "/api/tracks/3/stream"):
        assert main.NO_GZIP.match(path), path
    for path in ("/api/library/export/info", "/api/share/abc", "/api/tracks"):
        assert not main.NO_GZIP.match(path), path
