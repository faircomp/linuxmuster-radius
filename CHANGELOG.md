<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/) · [SemVer](https://semver.org/).

## [Unreleased]

### Changed
- **`DEFAULT_IMAGE` auf das `:0.1.2`-Release-Image gepinnt** (`@sha256:804a7e9d…`) — zuvor
  per Digest gezogen und gegen den echten linuxmuster-DC verifiziert (frisches
  Zustandsvolume = Neuinstallations-Pfad, `healthy` in 6 s, `ntlm_auth`-Matrix, volle
  PEAP-Matrix **7/7** mit `role-teacher`→VLAN 20 / `role-student`→VLAN 10; Protokoll in
  `docs/references.md`). Das nächste `.deb` liefert damit das Release-Image aus.
- **CI-Gate-Tools gepinnt** (`ruff`/`mypy`/`reuse`/`pytest`): eine neue ruff-Version hatte
  den Default-Regelsatz erweitert und die Pipeline ohne Codeänderung rot gefärbt.
  Ein Renovate-customManager hält die Pins als review-pflichtige PRs aktuell.
- **Doku präzisiert:** Das Join-Konto ist **teilverifiziert** (einfacher Benutzer scheitert
  mit `Insufficient access`; Administrator/delegiertes Konto funktioniert — belegt im
  Live-E2E); offen bleibt allein die Vorab-Adoption via `linuxmuster-import-devices`.
  Die früheren pauschalen „NICHT VERIFIZIERT"-Hinweise in `operations.md`,
  `radius-and-ad.md` und `provision-radius-account.sh` sagten das noch nicht.

## [0.1.2] - 2026-08-04

**Standort-Release:** beide DC-Helferskripte wurden gegen eine echte linuxmuster-7-Installation
und `devices.csv(5)` geprüft — dabei kamen zwei Fehler heraus, die auf einem produktiven DC
falsche Ergebnisse erzeugt hätten. Dazu die verifizierte Antwort auf „Lehrer **aller** Schulen
in einem WLAN".

### Fixed
- **`provision-radius-account.sh` schrieb die Rolle ins falsche `devices.csv`-Feld:** die
  `sophomorixRole` landete in Feld 3 (Gerätegruppe/Hardwareklasse), Feld 9 — die tatsächliche
  Rolle — blieb leer. Da **nur Feld 9** darüber entscheidet, ob ein Computerkonto angelegt wird,
  wäre das Gerät ohne `server`-Rolle importiert worden und der spätere Member-Join hätte kein
  Maschinenkonto zum Adoptieren gehabt. Jetzt wird das dokumentierte 15-Feld-Layout erzeugt
  (Rolle in Feld 9, Hardwareklasse `nopxe`, PXE-Flag 0), Raum/Gruppe/Rolle sind über
  `DEVICE_ROOM`/`DEVICE_GROUP`/`DEVICE_ROLE` überschreibbar, und bei abweichender Feldzahl warnt
  das Skript vor dem Anhängen.
- **`discover-ad-facts.sh` erfand Schulen:** Adminklassen tragen dasselbe Namensmuster wie
  Schulen (`testklasse-teachers`), wurden also als Schule ausgegeben — inklusive `create`-Vorlage
  für eine nicht existierende Schule. Die Schulliste kommt jetzt aus
  `/etc/linuxmuster/sophomorix/*/` (autoritativ) und die Namenspräfixe werden dagegen geprüft.
- **`architecture.md` behauptete einen rekursiven Gruppencheck** — `rlm_ldap` prüft hier über
  `memberOf`, also **direkt**. Korrigiert.
- **`docs/install.md`** zeigte nach dem v0.1.1-Release noch das 0.1.0-Paket und einen überholten
  Reifegrad-Hinweis; **`README.md`** zählte erledigte Punkte (GHCR-Image, Digest-Pin,
  Laufzeit-Beweis) noch als offen.

### Added
- **Anleitung für ein schulübergreifendes WLAN:** `install.md` erklärt jetzt die Gruppenwahl je
  SSID — **`role-teacher`/`role-student`** deckt Lehrer bzw. Schüler **aller Schulen** ab, weil
  diese Rollengruppen schulunabhängig und **direkt** am Nutzer hinterlegt sind.
  **`all-teachers` funktioniert dafür nicht:** die `all-*`-Aggregate enthalten die Schulgruppen
  statt der Nutzer, und `memberOf` ist nicht transitiv — ein solches Gate würde jeden Lehrer
  abweisen (an echter linuxmuster verifiziert, siehe `references.md`; als ehrliche Grenze in
  ADR-007 dokumentiert).

## [0.1.1] - 2026-07-13

**Erstes Runtime-Release:** die Data-Plane ist jetzt an einem **echten linuxmuster-DC**
bewiesen (Member-Join, PEAP-MSCHAPv2 via winbind, Per-Rollen-VLAN). Der Live-E2E deckte
mehrere Laufzeit-Bugs auf, die `radiusd -XC` und statische Reviews nicht finden konnten —
alle behoben und re-verifiziert. Protokoll in [`docs/references.md`](docs/references.md).

### Fixed
- **`rlm_ldap` über `ldaps://` bringt den *threaded* FreeRADIUS zum Absturz** (libldap=GnuTLS
  vs. FreeRADIUS=OpenSSL) — LDAP-TLS wird jetzt in einem lokalen **stunnel** terminiert
  (`rlm_ldap` spricht Klartext über die Loopback; stunnel re-verschlüsselt zum DC). (ADR-015)
- **Der Supervisor riss gesunde Container ab:** `kill -0` gibt unter `--cap-drop ALL` (kein
  `CAP_KILL`) `EPERM` auf den `freerad`-eigenen radiusd zurück (= „tot") — Liveness läuft jetzt
  über `/proc/<pid>`, der Teardown über den PID-Namespace-Collapse.
- **Das Per-SSID-Gate wies jede SSID ab:** `Called-Station-SSID` ist FreeRADIUS-intern und wird
  von `copy_request_to_tunnel` nicht in den PEAP-Tunnel getragen — `rewrite_called_station_id`
  läuft jetzt im **inner-tunnel** (Korrektur zu ADR-007).
- **Domänen-Join:** `net ads join` ohne `MEMBER`-Positional (das ist `net rpc join`);
  `kerberos method = secrets only` (kein Keytab-Schreibversuch auf dem read-only rootfs); der
  Join braucht ein **Admin-/delegiertes** Konto (ein einfacher Benutzer scheitert).
- **Config-Assemblierung unter dem gehärteten Profil:** `cp -dR --preserve=mode` statt `cp -a`
  (CAP_FOWNER weg), `chmod` vor `chown`, und `clients.conf` überschreiben statt anhängen (sonst
  Kollision mit dem Stock-`client localhost` auf 127.0.0.1).
- **`discover-ad-facts.sh`:** `all-*`/`global-*` als schulübergreifende Aggregatgruppen
  behandeln (keine Schulen).

### Added
- **`stunnel4`** im Data-Plane-Image + optionale DC-Zertifikatsprüfung via `LDAP_CA`.
- **Renovate-Digest-Automatik:** ein `renovate.json`-customManager, der den
  `DEFAULT_IMAGE`-`@sha256`-Pin in `controlplane/lmnradius/models.py` +
  `deploy/instances/*.yaml` verfolgt (Tag `:latest`), plus ein self-hosted
  `.github/workflows/renovate.yml` (wöchentlich + `workflow_dispatch`) — Renovate schlägt
  Digest-Bumps als PR vor (automerge aus, Mensch merged → neues `.deb`).
- **`docs/install.md`** (Schritt-für-Schritt-Erstinstallation), das Live-E2E-Verifikations-
  protokoll in **`docs/references.md`** und **ADR-015** (LDAP-TLS via stunnel).

### Changed
- **`DEFAULT_IMAGE` auf das E2E-verifizierte GHCR-Image gepinnt**, damit die Control-Plane
  exakt das von der CI gebaute, gegen einen echten DC getestete Image zieht.

## [0.1.0] - 2026-07-10

**Erstes Release — die vollständige linuxmuster-radius-Erstauslieferung (P0–P6):**
Control-/Data-Plane, dedizierte EAP-CA, Deployment-Anleitungen, Packaging/CI und die
crabbox-E2E-Harness. Der **Control-Plane-Stack ist lokal bewiesen** (114 pytest,
ruff/mypy/reuse grün); der Data-Plane-**Runtime**-Beweis (Container-Domänen-Join +
PEAP-Flow) läuft über den crabbox-E2E bzw. den Ersteinsatz. Details in
[`docs/`](docs/) und der [`README.md`](README.md).

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
- **P0 — Repository-Scaffold:** Verzeichnislayout für Control Plane
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
