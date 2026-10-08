"""Requêtes HTTP sortantes d'Onde : pochettes, métadonnées, paroles, playlists en ligne, liens directs.

Un seul client httpx, partagé par tout le serveur :
  * connexions réutilisées (keep-alive) : sur un Raspberry Pi, une poignée de main TLS par requête
    coûte cher, et la recherche automatique de pochettes en enchaîne des centaines ;
  * sûr entre threads (le pool de connexions de httpcore est protégé par des verrous) ;
  * délais stricts : 5 s pour se connecter, 10 s au plus sans recevoir de données, et une durée
    totale maximale par requête (10 s par défaut) vérifiée pendant toute la réception ;
  * taille des réponses plafonnée : une image géante ou un serveur farfelu ne remplit pas la mémoire ;
  * un nouvel essai sur les erreurs passagères (coupure, délai dépassé, 429, 502, 503, 504) ;
  * les proxys de l'environnement (HTTPS_PROXY…) sont respectés, comme avant avec urllib.

Ces fonctions sont bloquantes : elles sont faites pour les threads de travail (routes FastAPI
synchrones, tâches de fond). Elles refusent de tourner sur la boucle asyncio, ce qui figerait tout
le serveur (lecture en cours, autres appareils) pendant l'attente : depuis du code async, utiliser
aget_bytes() / aget_json(), qui passent par un thread.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx

from . import config

log = logging.getLogger("onde.net")
# httpx journalise chaque requête au niveau INFO : la recherche de pochettes en fond remplirait le
# journal (sur la carte SD du Pi) de centaines de lignes. On ne garde que les avertissements.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

UA = f"Onde/{config.VERSION} (serveur musical personnel)"
CONNECT_TIMEOUT = 5.0      # établir la connexion (DNS + TCP + TLS)
READ_TIMEOUT = 10.0        # au plus 10 s sans recevoir de données
DEFAULT_DEADLINE = 10.0    # durée totale d'une requête, réponse complète comprise
DEFAULT_LIMIT = 3_000_000  # octets
RETRY_STATUS = frozenset({429, 502, 503, 504})
BACKOFF = 0.6              # s avant le nouvel essai (« Retry-After » respecté, jusqu'à MAX_RETRY_WAIT)
MAX_RETRY_WAIT = 3.0


class NetError(OSError):
    """Échec d'une requête, avec un message lisible. Hérite d'OSError comme les erreurs d'urllib :
    le code qui attrapait OSError continue de fonctionner tel quel."""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status            # code HTTP (None : erreur réseau, délai dépassé…)
        self.retry_after = retry_after  # « Retry-After » envoyé par le serveur, en secondes


class TooLarge(NetError):
    """Réponse plus grosse que la limite demandée."""


_client: httpx.Client | None = None
_transport: httpx.BaseTransport | None = None  # tests : httpx.MockTransport (voir set_transport)
_client_lock = threading.Lock()


def client() -> httpx.Client:
    """Le client partagé, créé à la première utilisation."""
    global _client
    c = _client
    if c is not None and not c.is_closed:
        return c
    with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(
                headers={"User-Agent": UA, "Accept": "*/*"},
                timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
                follow_redirects=True,
                max_redirects=5,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=8, keepalive_expiry=30.0),
                # transport=None garde les proxys de l'environnement ; un transport explicite les ignore
                transport=_transport,
            )
        return _client


def set_transport(transport: httpx.BaseTransport | None) -> None:
    """Remplace la couche réseau (tests : httpx.MockTransport). None : retour au vrai réseau."""
    global _transport
    with _client_lock:
        _transport = transport
        _close_locked()


def close() -> None:
    """Ferme les connexions (arrêt d'Onde)."""
    with _client_lock:
        _close_locked()


def _close_locked() -> None:
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:  # fermeture au mieux, jamais bloquante pour l'arrêt
            pass
        _client = None


def _ensure_off_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # pas de boucle asyncio dans ce thread : on peut bloquer
    raise RuntimeError(
        "Appel réseau bloquant depuis la boucle asyncio : il figerait tout le serveur pendant l'attente. "
        "Utiliser net.aget_bytes()/net.aget_json(), ou run_in_threadpool()."
    )


def _check_url(url: str) -> None:
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        raise ValueError("URL invalide")


def _describe(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "délai dépassé (le serveur ne répond pas)"
    if isinstance(exc, httpx.ConnectError):
        return f"connexion impossible ({str(exc)[:120] or 'serveur injoignable'})"
    if isinstance(exc, httpx.TooManyRedirects):
        return "trop de redirections"
    if isinstance(exc, httpx.ProxyError):
        return f"proxy : {str(exc)[:120]}"
    return str(exc)[:160] or exc.__class__.__name__


def _size(n: int) -> str:
    return f"{n / 1_000_000:.1f} Mo".replace(".", ",") if n >= 1_000_000 else f"{max(1, n // 1000)} Ko"


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after", "").strip()
    return float(value) if value.isdigit() else None


def _attempt(url: str, limit: int, deadline: float, headers: dict | None) -> tuple[bytes, httpx.Headers]:
    start = time.monotonic()
    timeout = httpx.Timeout(min(READ_TIMEOUT, deadline), connect=min(CONNECT_TIMEOUT, deadline))
    with client().stream("GET", url, headers=headers, timeout=timeout) as resp:
        if resp.status_code >= 400:
            raise NetError(f"HTTP {resp.status_code}", status=resp.status_code, retry_after=_retry_after(resp))
        length = resp.headers.get("content-length", "")
        if length.isdigit() and int(length) > limit:
            raise TooLarge(f"réponse trop volumineuse ({_size(int(length))}, maximum {_size(limit)})")
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf += chunk
            if len(buf) > limit:
                raise TooLarge(f"réponse trop volumineuse (maximum {_size(limit)})")
            if time.monotonic() - start > deadline:
                raise NetError(f"délai dépassé (réponse incomplète au bout de {deadline:g} s)")
        return bytes(buf), resp.headers


def _fetch(url: str, *, limit: int, deadline: float, headers: dict | None, retries: int) -> tuple[bytes, httpx.Headers]:
    _check_url(url)
    _ensure_off_loop()
    attempt = 0
    while True:
        try:
            return _attempt(url, limit, deadline, headers)
        except TooLarge:
            raise
        except NetError as exc:
            if exc.status not in RETRY_STATUS or attempt >= retries:
                raise
            wait = exc.retry_after if exc.retry_after is not None else BACKOFF * (attempt + 1)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            # erreurs passagères (coupure, connexion gardée ouverte fermée par le serveur…)
            if attempt >= retries:
                raise NetError(_describe(exc)) from exc
            wait = BACKOFF * (attempt + 1)
        except httpx.HTTPError as exc:  # trop de redirections, protocole inconnu, proxy…
            raise NetError(_describe(exc)) from exc
        attempt += 1
        log.debug("nouvel essai (%d) dans %.1f s : %s", attempt, min(wait, MAX_RETRY_WAIT), url)
        time.sleep(min(wait, MAX_RETRY_WAIT))


def get_bytes(url: str, *, limit: int = DEFAULT_LIMIT, deadline: float = DEFAULT_DEADLINE,
              headers: dict | None = None, retries: int = 1) -> bytes:
    """Contenu complet d'une URL (lève NetError, TooLarge ou ValueError)."""
    return _fetch(url, limit=limit, deadline=deadline, headers=headers, retries=retries)[0]


_charset = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.I)


def get_text(url: str, *, limit: int = DEFAULT_LIMIT, deadline: float = DEFAULT_DEADLINE,
             headers: dict | None = None, retries: int = 1) -> str:
    """Page texte/HTML décodée (encodage annoncé par le serveur, UTF-8 sinon)."""
    data, hdrs = _fetch(url, limit=limit, deadline=deadline, headers=headers, retries=retries)
    m = _charset.search(hdrs.get("content-type", ""))
    try:
        return data.decode(m.group(1) if m else "utf-8", "replace")
    except LookupError:  # encodage inconnu
        return data.decode("utf-8", "replace")


_MISSING = object()


def get_json(url: str, *, limit: int = DEFAULT_LIMIT, deadline: float = DEFAULT_DEADLINE,
             headers: dict | None = None, retries: int = 1, not_found: Any = _MISSING) -> Any:
    """JSON d'une URL. `not_found` : valeur renvoyée sur une réponse 404 (sinon NetError)."""
    try:
        data, _ = _fetch(url, limit=limit, deadline=deadline, retries=retries,
                         headers={"Accept": "application/json", **(headers or {})})
    except NetError as exc:
        if exc.status == 404 and not_found is not _MISSING:
            return not_found
        raise
    try:
        return json.loads(data)
    except ValueError as exc:
        raise NetError("réponse illisible (JSON invalide)") from exc


@contextlib.contextmanager
def stream(url: str, *, read_timeout: float = 20.0, headers: dict | None = None) -> Iterator[httpx.Response]:
    """Réponse en flux, pour les gros fichiers : pas de durée totale, mais au plus `read_timeout` s
    sans recevoir de données. Lève NetError si la connexion échoue ou si le serveur répond une erreur.

        with net.stream(url) as resp:
            for chunk in resp.iter_bytes(1 << 20): ...
    """
    _check_url(url)
    _ensure_off_loop()
    timeout = httpx.Timeout(read_timeout, connect=CONNECT_TIMEOUT)
    try:
        with client().stream("GET", url, headers=headers, timeout=timeout) as resp:
            if resp.status_code >= 400:
                raise NetError(f"HTTP {resp.status_code}", status=resp.status_code)
            yield resp
    except httpx.HTTPError as exc:  # y compris pendant la lecture par l'appelant (délai, coupure)
        raise NetError(_describe(exc)) from exc


async def aget_bytes(url: str, **kwargs) -> bytes:
    """get_bytes() pour du code async : la requête tourne dans un thread, la boucle reste libre."""
    return await asyncio.to_thread(get_bytes, url, **kwargs)


async def aget_json(url: str, **kwargs) -> Any:
    """get_json() pour du code async (dans un thread)."""
    return await asyncio.to_thread(get_json, url, **kwargs)
