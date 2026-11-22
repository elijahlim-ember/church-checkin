"""HTTPS for the iPads (optional: set "https_port" in data/config.json).

Like the church database, check-in makes its own small certificate authority (CA) on first use. Each iPad
trusts its certificate once (from https://<main computer>:<https_port>/ca.crt); the server certificate it
signs is renewed here automatically. The CA is kept in data/keys and goes into backups, so a replacement
computer keeps the iPads' trust.
"""

import ipaddress
import os
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

CA_NAME = "Church Check-in CA"
ORG_NAME = os.environ.get("ORG_NAME", "Your Church")
CA_CERT = "checkin-ca-cert.pem"
CA_KEY = "checkin-ca-key.pem"
SERVER_CERT = "checkin-tls-cert.pem"
SERVER_KEY = "checkin-tls-key.pem"
CA_DAYS = 365 * 20
SERVER_DAYS = 825  # the longest Apple devices accept
RENEW_DAYS = 60


@dataclass(frozen=True)
class CertFiles:
    ca_cert: Path
    server_cert: Path
    server_key: Path


def _write_private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _pem_key(key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _make_ca(keys: Path, now: datetime) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORG_NAME)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    _write_private(keys / CA_KEY, _pem_key(key))
    (keys / CA_CERT).write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _sans(names: list[str], ips: list[str]) -> tuple[list[str], list]:
    dns = sorted({n.lower() for n in names if n} | {"localhost"})
    addresses = sorted({ipaddress.ip_address(i) for i in ips if i} | {ipaddress.ip_address("127.0.0.1")})
    return dns, addresses


def _still_good(cert_path: Path, ca, dns, addresses, now: datetime) -> bool:
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        cert.verify_directly_issued_by(ca)  # not just the same name: a restored CA replaces this computer's own
    except (OSError, ValueError, TypeError, x509.ExtensionNotFound, InvalidSignature):
        return False
    return (cert.issuer == ca.subject
            and cert.not_valid_after_utc - now > timedelta(days=RENEW_DAYS)
            and sorted(san.get_values_for_type(x509.DNSName)) == dns
            and sorted(san.get_values_for_type(x509.IPAddress)) == addresses)


def ensure_certificates(keys: Path, names: list[str], ips: list[str], now: datetime | None = None) -> CertFiles:
    """The CA (made once) and a server certificate for these names and addresses (renewed when needed)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    now = now or datetime.now(timezone.utc)
    files = CertFiles(keys / CA_CERT, keys / SERVER_CERT, keys / SERVER_KEY)
    if not (files.ca_cert.exists() and (keys / CA_KEY).exists()):
        _make_ca(keys, now)
    ca = x509.load_pem_x509_certificate(files.ca_cert.read_bytes())
    dns, addresses = _sans(names, ips)
    if files.server_key.exists() and _still_good(files.server_cert, ca, dns, addresses, now):
        return files
    ca_key = serialization.load_pem_private_key((keys / CA_KEY).read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns[0] if dns else "localhost")]))
            .issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=SERVER_DAYS - 1))
            .add_extension(x509.SubjectAlternativeName([*map(x509.DNSName, dns), *map(x509.IPAddress, addresses)]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    _write_private(files.server_key, _pem_key(key))
    files.server_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return files


def certificate_names(config: dict, addresses: list[str]) -> tuple[list[str], list[str]]:
    """This computer's names and network addresses, plus any in config.json's "tls_names"."""
    host = socket.gethostname()
    short = host.split(".")[0]
    names = [n.lower() for n in (host, short, f"{short}.local") if n and n.lower() != "localhost"]
    ips = list(addresses)
    for entry in config.get("tls_names") or []:
        try:
            ipaddress.ip_address(entry)
            ips.append(entry)
        except ValueError:
            names.append(entry)
    return names, ips


def server_context(files: CertFiles) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(files.server_cert), str(files.server_key))
    return context
