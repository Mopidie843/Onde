"""Lancement : python -m onde"""
from __future__ import annotations

import logging
import os
import platform
import socket
import sys

import uvicorn

from . import config, hotspot, netinfo, restart, tls, tray


def _silence_windows_proactor_noise() -> None:
    """Sur Windows, asyncio affiche une trace « ConnectionResetError [WinError 10054] » bruyante
    et sans conséquence à chaque connexion coupée brutalement (page rechargée, onglet fermé…) —
    un défaut connu de la boucle asyncio « Proactor » de Windows, pas un bug d'Onde. On rend cette
    étape de fermeture silencieuse pour cette seule erreur ; tout le reste continue de s'afficher."""
    if platform.system() != "Windows":
        return
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
    except ImportError:
        return
    orig = _ProactorBasePipeTransport._call_connection_lost

    def _quiet_call_connection_lost(self, exc):
        try:
            orig(self, exc)
        except ConnectionResetError:
            pass

    _ProactorBasePipeTransport._call_connection_lost = _quiet_call_connection_lost


def lan_addresses() -> list[str]:
    """Adresses IPv4 du Pi sur le réseau local, la plus utile d'abord (point d'accès Wi-Fi, puis
    réseau principal). Toutes les interfaces sont listées : en point d'accès sans box, il n'y a
    souvent pas de route par défaut, et l'ancienne méthode ne trouvait alors aucune adresse."""
    return netinfo.lan_ips()


def _missing_requirements() -> dict[str, str]:
    """Paquets de requirements.txt absents (ex. Onde mis à jour en copiant les fichiers, puis
    redémarré par le service sans passer par ./start.sh, qui installe les nouveautés)."""
    import re
    from importlib import metadata
    req = config.ROOT / "requirements.txt"
    try:
        lines = req.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    missing = {}
    for line in lines:
        spec = line.split("#", 1)[0].strip()
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
        if not m:
            continue
        try:
            metadata.distribution(m.group(1))
        except metadata.PackageNotFoundError:
            missing[m.group(1)] = spec
    return missing


# sans eux, Onde ne peut pas démarrer ; les autres paquets sont facultatifs (HTTPS, QR codes,
# téléchargement par nom… se désactivent proprement et s'installent plus tard)
CORE = {"fastapi", "uvicorn", "mutagen", "pillow", "httpx"}  # (pystray : Windows seulement, facultatif)


def _ensure_requirements() -> None:
    if config.FROZEN:  # Onde.exe contient déjà tout
        return
    missing = {k: v for k, v in _missing_requirements().items() if k.lower() in CORE}
    if not missing:
        return
    import subprocess
    print(f"\n  ♪  Installation des modules manquants : {', '.join(missing)} (Internet nécessaire)…", flush=True)
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", *missing.values()]
    try:
        subprocess.run(cmd, timeout=900)
    except (OSError, subprocess.SubprocessError):
        pass
    import importlib
    importlib.invalidate_caches()
    still = [m for m in _missing_requirements() if m.lower() in CORE]
    if not still:
        return
    print(f"\n  ✖ Il manque à Onde : {', '.join(still)}.\n"
          "    Connecte le Pi à Internet, puis lance :  ./start.sh   (Windows : start.bat)\n", flush=True)
    sys.exit(1)


class _Server(uvicorn.Server):
    """uvicorn, qui ferme d'abord les flux en direct (SSE) pour que l'arrêt soit immédiat."""

    def handle_exit(self, sig, frame) -> None:
        restart.run_exit_hooks()
        super().handle_exit(sig, frame)


class _HideInternalAddress(logging.Filter):
    """Derrière la porte d'entrée, uvicorn écoute sur un port interne : inutile de l'afficher."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("Uvicorn running on")


def _start_gateway():
    """Démarre la porte d'entrée HTTP/HTTPS si besoin. Renvoie (gateway, socket interne) ou (None, None)."""
    want_https = config.HTTPS
    if not want_https and not tls.has_cert():
        return None, None  # HTTP simple, comme toujours : uvicorn écoute directement le port public
    try:
        ctx = tls.ssl_context()
    except Exception as exc:  # certificat illisible, etc. : on ne bloque jamais le démarrage
        logging.getLogger("onde.tls").warning("HTTPS indisponible : %s", exc)
        ctx = None
    if ctx is None:
        if want_https:
            print("\n  ⚠ Le HTTPS est demandé, mais le module « cryptography » manque. Ouvre Réglages >"
                  "\n    Accès sécurisé et réactive le HTTPS : Onde l'installera tout seul."
                  "\n    En attendant, Onde démarre en HTTP.\n")
        config.HTTPS = False
        return None, None

    from .gateway import Gateway

    internal = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    internal.bind(("127.0.0.1", 0))
    internal.listen(128)
    gw = Gateway(config.HOST, config.PORT, internal.getsockname()[1],
                 "https" if want_https else "http", ctx, tls.CA_CERT)
    try:
        gw.start()
    except OSError as exc:
        internal.close()
        msg = (f"Impossible d'ouvrir le port {config.PORT} ({exc.strerror or exc}).\n"
               "Onde tourne peut-être déjà ? Sinon, change ONDE_PORT dans onde.env.")
        if tray.windowed():
            tray.alert(msg, error=True)
        print(f"\n  ✖ {msg}\n")
        sys.exit(1)
    return gw, internal


def _open_browser_when_ready() -> None:
    """Onde.exe : ouvre Onde dans le navigateur dès que le serveur répond (une fois, pas après un
    redémarrage). ONDE_NO_BROWSER=1 pour s'en passer."""
    if (os.environ.get("ONDE_NO_BROWSER") or os.environ.get("ONDE_BROWSER_OPENED")
            or tray.BACKGROUND_FLAG in sys.argv):
        return
    os.environ["ONDE_BROWSER_OPENED"] = "1"  # hérité par le redémarrage : on ne rouvre pas d'onglet

    def wait_and_open() -> None:
        import time
        import webbrowser
        for _ in range(120):
            try:
                with socket.create_connection(("127.0.0.1", config.PORT), timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            return
        webbrowser.open(f"http://localhost:{config.PORT}")  # localhost : marche aussi en HTTPS

    import threading
    threading.Thread(target=wait_and_open, name="onde-browser", daemon=True).start()


def _already_running() -> bool:
    """Onde.exe : un second double-clic ne lance pas un deuxième Onde, il rouvre la page."""
    try:
        with socket.create_connection(("127.0.0.1", config.PORT), timeout=1.5):
            pass
    except OSError:
        return False
    import urllib.error
    import urllib.request
    scheme = "https" if config.HTTPS else "http"
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        urllib.request.urlopen(f"{scheme}://127.0.0.1:{config.PORT}/api/info", timeout=3, context=ctx)
        ours = True
    except urllib.error.HTTPError:
        ours = True  # 401 : mot de passe défini, c'est bien Onde qui répond
    except Exception:
        ours = False
    if not ours:
        tray.alert(f"Le port {config.PORT} est déjà utilisé par un autre programme.\n\n"
                   "Ferme-le, ou choisis un autre port : ONDE_PORT=8001 dans le fichier onde.env, "
                   "à côté de Onde.exe.", error=True)
        return True
    if tray.BACKGROUND_FLAG not in sys.argv:
        import webbrowser
        webbrowser.open(f"http://localhost:{config.PORT}")
    return True


def main() -> None:
    _silence_windows_proactor_noise()
    _ensure_requirements()
    config.ensure_dirs()
    host = socket.gethostname()
    windowed = tray.windowed()
    if windowed and _already_running():
        return

    gw, internal = _start_gateway()
    scheme = "https" if config.HTTPS else "http"

    try:
        hs = netinfo.detect_hotspot()  # le Pi diffuse-t-il son propre Wi-Fi (voiture, sans box) ?
    except Exception:
        hs = None
    ips = lan_addresses()
    if hs and hs.ip in ips:
        ips.remove(hs.ip)
    if hs:
        ips.insert(0, hs.ip)
    urls = [f"{scheme}://{ip}:{config.PORT}" for ip in ips]
    urls.append(f"{scheme}://{host}.local:{config.PORT}")
    if hs:
        print(f"\n  ♪  Onde est prêt !  Mode point d'accès : le Pi diffuse le Wi-Fi « {hs.ssid} ».")
    else:
        print("\n  ♪  Onde est prêt !  Ouvre sur ton téléphone / PC (même Wi-Fi) :")
    for u in urls:
        print(f"       {u}")
    print(f"     Musique : {config.MUSIC_DIR}")
    if scheme == "https":
        print("     HTTPS actif. Pour que le navigateur ne t'avertisse plus, installe l'autorité « Onde »\n"
              "     une fois par appareil : Réglages > Accès sécurisé, dans Onde (c'est guidé).")
    try:  # QR codes à scanner avec l'appareil photo du téléphone
        if windowed:
            raise RuntimeError  # pas de console : l'adresse et le QR code sont dans les Réglages
        print()
        if hs:  # 1) rejoindre le Wi-Fi du Pi  2) ouvrir Onde
            print(hotspot.render(hs.ssid or config.HOTSPOT_SSID, hs.password, urls[0]))
        else:
            import segno
            print("     Ou scanne ce code avec ton téléphone :")
            segno.make(urls[0], error="l").terminal(compact=True, border=2)
    except Exception:
        pass
    if gw is not None or config.FROZEN:
        print("     (Ctrl+C ou fermer cette fenêtre pour arrêter)")
    print()
    if config.FROZEN:
        _open_browser_when_ready()
    icon = None
    if windowed:
        icon = tray.Tray(config.PORT, scheme, restart.shutdown)
        icon.start()

    try:
        import uvloop  # noqa: F401
        loop = "uvloop"
    except ImportError:
        loop = "asyncio"
    try:
        import httptools  # noqa: F401
        http = "httptools"
    except ImportError:
        http = "h11"
    from .main import app  # l'objet lui-même (et non « onde.main:app ») : visible pour PyInstaller

    uv_config = uvicorn.Config(
        app,
        host="127.0.0.1" if gw else config.HOST,
        port=internal.getsockname()[1] if gw else config.PORT,
        loop=loop, http=http, workers=1, access_log=False, log_level="info",
        timeout_keep_alive=30, timeout_graceful_shutdown=5, proxy_headers=False, server_header=False,
    )
    if gw is not None:
        logging.getLogger("uvicorn.error").addFilter(_HideInternalAddress())
    server = _Server(uv_config)
    restart.attach(server)
    try:
        server.run(sockets=[internal] if internal else None)
    except KeyboardInterrupt:
        pass
    finally:
        if icon is not None:
            icon.stop()
        if gw is not None:
            gw.stop()
    if restart.requested:
        restart.relaunch()


if __name__ == "__main__":
    main()
