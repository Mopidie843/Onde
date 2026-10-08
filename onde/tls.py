"""HTTPS sur le réseau local : une petite « autorité » propre à Onde + un certificat de serveur.

Pourquoi deux certificats ?
  * L'**autorité locale** (`onde-ca.pem`) est ce qu'on installe *une seule fois* sur chaque appareil
    (iPhone, Android, PC, Mac) pour que le navigateur fasse confiance à Onde. Elle est valable 10 ans.
  * Le **certificat du serveur** (`server.pem`), signé par cette autorité, est celui que présente Onde.
    Il suit les règles d'Apple — sans elles, Safari (iPhone, iPad, Mac) refuse la connexion même
    quand le certificat est installé et approuvé : au plus 825 jours de validité, usage « serveur »
    (extension EKU serverAuth), noms dans le Subject Alternative Name, clé ≥ P-256/RSA-2048, SHA-256.
    Il est renouvelé tout seul (bientôt expiré, adresse IP du Pi changée, nom d'hôte changé) ; comme
    l'autorité ne change pas, les appareils n'ont rien à réinstaller.

Sans l'autorité installée, la connexion est quand même chiffrée, mais le navigateur affiche un
avertissement au premier accès (normal pour un serveur privé, sans nom de domaine public).
"""
from __future__ import annotations

import datetime
import ipaddress
import logging
import os
import socket
import ssl
from pathlib import Path

from . import config, netinfo

log = logging.getLogger("onde.tls")

TLS_DIR = config.DATA_DIR / "tls"
CA_CERT = TLS_DIR / "onde-ca.pem"          # à installer sur les appareils (téléchargeable)
CA_KEY = TLS_DIR / "onde-ca-key.pem"       # reste sur le serveur
SERVER_CERT = TLS_DIR / "server.pem"       # certificat du serveur + autorité (chaîne complète)
SERVER_KEY = TLS_DIR / "server-key.pem"
CERT_PATH = CA_CERT                        # ce que les appareils téléchargent

CA_DAYS = 3650
LEAF_DAYS = 800          # < 825 jours : limite d'Apple pour les certificats de serveur TLS
RENEW_BEFORE_DAYS = 30


def available() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


# ------------------------------------------------------------------ noms couverts par le certificat
def _lan_ips() -> list[str]:
    # toutes les interfaces (pas seulement la route par défaut, absente en point d'accès sans box),
    # plus les adresses habituelles d'un point d'accès : passer en mode point d'accès plus tard
    # (./start.sh hotspot) ne demande alors ni nouveau certificat ni redémarrage
    ips = {"127.0.0.1", *netinfo.HOTSPOT_IPS}
    try:
        ips.update(netinfo.lan_ips())
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def wanted_names() -> tuple[set[str], set[str]]:
    """(noms DNS, adresses IP) sous lesquels on peut joindre Onde sur le réseau local."""
    host = socket.gethostname().strip() or "onde"
    dns = {"localhost", host, f"{host}.local"}
    ips = set(_lan_ips()) | {"::1"}
    return dns, ips


# ------------------------------------------------------------------ utilitaires
def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _not_after(cert) -> datetime.datetime:
    value = getattr(cert, "not_valid_after_utc", None)
    if value is None:  # cryptography < 42
        value = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
    return value


def _write(path: Path, data: bytes, private: bool = False) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    if private:
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
    os.replace(tmp, path)


def _load_key(path: Path):
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def _load_cert(path: Path):
    from cryptography import x509
    return x509.load_pem_x509_certificate(path.read_bytes())


def _key_pem(key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


# ------------------------------------------------------------------ autorité locale
def _ensure_ca():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    if CA_CERT.is_file() and CA_KEY.is_file():
        try:
            ca, key = _load_cert(CA_CERT), _load_key(CA_KEY)
            if _not_after(ca) > _utc_now() + datetime.timedelta(days=RENEW_BEFORE_DAYS):
                return ca, key
        except Exception as exc:  # fichiers abîmés : on en refait
            log.warning("autorité HTTPS illisible, nouvelle autorité créée (%s)", exc)

    host = socket.gethostname().strip() or "onde"
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"Onde ({host})"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Onde — autorité locale"),
    ])
    now = _utc_now()
    ca = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    from cryptography.hazmat.primitives import serialization
    _write(CA_KEY, _key_pem(key), private=True)
    _write(CA_CERT, ca.public_bytes(serialization.Encoding.PEM))
    for stale in (SERVER_CERT, SERVER_KEY):  # l'ancien certificat de serveur n'est plus signé par elle
        stale.unlink(missing_ok=True)
    log.info("autorité HTTPS locale créée (%s)", CA_CERT)
    return ca, key


# ------------------------------------------------------------------ certificat du serveur
def _leaf_ok(ca) -> bool:
    if not (SERVER_CERT.is_file() and SERVER_KEY.is_file()):
        return False
    try:
        from cryptography import x509
        leaf = _load_cert(SERVER_CERT)
        _load_key(SERVER_KEY)
        leaf.verify_directly_issued_by(ca)
        if _not_after(leaf) < _utc_now() + datetime.timedelta(days=RENEW_BEFORE_DAYS):
            return False
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have_dns = set(san.get_values_for_type(x509.DNSName))
        have_ip = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
        want_dns, want_ip = wanted_names()
        return want_dns <= have_dns and want_ip <= have_ip
    except Exception:
        return False


def _issue_leaf(ca, ca_key) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    dns, ips = wanted_names()
    san: list[x509.GeneralName] = [x509.DNSName(d) for d in sorted(dns)]
    for ip in sorted(ips):
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    host = socket.gethostname().strip() or "onde"
    # même clé qu'avant si elle existe : l'appli Android retient la clé du serveur à la 1re connexion
    # (épinglage) — un nouveau certificat (nouvelle adresse IP…) ne doit pas la faire refuser
    key = None
    if SERVER_KEY.is_file():
        try:
            key = _load_key(SERVER_KEY)
            if not isinstance(key, ec.EllipticCurvePrivateKey):
                key = None
        except Exception:
            key = None
    key = key or ec.generate_private_key(ec.SECP256R1())
    now = _utc_now()
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(ca.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=LEAF_DAYS))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_encipherment=False, content_commitment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write(SERVER_KEY, _key_pem(key), private=True)
    _write(SERVER_CERT, leaf.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
    log.info("certificat HTTPS du serveur émis pour %s", ", ".join(sorted(dns) + sorted(ips)))


def ensure_cert() -> tuple[Path, Path] | None:
    """Prépare l'autorité locale et un certificat de serveur à jour.
    Renvoie (chaîne du serveur, clé du serveur), ou None si `cryptography` n'est pas installé."""
    if not available():
        return None
    TLS_DIR.mkdir(parents=True, exist_ok=True)
    for old in ("cert.pem", "key.pem"):  # ancien format (certificat unique), remplacé par l'autorité
        (TLS_DIR / old).unlink(missing_ok=True)
    ca, ca_key = _ensure_ca()
    if not _leaf_ok(ca):
        _issue_leaf(ca, ca_key)
    return SERVER_CERT, SERVER_KEY


def ssl_context() -> ssl.SSLContext | None:
    """Contexte TLS côté serveur (HTTP/1.1, TLS 1.2 minimum), ou None si indisponible."""
    paths = ensure_cert()
    if not paths:
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(paths[0]), str(paths[1]))
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def has_cert() -> bool:
    """Un certificat existe déjà (HTTPS a été activé au moins une fois)."""
    return SERVER_CERT.is_file() and SERVER_KEY.is_file() and CA_CERT.is_file()
