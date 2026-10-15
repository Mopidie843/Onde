"""Réglages : seules les vraies modifications comptent, booléens lus correctement, adresse publique."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from onde import main, settings


@pytest.fixture
def s(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "_cache", None)
    yield settings
    settings._cache = None


def test_only_real_changes_are_reported(s):
    assert s.get("autoplay") is True
    assert s.update({"autoplay": True, "unwanted_words": "slowed, reverb"}) == {}
    assert s.update({"autoplay": False}) == {"autoplay": False}
    assert s.update({"autoplay": False, "inconnu": 1}) == {}


def test_booleans_from_text(s):
    assert s.update({"autoplay": "false"}) == {"autoplay": False}  # (bool("false") vaudrait True)
    assert s.update({"autoplay": "1"}) == {"autoplay": True}
    assert s.update({"normalize": 0}) == {"normalize": False}
    for bad in ("peut-être", [], {"a": 1}, 2):
        with pytest.raises(ValueError):
            s.update({"autoplay": bad})
    # une valeur invalide : rien n'est enregistré, même ce qui était valable
    with pytest.raises(ValueError):
        s.update({"normalize": True, "autoplay": "oui-non"})
    assert s.get("normalize") is False


def test_public_url_is_checked(s):
    assert s.update({"public_url": " https://Onde.exemple.fr/ "}) == {"public_url": "https://Onde.exemple.fr"}
    assert s.public_url() == "https://Onde.exemple.fr"
    for bad in ("onde.exemple.fr", "ftp://x", "https://a b", "https://x/?q=1", "https://:99999"):
        with pytest.raises(ValueError):
            s.update({"public_url": bad})
    assert s.update({"public_url": ""}) == {"public_url": ""} and s.public_url() == ""


def test_api_refuses_invalid_values(s):
    c = TestClient(main.app)
    r = c.put("/api/settings", json={"autoplay": "plop"})
    assert r.status_code == 400 and "autoplay" in r.json()["detail"]
    assert c.put("/api/settings", json={"autoplay": "false"}).json()["autoplay"] is False
    assert c.put("/api/settings", json={"public_url": {"x": 1}}).status_code == 400
