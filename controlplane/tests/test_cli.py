# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI (Typer) tests: drive the API via a TestClient-backed httpx client (fake docker).

The CLI is a thin REST client, so these tests monkeypatch ``cli._get_client`` to
return an authenticated TestClient bound to the in-memory app — no real Docker and
no live server. Read/lifecycle commands are exercised against an instance seeded
through the API so they stay decoupled from the exact ``create`` flag encoding.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from lmnradius import cli

runner = CliRunner()

NAME = "default-school"
GOOD_V2 = "ghcr.io/faircomp/linuxmuster-radius:0.2.0"


@pytest.fixture
def patch_client(monkeypatch: pytest.MonkeyPatch, app: Any, token: str) -> None:
    """Make cli._get_client() return a fresh authenticated TestClient for `app`."""

    def factory() -> TestClient:
        tc = TestClient(app)
        tc.headers.update({"Authorization": f"Bearer {token}"})
        return tc

    monkeypatch.setattr(cli, "_get_client", factory)


def _seed(app: Any, token: str, instance_data: dict[str, Any]) -> None:
    """Create an instance directly through the API (bypasses CLI create encoding)."""
    tc = TestClient(app)
    tc.headers.update({"Authorization": f"Bearer {token}"})
    resp = tc.post("/v1/instances", json=instance_data)
    assert resp.status_code == 201, resp.text


def test_cli_lifecycle_read_commands(
    patch_client: None, app: Any, token: str, instance_data: dict[str, Any]
) -> None:
    _seed(app, token, instance_data)

    assert NAME in runner.invoke(cli.app, ["list"]).output
    assert runner.invoke(cli.app, ["show", NAME]).exit_code == 0
    assert runner.invoke(cli.app, ["status", NAME]).exit_code == 0
    assert runner.invoke(cli.app, ["stop", NAME]).exit_code == 0
    assert runner.invoke(cli.app, ["start", NAME]).exit_code == 0
    assert runner.invoke(cli.app, ["restart", NAME]).exit_code == 0
    assert runner.invoke(cli.app, ["logs", NAME]).exit_code == 0


def test_cli_logs_renders_plain_lines(
    patch_client: None, app: Any, token: str, instance_data: dict[str, Any]
) -> None:
    _seed(app, token, instance_data)

    # Default: raw log text with real newlines, not a JSON blob with escaped \n.
    r = runner.invoke(cli.app, ["logs", NAME])
    assert r.exit_code == 0, r.output
    assert '{"logs"' not in r.output
    assert "\\n" not in r.output
    assert "radiusd: Ready to process requests" in r.output
    assert r.output.count("\n") >= 2  # multiple real lines

    # --json keeps the wrapped form for scripting.
    rj = runner.invoke(cli.app, ["logs", NAME, "--json"])
    assert rj.exit_code == 0, rj.output
    assert '"logs"' in rj.output


def test_cli_update_and_rollback(
    patch_client: None, app: Any, token: str, instance_data: dict[str, Any]
) -> None:
    _seed(app, token, instance_data)

    r = runner.invoke(cli.app, ["update", NAME, GOOD_V2])
    assert r.exit_code == 0, r.output
    assert '"updated": true' in r.output

    r = runner.invoke(cli.app, ["rollback", NAME])
    assert r.exit_code == 0, r.output
    assert instance_data["image"] in r.output

    assert runner.invoke(cli.app, ["rm", NAME]).exit_code == 0
    # gone now -> show fails (negative)
    assert runner.invoke(cli.app, ["show", NAME]).exit_code == 1


def test_cli_update_all(
    patch_client: None, app: Any, token: str, instance_data: dict[str, Any]
) -> None:
    from lmnradius.models import DEFAULT_IMAGE

    # seed on a non-default image so update-all lifts it
    _seed(app, token, {**instance_data, "image": "ghcr.io/faircomp/linuxmuster-radius:0.0.9"})
    r = runner.invoke(cli.app, ["update-all"])
    assert r.exit_code == 0, r.output
    assert DEFAULT_IMAGE in runner.invoke(cli.app, ["show", NAME]).output


def test_cli_reconcile(
    patch_client: None, app: Any, token: str, instance_data: dict[str, Any]
) -> None:
    _seed(app, token, instance_data)
    r = runner.invoke(cli.app, ["reconcile"])
    assert r.exit_code == 0, r.output
    assert NAME in r.output


# ------------------------------------------------------------------------- negatives


def test_cli_show_missing_is_error(patch_client: None) -> None:
    r = runner.invoke(cli.app, ["show", "does-not-exist"])
    assert r.exit_code == 1


def test_cli_status_missing_is_error(patch_client: None) -> None:
    r = runner.invoke(cli.app, ["status", "does-not-exist"])
    assert r.exit_code == 1


def test_cli_health_no_auth(monkeypatch: pytest.MonkeyPatch, app: Any) -> None:
    # health needs no token; the CLI must reach it even with an unauthenticated client.
    monkeypatch.setattr(cli, "_get_client", lambda: TestClient(app))
    r = runner.invoke(cli.app, ["health"])
    assert r.exit_code == 0
    assert "ok" in r.output


# ------------------------------------------------------------------------- create
# NOTE: the exact `create` flag encoding is owned by the parallel-written cli.py.
# These assume the field->--kebab-flag convention of the squid template, a
# repeatable --client-subnet, an optional --image (defaults to DEFAULT_IMAGE), and
# a repeatable --ssid "<name>:<allowed_group>:<vlan>" (vlan optional).


def _create_args(instance_data: dict[str, Any], *extra: str) -> list[str]:
    return [
        "create",
        "--name",
        "schule-c",
        "--realm",
        instance_data["realm"],
        "--workgroup",
        instance_data["workgroup"],
        "--server-fqdn",
        "radius-c.linuxmuster.lan",
        "--ldap-server",
        instance_data["ldap_server"],
        "--ldap-base-dn",
        instance_data["ldap_base_dn"],
        "--ldap-bind-dn",
        instance_data["ldap_bind_dn"],
        "--client-subnet",
        "10.3.0.0/16",
        "--ssid",
        "c-lehrer:teachers:20",
        "--join-secret",
        "c-join.secret",
        "--ldap-bind-secret",
        "c-ldap.secret",
        "--radius-secret",
        "c-radius.secret",
        *extra,
    ]


def test_cli_create_defaults_image(
    patch_client: None, instance_data: dict[str, Any], tmp_path: Any, dc_ca_pem: str
) -> None:
    from lmnradius.models import DEFAULT_IMAGE

    ca_file = tmp_path / "cacert.pem"
    ca_file.write_text(dc_ca_pem)
    r = runner.invoke(
        cli.app,
        [
            "create",
            "--name",
            "schule-c",
            "--ldap-ca",
            str(ca_file),
            "--realm",
            instance_data["realm"],
            "--workgroup",
            instance_data["workgroup"],
            "--server-fqdn",
            "radius-c.linuxmuster.lan",
            "--ldap-server",
            instance_data["ldap_server"],
            "--ldap-base-dn",
            instance_data["ldap_base_dn"],
            "--ldap-bind-dn",
            instance_data["ldap_bind_dn"],
            "--client-subnet",
            "10.3.0.0/16",
            "--client-subnet",
            "10.4.0.0/16",
            "--ssid",
            "c-lehrer:teachers:20",
            "--ssid",
            "c-gast:wifi",
            "--join-secret",
            "c-join.secret",
            "--ldap-bind-secret",
            "c-ldap.secret",
            "--radius-secret",
            "c-radius.secret",
        ],
    )
    assert r.exit_code == 0, r.output

    show = runner.invoke(cli.app, ["show", "schule-c"]).output
    assert DEFAULT_IMAGE in show  # image defaulted (no --image given)
    assert "10.3.0.0/16" in show and "10.4.0.0/16" in show
    assert "c-lehrer" in show and "c-gast" in show

    assert runner.invoke(cli.app, ["rm", "schule-c"]).exit_code == 0


# ------------------------------------------------------- LDAPS CA pinning (7.3.1)


def test_cli_create_ldaps_without_ca_is_refused(
    patch_client: None, instance_data: dict[str, Any]
) -> None:
    r = runner.invoke(cli.app, _create_args(instance_data))
    assert r.exit_code == 1, r.output
    assert "error 422" in r.output
    assert "--ldap-ca" in r.output
    assert runner.invoke(cli.app, ["show", "schule-c"]).exit_code == 1  # not created


def test_cli_create_with_ldap_ca_file(
    patch_client: None, instance_data: dict[str, Any], tmp_path: Any, dc_ca_pem: str, settings: Any
) -> None:
    import os

    ca_file = tmp_path / "cacert.pem"
    ca_file.write_text(dc_ca_pem)
    r = runner.invoke(cli.app, _create_args(instance_data, "--ldap-ca", str(ca_file)))
    assert r.exit_code == 0, r.output
    assert '"ldap_ca": "ldap-ca.pem"' in r.output
    assert "trust_anchor" in r.output
    assert os.path.isfile(os.path.join(settings.certs_dir, "schule-c", "ldap-ca.pem"))
    # a pinned instance produces no warning in list
    r = runner.invoke(cli.app, ["list"])
    assert r.exit_code == 0
    assert "LDAPS unverified" not in r.output


def test_cli_create_ldap_ca_unreadable_file(
    patch_client: None, instance_data: dict[str, Any], tmp_path: Any
) -> None:
    r = runner.invoke(cli.app, _create_args(instance_data, "--ldap-ca", str(tmp_path / "nope.pem")))
    assert r.exit_code == 2
    assert "cannot read" in r.output


def test_cli_create_ldap_ca_and_tofu_exclusive(
    patch_client: None, instance_data: dict[str, Any], tmp_path: Any
) -> None:
    f = tmp_path / "x.pem"
    f.write_text("x")
    r = runner.invoke(cli.app, _create_args(instance_data, "--ldap-ca", str(f), "--ldap-ca-tofu"))
    assert r.exit_code == 2
    assert "mutually exclusive" in r.output


def _fake_s_client(pem: str) -> Any:
    """A subprocess.run stand-in returning what `openssl s_client -showcerts` prints."""
    import subprocess

    real_run = subprocess.run

    def run(cmd: list[str], **kwargs: Any) -> Any:
        if cmd[:1] != ["openssl"]:
            return real_run(cmd, **kwargs)  # the store's git calls share the module
        assert cmd[:2] == ["openssl", "s_client"]
        assert "-showcerts" in cmd and "-connect" in cmd
        run.calls.append(cmd)  # type: ignore[attr-defined]
        out = "CONNECTED(00000003)\n---\nCertificate chain\n 0 s:CN=dc\n" + pem + "---\nDONE\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    run.calls = []  # type: ignore[attr-defined]
    return run


def test_cli_create_tofu_prints_fingerprint_and_warns(
    patch_client: None, instance_data: dict[str, Any], monkeypatch: Any, dc_ca_pem: str
) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    fake = _fake_s_client(dc_ca_pem)
    monkeypatch.setattr(cli.subprocess, "run", fake)
    r = runner.invoke(cli.app, _create_args(instance_data, "--ldap-ca-tofu"))
    assert r.exit_code == 0, r.output
    # fetched host:port from --ldap-server
    assert "dc.linuxmuster.lan:636" in " ".join(fake.calls[0])
    fp = x509.load_pem_x509_certificate(dc_ca_pem.encode()).fingerprint(hashes.SHA256()).hex()
    assert fp in r.output
    assert "TRUST ON FIRST USE" in r.output
    assert "trust anchor" in r.output
    assert '"ldap_ca": "ldap-ca.pem"' in r.output


def test_cli_tofu_leaf_only_warns_about_renewal(
    patch_client: None, instance_data: dict[str, Any], monkeypatch: Any, leaf_cert_pem: str
) -> None:
    leaf = leaf_cert_pem
    monkeypatch.setattr(cli.subprocess, "run", _fake_s_client(leaf))
    r = runner.invoke(cli.app, _create_args(instance_data, "--ldap-ca-tofu"))
    assert r.exit_code == 0, r.output
    assert "end-entity certificate" in r.output
    assert "FAILS CLOSED" in r.output


def test_cli_tofu_no_certificate_is_error(
    patch_client: None, instance_data: dict[str, Any], monkeypatch: Any
) -> None:
    import subprocess

    real_run = subprocess.run

    def run(cmd: list[str], **kwargs: Any) -> Any:
        if cmd[:1] != ["openssl"]:
            return real_run(cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="connect: Connection refused")

    monkeypatch.setattr(cli.subprocess, "run", run)
    r = runner.invoke(cli.app, _create_args(instance_data, "--ldap-ca-tofu"))
    assert r.exit_code == 1
    assert "no certificate received" in r.output
    assert "Connection refused" in r.output


def test_cli_tofu_needs_ldaps(patch_client: None, instance_data: dict[str, Any]) -> None:
    args = _create_args(instance_data, "--ldap-ca-tofu")
    args[args.index("--ldap-server") + 1] = "ldap://dc.linuxmuster.lan"
    r = runner.invoke(cli.app, args)
    assert r.exit_code == 2
    assert "ldaps://" in r.output


def _seed_legacy(store: Any, instance_data: dict[str, Any]) -> None:
    from lmnradius.models import Instance

    store.put(Instance(**instance_data))  # no ldap_ca -> as written by 7.3.0


def test_cli_list_and_health_warn_ldaps_unverified(
    patch_client: None, store: Any, instance_data: dict[str, Any]
) -> None:
    _seed_legacy(store, instance_data)
    r = runner.invoke(cli.app, ["list"])
    assert r.exit_code == 0, r.output  # a warning, not a failure
    assert "LDAPS unverified" in r.output
    assert f"lmnradius set-ldap-ca {NAME}" in r.output
    r = runner.invoke(cli.app, ["health"])
    assert r.exit_code == 0, r.output
    assert '"status": "ok"' in r.output
    assert "LDAPS unverified" in r.output


def test_cli_set_ldap_ca_clears_warning(
    patch_client: None, store: Any, instance_data: dict[str, Any], tmp_path: Any, dc_ca_pem: str
) -> None:
    _seed_legacy(store, instance_data)
    ca_file = tmp_path / "cacert.pem"
    ca_file.write_text(dc_ca_pem)
    r = runner.invoke(cli.app, ["set-ldap-ca", NAME, "--ldap-ca", str(ca_file)])
    assert r.exit_code == 0, r.output
    assert '"ldap_ca": "ldap-ca.pem"' in r.output
    for cmd in (["list"], ["health"]):
        r = runner.invoke(cli.app, cmd)
        assert r.exit_code == 0
        assert "LDAPS unverified" not in r.output


def test_cli_set_ldap_ca_tofu_uses_stored_ldap_server(
    patch_client: None, store: Any, instance_data: dict[str, Any], monkeypatch: Any, dc_ca_pem: str
) -> None:
    _seed_legacy(store, instance_data)
    fake = _fake_s_client(dc_ca_pem)
    monkeypatch.setattr(cli.subprocess, "run", fake)
    r = runner.invoke(cli.app, ["set-ldap-ca", NAME, "--ldap-ca-tofu"])
    assert r.exit_code == 0, r.output
    assert "dc.linuxmuster.lan:636" in " ".join(fake.calls[0])
    assert "TRUST ON FIRST USE" in r.output


def test_cli_set_ldap_ca_requires_an_option(
    patch_client: None, store: Any, instance_data: dict[str, Any]
) -> None:
    _seed_legacy(store, instance_data)
    assert runner.invoke(cli.app, ["set-ldap-ca", NAME]).exit_code == 2
    assert runner.invoke(cli.app, ["set-ldap-ca", "nope", "--ldap-ca-tofu"]).exit_code == 1


# ---------------------------------------------------- honest status / domain leave


def test_cli_create_exits_nonzero_when_crash_looping(
    patch_client: None,
    instance_data: dict[str, Any],
    docker: Any,
    monkeypatch: Any,
    tmp_path: Any,
    dc_ca_pem: str,
) -> None:
    real_status = docker.status

    def crashing(name: str) -> dict[str, Any]:
        st = real_status(name)
        if st["exists"]:
            st.update(
                running=False,
                crash_looping=True,
                restart_count=3,
                health="starting",
                last_error="FATAL: FreeRADIUS configuration check ('radiusd -XC') failed",
            )
        return st

    monkeypatch.setattr(docker, "status", crashing)
    ca_file = tmp_path / "cacert.pem"
    ca_file.write_text(dc_ca_pem)
    r = runner.invoke(cli.app, _create_args(instance_data, "--ldap-ca", str(ca_file)))
    assert r.exit_code == 1, r.output
    assert "crash-looping" in r.output
    assert "radiusd -XC" in r.output
    assert runner.invoke(cli.app, ["show", "schule-c"]).exit_code == 0  # saved nonetheless
    r = runner.invoke(cli.app, ["reconcile"])
    assert r.exit_code == 1
    assert "crash-looping" in r.output


def test_cli_rm_reports_failed_domain_leave(
    patch_client: None, app: Any, token: str, instance_data: dict[str, Any], docker: Any
) -> None:
    _seed(app, token, instance_data)
    docker.leave_ok = False
    r = runner.invoke(cli.app, ["rm", NAME])
    assert r.exit_code == 1, r.output
    assert "did NOT leave the domain" in r.output
    assert "samba-tool computer delete" in r.output
    assert runner.invoke(cli.app, ["show", NAME]).exit_code == 1  # removed locally
