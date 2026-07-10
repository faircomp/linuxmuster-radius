# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the dedicated EAP CA (P3): crypto core, API routes and CLI.

Everything runs with REAL ``cryptography`` (it is a hard dependency) and WITHOUT
Docker: the pure-crypto layer (:mod:`lmnradius.ca`) only touches the filesystem,
the API is driven through the in-memory ``FakeDockerService`` TestClient, and the
CLI monkeypatches ``cli._get_client`` onto that same TestClient. Certificates are
re-parsed with ``x509.load_pem_x509_certificate`` so the extension/signature
assertions verify the bytes actually written to disk, not the builder in memory.

The EAP cert is the trust anchor supplicants pin (PEAP's inner MSCHAPv2 is weak),
so the load-bearing assertions here are: the CA key is encrypted at rest, the
server cert is genuinely signed by the CA, and it carries BOTH serverAuth and
id-kp-eapOverLAN in its EKU (a plain TLS server cert would be rejected by EAP
supplicants that check the EKU).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID
from starlette.testclient import TestClient
from typer.testing import CliRunner

from lmnradius import ca, cli
from lmnradius.config import Settings

# The three filenames docker_service bind-mounts into the container. Imported
# (not hard-coded) so a drift between ca.py and docker_service is caught here.
from lmnradius.docker_service import _CA_FILE, _CERT_FILE, _KEY_FILE

runner = CliRunner()

NAME = "default-school"
FQDN = "radius.linuxmuster.lan"
PASSPHRASE = "correct horse battery staple"
WRONG_PASSPHRASE = "not-the-passphrase"

# OIDs the EAP server cert MUST advertise in its ExtendedKeyUsage.
_SERVER_AUTH_OID = "1.3.6.1.5.5.7.3.1"
_EAP_OVER_LAN_OID = "1.3.6.1.5.5.7.3.14"


# --------------------------------------------------------------------- helpers


def _mode(path: str) -> int:
    """Return the permission bits (e.g. 0o600) of ``path``."""
    return stat.S_IMODE(os.stat(path).st_mode)


def _ca_paths(settings: Settings) -> tuple[str, str]:
    """(ca.key.pem, ca.cert.pem) absolute paths under the settings certs_dir."""
    ca_dir = os.path.join(settings.certs_dir, "ca")
    return os.path.join(ca_dir, "ca.key.pem"), os.path.join(ca_dir, "ca.cert.pem")


def _init(settings: Settings) -> dict[str, Any]:
    """Initialise the CA into the settings certs_dir (short-validity for speed)."""
    return ca.init_ca(settings.certs_dir, PASSPHRASE, validity_days=30)


# ---------------------------------------------------------------- crypto: init_ca


def test_init_ca_writes_encrypted_key_and_cert(settings: Settings) -> None:
    result = ca.init_ca(settings.certs_dir, PASSPHRASE, common_name="linuxmuster-radius EAP CA")
    key_path, cert_path = _ca_paths(settings)

    # Files exist with the documented permissions.
    assert os.path.isfile(key_path) and os.path.isfile(cert_path)
    assert _mode(key_path) == 0o600, "CA private key must be 0600"
    assert _mode(cert_path) == 0o644, "CA cert must be 0644"
    # The CA directory itself is locked down.
    assert _mode(os.path.join(settings.certs_dir, "ca")) == 0o700

    # The private key is genuinely ENCRYPTED: loading without the passphrase
    # fails, a wrong passphrase fails, and only the real passphrase succeeds.
    key_bytes = Path(key_path).read_bytes()
    with pytest.raises(TypeError):
        serialization.load_pem_private_key(key_bytes, password=None)
    with pytest.raises(ValueError):
        serialization.load_pem_private_key(key_bytes, password=WRONG_PASSPHRASE.encode())
    key = serialization.load_pem_private_key(key_bytes, password=PASSPHRASE.encode())
    assert isinstance(key, rsa.RSAPrivateKey)
    assert key.key_size == 4096

    # Self-signed CA cert: issuer == subject, BasicConstraints CA:TRUE.
    cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    assert cert.issuer == cert.subject
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    # The status dict describes the freshly minted cert.
    assert result["subject"] == cert.subject.rfc4514_string()
    assert result["sha256_fingerprint"] == cert.fingerprint(hashes.SHA256()).hex()


def test_init_ca_reinit_raises_file_exists(settings: Settings) -> None:
    # NEGATIVE: the idempotency guard refuses to clobber an existing CA.
    _init(settings)
    with pytest.raises(FileExistsError):
        ca.init_ca(settings.certs_dir, PASSPHRASE)


def test_init_ca_empty_passphrase_raises(settings: Settings) -> None:
    # NEGATIVE: an unencrypted CA key is never acceptable.
    with pytest.raises(ValueError):
        ca.init_ca(settings.certs_dir, "")


# -------------------------------------------------------- crypto: issue_server_cert


def test_issue_server_cert_extensions_and_signature(settings: Settings) -> None:
    _init(settings)
    result = ca.issue_server_cert(settings.certs_dir, NAME, FQDN, PASSPHRASE, validity_days=30)

    cert_path = os.path.join(settings.certs_dir, NAME, _CERT_FILE)
    server_cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())

    # SAN carries exactly the requested FQDN as a DNS name.
    san = server_cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == [FQDN]

    # EKU carries BOTH serverAuth AND id-kp-eapOverLAN (the load-bearing pair).
    eku = server_cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    oids = {oid.dotted_string for oid in eku}
    assert _SERVER_AUTH_OID in oids
    assert _EAP_OVER_LAN_OID in oids
    assert ExtendedKeyUsageOID.SERVER_AUTH in list(eku)

    # It is a leaf, not a CA.
    bc = server_cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca is False

    # It is signed by the CA: issuer name matches, and the CA public key verifies
    # the certificate signature (raises InvalidSignature otherwise).
    _key_path, ca_cert_path = _ca_paths(settings)
    ca_cert = x509.load_pem_x509_certificate(Path(ca_cert_path).read_bytes())
    assert server_cert.issuer == ca_cert.subject
    ca_pub = ca_cert.public_key()
    assert isinstance(ca_pub, rsa.RSAPublicKey)
    sig_hash = server_cert.signature_hash_algorithm
    assert sig_hash is not None
    ca_pub.verify(
        server_cert.signature,
        server_cert.tbs_certificate_bytes,
        padding.PKCS1v15(),
        sig_hash,
    )
    # Cross-check with the high-level chain helper too.
    server_cert.verify_directly_issued_by(ca_cert)

    # The status view reflects the parsed cert.
    assert result["san"] == [FQDN]
    assert result["issuer"] == ca_cert.subject.rfc4514_string()


def test_issue_server_cert_writes_files_and_modes(settings: Settings) -> None:
    _init(settings)
    ca.issue_server_cert(settings.certs_dir, NAME, FQDN, PASSPHRASE, validity_days=30)
    inst_dir = os.path.join(settings.certs_dir, NAME)

    # The three docker_service filenames all exist (drift-proofed against import).
    assert (_CA_FILE, _CERT_FILE, _KEY_FILE) == ("ca.pem", "server.pem", "server.key")
    for fname in (_CA_FILE, _CERT_FILE, _KEY_FILE):
        assert os.path.isfile(os.path.join(inst_dir, fname)), fname

    # server.key is an UNENCRYPTED PKCS8 key (FreeRADIUS starts unattended) but is
    # locked to 0600 in a 0700 dir.
    key_path = os.path.join(inst_dir, _KEY_FILE)
    assert _mode(key_path) == 0o600
    assert _mode(inst_dir) == 0o700
    key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    assert isinstance(key, rsa.RSAPrivateKey)
    assert key.key_size == 2048

    # The bundled ca.pem is byte-for-byte the CA cert.
    _k, ca_cert_path = _ca_paths(settings)
    assert Path(os.path.join(inst_dir, _CA_FILE)).read_bytes() == Path(ca_cert_path).read_bytes()


def test_issue_server_cert_wrong_passphrase_raises_value_error(settings: Settings) -> None:
    # NEGATIVE: a wrong CA passphrase cannot unlock the signing key -> ValueError.
    _init(settings)
    with pytest.raises(ValueError):
        ca.issue_server_cert(settings.certs_dir, NAME, FQDN, WRONG_PASSPHRASE)
    # And nothing was written for the instance.
    assert not os.path.exists(os.path.join(settings.certs_dir, NAME, _CERT_FILE))


def test_issue_server_cert_before_init_raises_file_not_found(settings: Settings) -> None:
    # NEGATIVE: cannot sign before the CA exists.
    with pytest.raises(FileNotFoundError):
        ca.issue_server_cert(settings.certs_dir, NAME, FQDN, PASSPHRASE)


# ------------------------------------------------------------------- crypto: export


def test_export_ca_returns_pem(settings: Settings) -> None:
    _init(settings)
    pem = ca.export_ca(settings.certs_dir)
    assert "BEGIN CERTIFICATE" in pem
    # It is exactly the on-disk CA cert and re-parses to the CA subject.
    _k, ca_cert_path = _ca_paths(settings)
    assert pem == Path(ca_cert_path).read_text(encoding="utf-8")
    cert = x509.load_pem_x509_certificate(pem.encode("utf-8"))
    assert cert.issuer == cert.subject


def test_export_ca_before_init_raises(settings: Settings) -> None:
    # NEGATIVE: exporting an uninitialised CA is a FileNotFoundError (API -> 404).
    with pytest.raises(FileNotFoundError):
        ca.export_ca(settings.certs_dir)


# ------------------------------------------------------------------------ API: CA


def _seed_instance(client: TestClient, auth_headers: dict[str, str], data: dict[str, Any]) -> None:
    resp = client.post("/v1/instances", json=data, headers=auth_headers)
    assert resp.status_code == 201, resp.text


def test_api_ca_init_201_then_409(client: TestClient, auth_headers: dict[str, str]) -> None:
    body = {"passphrase": PASSPHRASE, "validity_days": 30}
    resp = client.post("/v1/ca", json=body, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    assert "sha256_fingerprint" in resp.json()

    # NEGATIVE: re-initialising is refused with 409 (idempotency guard).
    again = client.post("/v1/ca", json=body, headers=auth_headers)
    assert again.status_code == 409


def test_api_ca_get_and_export(client: TestClient, auth_headers: dict[str, str]) -> None:
    client.post(
        "/v1/ca", json={"passphrase": PASSPHRASE, "validity_days": 30}, headers=auth_headers
    )

    status = client.get("/v1/ca", headers=auth_headers)
    assert status.status_code == 200
    assert status.json()["subject"].startswith("CN=")

    export = client.get("/v1/ca/export", headers=auth_headers)
    assert export.status_code == 200
    assert "BEGIN CERTIFICATE" in export.text
    # The exported PEM parses back to a self-signed CA cert.
    cert = x509.load_pem_x509_certificate(export.text.encode("utf-8"))
    assert cert.issuer == cert.subject


def test_api_ca_get_404_before_init(client: TestClient, auth_headers: dict[str, str]) -> None:
    # NEGATIVE: no CA yet -> both status and export are 404.
    assert client.get("/v1/ca", headers=auth_headers).status_code == 404
    assert client.get("/v1/ca/export", headers=auth_headers).status_code == 404


# ---------------------------------------------------------------------- API: certs


def test_api_issue_cert_unknown_instance_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    # NEGATIVE: cert issuance for an instance that does not exist is a 404
    # (the instance lookup fails before the CA is ever touched).
    client.post(
        "/v1/ca", json={"passphrase": PASSPHRASE, "validity_days": 30}, headers=auth_headers
    )
    resp = client.post(
        "/v1/instances/does-not-exist/cert", json={"passphrase": PASSPHRASE}, headers=auth_headers
    )
    assert resp.status_code == 404


def test_api_issue_cert_no_ca_409(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    # NEGATIVE: instance exists but the CA is not initialised -> 409.
    _seed_instance(client, auth_headers, instance_data)
    resp = client.post(
        f"/v1/instances/{NAME}/cert", json={"passphrase": PASSPHRASE}, headers=auth_headers
    )
    assert resp.status_code == 409


def test_api_issue_cert_bad_passphrase_422(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    # NEGATIVE: a wrong CA passphrase surfaces as 422 (ca.py ValueError mapping).
    client.post(
        "/v1/ca", json={"passphrase": PASSPHRASE, "validity_days": 30}, headers=auth_headers
    )
    _seed_instance(client, auth_headers, instance_data)
    resp = client.post(
        f"/v1/instances/{NAME}/cert", json={"passphrase": WRONG_PASSPHRASE}, headers=auth_headers
    )
    assert resp.status_code == 422


def test_api_issue_cert_success_and_get(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    client.post(
        "/v1/ca", json={"passphrase": PASSPHRASE, "validity_days": 30}, headers=auth_headers
    )
    _seed_instance(client, auth_headers, instance_data)

    # POST issues the cert (fqdn defaults to the instance server_fqdn) -> 201.
    resp = client.post(
        f"/v1/instances/{NAME}/cert",
        json={"passphrase": PASSPHRASE, "validity_days": 30},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["san"] == [FQDN]

    # GET returns the issued-cert status -> 200.
    got = client.get(f"/v1/instances/{NAME}/cert", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["san"] == [FQDN]

    # Re-issuing (rotation) overwrites and stays 201.
    reissue = client.post(
        f"/v1/instances/{NAME}/cert", json={"passphrase": PASSPHRASE}, headers=auth_headers
    )
    assert reissue.status_code == 201


def test_api_cert_get_404_before_issue(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    # NEGATIVE: an instance with no issued cert returns 404 on GET .../cert.
    _seed_instance(client, auth_headers, instance_data)
    resp = client.get(f"/v1/instances/{NAME}/cert", headers=auth_headers)
    assert resp.status_code == 404


def test_api_ca_and_cert_endpoints_require_auth(client: TestClient) -> None:
    # NEGATIVE: every CA/cert route rejects a missing token with 401 and a wrong
    # token with 403 (auth runs before any CA/instance work).
    endpoints = [
        ("post", "/v1/ca", {"passphrase": PASSPHRASE}),
        ("get", "/v1/ca", None),
        ("get", "/v1/ca/export", None),
        ("post", f"/v1/instances/{NAME}/cert", {"passphrase": PASSPHRASE}),
        ("get", f"/v1/instances/{NAME}/cert", None),
    ]
    for method, path, json_body in endpoints:
        call = getattr(client, method)
        kwargs: dict[str, Any] = {"json": json_body} if json_body is not None else {}
        assert call(path, **kwargs).status_code == 401, (path, "no token")
        bad = call(path, headers={"Authorization": "Bearer nope"}, **kwargs)
        assert bad.status_code == 403, (path, "wrong token")


# ------------------------------------------------------------------------ CLI


@pytest.fixture
def patch_client(monkeypatch: pytest.MonkeyPatch, app: Any, token: str) -> None:
    """Make cli._get_client() return a fresh authenticated TestClient for `app`."""

    def factory() -> TestClient:
        tc = TestClient(app)
        tc.headers.update({"Authorization": f"Bearer {token}"})
        return tc

    monkeypatch.setattr(cli, "_get_client", factory)


def _seed_via_api(app: Any, token: str, instance_data: dict[str, Any]) -> None:
    tc = TestClient(app)
    tc.headers.update({"Authorization": f"Bearer {token}"})
    assert tc.post("/v1/instances", json=instance_data).status_code == 201


def test_cli_ca_init_show_export(patch_client: None) -> None:
    # `ca init` flattens --common-name/--validity-days/--passphrase into the body;
    # passing --passphrase bypasses the (confirmation) prompt.
    r = runner.invoke(
        cli.app,
        [
            "ca",
            "init",
            "--common-name",
            "CLI Test EAP CA",
            "--validity-days",
            "30",
            "--passphrase",
            PASSPHRASE,
        ],
    )
    assert r.exit_code == 0, r.output
    assert "sha256_fingerprint" in r.output

    show = runner.invoke(cli.app, ["ca", "show"])
    assert show.exit_code == 0, show.output
    assert "CLI Test EAP CA" in show.output  # the custom common_name round-tripped

    export = runner.invoke(cli.app, ["ca", "export"])
    assert export.exit_code == 0, export.output
    assert "BEGIN CERTIFICATE" in export.output


def test_cli_cert_issue_and_show(
    patch_client: None, app: Any, token: str, instance_data: dict[str, Any]
) -> None:
    _seed_via_api(app, token, instance_data)
    init = runner.invoke(
        cli.app, ["ca", "init", "--validity-days", "30", "--passphrase", PASSPHRASE]
    )
    assert init.exit_code == 0, init.output

    # `cert issue <name>` flattens --passphrase (and default fqdn) into the body.
    issue = runner.invoke(
        cli.app, ["cert", "issue", NAME, "--validity-days", "30", "--passphrase", PASSPHRASE]
    )
    assert issue.exit_code == 0, issue.output
    assert FQDN in issue.output  # SAN defaulted to the instance server_fqdn

    show = runner.invoke(cli.app, ["cert", "show", NAME])
    assert show.exit_code == 0, show.output
    assert FQDN in show.output


def test_cli_cert_issue_wrong_passphrase_errors(
    patch_client: None, app: Any, token: str, instance_data: dict[str, Any]
) -> None:
    # NEGATIVE: a wrong CA passphrase makes the CLI exit non-zero (server 422).
    _seed_via_api(app, token, instance_data)
    runner.invoke(cli.app, ["ca", "init", "--validity-days", "30", "--passphrase", PASSPHRASE])
    r = runner.invoke(cli.app, ["cert", "issue", NAME, "--passphrase", WRONG_PASSPHRASE])
    assert r.exit_code == 1
