<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/) · [SemVer](https://semver.org/).

## [Unreleased]

### Added
- **P3 — Dedizierte EAP-CA (`controlplane/lmnradius/ca.py` + CLI/API + `docs/certs-and-ca.md`):**
  Single-Purpose-EAP-CA (via `cryptography`), die **nur** das RADIUS-Server-Zertifikat signiert.
  - `lmnradius ca init` (RSA-4096-Root ~10 J., passphrase-verschlüsselt), `cert issue
    <instance> [--fqdn]` (Server-Cert mit EKU `serverAuth` + `eapOverLAN`, SAN=FQDN,
    mehrjährig), `ca export`, plus `ca show` / `cert show`; Ablage `certs_dir/{ca,<name>}/…`
    → gemountet nach `/run/secrets/eap/*` (der Container ist **fail-closed** ohne Cert);
  - `docs/certs-and-ca.md`: Anleitung inkl. Offline-Root-Empfehlung, GPO/MDM-Verteilung und
    der Client-Pinning-Pflichten (Validierung AN + CA-Pin + Server-Name-Pin + Prompt AUS);
  - **Tests: 114 passed** (+21), `ruff` + `mypy` sauber.
- **P2 — Control-Plane + CLI (`controlplane/`):** FastAPI-REST-API + Typer-CLI
  (dünner REST-Client) im Squid-Muster, die den P1-Container-Env-Contract bedient.
  - `models.py` (`Instance` mit `client_subnets: list`, `ssids: [{name, allowed_group,
    vlan?}]`, strenge Boundary-Validierung), `config.py`, `security.py` (Bearer-Token,
    `hmac.compare_digest`), `store.py` (git-backed YAML), `docker_service.py`
    (`env_for` + Mounts), `reconciler.py`, `updater.py` (digest-pinned, health-gated
    Auto-Rollback), `api.py`, `cli.py`, `main.py`;
  - `render.py`: `Instance` → `clients.conf` (ein `client{}` pro `--client-subnet`) +
    `ssid-policy` (Per-SSID-`Called-Station-SSID`-Gate + optionales VLAN), gemountet
    nach `/etc/lmnradius/instance.d`;
  - **Tests: 93 passed**, `ruff` + `mypy` sauber (lokal via `FakeDockerService`, ohne
    Docker). Die EAP-CA/Cert-Verwaltung (`ca`/`cert`) folgt in P3.
- **P1 — Data-Plane-Image (`image/`):** generisches, self-contained
  FreeRADIUS-3.2-Image auf Ubuntu 24.04, das die Domäne als Samba-AD-**Member**
  joint (winbind) und PEAP-MSCHAPv2 gegen das AD prüft.
  - `Dockerfile` (freeradius + winbind + krb5, Build-Assertions, `tini` als PID 1,
    Healthcheck, read-only-rootfs-tauglich);
  - `entrypoint.sh`: assembliert den raddb-Config-Tree auf tmpfs, rendert
    `smb.conf`/`krb5.conf` + FreeRADIUS-mods/-sites aus Templates über eine
    `envsubst`-Allow-List, joint als Member (nur wenn nötig), startet winbindd +
    radiusd unter einem Supervisor;
  - Templates `mods/{eap,ldap,mschap}` (PEAP, LDAP-Gruppen-Lookup, `ntlm_auth`),
    `sites/{default,inner-tunnel}` (`Called-Station-SSID`-Branching, Per-SSID-Gate),
    `smb.conf`, `krb5.conf`, `instance.d/{ssid-policy,clients.conf.example}`;
  - `healthcheck.sh`: „up **und** enforcing" via `wbinfo -t` (AD-Trust) + `radclient`
    Status-Server-Probe.
  - Gegen die offiziellen FreeRADIUS-/Samba-Docs verifiziert (`$INCLUDE`-Auflösung,
    `confdir`/`raddbdir`, `winbindd_privileged`-Pfad, `ntlm_auth --configfile`).
    **Runtime-Verifikation (Join + PEAP-Flow) folgt im crabbox-E2E in P6.**

## [0.1.0] - 2026-07-10

**Projekt-Gerüst / P0** — Scaffold, Konventionen und die Architektur-/
Entscheidungs-Docs. Noch **kein lauffähiger Code**: Control-/Data-Plane,
Image und E2E folgen; die menschlichen Gates stehen in der
[`README.md`](README.md).

### Added
- **Repository-Scaffold:** Verzeichnislayout für Control Plane
  (`controlplane/lmnradius/`), Data-plane-Image (`image/`), Deployment/E2E
  (`deploy/`), Packaging (`packaging/debian/`, `packaging/systemd/`) und Tests
  (`scripts/tests/`); dazu `LICENSES/GPL-3.0-or-later.txt`, `renovate.json`,
  `.gitignore` und durchgängig REUSE-3.3-konforme SPDX-Header.
- **`CLAUDE.md`:** Arbeitsweise, Sicherheits-Leitplanken und Code-/Docs-
  Konventionen (Conventional Commits, SemVer + Keep a Changelog, ruff + mypy
  clean, pydantic v2, deutsche Prosa mit englischen Code-Identifiern) sowie die
  crabbox-Heavy-Tier-Regeln.
- **`docs/architecture.md`:** Control-/Data-Plane-Split, **eine** FreeRADIUS-
  Instanz pro Server mit **SSIDs als Config** (statt Container pro SSID),
  PEAP-MSCHAPv2 via winbind (AD-Member-Join), Pro-SSID-Gating über `rlm_ldap`,
  das VLAN-Modell (statisch pro SSID in UniFi, dynamisch per RFC 2868 optional)
  und das UniFi-NAS-/CIDR-Client-Modell.
- **`docs/decisions.md`:** die gesperrten ADRs samt verworfener Alternativen und
  ehrlicher Grenzen — Ein-Instanz-Topologie, PEAP + AD-Join statt EAP-TTLS-PAP,
  **dedizierte EAP-CA** mit load-bearing Client-Pinning (keine linuxmuster-CA,
  kein Let’s Encrypt), statisches vs. dynamisches VLAN und die AD-Member-
  Registrierung des Servers via `devices.csv`/Sophomorix.
- **`docs/references.md`:** verifizierte Quellen (FreeRADIUS-, Samba-/winbind-,
  docs.linuxmuster.net- sowie UniFi-/RADIUS-RFC-Belege) für die tragenden
  Architekturannahmen.
