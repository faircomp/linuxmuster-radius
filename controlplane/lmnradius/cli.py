# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Typer CLI — a thin client of the control-plane REST API (no direct Docker access)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

import httpx
import typer

from .ca import describe_pem_bundle
from .config import load_settings
from .models import is_ldaps, ldap_url_parts

_PEM_BLOCK_RE = re.compile(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----\n?", re.DOTALL)
_LDAP_CA_HELP = (
    "PEM file with the CA that signed the DC's LDAPS certificate (on a linuxmuster.net "
    "server: /etc/linuxmuster/ssl/cacert.pem). Mandatory for ldaps://."
)
_LDAP_CA_TOFU_HELP = (
    "TRUST ON FIRST USE instead of --ldap-ca: fetch the certificate chain the DC serves "
    "now (openssl s_client), print its SHA-256 fingerprints and pin it. Only when the CA "
    "file is unobtainable; verify the fingerprint out of band afterwards."
)

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


def _warn(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.YELLOW, err=True)


def _ldaps_warnings(instances: list[dict[str, Any]]) -> int:
    """Print one 'LDAPS unverified' warning per ldaps:// instance without a pinned CA
    (records created before 7.3.1); returns the number of warnings."""
    count = 0
    for inst in instances:
        server = str(inst.get("ldap_server", ""))
        if is_ldaps(server) and not inst.get("ldap_ca"):
            count += 1
            _warn(
                f"WARNING: instance '{inst.get('name')}': LDAPS unverified — the certificate of "
                f"{server} is NOT checked (no CA pinned); an on-path attacker between this host "
                "and the DC can spoof the group lookup (role gate / VLAN). Fix: "
                f"lmnradius set-ldap-ca {inst.get('name')} --ldap-ca /path/to/cacert.pem "
                "(the DC's CA; on a linuxmuster.net server /etc/linuxmuster/ssl/cacert.pem)"
            )
    return count


def _status_problem(st: dict[str, Any]) -> str | None:
    """Human-readable reason if a container status is not a running one, else None."""
    if not st.get("exists"):
        return "no container"
    if st.get("crash_looping"):
        return (
            f"crash-looping (restarted {st.get('restart_count', 0)}x, health "
            f"{st.get('health')}); last error: {st.get('last_error') or '-'}"
        )
    if not st.get("running"):
        return f"not running (exit code {st.get('exit_code')})"
    if st.get("health") == "unhealthy":
        return "running but unhealthy (winbind trust or radiusd probe failing)"
    return None


def _check_status(name: str, st: dict[str, Any]) -> bool:
    problem = _status_problem(st)
    if problem is None:
        return True
    typer.secho(
        f"WARNING: instance '{name}' is {problem} — see 'lmnradius logs {name}'",
        fg=typer.colors.RED,
        err=True,
    )
    return False


def _tofu_fetch_chain(ldap_server: str) -> str:
    """Trust on first use: fetch the DC's certificate chain via `openssl s_client`,
    print every certificate's SHA-256 fingerprint plus a loud warning, and return the
    PEM bundle to pin."""
    try:
        scheme, host, port = ldap_url_parts(ldap_server)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    if scheme != "ldaps":
        raise typer.BadParameter("--ldap-ca-tofu needs an ldaps:// --ldap-server")
    cmd = ["openssl", "s_client", "-connect", f"{host}:{port}", "-servername", host, "-showcerts"]
    try:
        proc = subprocess.run(
            cmd, input="", capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        typer.secho(f"error: {' '.join(cmd)}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    pems = _PEM_BLOCK_RE.findall(proc.stdout)
    if not pems:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        typer.secho(
            f"error: no certificate received from {host}:{port} (openssl exit {proc.returncode})"
            f"{': ' + tail if tail else ''}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    bundle = "".join(pem if pem.endswith("\n") else pem + "\n" for pem in pems)
    try:
        certs = describe_pem_bundle(bundle)
    except ValueError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    _warn(f"TRUST ON FIRST USE: pinning what {host}:{port} served right now — NOT verified:")
    for cert in certs:
        kind = "trust anchor (CA)" if cert["trust_anchor"] else "end-entity certificate"
        _warn(
            f"  {kind}: {cert['subject']}  (issuer {cert['issuer']}, expires {cert['not_after']})"
        )
        _warn(f"    SHA-256 {cert['sha256_fingerprint']}")
    if any(c["trust_anchor"] for c in certs):
        _warn(
            "  Compare the CA fingerprint on the DC: "
            "openssl x509 -in /etc/linuxmuster/ssl/cacert.pem -noout -fingerprint -sha256"
        )
    else:
        _warn(
            "  The DC served no CA, only its own certificate: it is pinned as-is and a renewed DC "
            "certificate FAILS CLOSED until you re-run set-ldap-ca. Prefer --ldap-ca with the "
            "CA file (/etc/linuxmuster/ssl/cacert.pem on the DC). Compare the fingerprint there: "
            "openssl x509 -in /etc/linuxmuster/ssl/server.cert.pem -noout -fingerprint -sha256"
        )
    return bundle


def _resolve_ldap_ca(ldap_ca: Optional[str], tofu: bool, ldap_server: str) -> Optional[str]:
    """Turn --ldap-ca FILE / --ldap-ca-tofu into the PEM text for the API (or None)."""
    if ldap_ca is not None and tofu:
        raise typer.BadParameter("--ldap-ca and --ldap-ca-tofu are mutually exclusive")
    if ldap_ca is not None:
        try:
            return Path(ldap_ca).read_text(encoding="utf-8")
        except OSError as exc:
            raise typer.BadParameter(f"--ldap-ca: cannot read {ldap_ca}: {exc}") from None
    if tofu:
        return _tofu_fetch_chain(ldap_server)
    return None


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
    """List all instances (warns about instances whose LDAPS link is unverified)."""
    with _get_client() as c:
        resp = c.get("/v1/instances")
    _emit(resp)
    _ldaps_warnings(resp.json())


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
    ldap_ca: Optional[str] = typer.Option(None, "--ldap-ca", help=_LDAP_CA_HELP),
    ldap_ca_tofu: bool = typer.Option(False, "--ldap-ca-tofu", help=_LDAP_CA_TOFU_HELP),
) -> None:
    """Create (and reconcile) an instance.

    An ldaps:// instance must pin the DC's CA (--ldap-ca, or --ldap-ca-tofu as the
    documented trust-on-first-use fallback); without it the API refuses."""
    ldap_ca_pem = _resolve_ldap_ca(ldap_ca, ldap_ca_tofu, ldap_server)
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
    if ldap_ca_pem is not None:
        body["ldap_ca_pem"] = ldap_ca_pem
    with _get_client() as c:
        resp = c.post("/v1/instances", json=body)
    _emit(resp)
    # The instance is saved either way; a container that crash-loops right after the
    # start (e.g. a config the image rejects) must not pass as success.
    if not _check_status(name, resp.json().get("status", {})):
        raise typer.Exit(1)


@app.command("set-ldap-ca")
def set_ldap_ca(
    name: str,
    ldap_ca: Optional[str] = typer.Option(None, "--ldap-ca", help=_LDAP_CA_HELP),
    ldap_ca_tofu: bool = typer.Option(False, "--ldap-ca-tofu", help=_LDAP_CA_TOFU_HELP),
) -> None:
    """Pin (or re-pin) the CA the DC's LDAPS certificate is verified against and
    re-apply the instance. The upgrade step for instances created before 7.3.1."""
    with _get_client() as c:
        current = c.get(f"/v1/instances/{name}")
        if current.status_code >= 400:
            _emit(current)
        pem = _resolve_ldap_ca(ldap_ca, ldap_ca_tofu, str(current.json().get("ldap_server", "")))
        if pem is None:
            raise typer.BadParameter("give --ldap-ca <file> or --ldap-ca-tofu")
        resp = c.put(f"/v1/instances/{name}/ldap-ca", json={"pem": pem})
    _emit(resp)
    if not _check_status(name, resp.json().get("status", {})):
        raise typer.Exit(1)


@app.command()
def rm(name: str) -> None:
    """Remove an instance: leave the domain (DNS record + computer account), remove
    the container, the machine-account volume, the rendered config and the record."""
    with _get_client() as c:
        resp = c.delete(f"/v1/instances/{name}")
    _emit(resp)
    leave = resp.json().get("domain_leave", {})
    if not leave.get("ok", False):
        typer.secho(
            f"WARNING: instance '{name}' was removed locally but did NOT leave the domain: "
            f"{leave.get('detail', '-')}\nClean up on the DC: samba-tool computer delete "
            "<NAME>$ and remove the DNS A record of the RADIUS FQDN.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)


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


def _mark(ok: bool) -> str:
    return (
        typer.style("  OK  ", fg=typer.colors.GREEN)
        if ok
        else typer.style(" FAIL ", fg=typer.colors.RED)
    )


def _render_test(data: dict[str, Any]) -> None:
    """Human-readable summary of the /test diagnostics (operator console)."""
    if not data.get("container_running"):
        typer.secho(data.get("detail", "container not running"), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    trust = data.get("trust", {})
    typer.echo(f"[{_mark(trust.get('ok', False))}] winbind trust  — {trust.get('detail', '')}")
    login = data.get("login")
    if login is None:
        typer.echo("(no user given — trust-only check; add --user <name> to test a domain login)")
        return
    typer.echo(f"[{_mark(login['ok'])}] domain login   — {login['detail']} ({login['code']})")
    for g in data.get("gates", []):
        vlan = f" -> VLAN {g['vlan']}" if g.get("vlan") is not None else ""
        typer.echo(
            f"[{_mark(g['member'])}] SSID {g['ssid']}  — member of {g['group']}?{vlan if g['member'] else ''}"
        )
    if not login["ok"]:
        raise typer.Exit(1)
    if data.get("gates") and not any(g["member"] for g in data["gates"]):
        typer.secho(
            "note: login OK but the user is in NONE of the configured SSID groups — every SSID would reject.",
            fg=typer.colors.YELLOW,
        )


@app.command()
def test(
    name: str,
    user: Optional[str] = typer.Option(
        None, "--user", "-u", help="domain user to test a real login for (bare sAMAccountName)"
    ),
    password: Optional[str] = typer.Option(
        None, help="password (prompted hidden if --user is given and this is omitted)"
    ),
    json_out: bool = typer.Option(False, "--json", help="raw JSON instead of the readable summary"),
) -> None:
    """Diagnose an instance from the console: winbind trust, and — with --user —
    a real domain-login test (password + wifi gate) and a per-SSID gate preview.

    Without --user it only checks the DC trust. With --user it runs ntlm_auth
    exactly as the server does per WLAN request. The password is prompted
    hidden, sent once over the local API, used for one ntlm_auth run in the
    container, and never stored."""
    body: dict[str, Any] = {}
    if user is not None:
        body["user"] = user
        if not password:
            password = typer.prompt("Password", hide_input=True)
        body["password"] = password
    with _get_client() as c:
        resp = c.post(f"/v1/instances/{name}/test", json=body)
    if resp.status_code >= 400:
        typer.secho(f"error {resp.status_code}: {resp.text}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if json_out:
        typer.echo(json.dumps(resp.json(), indent=2, ensure_ascii=False))
        return
    _render_test(resp.json())


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
    json_out: bool = typer.Option(False, "--json", help="raw JSON instead of plain log lines"),
) -> None:
    """Show recent container log lines (radiusd), optional time/substring filter.

    Prints the log as real lines (not a JSON blob with escaped \\n). Use --json
    for the wrapped form."""
    with _get_client() as c:
        resp = c.get(f"/v1/instances/{name}/logs", params=_log_params(tail, since, until, grep))
    if resp.status_code >= 400:
        typer.secho(f"error {resp.status_code}: {resp.text}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if json_out:
        typer.echo(json.dumps(resp.json(), indent=2, ensure_ascii=False))
        return
    typer.echo(resp.json().get("logs", "").rstrip("\n"))


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
    """Check the control-plane API health; also warns about instances whose LDAPS
    link is unverified (the API health itself needs no auth)."""
    with _get_client() as c:
        _emit(c.get("/v1/health"))
        try:
            listing = c.get("/v1/instances")
        except httpx.HTTPError:
            return
    if listing.status_code == 200:
        _ldaps_warnings(listing.json())


@app.command()
def reconcile() -> None:
    """Re-apply all stored instances (reconverge drift / restore on a fresh host)."""
    with _get_client() as c:
        resp = c.post("/v1/reconcile")
    _emit(resp)
    ok = all(_check_status(st.get("name", "?"), st) for st in resp.json().get("reconciled", []))
    if not ok:
        raise typer.Exit(1)


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
