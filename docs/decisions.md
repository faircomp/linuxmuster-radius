<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Decisions (ADRs) — linuxmuster-radius

Kurze Architecture Decision Records. Neue Entscheidung = neuer Eintrag; wird eine
Entscheidung revidiert, wird der alte Eintrag auf `Superseded by ADR-XXX` gesetzt
statt gelöscht. Status: `Accepted` (bestätigt) · `Assumed` (Default, noch zu
bestätigen) · `Proposed` · `Superseded`. Jeder Eintrag nennt die **verworfene
Alternative** und eine **Quelle** oder ein **datiertes E2E-Ergebnis**.

Schwesterprojekt: `linuxmuster-squid` (gleicher Hausstil). linuxmuster-radius läuft
auf einer **separaten RADIUS-VM** und ist ein WPA2/WPA3-Enterprise-RADIUS für
linuxmuster.net-7.x-Schul-WLANs mit rollenbezogenen VLANs.

---

### ADR-000 — License & SPDX
**Status:** Accepted (bestätigt 2026-07-10). **Entscheidung:** `GPL-3.0-or-later`,
© Kevin Stenzel; jede Datei trägt einen REUSE/SPDX-Header. Das Projekt ist
REUSE-3.3-konform (`reuse lint` grün, in CI gegated); Lizenztexte liegen in
`LICENSES/`, Nicht-Kommentar-Dateien nutzen `.license`-Sidecars.
**Begründung:** konsistent mit dem übrigen Stack des Autors und dem GPL-Ökosystem
von linuxmuster.net. **Verworfene Alternative:** permissive Lizenz (MIT/Apache-2.0)
— verworfen, weil das Umfeld (linuxmuster.net, Schwesterprojekt squid) GPL ist und
Copyleft für ein Schul-Infrastruktur-Tool gewollt ist. **Org/Brand:** `faircomp` —
kanonisches Repository `github.com/faircomp/linuxmuster-radius`; Autor Kevin Stenzel;
Version 0.1.0 (greenfield).

### ADR-001 — Stack Python/FastAPI + Typer
**Status:** Accepted (Default 2026-07-10; jederzeit änderbar). **Entscheidung:**
Control-Plane = FastAPI/uvicorn (`lmnradius-api`), CLI = Typer (`lmnradius`, ein
**dünner** REST-Client ohne direkten Docker-Zugriff); Docker via **docker-py
(`docker`≥7)** nur in der Service-Schicht, nicht durch stdout-Parsing von
`docker compose`. **Begründung:** linuxmuster-api7/webui7 **und** das Schwesterprojekt
linuxmuster-squid sind FastAPI/Python (Ökosystem-Nähe, ein gemeinsamer Hausstil);
docker-py liefert strukturierte Lifecycle-/Health-/Digest-APIs. **Verworfene
Alternative:** Go (Single-Binary) — abgewogen, zurückgestellt: verliert die direkte
Nähe zu api7/webui7/squid und die geteilte pydantic-/Typer-Basis. **Quelle:**
linuxmuster-api7 (FastAPI) als Ökosystem-Referenz.

### ADR-002 — Genau EINE self-contained Instanz pro Server, SSIDs als Config
**Status:** Accepted (Architekturentscheidung). **Entscheidung:** Default ist genau
**eine** self-contained FreeRADIUS-Instanz pro linuxmuster-Server; **mehrere SSIDs**
werden **innerhalb** dieser einen Instanz als Config abgebildet (virtual-server /
Called-Station-SSID-Branching, siehe ADR-007), **nicht** als getrennte Container.
Mehrere Instanzen nur für harte Isolation (z. B. ein separates Gäste-RADIUS).
**Begründung:** ein Join, ein winbindd, ein Machine-Account, ein Satz
AP-Shared-Secrets — minimaler beweglicher Teil. **Verworfene Alternative:** ein
Container pro SSID / pro Instanz — vervielfacht Domain-Joins, winbind-Daemons,
Maschinenkonten und die Duplizierung der AP-Shared-Secrets ohne Sicherheitsgewinn.
**E2E:** Proof-Matrix (crabbox) validiert Mehr-SSID-Branching in **einer** Instanz.

### ADR-003 — PEAP-MSCHAPv2 via winbind + Member-Join
**Status:** Accepted (Architekturentscheidung; E2E-belegt auf crabbox, nicht
angenommen). **Entscheidung:** Authentifizierung = **PEAP-MSCHAPv2**. Der Container
tritt der Samba-AD als **Member-Server** bei; `mschap` ruft `ntlm_auth`
(`--request-nt-key --allow-mschapv2`), die AD validiert den NT-Hash via winbind.
**Begründung:** genau **eine** Instanz = **ein** Join (ADR-002), und PEAP hat die
beste Client-Kompatibilität (Ökosystem-Standard für Windows/GPO, iOS/MDM, Android).
**Verworfene Alternative:** EAP-TTLS-PAP + `rlm_ldap`-Bind (ohne Join) — vermeidet
zwar den Join, verlagert das Klartext-Passwort in den Server und hat schwächere
Out-of-the-box-Client-Unterstützung. **Ehrliche Grenze:** PEAP macht den Container
**stateful** (Machine-Account-Secret in einem `/var/lib/samba`-Volume; Re-Join bei
Verlust) und fügt `winbindd` als zweiten Daemon hinzu (Mini-Supervisor) — eine
Abweichung vom stateless Keytab-Modell des Schwesterprojekts squid. **Quelle:**
SambaWiki, „Authenticating FreeRADIUS against Active Directory".

### ADR-004 — Management via REST API + dünner CLI
**Status:** Accepted (Architekturentscheidung). **Entscheidung:** eine Core-Engine,
REST-API als Schnittstelle, CLI als **dünner** Client. Genau **ein** auditierter Pfad,
docker-py ausschließlich in der Service-Schicht. **Begründung:** kein duplizierter
Code, ein Pfad für Lifecycle + digest-gepinntes Update; `lmnradius` (Typer) spricht
nur die API auf `127.0.0.1` (Bearer-Token, `hmac.compare_digest`), niemals den
Docker-Socket direkt. **Verworfene Alternative:** CLI mit direktem docker-py-Zugriff
(zwei Pfade, doppelte Validierung, doppelte Angriffsfläche). **Quelle:**
linuxmuster-squid ADR-004 (bewährtes Schwestermuster).

### ADR-005 — Dedizierte EAP-CA
**Status:** Accepted (Architekturentscheidung). **Entscheidung:** eine **dedizierte,
single-purpose** private EAP-CA, verwaltet von der Control-Plane (`lmnradius ca init`
/ `cert issue` / `ca export`). Root ~10 Jahre, **offline/passphrase**;
Server-Zertifikat mehrjährig mit EKU `serverAuth` (1.3.6.1.5.5.7.3.1) + eapOverLAN,
`SAN = FQDN`. Verteilung von CA + gesperrtem WLAN-Profil via Windows-GPO (Schwester
`linuxmuster-gpo-template`) und MDM-`.mobileconfig`. **Load-bearing:** weil das innere
MSCHAPv2 von PEAP kryptographisch schwach ist, ist die **tragende (load-bearing)
Client-Trias** verpflichtend: (1) Server-Zertifikatsprüfung **AN** + (2) Trusted-CA =
**diese eine** EAP-Root + (3) **Server-Name gepinnt**, „neues Zertifikat akzeptieren"
**AUS**. **Verworfene Alternativen:** (a) die linuxmuster-CA
(`/etc/linuxmuster/ssl`) wiederverwenden — pinnt eine **breite Vertrauensbasis** in
jedes Schülergerät; (b) Let's Encrypt — 90-Tage-Rotation bricht 802.1X, und jedes
öffentliche Zertifikat kann **ohne Server-Name-Pinning impersonieren**. **Quelle:**
eduroam (Server-Zertifikat/CAT-Leitfaden) + FreeRADIUS „Let's Encrypt/EAP"-HOWTO.

### ADR-006 — AD-Member lmn-konform registrieren
**Status:** Accepted (verifiziert). **Entscheidung:** Die RADIUS-VM wird als **Device
mit Rolle `server`** in `devices.csv` eingetragen und auf dem DC via
`linuxmuster-import-devices` importiert (der offiziell dokumentierte
Member-Server-Pfad, identisch zu einem zusätzlichen Fileserver) → **ein**
Maschinenkonto, konsistent mit Sophomorix. **USERS, die `wifi`-Gruppe,
Rollengruppen und Bind-User** bleiben **rein Sophomorix** und werden **nie** von Hand
angelegt — RADIUS **konsumiert** sie nur. LDAP-Bind-Account = der bestehende
`cn=global-binduser,ou=Management,ou=GLOBAL,dc=...`, Passwort auf dem lmn-Server in
`/etc/linuxmuster/.secret/global-binduser`. **Verworfene Alternative:**
Maschinenkonto/Gruppen von Hand via `samba-tool`/`net` anlegen — driftet von
Sophomorix ab und ist nicht lmn-konform. **Offen (E2E):** ob `devices.csv` Rolle
`server` bereits ein Computerkonto **vorab anlegt**, das ein späteres `net ads join`
**sauber adoptiert** — in der crabbox-E2E zu verifizieren. **Quelle:**
docs.linuxmuster.net, „setup-file-server" (Member-Server via devices.csv +
linuxmuster-import-devices).

### ADR-007 — Per-SSID-Gate via Called-Station-SSID + rlm_ldap-Gruppencheck
**Status:** Accepted (Architekturentscheidung). **Entscheidung:** Die SSID wird über
die FreeRADIUS-Policy `rewrite_called_station_id` in `Called-Station-SSID` geparst;
darauf wird verzweigt und die Gruppenmitgliedschaft über das LDAP-Modul (`rlm_ldap`)
erzwungen: SSID `<schule>-lehrer` → verlangt Gruppe `<schule>-teachers`;
`<schule>-schueler` → verlangt `<schule>-students`; sonst `Access-Reject`. Zusätzlich
die WLAN-Grundberechtigung über die `wifi`-Gruppe. **Begründung:** eine Instanz muss
mehrere SSIDs mit **je eigener** Gruppenregel bedienen. **Verworfene Alternative:**
`ntlm_auth --require-membership-of` allein — prüft **genau EINE** Gruppe und kann das
Per-SSID-Mapping nicht ausdrücken. **Quelle:** `ntlm_auth(1)` /
Samba-`--require-membership-of` (Ein-Gruppen-Grenze); FreeRADIUS-Policy
`rewrite_called_station_id`. **E2E (verifiziert 2026-07-12):** Proof-Matrix bestätigt
(Lehrer auf `…-Lehrer` → Access-Accept + `Tunnel-Private-Group-Id=20`; Lehrer auf
`…-Schueler` bzw. unbekannte SSID → Reject). **Korrektur aus dem E2E:**
`rewrite_called_station_id` läuft im **inner-tunnel**, nicht im äusseren Server:
`&Called-Station-SSID` ist ein FreeRADIUS-**internes** Attribut, das
`copy_request_to_tunnel` **nicht** über die Tunnelgrenze trägt — es muss dort (neu)
erzeugt werden, wo das Post-Auth-Gate es liest.

### ADR-008 — VLAN-Zuweisung
**Status:** Accepted (Default; dynamischer Modus optional). **Entscheidung:** Default
ist **statisches** VLAN pro SSID, konfiguriert im UniFi-Controller (SSID = Rolle).
**Optional** ist RADIUS-zugewiesenes **dynamisches** VLAN via RFC 2868:
`Tunnel-Type=13` (VLAN), `Tunnel-Medium-Type=6` (802), `Tunnel-Private-Group-Id=<vlan>`.
**Begründung:** statisch pro SSID ist der einfachste, robusteste Weg für rollenreine
SSIDs; dynamisch nur, wenn eine SSID mehrere Rollen tragen soll. **Verworfene
Alternative:** **nur** dynamisches VLAN aus RADIUS als Default — mehr bewegliche Teile
(Attribut-Rendering, Controller-Support) ohne Mehrwert bei rollenreinen SSIDs.
**Quelle:** RFC 2868 (Tunnel-Attribute für RADIUS).

### ADR-009 — clients.conf = AP-Management-Subnetz(e) als CIDR, MEHRERE unterstützt
**Status:** Accepted (verifiziert). **Entscheidung:** Die **Access Points** sind das
NAS und senden Access-Requests aus ihrer **eigenen** IP (der Controller ist **kein**
Proxy). Daher nutzt `clients.conf` das **AP-Management-Subnetz als CIDR** — und muss
**mehrere** Subnetze unterstützen (ein großes CIDR oder mehrere) über ein
wiederholbares `--client-subnet` (Liste, analog zu squids `--school-subnets`).
**Begründung:** deckt reale UniFi-Topologien (mehrere Mgmt-Netze/Standorte) ab.
**Verworfene Alternative:** nur die **Controller-IP** in `clients.conf` eintragen —
führt zu „unknown client" bei jedem AP, weil die Requests von den AP-IPs kommen.
**Quelle:** community.ui.com (UniFi + FreeRADIUS: AP als NAS) sowie neilzone und
dannyda (HowTos: Subnetz statt Controller-IP in clients.conf).

### ADR-010 — Updates: digest pin + Renovate + health-gated Rollback, kein Watchtower
**Status:** Accepted (Schwestermuster). **Entscheidung:** git als Source of Truth,
`image@sha256:`-Pin, Renovate (`automerge:false`, Merge = Go/No-Go), kontrolliertes
`pull`+`up` mit Health-Check-Auto-Rollback; Tooling als signiertes `.deb`. Ein
`.deb`-Upgrade hebt Instanzen auf den im Paket gepinnten `DEFAULT_IMAGE` (die
apt-Installation ist das menschliche Go/No-Go), jeweils mit Health-Auto-Rollback.
**Begründung:** deterministische, auditierbare Updates. **Verworfene Alternative:**
Watchtower — archiviert (2025-12-17), **kein** Rollback, wendet Breaking Changes blind
an, braucht einen Root-Socket. **Quelle:** Watchtower-Repo (archiviert 2025-12-17);
linuxmuster-squid ADR-010.

### ADR-011 — Packaging via dh-virtualenv
**Status:** Proposed. **Entscheidung:** `.deb` mit hermetischem venv zur Build-Zeit
(dh-virtualenv), **kein** pip-in-postinst; Auslieferung mit gehärteter systemd-Unit.
**Begründung:** reproduzierbar/signierbar, kein Netz/kein pip-als-root zur
Install-Zeit (Verbesserung gegenüber webui7/api7); Layout ansonsten an linuxmuster
angelehnt. **Verworfene Alternative:** pip-Install im postinst (Netzzugriff + pip als
root zur Installationszeit, nicht reproduzierbar). **Hinweis:** Build- und
Ziel-Python-Minor müssen übereinstimmen (Python 3.11+). **Quelle:** linuxmuster-squid
ADR-011 (bewährtes Schwestermuster).

### ADR-012 — Docker-Socket hinter einem Proxy (die „ehrliche Grenze")
**Status:** Accepted (verifiziert). **Entscheidung:** API strikt an **`127.0.0.1`** +
Token gebunden; Zugriff auf den Socket via `tecnativa/docker-socket-proxy` (nur die
benötigten Endpoints) auf `127.0.0.1`. **Begründung:** Schreibzugriff auf
`docker.sock` = passwortloses Root auf dem Host; das würde sonst die
systemd-Härtung untergraben. **Ehrliche Grenze:** Der Socket-Proxy braucht
`CONTAINERS`+`VOLUMES`+`POST`, um Instanzen zu starten — damit kann ein kompromittierter
Aufrufer einen Container **mit Host-Bind-Mount** erzeugen = weiterhin Host-Root. Der
Proxy **reduziert die Angriffsfläche, senkt sie aber NICHT unter Host-Root**; die
echte Antwort ist **rootless Docker**. Der Host ist die Vertrauensgrenze. **Verworfene
Alternative:** die API direkt auf den rohen Docker-Socket zeigen lassen (noch größere
Fläche, kein Endpoint-Filter). **Quelle:** tecnativa/docker-socket-proxy; Docker-Docs
(Socket = root-equivalent); linuxmuster-squid ADR-012.

### ADR-013 — Image-Registry: GHCR (Default)
**Status:** Accepted (Default 2026-07-10; jederzeit änderbar). **Entscheidung:** Das
Data-Plane-Image wird nach **GHCR (`ghcr.io/faircomp/linuxmuster-radius`)** publiziert;
Renovate pinnt den Digest. **Begründung:** kostenlos, integriert sauber mit GitHub-CI +
Renovate-Digest-Pinning. **Verworfene Alternative:** Docker Hub (Pull-Rate-Limits)
oder eine selbstgehostete/linuxmuster-Registry (mehr Infrastruktur). **Quelle:**
GitHub Container Registry (Docs); linuxmuster-squid ADR-013.

### ADR-014 — RadSec zurückgestellt, MVP = UDP + starkes Per-Subnetz-Secret
**Status:** Proposed (zurückgestellt). **Entscheidung:** RadSec (RADIUS über
TLS, **TCP/2083**) wird zurückgestellt; das MVP nutzt klassisches RADIUS über **UDP**
mit einem **starken Per-Subnetz-Shared-Secret** auf einem **vertrauenswürdigen
Management-VLAN**. RadSec ist in UniFi Network **≥ 8.4** verfügbar (Shared Secret
`radsec`) und wird nachgezogen, sobald die Flotte durchgängig ≥ 8.4 fährt.
**Begründung:** UDP+starkes Secret auf einem getrennten Mgmt-VLAN ist für den
Schulkontext ausreichend und minimiert bewegliche Teile; RadSec fügt TLS-Transport
und Zertifikatsverwaltung auf dem NAS-Pfad hinzu. **Verworfene Alternative:** RadSec
im MVP erzwingen — setzt UniFi Network ≥ 8.4 flottenweit voraus und erhöht die
Komplexität ohne unmittelbaren Bedarf. **Quelle:** Ubiquiti UniFi Network Release
Notes (RadSec ab 8.4, TCP/2083, Secret `radsec`).

### ADR-015 — LDAP-TLS via lokalem stunnel (nicht `rlm_ldap`-eigenes LDAPS)
**Status:** Accepted (im Live-E2E gegen einen echten DC verifiziert, 2026-07-12).
**Entscheidung:** `rlm_ldap` spricht **Klartext-LDAP** zu einem **lokalen stunnel** auf
`127.0.0.1`; stunnel (OpenSSL) re-verschlüsselt zum DC (`ldaps://…:636`). `libldap`
initialisiert damit **nie** TLS im radiusd-Prozess. **Begründung:** Auf Ubuntu ist
`libldap` gegen **GnuTLS** gebaut, FreeRADIUS gegen **OpenSSL**; sobald `rlm_ldap` im
**threaded** Server eine LDAPS/StartTLS-Verbindung öffnet, kollidieren beide TLS-Stacks
und radiusd **segfaultet** Sekunden nach „Ready to process requests" (die Stock-Warnung
„libldap is using GnuTLS … The server may also crash"). Der Klartext-Hop ist
loopback-only im Netzwerk-Namespace des Containers; die Wire-Verschlüsselung zum DC bleibt
erhalten. **Verworfene Alternativen:** (a) `libldap` gegen OpenSSL neu bauen — eigenes
Paket pflegen; (b) GSSAPI-Bind über Klartext — Kerberos-ccache-Lebenszyklus; (c) `rlm_ldap`
weglassen — das Per-SSID-Rollen-Gate braucht die AD-Gruppen. Optional verifiziert stunnel
das DC-Zertifikat gegen eine gemountete CA (`LDAP_CA`). **Quelle:** FreeRADIUS-Wiki
„Rlm_ldap" (GnuTLS-vs-OpenSSL-Warnung); reproduziert + behoben im Live-E2E (references.md).

---

## Site-Fakten zu verifizieren (P0, mit Quelle/Datum eintragen)

- Realer `REALM` / Workgroup der Zielumgebung.
- Base DN / DC-Suffix (`dc=...`).
- Exakte SSID-Namen (z. B. `<schule>-lehrer`, `<schule>-schueler`) und die je
  erlaubten Gruppen (`<schule>-teachers`, `<schule>-students`, `wifi`).
- VLAN-IDs für Lehrer / Schüler / Gäste (teachers/students/guest).
- UniFi-AP-Management-Subnetz(e) als CIDR (eines oder mehrere → `--client-subnet`).
- FQDN des RADIUS-Servers (= `SAN` des EAP-Server-Zertifikats, gepinnter Server-Name).
- **Ob `devices.csv` Rolle `server` bereits ein Computerkonto vorab anlegt, das ein
  späteres `net ads join` sauber adoptiert** — in der crabbox-E2E verifizieren
  (siehe ADR-006).
