"""Configuration commune des tests : Onde tourne isolé, dans un dossier temporaire.

Les tests ne touchent jamais ta vraie bibliothèque (base, pochettes, réglages) ni Internet :
  * les variables d'environnement sont posées AVANT l'import d'onde (onde.config les lit à l'import) ;
  * onde.env est ignoré (ONDE_ENV_FILE pointe vers un fichier qui n'existe pas) ;
  * le réseau est remplacé par un faux serveur httpx (fixture `http`).

Lancer :  python -m pytest        (depuis le dossier d'Onde ; pip install -r requirements-dev.txt)
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="onde-tests-"))
os.environ.update({
    "ONDE_ENV_FILE": str(_TMP / "absent.env"),
    "ONDE_DATA_DIR": str(_TMP / "data"),
    "ONDE_MUSIC_DIR": str(_TMP / "music"),
    "ONDE_EXTRA_DIRS": "",
    "ONDE_HTTPS": "0",
    "ONDE_PASSWORD": "",
    "ONDE_AUTO_COVERS": "0",
    "ONDE_STARTER_PACK": "0",
})

import httpx  # noqa: E402
import pytest  # noqa: E402

from onde import config, db, net  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)


def _close_connection() -> None:
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        conn.close()
        db._local.conn = None


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Base SQLite neuve, propre à chaque test."""
    _close_connection()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "onde.db")
    config.ensure_dirs()
    db.init()
    from onde import notify  # (le numéro du dernier événement est gardé en mémoire : on le remet à zéro avec la base)
    monkeypatch.setattr(notify, "_loaded", False)
    monkeypatch.setattr(notify, "_last", 0)
    yield db
    _close_connection()


@pytest.fixture
def http(monkeypatch):
    """Faux Internet. `http.routes["https://hôte/chemin"] = httpx.Response(...)` (ou une fonction
    request -> Response, qui peut aussi lever une erreur httpx). Requête non prévue : 404.
    `http.requests` : les requêtes reçues, dans l'ordre."""
    routes: dict = {}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        url = str(request.url)
        target = routes.get(url, routes.get(url.split("?", 1)[0]))
        if target is None:
            return httpx.Response(404)
        if callable(target):
            return target(request)
        # réponse neuve à chaque fois : un même objet Response ne se relit pas deux fois
        return httpx.Response(target.status_code, headers=target.headers, content=target.content)

    net.set_transport(httpx.MockTransport(handler))
    monkeypatch.setattr(net, "BACKOFF", 0.0)
    yield SimpleNamespace(routes=routes, requests=requests)
    net.set_transport(None)
