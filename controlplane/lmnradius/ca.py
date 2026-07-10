# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dedicated EAP Certificate Authority (crypto core, pure — no Docker).

ADR-005: this is a *single-purpose* private CA that signs ONLY the RADIUS EAP
server certificate. It is deliberately NOT the linuxmuster platform CA and NOT
Let's Encrypt. Because PEAP's inner MSCHAPv2 is weak, client-side server-cert
pinning is load-bearing — the CA minted here is the trust anchor supplicants pin.

Everything here is pure ``cryptography`` + filesystem work (no docker-py), so it
is unit-testable in isolation. The API layer maps the precise exceptions raised
below onto HTTP status codes. The passphrase and any private-key material are
NEVER logged.

On-disk layout under ``certs_dir`` (see :mod:`lmnradius.docker_service`):

* ``ca/ca.key.pem``   — RSA-4096 CA private key, PKCS8, *encrypted*, mode 0600
* ``ca/ca.cert.pem``  — self-signed CA certificate, PEM, mode 0644
* ``<name>/server.key`` — RSA-2048 server key, PKCS8, *unencrypted*, mode 0600
* ``<name>/server.pem`` — signed EAP server certificate, PEM
* ``<name>/ca.pem``     — a copy of ``ca/ca.cert.pem`` (the mounted trust anchor)

The CA directory and every per-instance directory are mode 0700.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_log = logging.getLogger("lmnradius.ca")

# -- CA-side filenames ---------------------------------------------------------
_CA_DIRNAME = "ca"
_CA_KEY = "ca.key.pem"
_CA_CERT = "ca.cert.pem"

# -- per-instance EAP cert material filenames ----------------------------------
# These MUST stay byte-for-byte identical to docker_service._CA_FILE /
# _CERT_FILE / _KEY_FILE — that module bind-mounts them read-only into the
# container at /run/secrets/eap/*. Redefined here (rather than imported) to keep
# this crypto core free of the docker-py dependency; a drift would make an
# issued cert invisible to the container. Keep the two lists in lock-step.
_CA_FILE = "ca.pem"
_CERT_FILE = "server.pem"
_KEY_FILE = "server.key"

# id-kp-eapOverLAN (RFC 4334 §3; formerly RFC 3770) — included alongside serverAuth.
# serverAuth is the hard requirement; eapOverLAN is belt-and-braces for legacy
# supplicants that check EKU and would reject a plain TLS server cert.
_EAP_OVER_LAN_OID = x509.ObjectIdentifier("1.3.6.1.5.5.7.3.14")

# FQDN validator, identical to models._HOST_RE (server_fqdn == cert CN/SAN).
_FQDN_RE = re.compile(
    r"^(?=.{1,253}$)[a-zA-Z0-9]([a-zA-Z0-9-]{0,62})(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,62}))*$"
)

# RSA parameters and clock skew.
_CA_KEY_BITS = 4096
_SERVER_KEY_BITS = 2048
_PUBLIC_EXPONENT = 65537
_BACKDATE = timedelta(minutes=5)  # tolerate small clock skew between host + client


# -- helpers -------------------------------------------------------------------


def _safe_name(name: str) -> None:
    """Reject an instance ``name`` that could escape ``certs_dir`` on join.

    Defence in depth: the API already validates the name shape, but this core is
    meant to be safe on its own."""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"unsafe instance name {name!r} (no '/', '\\\\' or '..')")


def _write_file(path: str, data: bytes, mode: int) -> None:
    """Write ``data`` to ``path`` with ``mode``.

    ``O_CREAT``'s mode only applies on creation, so re-assert perms with
    ``chmod`` for a pre-existing file (mirrors docker_service._write_private)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(path, mode)


def _iso(dt: datetime) -> str:
    """Format an X.509 validity instant as an ISO-8601 UTC string."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _describe_ca(cert: x509.Certificate) -> dict[str, Any]:
    """Public-facing status view of the CA certificate."""
    return {
        "subject": cert.subject.rfc4514_string(),
        "serial": f"{cert.serial_number:x}",
        "not_before": _iso(cert.not_valid_before_utc),
        "not_after": _iso(cert.not_valid_after_utc),
        "sha256_fingerprint": cert.fingerprint(hashes.SHA256()).hex(),
    }


def _describe_server(cert: x509.Certificate) -> dict[str, Any]:
    """Public-facing status view of a server certificate."""
    try:
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        san = []
    return {
        "subject": cert.subject.rfc4514_string(),
        "san": san,
        "issuer": cert.issuer.rfc4514_string(),
        "serial": f"{cert.serial_number:x}",
        "not_before": _iso(cert.not_valid_before_utc),
        "not_after": _iso(cert.not_valid_after_utc),
    }


# -- CA lifecycle --------------------------------------------------------------


def init_ca(
    certs_dir: str,
    passphrase: str,
    common_name: str = "linuxmuster-radius EAP CA",
    validity_days: int = 3652,
) -> dict[str, Any]:
    """Create the self-signed EAP CA (idempotency guard: refuse to overwrite).

    :param certs_dir: root cert directory (``Settings.certs_dir``).
    :param passphrase: encrypts the CA private key at rest (never logged).
    :param common_name: CA certificate subject/issuer CN.
    :param validity_days: CA lifetime in days (default ~10y).
    :raises FileExistsError: a CA certificate already exists.
    :raises ValueError: the passphrase is empty / common_name empty / bad validity.
    :returns: the :func:`ca_status` view of the freshly minted CA.
    """
    if not passphrase:
        raise ValueError("passphrase must not be empty")
    if not common_name:
        raise ValueError("common_name must not be empty")
    if validity_days < 1:
        raise ValueError("validity_days must be positive")

    ca_dir = os.path.join(certs_dir, _CA_DIRNAME)
    cert_path = os.path.join(ca_dir, _CA_CERT)
    key_path = os.path.join(ca_dir, _CA_KEY)
    if os.path.exists(cert_path):
        raise FileExistsError(f"EAP CA already initialised: {cert_path}")

    key = rsa.generate_private_key(public_exponent=_PUBLIC_EXPONENT, key_size=_CA_KEY_BITS)
    public_key = key.public_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _BACKDATE)
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .sign(key, hashes.SHA256())
    )

    key_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(passphrase.encode("utf-8")),
    )
    cert_bytes = cert.public_bytes(serialization.Encoding.PEM)

    os.makedirs(ca_dir, exist_ok=True)
    os.chmod(ca_dir, 0o700)
    _write_file(key_path, key_bytes, 0o600)
    _write_file(cert_path, cert_bytes, 0o644)

    _log.info("initialised EAP CA common_name=%s validity_days=%d", common_name, validity_days)
    return _describe_ca(cert)


def ca_status(certs_dir: str) -> dict[str, Any] | None:
    """Return the CA certificate status, or ``None`` if it is not initialised."""
    cert_path = os.path.join(certs_dir, _CA_DIRNAME, _CA_CERT)
    if not os.path.isfile(cert_path):
        return None
    cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    return _describe_ca(cert)


def export_ca(certs_dir: str) -> str:
    """Return the CA certificate PEM text (the trust anchor for clients).

    :raises FileNotFoundError: the CA is not initialised."""
    cert_path = os.path.join(certs_dir, _CA_DIRNAME, _CA_CERT)
    if not os.path.isfile(cert_path):
        raise FileNotFoundError("EAP CA not initialised")
    return Path(cert_path).read_text(encoding="utf-8")


# -- server certificates -------------------------------------------------------


def issue_server_cert(
    certs_dir: str,
    name: str,
    fqdn: str,
    passphrase: str,
    validity_days: int = 1095,
) -> dict[str, Any]:
    """Sign the EAP server certificate for instance ``name`` with the CA.

    Writes ``<name>/server.key`` (unencrypted, 0600), ``<name>/server.pem`` and a
    copy of the CA cert as ``<name>/ca.pem`` — the three filenames
    :mod:`lmnradius.docker_service` bind-mounts into the container.

    :param fqdn: cert CN and DNS SAN (validated as an FQDN).
    :param passphrase: the CA passphrase; unlocks the signing key (never logged).
    :param validity_days: server-cert lifetime in days (default ~3y).
    :raises FileNotFoundError: the CA is not initialised.
    :raises ValueError: bad name/fqdn/validity, empty passphrase, or a wrong CA
        passphrase (mapped from cryptography's decrypt error).
    :returns: the :func:`cert_status` view of the issued certificate.
    """
    _safe_name(name)
    if not _FQDN_RE.match(fqdn):
        raise ValueError(f"invalid fqdn {fqdn!r}")
    if not passphrase:
        raise ValueError("passphrase must not be empty")
    if validity_days < 1:
        raise ValueError("validity_days must be positive")

    ca_dir = os.path.join(certs_dir, _CA_DIRNAME)
    ca_cert_path = os.path.join(ca_dir, _CA_CERT)
    ca_key_path = os.path.join(ca_dir, _CA_KEY)
    if not os.path.isfile(ca_cert_path) or not os.path.isfile(ca_key_path):
        raise FileNotFoundError("EAP CA not initialised")

    ca_cert_pem = Path(ca_cert_path).read_bytes()
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    try:
        ca_key = serialization.load_pem_private_key(
            Path(ca_key_path).read_bytes(), password=passphrase.encode("utf-8")
        )
    except (ValueError, TypeError) as exc:
        # Wrong passphrase (or otherwise undecryptable key). Do not leak details.
        raise ValueError("invalid CA passphrase") from exc
    if not isinstance(ca_key, rsa.RSAPrivateKey):
        raise ValueError("CA key is not an RSA key")

    server_key = rsa.generate_private_key(
        public_exponent=_PUBLIC_EXPONENT, key_size=_SERVER_KEY_BITS
    )
    server_pub = server_key.public_key()
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, fqdn)]))
        .issuer_name(ca_cert.subject)
        .public_key(server_pub)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _BACKDATE)
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(fqdn)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, _EAP_OVER_LAN_OID]),
            critical=False,
        )
        .add_extension(
            # ca_key.public_key() is the CA's public key (narrowed to RSA above);
            # identical to ca_cert.public_key() but keeps the AKI helper well-typed.
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_pub), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    server_key_bytes = server_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    server_cert_bytes = cert.public_bytes(serialization.Encoding.PEM)

    inst_dir = os.path.join(certs_dir, name)
    os.makedirs(inst_dir, exist_ok=True)
    os.chmod(inst_dir, 0o700)
    # FreeRADIUS starts unattended, so the server key is unencrypted; the 0700 dir
    # and 0600 key keep it off-limits to non-root at rest.
    _write_file(os.path.join(inst_dir, _KEY_FILE), server_key_bytes, 0o600)
    _write_file(os.path.join(inst_dir, _CERT_FILE), server_cert_bytes, 0o644)
    _write_file(os.path.join(inst_dir, _CA_FILE), ca_cert_pem, 0o644)

    _log.info(
        "issued EAP server cert instance=%s fqdn=%s validity_days=%d", name, fqdn, validity_days
    )
    return _describe_server(cert)


def cert_status(certs_dir: str, name: str) -> dict[str, Any] | None:
    """Return the server-cert status for ``name``, or ``None`` if unissued."""
    _safe_name(name)
    cert_path = os.path.join(certs_dir, name, _CERT_FILE)
    if not os.path.isfile(cert_path):
        return None
    cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    return _describe_server(cert)
