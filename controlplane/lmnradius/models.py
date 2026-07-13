# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pydantic models describing a managed FreeRADIUS instance.

All externally-supplied string fields are strictly validated: they flow into
on-disk filenames, Docker container/volume names, bind-mount source paths and,
via :mod:`lmnradius.render`, into the rendered FreeRADIUS config (clients.conf
and the inner-tunnel ssid-policy) — so a lax field is a path-traversal or
config-injection sink. Fail closed at the API boundary.
"""

from __future__ import annotations

import ipaddress
import re

from pydantic import BaseModel, computed_field, field_validator

# instance name -> filename + container/volume name: no '/', '..' (case allowed).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,30}$")
# Kerberos realm, UPPERCASE dotted form (e.g. LINUXMUSTER.SCHULE.DE).
_REALM_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,254}$")
# NetBIOS short domain / workgroup, UPPERCASE (e.g. LINUXMUSTER).
_WORKGROUP_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,20}$")
# FQDN (server_fqdn == container hostname == EAP cert CN; also the LDAP host).
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)[a-zA-Z0-9]([a-zA-Z0-9-]{0,62})(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,62}))*$"
)
# AD group names (wifi_group, ssid allowed_group): may contain spaces/dots.
_GROUP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
# Conservative LDAP DN (ldap_base_dn, ldap_bind_dn): letters/digits and
# = , . _ - / and space. No newlines, no parens -> not an LDAP-filter injection
# sink and safe to interpolate into the rendered ldap module.
_DN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9=,._ /-]{0,254}$")
# LDAP server URI: ldap:// or ldaps:// + FQDN/IP + optional :port.
_LDAP_URI_RE = re.compile(
    r"^ldaps?://"
    r"[a-zA-Z0-9]([a-zA-Z0-9-]{0,62})(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,62}))*"
    r"(:\d+)?$"
)
# secret FILENAMES (join_secret, ldap_bind_secret, radius_secret) -> bind-mount
# source basename inside secrets_dir: no path separators, plus an explicit '..'
# reject (a '..' never matches this class, but reject it loudly all the same).
_SECRET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
# SSID name (Called-Station-SSID literal): printable, may contain spaces/dots.
_SSID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,31}$")
# image MUST carry an explicit :tag or @sha256:<digest> — never a bare repo
# (a bare repo makes docker-py pull EVERY tag = disk-exhaustion DoS).
_IMAGE_RE = re.compile(
    r"^[a-z0-9][a-z0-9._/:-]*(?::[A-Za-z0-9][A-Za-z0-9._-]{0,127}|@sha256:[a-f0-9]{64})$"
)

# Default data-plane image for new/updated instances, so callers need not pass
# --image. Pinned to an immutable ``@sha256:<digest>`` — the data-plane image built and
# published to GHCR by the build-image workflow (verified end-to-end against a real DC);
# Renovate keeps it current (same rationale as linuxmuster-squid). The image validator
# requires an explicit :tag or @sha256 and rejects a bare repo either way.
DEFAULT_IMAGE = "ghcr.io/faircomp/linuxmuster-radius@sha256:15110604361b8bf617a218a4bd9b4384ca9de098d56983926cc601742edf098c"


class SSID(BaseModel):
    """One broadcast SSID served by an instance, with its role gate + VLAN.

    ``name`` is matched against ``&Called-Station-SSID`` in the inner tunnel;
    ``allowed_group`` is the AD group whose members may use it; ``vlan``, if set,
    is the RADIUS-assigned dynamic VLAN (RFC 3580) returned on the Access-Accept.
    """

    name: str
    allowed_group: str
    vlan: int | None = None

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        if not _SSID_RE.match(v):
            raise ValueError("ssid name must match ^[A-Za-z0-9][A-Za-z0-9._ -]{0,31}$")
        return v

    @field_validator("allowed_group")
    @classmethod
    def _v_allowed_group(cls, v: str) -> str:
        if not _GROUP_RE.match(v):
            raise ValueError("allowed_group must match ^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
        return v

    @field_validator("vlan")
    @classmethod
    def _v_vlan(cls, v: int | None) -> int | None:
        if v is not None and not 1 <= v <= 4094:
            raise ValueError("vlan must be between 1 and 4094")
        return v


class Instance(BaseModel):
    """A single FreeRADIUS deployment, one per linuxmuster server."""

    name: str
    realm: str
    workgroup: str
    server_fqdn: str
    ldap_server: str
    ldap_base_dn: str
    ldap_bind_dn: str
    wifi_group: str = "wifi"
    client_subnets: list[str]
    ssids: list[SSID]
    join_secret: str
    ldap_bind_secret: str
    radius_secret: str
    image: str = DEFAULT_IMAGE

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("name must match ^[A-Za-z0-9][A-Za-z0-9-]{0,30}$ (no '/', '..')")
        return v

    @field_validator("realm")
    @classmethod
    def _v_realm(cls, v: str) -> str:
        if not _REALM_RE.match(v):
            raise ValueError("realm must be UPPERCASE ^[A-Z0-9][A-Z0-9.-]{0,254}$")
        return v

    @field_validator("workgroup")
    @classmethod
    def _v_workgroup(cls, v: str) -> str:
        if not _WORKGROUP_RE.match(v):
            raise ValueError("workgroup must be UPPERCASE ^[A-Z0-9][A-Z0-9-]{0,20}$")
        return v

    @field_validator("server_fqdn")
    @classmethod
    def _v_server_fqdn(cls, v: str) -> str:
        if not _HOST_RE.match(v):
            raise ValueError("server_fqdn must be a valid FQDN")
        return v

    @field_validator("ldap_server")
    @classmethod
    def _v_ldap_server(cls, v: str) -> str:
        if not _LDAP_URI_RE.match(v):
            raise ValueError("ldap_server must match ^ldaps?://<host>(:<port>)?$")
        return v

    @field_validator("ldap_base_dn", "ldap_bind_dn")
    @classmethod
    def _v_dn(cls, v: str) -> str:
        if not _DN_RE.match(v):
            raise ValueError(
                "ldap DN must match ^[A-Za-z0-9][A-Za-z0-9=,._ /-]{0,254}$ (no newlines or parens)"
            )
        return v

    @field_validator("wifi_group")
    @classmethod
    def _v_wifi_group(cls, v: str) -> str:
        if not _GROUP_RE.match(v):
            raise ValueError("wifi_group must match ^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
        return v

    @field_validator("client_subnets")
    @classmethod
    def _v_client_subnets(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("client_subnets must list at least one AP-management CIDR")
        for cidr in v:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid CIDR {cidr!r}") from exc
        return v

    @field_validator("ssids")
    @classmethod
    def _v_ssids(cls, v: list[SSID]) -> list[SSID]:
        if not v:
            raise ValueError("ssids must list at least one SSID")
        return v

    @field_validator("join_secret", "ldap_bind_secret", "radius_secret")
    @classmethod
    def _v_secret(cls, v: str) -> str:
        if ".." in v or not _SECRET_RE.match(v):
            raise ValueError(
                "secret filename must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ "
                "(no path separators or '..')"
            )
        return v

    @field_validator("image")
    @classmethod
    def _v_image(cls, v: str) -> str:
        if not _IMAGE_RE.match(v):
            raise ValueError("image must carry an explicit :tag or @sha256:<digest> (no bare repo)")
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def container_name(self) -> str:
        """Docker container name, e.g. ``lmnradius-default-school``."""
        return f"lmnradius-{self.name}"


class InstancePatch(BaseModel):
    """Partial update for ``PATCH /v1/instances/{name}``.

    ``name`` is the instance identity and is intentionally NOT patchable (a rename
    would orphan the old container/file). Everything else may change; the API
    re-validates the merged result against :class:`Instance`.
    """

    realm: str | None = None
    workgroup: str | None = None
    server_fqdn: str | None = None
    ldap_server: str | None = None
    ldap_base_dn: str | None = None
    ldap_bind_dn: str | None = None
    wifi_group: str | None = None
    client_subnets: list[str] | None = None
    ssids: list[SSID] | None = None
    join_secret: str | None = None
    ldap_bind_secret: str | None = None
    radius_secret: str | None = None
    image: str | None = None


class UpdateRequest(BaseModel):
    """Body for ``POST /v1/instances/{name}/update``.

    ``image`` may be omitted to update to the maintained :data:`DEFAULT_IMAGE`.
    """

    image: str = DEFAULT_IMAGE

    @field_validator("image")
    @classmethod
    def _v_image(cls, v: str) -> str:
        if not _IMAGE_RE.match(v):
            raise ValueError("image must carry an explicit :tag or @sha256:<digest> (no bare repo)")
        return v


class CaInitRequest(BaseModel):
    """Body for ``POST /v1/ca`` — initialise the dedicated EAP CA (ADR-005).

    ``passphrase`` encrypts the CA private key at rest and is required again to
    sign each server cert; it is never logged or persisted in an instance record.
    """

    passphrase: str
    common_name: str = "linuxmuster-radius EAP CA"
    validity_days: int = 3652  # ~10y

    @field_validator("passphrase")
    @classmethod
    def _v_passphrase(cls, v: str) -> str:
        if not v:
            raise ValueError("passphrase must not be empty")
        return v

    @field_validator("validity_days")
    @classmethod
    def _v_validity_days(cls, v: int) -> int:
        if not 1 <= v <= 7305:  # up to ~20y
            raise ValueError("validity_days must be between 1 and 7305")
        return v


class CertIssueRequest(BaseModel):
    """Body for ``POST /v1/instances/{name}/cert`` — sign the EAP server cert.

    ``fqdn`` defaults (in the API) to the instance's ``server_fqdn``;
    ``passphrase`` unlocks the CA signing key and is never logged.
    """

    passphrase: str
    fqdn: str | None = None
    validity_days: int = 1095  # ~3y

    @field_validator("passphrase")
    @classmethod
    def _v_passphrase(cls, v: str) -> str:
        if not v:
            raise ValueError("passphrase must not be empty")
        return v

    @field_validator("fqdn")
    @classmethod
    def _v_fqdn(cls, v: str | None) -> str | None:
        if v is not None and not _HOST_RE.match(v):
            raise ValueError("fqdn must be a valid FQDN")
        return v

    @field_validator("validity_days")
    @classmethod
    def _v_validity_days(cls, v: int) -> int:
        if not 1 <= v <= 3653:  # up to ~10y
            raise ValueError("validity_days must be between 1 and 3653")
        return v
