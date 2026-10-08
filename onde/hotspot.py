"""Mode « point d'accès Wi-Fi » d'Onde (voiture, vacances, pas de box) : deux QR codes dans le
terminal, l'un pour rejoindre le Wi-Fi d'Onde, l'autre pour ouvrir l'appli.

Utilisé au démarrage d'Onde (onde/__main__.py) et par « ./start.sh hotspot » :

    python -m onde.hotspot            # QR codes du point d'accès actif (ou de celui d'onde.env)
    python -m onde.hotspot --ip 10.42.0.1
"""
from __future__ import annotations

import argparse
import io
import itertools
import re
import shutil
import sys

from . import config, netinfo

_SPECIAL = re.compile(r'([\\;,:"])')


def _escape(value: str) -> str:
    return _SPECIAL.sub(r"\\\1", value)


def wifi_payload(ssid: str, password: str | None, hidden: bool = False) -> str:
    """Texte d'un QR code « rejoindre ce Wi-Fi », reconnu par l'appareil photo des iPhone et des
    Android : WIFI:T:WPA;S:<nom>;P:<mot de passe>;;  (\\ ; , : et " sont échappés)."""
    parts = [f"WIFI:T:{'WPA' if password else 'nopass'}", f"S:{_escape(ssid)}"]
    if password:
        parts.append(f"P:{_escape(password)}")
    if hidden:
        parts.append("H:true")
    return ";".join(parts) + ";;"


def qr_lines(data: str) -> list[str]:
    """QR code compact (2 lignes de modules par ligne de texte), prêt à afficher."""
    import segno
    buf = io.StringIO()
    segno.make(data, error="l").terminal(out=buf, compact=True, border=2)
    return buf.getvalue().rstrip("\n").split("\n")


def _columns(blocks: list[list[str]], indent: int, gap: int) -> list[str]:
    widths = [max(len(line) for line in b) for b in blocks]
    rows = []
    for parts in itertools.zip_longest(*blocks, fillvalue=""):
        rows.append(" " * indent + (" " * gap).join(p.ljust(w) for p, w in zip(parts, widths)).rstrip())
    return rows


def render(ssid: str, password: str | None, url: str, width: int | None = None) -> str:
    """Les deux QR codes (côte à côte si le terminal est assez large) et les instructions.
    password=None : mot de passe inconnu (point d'accès qui n'est pas celui d'Onde) — pas de QR
    code Wi-Fi dans ce cas, il ne pourrait pas fonctionner."""
    if password:
        wifi = [f"1. Rejoins le Wi-Fi « {ssid} »", f"   mot de passe : {password}", ""] + qr_lines(wifi_payload(ssid, password))
    else:
        wifi = [f"1. Rejoins le Wi-Fi « {ssid} »", "   (avec son mot de passe)"]
    app = ["2. Puis ouvre Onde", f"   {url}", ""] + qr_lines(url)
    indent, gap = 5, 4
    width = width or shutil.get_terminal_size((80, 24)).columns
    need = indent + max(len(line) for line in wifi) + gap + max(len(line) for line in app)
    if width >= need:
        body = _columns([wifi, app], indent, gap)
    else:  # terminal étroit (téléphone en SSH…) : l'un sous l'autre
        body = [" " * indent + line for line in wifi + [""] + app]
    tips = [
        "",
        f"     Sans QR code : Wi-Fi « {ssid} »" + (f", mot de passe « {password} »" if password else "") + f", puis {url}",
        "     Si le téléphone affiche « pas d'accès à Internet », choisis « Rester connecté ».",
    ]
    return "\n".join(body + tips)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m onde.hotspot",
                                 description="Affiche les QR codes du point d'accès Wi-Fi d'Onde.")
    ap.add_argument("--ip", help="adresse du Pi sur le point d'accès (par défaut : détectée, sinon 10.42.0.1)")
    ap.add_argument("--port", type=int, help="port web (par défaut : celui d'onde.env)")
    args = ap.parse_args(argv)
    hs = netinfo.detect_hotspot()
    ip = args.ip or (hs.ip if hs else netinfo.HOTSPOT_IPS[0])
    ssid = (hs.ssid if hs and hs.ssid else None) or config.HOTSPOT_SSID
    password = hs.password if hs else config.HOTSPOT_PASSWORD
    scheme = "https" if config.HTTPS else "http"
    try:
        print(render(ssid, password, f"{scheme}://{ip}:{args.port or config.PORT}"))
    except UnicodeEncodeError:  # terminal sans UTF-8 : au moins les informations
        print(f"Wi-Fi : {ssid}  mot de passe : {password}  puis {scheme}://{ip}:{args.port or config.PORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
