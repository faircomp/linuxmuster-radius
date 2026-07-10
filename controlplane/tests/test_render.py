# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the pure config-render functions (no I/O, no Docker).

``render_clients_conf`` and ``render_ssid_policy`` interpolate model-validated
values into the FreeRADIUS config the container mounts read-only. The negative
tests here prove the config-injection guards (empty/multi-line secret, quote and
backslash escaping) fail closed even if a caller hands in a hand-built model.
"""

from __future__ import annotations

from typing import Any

import pytest

from lmnradius.models import Instance
from lmnradius.render import (
    _escape_config_string,
    render_clients_conf,
    render_ssid_policy,
)


def _inst(**over: Any) -> Instance:
    data: dict[str, Any] = {
        "name": "default-school",
        "realm": "LINUXMUSTER.LAN",
        "workgroup": "LINUXMUSTER",
        "server_fqdn": "radius.linuxmuster.lan",
        "ldap_server": "ldaps://dc.linuxmuster.lan",
        "ldap_base_dn": "DC=linuxmuster,DC=lan",
        "ldap_bind_dn": "CN=global-binduser,OU=Management,DC=linuxmuster,DC=lan",
        "client_subnets": ["10.0.0.0/16"],
        "ssids": [{"name": "pgw-lehrer", "allowed_group": "teachers", "vlan": 20}],
        "join_secret": "join.secret",
        "ldap_bind_secret": "ldap.secret",
        "radius_secret": "radius.secret",
        "image": "ghcr.io/faircomp/linuxmuster-radius:0.1.0",
    }
    data.update(over)
    return Instance(**data)


# ------------------------------------------------------------------- clients.conf


def test_clients_conf_one_block_per_subnet() -> None:
    inst = _inst(client_subnets=["10.1.0.0/16", "10.2.0.0/16"])
    out = render_clients_conf(inst, "s3cr3t")

    assert out.count("client ap-") == 2
    assert "client ap-1 {" in out
    assert "client ap-2 {" in out
    assert "ipaddr = 10.1.0.0/16" in out
    assert "ipaddr = 10.2.0.0/16" in out
    assert out.endswith("\n")


def test_clients_conf_embeds_secret_and_hardening() -> None:
    out = render_clients_conf(_inst(), "topsecret")
    assert "secret = topsecret" in out
    # message-authenticator required (Blast-RADIUS mitigation) + UDP
    assert "require_message_authenticator = yes" in out
    assert "proto = udp" in out


@pytest.mark.parametrize("bad", ["", "line1\nline2", "carriage\rreturn"])
def test_clients_conf_rejects_empty_or_multiline_secret(bad: str) -> None:
    # A newline in the secret would break out of the client{} block: config injection.
    with pytest.raises(ValueError):
        render_clients_conf(_inst(), bad)


# ------------------------------------------------------------------- ssid-policy


def test_ssid_policy_branches_if_then_elsif_then_else() -> None:
    inst = _inst(
        ssids=[
            {"name": "pgw-lehrer", "allowed_group": "teachers", "vlan": 20},
            {"name": "pgw-schueler", "allowed_group": "students", "vlan": 30},
        ]
    )
    out = render_ssid_policy(inst)

    assert 'if (&Called-Station-SSID == "pgw-lehrer") {' in out
    assert 'elsif (&Called-Station-SSID == "pgw-schueler") {' in out
    assert '&LDAP-Group == "teachers"' in out
    assert '&LDAP-Group == "students"' in out
    # dynamic VLAN attributes (RFC 3580)
    assert '&Tunnel-Private-Group-Id := "20"' in out
    assert '&Tunnel-Private-Group-Id := "30"' in out
    # a catch-all deny for any SSID not served here
    assert out.rstrip().endswith("else {\n    reject\n}")


def test_ssid_policy_without_vlan_uses_noop_not_vlan() -> None:
    inst = _inst(ssids=[{"name": "guest", "allowed_group": "wifi"}])
    out = render_ssid_policy(inst)
    assert "noop" in out
    assert "Tunnel-Type" not in out
    assert "Tunnel-Private-Group-Id" not in out


def test_ssid_policy_wrong_group_is_rejected() -> None:
    # Every SSID branch has an inner else{reject}: matching SSID, wrong group -> deny.
    out = render_ssid_policy(_inst())
    assert out.count("reject") >= 2  # inner wrong-group reject + final unknown-SSID reject


# ------------------------------------------------------------- escaping (defence in depth)


def test_escape_config_string_neutralises_quote_and_backslash() -> None:
    assert _escape_config_string('a"b') == 'a\\"b'
    assert _escape_config_string("a\\b") == "a\\\\b"
    assert _escape_config_string('x\\"y') == 'x\\\\\\"y'
