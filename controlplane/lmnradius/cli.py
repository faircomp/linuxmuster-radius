# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Typer CLI — a thin client of the control-plane REST API (no direct Docker access)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import httpx
import typer

from .config import load_settings

app = typer.Typer(
    help="linuxmuster-radius control-plane CLI (thin REST client).",
    no_args_is_help=True,
)

# Dedicated EAP-CA management (ADR-005): a self-contained sub-CA that signs ONLY
# the RADIUS EAP server cert — the trust anchor supplicants pin.
ca_app = typer.Typer(
    help="Manage the dedicated EAP certificate authority.",
    no_args_is_help=True,
)
app.add_typer(ca_app, name="ca")

cert_app = typer.Typer(
    help="Manage per-instance EAP server certificates.",
    no_args_is_help=True,
)
app.add_typer(cert_app, name="cert")


def _get_client() -> httpx.Client:
    """Build an HTTP client for the API from settings (localhost, bearer token)."""
    settings = load_settings()
    headers = {"Authorization": f"Bearer {settings.api_token}"} if settings.api_token else {}
    # Only skip TLS verification for a loopback API (self-signed localhost); the token
    # is a full-privilege credential, so verify certs for any off-host api_url.
    loopback = any(s in settings.api_url for s in ("://127.0.0.1", "://localhost", "://[::1]"))
    # `update` / `update-all` are health-gated server-side (up to ~90s per instance,
    # times the instance count), so cap only connect and let reads run as long as the
    # (bounded) server operation needs — otherwise the CLI aborts a working update.
    timeout = httpx.Timeout(30.0, connect=10.0, read=None)
    return httpx.Client(
        base_url=settings.api_url, headers=headers, timeout=timeout, verify=not loopback
    )


def _emit(resp: httpx.Response) -> None:
    """Print the response as pretty JSON; exit non-zero on HTTP error."""
    if resp.status_code >= 400:
        typer.secho(f"error {resp.status_code}: {resp.text}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if resp.status_code == 204 or not resp.content:
        typer.echo("ok")
        return
    try:
        typer.echo(json.dumps(resp.json(), indent=2, ensure_ascii=False))
    except ValueError:
        typer.echo(resp.text)


def _parse_ssid(spec: str) -> dict[str, Any]:
    """Parse one ``--ssid`` value ``name:group[:vlan]`` into an ``ssids[]`` entry.

    ``:`` is an unambiguous delimiter: neither an SSID name nor an AD group name
    may contain a colon (see the model's ``_SSID_RE`` / ``_GROUP_RE``), so a name
    or group carrying spaces/dots still splits correctly. Structural parsing only —
    the server re-validates name/group/vlan against the model.
    """
    parts = spec.split(":")
    if len(parts) not in (2, 3):
        raise typer.BadParameter(f"--ssid must be 'name:group' or 'name:group:vlan', got {spec!r}")
    entry: dict[str, Any] = {"name": parts[0], "allowed_group": parts[1]}
    if len(parts) == 3:
        try:
            entry["vlan"] = int(parts[2])
        except ValueError:
            raise typer.BadParameter(f"--ssid vlan must be an integer, got {parts[2]!r}") from None
    return entry


@app.command("list")
def list_() -> None:
    """List all instances."""
    with _get_client() as c:
        _emit(c.get("/v1/instances"))


@app.command()
def show(name: str) -> None:
    """Show one instance."""
    with _get_client() as c:
        _emit(c.get(f"/v1/instances/{name}"))


@app.command()
def create(
    name: str = typer.Option(..., help="instance name -> container lmnradius-<name>"),
    realm: str = typer.Option(..., help="Kerberos realm, UPPERCASE (e.g. LINUXMUSTER.LAN)"),
    workgroup: str = typer.Option(..., help="NetBIOS/workgroup, UPPERCASE (e.g. LINUXMUSTER)"),
    server_fqdn: str = typer.Option(..., help="FQDN == container hostname == EAP cert CN/SAN"),
    ldap_server: str = typer.Option(..., help="LDAP URI, ldap:// or ldaps://<host>(:port)"),
    ldap_base_dn: str = typer.Option(..., help="search base, e.g. DC=linuxmuster,DC=lan"),
    ldap_bind_dn: str = typer.Option(
        ..., help="bind DN, the existing global-binduser (CN=global-binduser,OU=Management,...)"
    ),
    client_subnet: list[str] = typer.Option(
        ...,
        "--client-subnet",
        help="AP-management subnet CIDR the APs (the NAS) send Access-Requests from; "
        "repeat per subnet. NOT the controller IP — that causes 'unknown client'.",
    ),
    ssid: list[str] = typer.Option(
        ...,
        "--ssid",
        help="SSID as 'name:group[:vlan]' (e.g. 'school-lehrer:school-teachers:20'); "
        "repeat per SSID. Parsed into the ssids[] policy (name, allowed_group, vlan).",
    ),
    join_secret: str = typer.Option(..., help="secret-file reference for the AD domain join"),
    ldap_bind_secret: str = typer.Option(
        ..., help="secret-file reference for the global-binduser password"
    ),
    radius_secret: str = typer.Option(
        ..., help="secret-file reference for the AP shared secret (clients.conf)"
    ),
    wifi_group: str = typer.Option("wifi", help="AD group every WLAN user must be in"),
    image: Optional[str] = typer.Option(
        None, help="data-plane image; omit to use the maintained pinned digest"
    ),
) -> None:
    """Create (and reconcile) an instance."""
    body: dict[str, Any] = {
        "name": name,
        "realm": realm,
        "workgroup": workgroup,
        "server_fqdn": server_fqdn,
        "ldap_server": ldap_server,
        "ldap_base_dn": ldap_base_dn,
        "ldap_bind_dn": ldap_bind_dn,
        "wifi_group": wifi_group,
        "client_subnets": client_subnet,
        "ssids": [_parse_ssid(s) for s in ssid],
        "join_secret": join_secret,
        "ldap_bind_secret": ldap_bind_secret,
        "radius_secret": radius_secret,
    }
    if image is not None:
        body["image"] = image
    with _get_client() as c:
        _emit(c.post("/v1/instances", json=body))


@app.command()
def rm(name: str) -> None:
    """Remove an instance (and its container)."""
    with _get_client() as c:
        _emit(c.delete(f"/v1/instances/{name}"))


@app.command()
def start(name: str) -> None:
    """Start the instance container."""
    with _get_client() as c:
        _emit(c.post(f"/v1/instances/{name}/start"))


@app.command()
def stop(name: str) -> None:
    """Stop the instance container."""
    with _get_client() as c:
        _emit(c.post(f"/v1/instances/{name}/stop"))


@app.command()
def restart(name: str) -> None:
    """Restart the instance container."""
    with _get_client() as c:
        _emit(c.post(f"/v1/instances/{name}/restart"))


@app.command()
def status(name: str) -> None:
    """Show container status for the instance."""
    with _get_client() as c:
        _emit(c.get(f"/v1/instances/{name}/status"))


def _log_params(
    tail: int, since: Optional[int], until: Optional[int], grep: Optional[str]
) -> dict[str, Any]:
    params: dict[str, Any] = {"tail": tail}
    if since is not None:
        params["since"] = since
    if until is not None:
        params["until"] = until
    if grep is not None:
        params["grep"] = grep
    return params


@app.command()
def logs(
    name: str,
    tail: int = typer.Option(100),
    since: Optional[int] = typer.Option(None, help="only lines after this Unix epoch second"),
    until: Optional[int] = typer.Option(None, help="only lines before this Unix epoch second"),
    grep: Optional[str] = typer.Option(None, help="substring filter"),
) -> None:
    """Show recent container log lines (radiusd), optional time/substring filter."""
    with _get_client() as c:
        _emit(c.get(f"/v1/instances/{name}/logs", params=_log_params(tail, since, until, grep)))


@app.command()
def update(
    name: str,
    image: Optional[str] = typer.Argument(
        None, help="new image; omit to update to the maintained pinned digest"
    ),
) -> None:
    """Digest-pinned update with health-check auto-rollback."""
    body = {} if image is None else {"image": image}
    with _get_client() as c:
        _emit(c.post(f"/v1/instances/{name}/update", json=body))


@app.command("update-all")
def update_all() -> None:
    """Lift every instance onto the maintained default image (health auto-rollback)."""
    with _get_client() as c:
        _emit(c.post("/v1/update-all"))


@app.command()
def rollback(name: str) -> None:
    """Roll the instance back to the last known-good image."""
    with _get_client() as c:
        _emit(c.post(f"/v1/instances/{name}/rollback"))


@app.command()
def health() -> None:
    """Check the control-plane API health (no auth required)."""
    with _get_client() as c:
        _emit(c.get("/v1/health"))


@app.command()
def reconcile() -> None:
    """Re-apply all stored instances (reconverge drift / restore on a fresh host)."""
    with _get_client() as c:
        _emit(c.post("/v1/reconcile"))


# ------------------------------------------------------------- dedicated EAP CA
@ca_app.command("init")
def ca_init(
    common_name: str = typer.Option(
        "linuxmuster-radius EAP CA", "--common-name", help="CA certificate subject CN"
    ),
    validity_days: int = typer.Option(3652, help="CA validity in days (~10y default)"),
    passphrase: str = typer.Option(
        ...,
        prompt=True,
        hide_input=True,
        confirmation_prompt=True,
        help="passphrase that encrypts the CA private key (prompted; never printed)",
    ),
) -> None:
    """Initialise the dedicated EAP CA (self-signed trust anchor)."""
    body = {
        "passphrase": passphrase,
        "common_name": common_name,
        "validity_days": validity_days,
    }
    with _get_client() as c:
        _emit(c.post("/v1/ca", json=body))


@ca_app.command("show")
def ca_show() -> None:
    """Show the EAP CA status (subject, serial, validity, fingerprint)."""
    with _get_client() as c:
        _emit(c.get("/v1/ca"))


@ca_app.command("export")
def ca_export(
    out: Optional[str] = typer.Option(
        None, "--out", help="write the CA cert PEM to this path (default: stdout)"
    ),
) -> None:
    """Export the CA certificate PEM (the trust anchor to pin on clients)."""
    with _get_client() as c:
        resp = c.get("/v1/ca/export")
    if resp.status_code >= 400:
        typer.secho(f"error {resp.status_code}: {resp.text}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if out is not None:
        Path(out).write_text(resp.text, encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(resp.text)


# --------------------------------------------------- per-instance EAP server certs
@cert_app.command("issue")
def cert_issue(
    name: str,
    fqdn: Optional[str] = typer.Option(
        None, "--fqdn", help="cert CN/SAN; defaults to the instance's server_fqdn"
    ),
    validity_days: int = typer.Option(1095, help="server-cert validity in days (~3y default)"),
    passphrase: str = typer.Option(
        ...,
        prompt=True,
        hide_input=True,
        help="CA passphrase to unlock the signing key (prompted; never printed)",
    ),
) -> None:
    """Issue (sign) the EAP server cert for an instance."""
    body: dict[str, Any] = {"passphrase": passphrase, "validity_days": validity_days}
    if fqdn is not None:
        body["fqdn"] = fqdn
    with _get_client() as c:
        _emit(c.post(f"/v1/instances/{name}/cert", json=body))


@cert_app.command("show")
def cert_show(name: str) -> None:
    """Show the EAP server-cert status for an instance."""
    with _get_client() as c:
        _emit(c.get(f"/v1/instances/{name}/cert"))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
