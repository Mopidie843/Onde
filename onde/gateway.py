"""Porte d'entrée d'Onde quand le HTTPS est (ou a été) activé : un seul port, deux langues.

Chaque connexion est reconnue à son premier octet (une poignée de main TLS commence toujours par
0x16), sans le consommer, puis traitée ainsi :

  mode « https » (HTTPS activé)
    * https://…             → déchiffré ici, puis relayé à Onde ;
    * http://… depuis la machine elle-même (localhost : kiosque du Pi, start.bat) → relayé tel quel,
      sans avertissement (la connexion ne sort pas de la machine) ;
    * http://…/onde.crt depuis le réseau → l'autorité locale est servie directement, sans
      avertissement, pour pouvoir l'installer avant tout (ex. QR code scanné par l'iPhone) ;
    * tout autre http://… depuis le réseau → redirigé vers https:// (anciens favoris, icône
      d'écran d'accueil…).
  mode « http » (HTTPS désactivé, mais un certificat existe)
    * http://…  → relayé à Onde ;
    * https://… → redirigé vers http:// (les anciens favoris en https continuent de marcher).

Les redirections sont temporaires (307, jamais mises en cache, pas de HSTS) : activer puis
désactiver le HTTPS ne « coince » jamais un navigateur sur l'un ou l'autre.
Onde lui-même (uvicorn) écoute en HTTP simple sur 127.0.0.1, sur un port interne.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import selectors
import socket
import ssl
import threading
import time
from pathlib import Path

log = logging.getLogger("onde.gateway")

CERT_PATHS = ("/onde.crt", "/onde-cert.pem")
_HOST_OK = re.compile(r"^[A-Za-z0-9._\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$")
PEEK_TIMEOUT = 30.0   # une connexion ouverte qui n'envoie rien (pré-connexion du navigateur)
HEAD_TIMEOUT = 15.0
CHUNK = 64 * 1024

# Connexions relayées en cours : port local (côté porte) de la connexion vers Onde -> (adresse
# d'origine du visiteur, True s'il est arrivé en HTTPS). Onde ne voit que 127.0.0.1 derrière la
# porte : il retrouve ainsi le vrai visiteur (blocage après 8 essais ratés, cookie « Secure »)
# sans croire un en-tête que n'importe qui peut inventer.
PEERS: dict[int, tuple[str, bool]] = {}


def origin(port: int | None) -> tuple[str, bool] | None:
    """(adresse d'origine, HTTPS ?) de la connexion relayée qui arrive à Onde depuis ce port local."""
    return PEERS.get(port) if port else None


def _is_loopback(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_loopback


def is_loopback(addr: str) -> bool:
    """La machine elle-même (127.0.0.1, ::1, « localhost ») ?"""
    return addr == "localhost" or _is_loopback(addr)


class Gateway:
    def __init__(self, host: str, port: int, upstream_port: int, mode: str,
                 ssl_ctx: ssl.SSLContext, ca_cert: Path):
        assert mode in ("https", "http")
        self.host, self.port, self.upstream_port, self.mode = host, port, upstream_port, mode
        self.ssl_ctx, self.ca_cert = ssl_ctx, ca_cert
        self._stop = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._loop.set_exception_handler(self._quiet)
        self._lsock: socket.socket | None = None
        # asyncio ne garde que des références *faibles* vers les tâches (et un flux ne référence
        # son lecteur que faiblement) : sans cet ensemble, une connexion en attente de données
        # pourrait être ramassée par le ramasse-miettes en pleine requête et coupée sans prévenir.
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ cycle de vie
    def start(self) -> None:
        """Ouvre le port public (lève OSError s'il est déjà pris) et démarre la porte d'entrée."""
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        if os.name != "nt":  # relance immédiate possible (sous Windows, SO_REUSEADDR est dangereux)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((self.host, self.port))
            s.listen(128)
        except OSError:
            s.close()
            raise
        s.setblocking(False)
        self._lsock = s
        threading.Thread(target=self._loop.run_forever, daemon=True, name="onde-gateway-loop").start()
        threading.Thread(target=self._accept_loop, daemon=True, name="onde-gateway-accept").start()

    def stop(self) -> None:
        self._stop.set()
        if self._lsock is not None:
            try:
                self._lsock.close()
            except OSError:
                pass

        def _halt() -> None:
            for task in asyncio.all_tasks(self._loop):
                task.cancel()
            self._loop.call_later(0.2, self._loop.stop)

        try:
            self._loop.call_soon_threadsafe(_halt)
        except RuntimeError:
            pass

    @staticmethod
    def _quiet(loop, context) -> None:
        exc = context.get("exception")
        if isinstance(exc, (OSError, ssl.SSLError, asyncio.CancelledError, EOFError)):
            return  # connexions coupées, certificat refusé par le navigateur… : rien d'anormal
        loop.default_exception_handler(context)

    # ------------------------------------------------------------------ accueil des connexions
    def _accept_loop(self) -> None:
        sel = selectors.DefaultSelector()
        sel.register(self._lsock, selectors.EVENT_READ)
        pending: dict[socket.socket, tuple[tuple, float]] = {}
        try:
            while not self._stop.is_set():
                try:
                    events = sel.select(timeout=0.5)
                except (OSError, ValueError):
                    break
                now = time.monotonic()
                for key, _ in events:
                    if key.fileobj is self._lsock:
                        self._accept_all(sel, pending, now)
                        continue
                    conn = key.fileobj
                    addr, _deadline = pending.pop(conn)
                    sel.unregister(conn)
                    try:
                        first = conn.recv(1, socket.MSG_PEEK)
                    except (BlockingIOError, InterruptedError):
                        pending[conn] = (addr, now + PEEK_TIMEOUT)
                        sel.register(conn, selectors.EVENT_READ)
                        continue
                    except OSError:
                        conn.close()
                        continue
                    if not first:
                        conn.close()
                        continue
                    self._loop.call_soon_threadsafe(self._spawn, conn, addr, first)
                for conn, (_addr, deadline) in list(pending.items()):
                    if deadline < now:
                        pending.pop(conn)
                        sel.unregister(conn)
                        conn.close()
        finally:
            for conn in pending:
                conn.close()
            sel.close()

    def _accept_all(self, sel, pending, now) -> None:
        while True:
            try:
                conn, addr = self._lsock.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            conn.setblocking(False)
            pending[conn] = (addr, now + PEEK_TIMEOUT)
            sel.register(conn, selectors.EVENT_READ)

    # ------------------------------------------------------------------ traitement
    def _spawn(self, sock: socket.socket, addr: tuple, first: bytes) -> None:
        task = self._loop.create_task(self._handle(sock, addr, first))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _open(self, sock: socket.socket, tls: bool):
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=CHUNK, loop=loop)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        kwargs = {"ssl": self.ssl_ctx, "ssl_handshake_timeout": HEAD_TIMEOUT} if tls else {}
        transport, _ = await loop.connect_accepted_socket(lambda: protocol, sock=sock, **kwargs)
        return reader, asyncio.StreamWriter(transport, protocol, reader, loop)

    async def _handle(self, sock: socket.socket, addr: tuple, first: bytes) -> None:
        tls = first == b"\x16"
        writer = None
        try:
            try:
                reader, writer = await self._open(sock, tls)
            except BaseException:
                sock.close()
                raise
            if self.mode == "https":
                if tls or _is_loopback(addr[0]):
                    await self._relay(reader, writer, addr[0], tls)
                else:
                    await self._plain_from_lan(reader, writer)
            else:
                if tls:
                    await self._redirect(reader, writer, "http")
                else:
                    await self._relay(reader, writer, addr[0], tls)
        except (OSError, ssl.SSLError, EOFError, asyncio.TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError, ValueError, asyncio.CancelledError):
            pass
        finally:
            if writer is not None:
                writer.close()

    async def _relay(self, reader, writer, peer: str = "", tls: bool = False) -> None:
        """Relaie les octets dans les deux sens entre le navigateur et Onde (flux, SSE, Range…).
        `peer`, `tls` : adresse d'origine du visiteur et HTTPS, notés dans PEERS pour Onde."""
        try:
            up_r, up_w = await asyncio.open_connection("127.0.0.1", self.upstream_port, limit=CHUNK)
        except OSError:
            # Onde est en train de redémarrer : on coupe simplement la connexion, comme si le port
            # était fermé. Surtout pas de « 503 » : un EventSource (mises à jour en direct) abandonne
            # définitivement sur un statut d'erreur, alors qu'il se reconnecte seul après une coupure.
            return
        # noté avant de relayer le moindre octet : Onde ne peut pas lire la requête avant
        sockname = up_w.get_extra_info("sockname")
        port = sockname[1] if sockname else None
        entry = (peer, tls)
        if port:
            PEERS[port] = entry
        try:
            await self._pipe(reader, writer, up_r, up_w)
        finally:
            if port and PEERS.get(port) is entry:
                PEERS.pop(port, None)

    @staticmethod
    async def _pipe(reader, writer, up_r, up_w) -> None:
        """Copie les octets dans les deux sens jusqu'à ce que l'un des côtés ferme."""
        async def to_upstream() -> None:
            try:
                while data := await reader.read(CHUNK):
                    up_w.write(data)
                    await up_w.drain()
                if up_w.can_write_eof():
                    up_w.write_eof()
            except (OSError, ssl.SSLError):
                up_w.close()

        async def to_client() -> None:
            try:
                while data := await up_r.read(CHUNK):
                    writer.write(data)
                    await writer.drain()
            except (OSError, ssl.SSLError):
                pass

        up_task = asyncio.create_task(to_upstream())
        try:
            await to_client()
        finally:
            up_task.cancel()
            up_w.close()

    async def _read_head(self, reader) -> tuple[str, str, dict[str, str]]:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        lines = raw.decode("latin-1").split("\r\n")
        method, target, _version = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        return method.upper(), target, headers

    def _host(self, headers: dict[str, str], writer) -> str:
        host = headers.get("host", "")
        if _HOST_OK.match(host):
            return host
        ip, port = writer.get_extra_info("sockname")[:2]
        return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"

    async def _redirect(self, reader, writer, scheme: str, head=None) -> None:
        method, target, headers = head or await self._read_head(reader)
        if not target.startswith("/"):
            target = "/"
        location = f"{scheme}://{self._host(headers, writer)}{target}"
        await self._respond(writer, "307 Temporary Redirect",
                            [("Location", location), ("Cache-Control", "no-store"),
                             ("Content-Type", "text/plain; charset=utf-8")],
                            f"Onde est ici : {location}".encode(), head_only=method == "HEAD")

    async def _plain_from_lan(self, reader, writer) -> None:
        head = await self._read_head(reader)
        method, target, _headers = head
        if method in ("GET", "HEAD") and target.split("?", 1)[0] in CERT_PATHS:
            try:
                body = self.ca_cert.read_bytes()
            except OSError:
                body = b""
            if body:
                await self._respond(writer, "200 OK", [
                    ("Content-Type", "application/x-x509-ca-cert"),
                    ("Content-Disposition", 'attachment; filename="onde.crt"'),
                    ("Cache-Control", "no-store")], body, head_only=method == "HEAD")
                return
        await self._redirect(reader, writer, "https", head)

    @staticmethod
    async def _respond(writer, status: str, headers: list[tuple[str, str]], body: bytes,
                       head_only: bool = False) -> None:
        lines = [f"HTTP/1.1 {status}", f"Content-Length: {len(body)}", "Connection: close", *(f"{k}: {v}" for k, v in headers)]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (b"" if head_only else body))
        await writer.drain()
