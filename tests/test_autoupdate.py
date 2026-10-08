"""Mise à jour automatique (GitHub / fichier json), appli Android servie par le serveur, conditions d'utilisation."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from onde import autoupdate, config, main, restart, settings, terms, users


def _zip(version="9.9.9") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("onde/onde/main.py", "new")
        z.writestr("onde/onde/config.py", f'VERSION = "{version}"\n')
    return buf.getvalue()


@pytest.fixture
def install(tmp_path, monkeypatch, fresh_db):
    root = tmp_path / "install"
    (root / "onde").mkdir(parents=True)
    (root / "onde" / "main.py").write_text("old")
    monkeypatch.setattr(config, "ROOT", root)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    restarts = []
    monkeypatch.setattr(restart, "request", lambda delay=0.6: restarts.append(delay) or True)
    return root, restarts


def test_versions():
    assert autoupdate.newer("1.10.0", "1.9.9") and not autoupdate.newer("1.3.0", "1.3.0")
    assert autoupdate.newer("v2", "1.9") and not autoupdate.newer("", "1.0")


def test_github_release_installs(install, http):
    root, restarts = install
    data = _zip()
    settings.update({"update_source": "moi/onde"})
    http.routes["https://api.github.com/repos/moi/onde/releases"] = httpx.Response(200, json=[
        {"tag_name": "onde-77", "assets": [{"name": "Onde.exe", "browser_download_url": "https://github.com/moi/onde/releases/download/onde-77/Onde.exe"}]},  # (release de l'exe, plus récente)
        {"tag_name": "v9.9.9", "body": "nouveautés",
         "assets": [{"name": "onde-tout.zip", "browser_download_url": "https://github.com/moi/onde/releases/download/v9.9.9/onde-tout.zip",
                     "digest": "sha256:" + hashlib.sha256(data).hexdigest()}]}])
    http.routes["https://github.com/moi/onde/releases/download/v9.9.9/onde-tout.zip"] = httpx.Response(200, content=data)
    st = autoupdate.check()
    assert st["error"] is None and st["installed"]["to"] == "9.9.9"
    assert (root / "onde" / "main.py").read_text() == "new" and restarts


def test_bad_digest_refused(install, http):
    root, restarts = install
    settings.update({"update_source": "moi/onde"})
    http.routes["https://api.github.com/repos/moi/onde/releases"] = httpx.Response(200, json=[
        {"tag_name": "9.9.9", "assets": [{"name": "onde.zip", "browser_download_url": "https://x.example/onde.zip",
                                          "digest": "sha256:" + "0" * 64}]}])
    http.routes["https://x.example/onde.zip"] = httpx.Response(200, content=_zip())
    st = autoupdate.check()
    assert "Empreinte" in st["error"] and (root / "onde" / "main.py").read_text() == "old" and not restarts


def test_manifest_and_up_to_date(install, http):
    root, restarts = install
    settings.update({"update_source": "https://maj.example/onde.json"})
    http.routes["https://maj.example/onde.json"] = httpx.Response(200, json={"version": config.VERSION, "url": "https://maj.example/z.zip",
                                                                              "sha256": "a" * 64})
    st = autoupdate.check()
    assert st["latest"] == config.VERSION and st["installed"] is None and not restarts
    http.routes["https://maj.example/onde.json"] = httpx.Response(200, json={"version": "99.0", "url": "https://maj.example/z.zip"})
    assert "empreinte" in autoupdate.check()["error"]  # sans sha256 : refusé
    settings.update({"update_source": "http://pas-https.example/x.json"})
    assert "https" in autoupdate.check()["error"]


def test_apk_served(install):
    root, _ = install
    (root / "android").mkdir()
    (root / "android" / "Onde.apk").write_bytes(b"PK-apk")
    (root / "android" / "Onde.apk.json").write_text(json.dumps({"versionCode": 7, "versionName": "1.6"}))
    users.update(1, password="admin-pass")
    anon = TestClient(main.app)  # (pas besoin d'être connecté : l'appli vérifie avant la connexion)
    v = anon.get("/api/app/version").json()["apk"]
    assert v["versionCode"] == 7 and v["sha256"] == hashlib.sha256(b"PK-apk").hexdigest()
    r = anon.get("/api/app/apk")
    assert r.status_code == 200 and r.content == b"PK-apk" and "android.package-archive" in r.headers["content-type"]


def test_terms_accept(fresh_db):
    users.update(1, password="admin-pass")
    users.create("Pote", "pote-pass", "member")
    c = TestClient(main.app)
    c.post("/login", data={"name": "Pote", "password": "pote-pass"})
    assert c.get("/api/me").json()["terms"] is False
    t = c.get("/api/terms").json()
    assert t["version"] == terms.VERSION and "Usage strictement privé" in t["html"] and t["accepted"] is False
    assert c.post("/api/me/terms").json()["ok"]
    users._cache.clear()
    assert c.get("/api/me").json()["terms"] is True
    assert "partager" in terms.text().lower()


def test_no_zip_in_any_release(install, http):
    settings.update({"update_source": "moi/onde"})
    http.routes["https://api.github.com/repos/moi/onde/releases"] = httpx.Response(200, json=[
        {"tag_name": "onde-1", "assets": [{"name": "Onde.exe", "browser_download_url": "https://x.example/Onde.exe"}]}])
    assert "zip" in autoupdate.check()["error"]


@pytest.mark.parametrize("typed", [
    "Mopidie843/Onde", "https://github.com/Mopidie843/Onde", "https://github.com/Mopidie843/Onde/", "github.com/Mopidie843/Onde.git",
    "https://github.com/Mopidie843/Onde/releases/latest", "https://api.github.com/repos/Mopidie843/Onde/releases/latest",
])
def test_github_source_accepts_urls(typed):
    assert autoupdate.normalize_source(typed) == "Mopidie843/Onde"


def test_manifest_url_is_left_alone():
    url = "https://maj.example/onde.json"
    assert autoupdate.normalize_source(url) == url


def test_github_url_source_installs(install, http):
    root, restarts = install
    data = _zip()
    settings.update({"update_source": "https://github.com/moi/onde"})
    http.routes["https://api.github.com/repos/moi/onde/releases"] = httpx.Response(200, json=[
        {"tag_name": "v9.9.9", "assets": [{"name": "onde-tout.zip", "browser_download_url": "https://github.com/moi/onde/releases/download/v9.9.9/onde-tout.zip",
                                          "digest": "sha256:" + hashlib.sha256(data).hexdigest()}]}])
    http.routes["https://github.com/moi/onde/releases/download/v9.9.9/onde-tout.zip"] = httpx.Response(200, content=data)
    assert autoupdate.check()["installed"]["to"] == "9.9.9"
