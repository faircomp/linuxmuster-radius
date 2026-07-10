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


def test_cli_create_defaults_image(patch_client: None, instance_data: dict[str, Any]) -> None:
    from lmnradius.models import DEFAULT_IMAGE

    r = runner.invoke(
        cli.app,
        [
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
