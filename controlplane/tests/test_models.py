# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Model tests: derived properties, defaults, and the strict pydantic boundary.

Every externally-supplied string flows into on-disk filenames, Docker
container/volume names, bind-mount source paths and the rendered FreeRADIUS
config -> a lax field is a path-traversal or config-injection sink. The negative
tests below are MANDATORY: they prove the boundary fails closed.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from lmnradius.models import (
    DEFAULT_IMAGE,
    SSID,
    Instance,
    InstancePatch,
    UpdateRequest,
)


def _base(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "default-school",
        "realm": "LINUXMUSTER.LAN",
        "workgroup": "LINUXMUSTER",
        "server_fqdn": "radius.linuxmuster.lan",
        "ldap_server": "ldaps://dc.linuxmuster.lan",
        "ldap_base_dn": "DC=linuxmuster,DC=lan",
        "ldap_bind_dn": "CN=global-binduser,OU=Management,OU=GLOBAL,DC=linuxmuster,DC=lan",
        "client_subnets": ["10.0.0.0/16"],
        "ssids": [{"name": "pgw-lehrer", "allowed_group": "teachers", "vlan": 20}],
        "join_secret": "join.secret",
        "ldap_bind_secret": "ldap.secret",
        "radius_secret": "radius.secret",
        "image": "ghcr.io/faircomp/linuxmuster-radius:0.1.0",
    }
    data.update(over)
    return data


# --------------------------------------------------------------- derived / defaults


def test_container_name_is_prefixed(instance: Instance) -> None:
    assert instance.container_name == "lmnradius-default-school"


def test_container_name_tracks_name() -> None:
    inst = Instance(**_base(name="schuleB"))
    assert inst.name == "schuleB"
    assert inst.container_name == "lmnradius-schuleB"


def test_defaults_applied() -> None:
    inst = Instance(**{k: v for k, v in _base().items() if k not in ("image",)})
    assert inst.wifi_group == "wifi"
    assert inst.image == DEFAULT_IMAGE
    assert "linuxmuster-radius" in inst.image


def test_ssids_parsed_into_models(instance: Instance) -> None:
    assert all(isinstance(s, SSID) for s in instance.ssids)
    assert instance.ssids[0].name == "pgw-lehrer"
    assert instance.ssids[0].vlan == 20


def test_ssid_vlan_optional() -> None:
    inst = Instance(**_base(ssids=[{"name": "guest", "allowed_group": "wifi"}]))
    assert inst.ssids[0].vlan is None


def test_patch_all_fields_optional() -> None:
    patch = InstancePatch()
    assert patch.model_dump(exclude_unset=True) == {}


def test_patch_partial() -> None:
    patch = InstancePatch(wifi_group="wlan")
    assert patch.model_dump(exclude_unset=True) == {"wifi_group": "wlan"}


def test_patch_has_no_name_field() -> None:
    # name is the instance identity and must NOT be patchable (a rename would
    # orphan the old container/file).
    assert "name" not in InstancePatch.model_fields


def test_update_request_defaults_to_pinned_image() -> None:
    assert UpdateRequest().image == DEFAULT_IMAGE


def test_instance_accepts_digest_pinned_image() -> None:
    Instance(**_base(image="ghcr.io/faircomp/linuxmuster-radius@sha256:" + "a" * 64))


# ---------------------------------------------------------------- NEGATIVE (boundary)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        # name -> filename + container/volume name: no '/', '..', no leading '-'
        ("name", "../../../etc/cron.d/x"),
        ("name", "a/b"),
        ("name", "a b"),
        ("name", "-leading-dash"),
        # realm must be UPPERCASE dotted Kerberos form
        ("realm", "linuxmuster.lan"),
        ("realm", "BAD REALM"),
        # workgroup UPPERCASE NetBIOS
        ("workgroup", "linuxmuster"),
        ("workgroup", "WORK GROUP"),
        # server_fqdn must be a valid FQDN (EAP cert CN)
        ("server_fqdn", "bad host name"),
        ("server_fqdn", "-nope.example"),
        # ldap_server must be ldap:// or ldaps:// URI
        ("ldap_server", "http://dc.example"),
        ("ldap_server", "dc.example.lan"),
        # ldap DNs: no parens (LDAP-filter injection), no newlines
        ("ldap_base_dn", "DC=x)(uid=*"),
        ("ldap_base_dn", "DC=x\nDC=y"),
        ("ldap_bind_dn", "CN=a(b),DC=x"),
        # wifi_group: no path separators
        ("wifi_group", "bad/group"),
        # client_subnets: non-empty list of valid CIDRs
        ("client_subnets", []),
        ("client_subnets", ["not-a-cidr"]),
        ("client_subnets", ["10.0.0.0/16", "nope"]),
        # ssids: at least one
        ("ssids", []),
        # secret filenames: no '..', no path separators
        ("join_secret", "../../../../etc/shadow"),
        ("join_secret", "sub/dir.secret"),
        ("ldap_bind_secret", "..bad"),
        ("radius_secret", "a/b"),
        # image MUST carry an explicit :tag or @sha256 (bare repo = pull-all-tags DoS)
        ("image", "ubuntu"),
        ("image", "registry.local/radius"),
    ],
)
def test_instance_rejects_bad_field(field: str, bad: Any) -> None:
    with pytest.raises(ValidationError):
        Instance(**_base(**{field: bad}))


def test_instance_accepts_good() -> None:
    Instance(**_base())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "bad/ssid", "allowed_group": "teachers"},
        {"name": 'has"quote', "allowed_group": "teachers"},
        {"name": "ok", "allowed_group": "bad/group"},
        {"name": "ok", "allowed_group": "teachers", "vlan": 0},
        {"name": "ok", "allowed_group": "teachers", "vlan": 4095},
        {"name": "ok", "allowed_group": "teachers", "vlan": -1},
    ],
)
def test_ssid_rejects_bad_field(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SSID(**kwargs)


def test_ssid_accepts_valid_vlan_bounds() -> None:
    assert SSID(name="a", allowed_group="teachers", vlan=1).vlan == 1
    assert SSID(name="a", allowed_group="teachers", vlan=4094).vlan == 4094


def test_update_request_rejects_bare_repo() -> None:
    with pytest.raises(ValidationError):
        UpdateRequest(image="ubuntu")
    UpdateRequest(image="ghcr.io/faircomp/linuxmuster-radius:0.2.0")  # tag ok
