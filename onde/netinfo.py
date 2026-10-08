"""Adresses réseau du Pi, et détection du mode « point d'accès Wi-Fi » (hotspot).

Pourquoi ne pas se contenter de l'adresse « de la route par défaut » (connexion UDP fictive vers
10.255.255.255, comme avant) ? En point d'accès sans box, il n'y a en général AUCUNE route par
défaut : cette méthode ne trouvait rien, et Onde n'affichait que http://raspberrypi.local:8000, que
beaucoup de téléphones Android ne savent pas ouvrir. On énumère donc toutes les interfaces.

Point d'accès reconnu, du plus sûr au plus approximatif :
  1. NetworkManager (nmcli) : connexion Wi-Fi active en mode « ap » (SSID exact) ;
  2. hostapd : /etc/hostapd/hostapd.conf lisible (SSID et mot de passe) ;
  3. adresse typique d'un point d'accès sur une interface Wi-Fi : 10.42.0.1 (partage de
     NetworkManager) ou 192.168.4.1 (hostapd/dnsmasq, RaspAP…).
"""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
from dataclasses import dataclass

from . import config

HOTSPOT_IPS = ("10.42.0.1", "192.168.4.1")
HOTSPOT_CON = "Onde-Hotspot"  # profil NetworkManager créé par « ./start.sh hotspot »
# interfaces virtuelles sans intérêt pour un téléphone (conteneurs, ponts de machines virtuelles…)
_SKIP_IF = re.compile(r"^(lo|docker\d+|br-[0-9a-f]+|veth\w*|virbr\d+|lxcbr\d+|lxdbr\d+|cni\d+|flannel\.\d+"
                      r"|vmnet\d+|vboxnet\d+|ifb\d+)$", re.I)


@dataclass(frozen=True)
class Hotspot:
    ip: str
    ifname: str = ""
    ssid: str | None = None
    password: str | None = None
    source: str = "ip"  # "networkmanager", "hostapd" ou "ip"


# ------------------------------------------------------------------ adresses
def _linux_interfaces() -> list[tuple[str, str]]:
    """(interface, IPv4) de chaque interface, via ioctl (ne dépend d'aucune commande externe)."""
    import fcntl
    out = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for _, name in socket.if_nameindex():
            try:
                res = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))  # SIOCGIFADDR
            except OSError:
                continue  # pas d'adresse IPv4 (interface éteinte, ou IPv6 seulement)
            out.append((name, socket.inet_ntoa(res[20:24])))
    return out


def default_route_ip() -> str | None:
    """Adresse utilisée pour sortir vers Internet (None sans route par défaut, ex. point d'accès seul)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # aucun paquet n'est envoyé
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def interfaces() -> list[tuple[str, str]]:
    """[(interface, IPv4)] joignables sur le réseau local (sans boucle locale, Docker, VM…)."""
    pairs: list[tuple[str, str]] = []
    if sys.platform.startswith("linux"):
        try:
            pairs = _linux_interfaces()
        except Exception:
            pairs = []
    if not pairs:  # Windows, macOS, ou ioctl indisponible
        ips: set[str] = set()
        route = default_route_ip()
        if route:
            ips.add(route)
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(info[4][0])
        except OSError:
            pass
        pairs = [("", ip) for ip in sorted(ips)]
    out: list[tuple[str, str]] = []
    for name, ip in pairs:
        try:
            addr = ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local or addr.is_unspecified or (name and _SKIP_IF.match(name)):
            continue
        if (name, ip) not in out:
            out.append((name, ip))
    return out


def is_wireless(ifname: str) -> bool:
    return bool(ifname) and os.path.isdir(f"/sys/class/net/{ifname}/wireless")


def lan_ips(addrs: list[tuple[str, str]] | None = None) -> list[str]:
    """Adresses IPv4 où joindre Onde, la plus utile d'abord : point d'accès Wi-Fi, puis réseau
    principal (route par défaut), puis les autres. Rapide (aucune commande externe) : utilisable
    à chaque requête."""
    addrs = interfaces() if addrs is None else addrs
    route = default_route_ip()

    def rank(pair: tuple[str, str]) -> int:
        name, ip = pair
        if ip in HOTSPOT_IPS and (not name or is_wireless(name)):
            return 0
        return 1 if ip == route else 2

    ordered = sorted(addrs, key=rank)  # tri stable : l'ordre des interfaces est gardé à rang égal
    return list(dict.fromkeys(ip for _, ip in ordered))


# ------------------------------------------------------------------ point d'accès
def _run(cmd: list[str], timeout: float = 2.0) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _nm_fields(line: str) -> list[str]:
    """Découpe une ligne de « nmcli -t » (« : » sépare les champs ; « \\: » et « \\\\ » échappés)."""
    parts, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _nm_get(field: str, con: str, secrets: bool = False) -> str:
    cmd = ["nmcli"] + (["-s"] if secrets else []) + ["-t", "-g", field, "connection", "show", con]
    out = _run(cmd)
    return _nm_fields(out.strip())[0] if out else ""


def _nm_hotspot() -> tuple[str, str, str | None] | None:
    """(interface, SSID, mot de passe ou None) du point d'accès actif selon NetworkManager."""
    out = _run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"])
    if not out:
        return None
    for line in out.splitlines():
        fields = _nm_fields(line)
        if len(fields) < 3 or fields[1] not in ("802-11-wireless", "wifi"):
            continue
        name, dev = fields[0], fields[2]
        if _nm_get("802-11-wireless.mode", name) != "ap":
            continue
        ssid = _nm_get("802-11-wireless.ssid", name)
        if ssid == config.HOTSPOT_SSID:
            password = config.HOTSPOT_PASSWORD
        else:  # autre point d'accès : NetworkManager le donne s'il en a le droit (sinon, sans QR Wi-Fi)
            password = _nm_get("802-11-wireless-security.psk", name, secrets=True) or None
        return dev, ssid, password
    return None


def _hostapd_conf() -> tuple[str, str, str | None] | None:
    try:
        with open("/etc/hostapd/hostapd.conf", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    vals = dict(re.findall(r"^[ \t]*(interface|ssid|wpa_passphrase)[ \t]*=[ \t]*(.*?)[ \t]*$", text, re.M))
    return (vals.get("interface", ""), vals["ssid"], vals.get("wpa_passphrase")) if vals.get("ssid") else None


def detect_hotspot(addrs: list[tuple[str, str]] | None = None) -> Hotspot | None:
    """Le point d'accès Wi-Fi actif sur ce Pi, ou None. Appelle nmcli : à utiliser au démarrage ou
    à la demande, pas à chaque requête (voir lan_ips pour ça)."""
    addrs = interfaces() if addrs is None else addrs
    first_ip = {}
    for name, ip in addrs:
        first_ip.setdefault(name, ip)
    nm = _nm_hotspot()
    if nm:
        dev, ssid, password = nm
        return Hotspot(ip=first_ip.get(dev) or HOTSPOT_IPS[0], ifname=dev, ssid=ssid or config.HOTSPOT_SSID,
                       password=password, source="networkmanager")
    for name, ip in addrs:
        if ip in HOTSPOT_IPS and (not name or is_wireless(name)):
            conf = _hostapd_conf()
            if conf and (not conf[0] or conf[0] == name):
                return Hotspot(ip=ip, ifname=name, ssid=conf[1], password=conf[2], source="hostapd")
            return Hotspot(ip=ip, ifname=name, ssid=config.HOTSPOT_SSID, password=config.HOTSPOT_PASSWORD)
    return None
