<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/) · [SemVer](https://semver.org/).

## [Unreleased]

### Added
- **P6 — E2E-Harness + Rest-Docs (`deploy/e2e/`, `scripts/tests/`, `.claude/`, `docs/operations.md`):**
  die crabbox-E2E, die das P1-Image zur Laufzeit beweist.
  - `deploy/e2e/docker-compose.yml`: Samba-AD-DC + die als Member gejointe FreeRADIUS-Instanz +
    `eapol_test`-Supplicant; 5-Fälle-Matrix (Lehrer@Lehrer→Accept+VLAN20, Schüler@Lehrer→Reject,
    Schüler@Schüler→Accept+VLAN10, falsches PW→Reject, kein `wifi`→Reject);
  - `scripts/tests/e2e_radius.sh` (dünner Wrapper, `--exit-code-from client`) +
    `crabbox_bootstrap.sh`; `run.sh` `e2e`-Tier verdrahtet (verweigert ohne `LMNRADIUS_ALLOW_REAL=1`);
  - `.claude/skills/test/SKILL.md` (crabbox-Lifecycle) + `.claude/settings.json`;
  - `docs/operations.md` (Day-2-Betrieb).
  - **Ehrlich:** beweist Join + PEAP + Gruppe→VLAN, **nicht** den linuxmuster-`devices.csv`-Pfad
    (braucht einen echten linuxmuster-Server). Der Lauf erfolgt auf crabbox, nicht auf der Dev-Box.
- **P5 — Packaging + CI (`packaging/`, `deploy/`, `.github/workflows/`, `scripts/tests/run.sh`):**
  - `.deb` via `packaging/build-deb.sh` (hermetisches venv unter `/opt/linuxmuster-radius/venv`)
    + `packaging/debian/{control,postinst,prerm,postrm}` (postinst: System-User `lmnradius`,
    zufälliges API-Token, `config.yml` mit den echten `Settings`-Keys inkl. `certs_dir`/
    `render_dir`, `0700` secrets+certs, git-init, `update-all` beim Upgrade) + gehärtete
    `packaging/systemd/linuxmuster-radius.service`;
  - `deploy/docker-socket-proxy.yml` (minimale Allow-List) + `deploy/instances/default-school.yaml`
    (Beispiel, läuft durchs Instance-Model) + `scripts/tests/run.sh`
    (Aggregat-Runner `lint|unit|quick|e2e`; `e2e` verweigert ohne `LMNRADIUS_ALLOW_REAL=1`);
  - CI: `ci.yml` (ruff/mypy/pytest/shellcheck/reuse), `build-image.yml` (GHCR) und `build-deb.yml`.
  - Lokal verifiziert: `shellcheck` sauber, YAMLs valide, `reuse lint` 62/62 konform,
    `run.sh quick` grün (114 pytest). Der echte `.deb`-Build/Install läuft in der CI.
- **P4 — Deployment (`docs/{radius-and-ad,deployment-gpo}.md` + `scripts/`):** die Betriebs-/
  Rollout-Anleitung plus zwei DC-Helfer.
  - `docs/radius-and-ad.md`: AD-Member-Setup (winbind/`ntlm_auth`, `devices.csv` Rolle
    `server` + `linuxmuster-import-devices`, `global-binduser`, DC-`ntlm auth`, DNS/Hostname) —
    Sophomorix bleibt unangetastet;
  - `docs/deployment-gpo.md`: UniFi (ein RADIUS-Profil, SSID→statisches VLAN, AP-Subnetz als
    Client-CIDR), OPNsense (`1812-1813/udp`), Client-Trust via GPO/MDM mit den Pinning-Pflichten,
    „Steering vs. Enforcement" und eine Abnahme-Checkliste (human gate);
  - `scripts/discover-ad-facts.sh` (read-only: realm/workgroup/Base-DN/Gruppen → fertige
    `lmnradius create`-Vorlage) + `scripts/provision-radius-account.sh` (idempotente
    Device-Registrierung + Secret-Dateien); beide `shellcheck`-sauber.
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
