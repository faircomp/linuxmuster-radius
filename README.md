<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# linuxmuster-radius

Containerisierter **RADIUS-Server (WPA2/WPA3-Enterprise) für die Schul-WLANs von
[linuxmuster.net](https://linuxmuster.net) 7.x**: **PEAP-MSCHAPv2** gegen Samba
Active Directory (winbind), **rollenbasierte VLANs pro SSID** (Lehrer/Schüler)
und **erzwungene Server-Zertifikatsprüfung** gegen eine dedizierte EAP-CA —
**eine** self-contained FreeRADIUS-Instanz pro Server, verwaltet über
**REST-API + CLI** auf einer **eigenen RADIUS-VM**.

> **Status:** **`v0.1.6`** — vollständig (P0–P6); die Data-Plane ist an einem echten
> linuxmuster-Setup **runtime-bewiesen** (Member-Join, PEAP-MSCHAPv2 via winbind,
> Per-Rollen-VLAN). Details in **[`CHANGELOG.md`](CHANGELOG.md)**.
> Erledigt: Image auf **GHCR** veröffentlicht + Digest gepinnt, und der
> **Laufzeit-Beweis** (separate-VM-Member + winbind + PEAP + Rollen-VLAN) ist an einem
> echten linuxmuster-DC erbracht — beides bewiesen, nicht angenommen.
> Offen bleiben **menschliche Gates** je Standort: **GPG-Signatur** des `.deb` mit dem
> linuxmuster-Schlüssel; die standortspezifischen **AD-Fakten** (Realm/Base-DN/
> Rollengruppen/`global-binduser`); die **AD-Member-Registrierung** des Servers als
> Gerät (role `server`) in `devices.csv` + `linuxmuster-import-devices` auf dem DC;
> und die **Windows-GPO-/MDM-Verteilung** von CA + gesperrtem WLAN-Profil
> (Servername gepinnt).

## Warum

linuxmuster.net liefert Sophomorix-Identitäten und Rollengruppen, aber **keinen
fertigen 802.1X-Weg fürs Schul-WLAN**. In der Praxis heißt das: pro Rolle eine
eigene SSID mit eigenem VLAN, eine sichere Server-Zertifikatsprüfung auf den
Clients und ein RADIUS, der **Rolle + Passwort gegen die bestehende AD** prüft —
ohne die Firewall oder den DC zu belasten und **ohne von Hand Nutzer oder Gruppen
anzulegen**.

`linuxmuster-radius` schließt genau diese Lücke: ein **RADIUS-Server auf einer
eigenen VM**, der die vorhandene AD/Sophomorix-Infrastruktur **nur konsumiert**
(Nutzer, die `wifi`-Gruppe, Rollengruppen und Bind-User bleiben rein Sophomorix),
**PEAP-MSCHAPv2 via winbind** authentifiziert, **pro SSID die passende
Rollengruppe erzwingt** und **rollenbasierte VLANs** liefert — deklarativ und
git-versioniert verwaltet über **REST-API + Typer-CLI**.

## Architektur auf einen Blick

- **Data Plane:** genau **eine** self-contained FreeRADIUS-3.2-Instanz pro
  linuxmuster-Server (ein generisches, env-getriebenes Ubuntu-24.04-Image).
  **Mehrere SSIDs** werden **innerhalb** dieser einen Instanz als Konfiguration
  abgebildet (virtual-server-/`Called-Station-SSID`-Verzweigung), **nicht** als
  eigene Container. Authentifizierung: **PEAP-MSCHAPv2** — der Container tritt
  der Samba-AD als **Member-Server** bei; `mschap` → `ntlm_auth`
  (`--request-nt-key --allow-mschapv2`) lässt die AD den NT-Hash prüfen (winbind).
  Damit ist der Container **stateful** (Maschinenkonto-Secret im
  `/var/lib/samba`-Volume) und `winbindd` läuft als zweiter Daemon
  (Mini-Supervisor). Mehrere Instanzen nur für **harte Isolation**
  (z. B. ein separates Gäste-RADIUS).
- **Pro-SSID-Gating & VLAN:** die SSID wird via `rewrite_called_station_id` in
  `Called-Station-SSID` geparst; pro SSID erzwingt `rlm_ldap` die Rollengruppe —
  schulübergreifend `role-teacher`/`role-student` (Regelfall) oder pro Schule
  `<schule>-lehrer` → `<schule>-teachers`, sonst **Access-Reject**. Geprüft wird die
  **direkte** Mitgliedschaft, daher **nicht** die verschachtelten `all-*`-Aggregate
  (siehe ADR-007). VLAN standardmäßig **statisch
  pro SSID in UniFi** (SSID = Rolle); RADIUS-vergebenes **dynamisches VLAN**
  (RFC 2868: `Tunnel-Type=13`, `Tunnel-Medium-Type=6`,
  `Tunnel-Private-Group-Id=<vlan>`) ist ein optionaler Modus.
- **UniFi:** die **Access Points** sind der NAS und senden Access-Requests aus
  **ihrer eigenen IP** (der Controller ist **kein** Proxy). `clients.conf` nutzt
  daher das AP-Management-**Subnetz** als CIDR und unterstützt **mehrere**
  Subnetze (`--client-subnet`, wiederholbar).
- **Control Plane:** gehärteter systemd-Dienst mit **REST-API** (FastAPI) und
  **CLI** (Typer, Thin Client, **kein** direkter Docker-Zugriff) — legt Instanzen
  an, pflegt SSID-/Policy-Config, verwaltet die **dedizierte EAP-CA**
  (`lmnradius ca init` / `cert issue` / `ca export`) und **aktualisiert
  digest-pinned mit Health-Check-Auto-Rollback**. Als `.deb` ausgeliefert.
- **Zertifikat:** eine **dedizierte, single-purpose EAP-CA** (Root ~10 J
  offline/Passphrase; Server-Zert mehrjährig, EKU `serverAuth`
  (1.3.6.1.5.5.7.3.1) + eapOverLAN, `SAN=FQDN`). Verteilung von CA + gesperrtem
  WLAN-Profil per Windows-GPO (`linuxmuster-gpo-template`) und MDM-`.mobileconfig`,
  mit der Client-Trias: Server-Zert-Prüfung **an**, Trust nur auf **diese**
  EAP-Root, **Servername gepinnt**, „neues Zert akzeptieren“ **aus**.

| Komponente | Pfad | Stack |
|---|---|---|
| Data-plane-Image (FreeRADIUS) | `image/` | Ubuntu 24.04 · **FreeRADIUS 3.2** · winbind/Samba-Member · envsubst-Entrypoint |
| Control Plane (REST-API) | `controlplane/lmnradius/` | Python 3.11+ · FastAPI · uvicorn · **docker-py** (Container-Lifecycle) · pydantic v2 |
| CLI | `controlplane/lmnradius/cli.py` | Python · Typer · httpx (Thin Client der REST-API) |
| E2E / Deploy | `deploy/` | docker-compose (Samba AD DC + gejointer FreeRADIUS + `eapol_test`), Instanz-Definitionen |
| Packaging | `packaging/debian/` | `.deb` via dh-virtualenv, gehärteter systemd-Dienst |
| Tests | `scripts/tests/` | `run.sh`-Aggregator; Heavy-Tier auf **crabbox** |

Details: [`docs/architecture.md`](docs/architecture.md).

## Entwicklung & Tests

Der **Fast-Tier** (ruff/mypy/pytest/shellcheck/reuse) läuft lokal/in CI. Der
**Heavy-Tier** — die echte Kerberos/winbind-+-FreeRADIUS-E2E (**Samba AD DC +
gejointer FreeRADIUS + `eapol_test`-Supplicant**), die *Lehrer auf Lehrer-SSID →
Access-Accept (+ korrektes VLAN) / Schüler auf Lehrer-SSID → Reject / falsches
Passwort → Reject / Nicht-WLAN-Nutzer → Reject* beweist — braucht einen
**Linux-Host mit Docker**. Die Dev-Box hat **kein** Docker; der Heavy-Tier läuft
auf **crabbox**. Aggregator: `bash scripts/tests/run.sh [lint|unit|quick|e2e|all]`.

## Sicherheit (kurz)

- **Die erzwungene Server-Zert-Prüfung (CA-Pinning) ist load-bearing, nicht
  optional.** PEAPs innerer MSCHAPv2 ist kryptografisch schwach; erst die auf den
  Clients erzwungene Prüfung gegen die **dedizierte** EAP-Root (mit gepinntem
  Servername) verhindert Rogue-AP-Angriffe und Credential-Diebstahl. Deshalb
  **keine** Wiederverwendung der breiten linuxmuster-CA (ihre Trust-Basis steckt
  in jedem Schülergerät) und **kein** Let’s Encrypt (90-Tage-Rotation bricht
  802.1X; jedes öffentliche Zert würde ohne Servername-Pinning impersonieren).
- **Docker-Socket-Zugriff = root-äquivalent.** Die Control-Plane bindet nur
  `127.0.0.1` (Bearer-Token, `hmac.compare_digest`); Docker läuft ausschließlich
  über den einen auditierten Service-Pfad (docker-py) hinter
  `tecnativa/docker-socket-proxy`. Jeder extern gelieferte String wird an der
  pydantic-Grenze streng validiert (er fließt in Dateinamen, Container-Namen,
  Mounts und gerenderte Config).
- **Sophomorix bleibt unangetastet.** Nutzer, die `wifi`-Gruppe, Rollengruppen
  und Bind-User werden **nie** von Hand angelegt — RADIUS **konsumiert** sie nur.
  Der RADIUS-Server wird als **Gerät** (role `server`) in `devices.csv`
  registriert (`linuxmuster-import-devices` auf dem DC) → **ein** Maschinenkonto,
  konsistent mit Sophomorix. Der LDAP-Bind läuft über den bestehenden
  `global-binduser`.

Mehr: [`docs/decisions.md`](docs/decisions.md) · [`docs/architecture.md`](docs/architecture.md).

## Lizenz

**`GPL-3.0-or-later`** — konsistent mit dem GPL-Ökosystem von linuxmuster.net.
Das Projekt ist [REUSE 3.3](https://reuse.software)-konform: jede Datei trägt
einen SPDX-Header, die Lizenztexte liegen in [`LICENSES/`](LICENSES/), und
`reuse lint` ist in CI gated. Siehe [`docs/decisions.md`](docs/decisions.md).
© Kevin Stenzel.
