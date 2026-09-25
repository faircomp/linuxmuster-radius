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
Sophomorix ab und ist nicht lmn-konform. **Verifiziert (Live-E2E 2026-07-12):** das
Join-Konto braucht Admin-/delegierte Rechte — ein einfacher Benutzer scheitert mit
`Insufficient access`; `net ads join` nimmt kein `MEMBER`-Positional. **Offen (E2E):**
ob `devices.csv` Rolle `server` bereits ein Computerkonto **vorab anlegt**, das ein
späteres `net ads join` **sauber adoptiert** (im E2E legte der Join das Konto selbst
an). **Quelle:**
docs.linuxmuster.net, „setup-file-server" (Member-Server via devices.csv +
linuxmuster-import-devices).

### ADR-007 — Per-SSID-Gate via Called-Station-SSID + rlm_ldap-Gruppencheck
**Status:** Accepted (Architekturentscheidung). **Entscheidung:** Die SSID wird über
die FreeRADIUS-Policy `rewrite_called_station_id` in `Called-Station-SSID` geparst;
darauf wird verzweigt und die Gruppenmitgliedschaft über das LDAP-Modul (`rlm_ldap`)
erzwungen. Die Zielgruppe je SSID ist **freie Konfiguration** (`ssids[].allowed_group`), die
beiden üblichen Muster sind: **schulübergreifend** eine SSID auf `role-teacher` bzw.
`role-student` (schulunabhängige, direkt zugewiesene Sophomorix-Rollengruppen — der Regelfall,
wenn Lehrer *aller* Schulen ein gemeinsames WLAN bekommen sollen), **oder** pro Schule:
SSID `<schule>-lehrer` → verlangt Gruppe `<schule>-teachers`;
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
erzeugt werden, wo das Post-Auth-Gate es liest. **Ehrliche Grenze (verifiziert 2026-08-04):**
`rlm_ldap` prüft hier über `membership_attribute = memberOf`, also **direkte** Mitgliedschaft.
Verschachtelte Gruppen — insbesondere die linuxmuster-Aggregate `all-teachers`/`all-students`,
die die Schulgruppen *enthalten* statt die Nutzer — greifen damit **nicht** und würden jeden
Nutzer abweisen. Für ein schulübergreifendes WLAN daher `role-teacher`/`role-student`
(direkt zugewiesen) verwenden; wollte man die `all-*`-Gruppen nutzen, bräuchte es eine
rekursive Auflösung (`member:1.2.840.113556.1.4.1941:=<userDN>`) — bewusst nicht umgesetzt,
weil die Rollengruppen denselben Zweck ohne Zusatzkomplexität erfüllen. **Gegenstück
(verifiziert 2026-08-05):** das **wifi-Grund-Gate** läuft über `ntlm_auth
--require-membership-of` und prüft das **NT-Token — transitiv**: verschachtelte Gruppen wie
`all-wifi` greifen dort (empirisch am echten DC: User nur in `wifi` ⊂ `all-wifi` →
`NT_STATUS_OK` gegen `all-wifi`; Nicht-Mitglied → `LOGON_FAILURE`). Für Mehrschul-Setups ist
deshalb `--wifi-group all-wifi` der richtige Basis-Gate-Wert.

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
an, braucht einen Root-Socket. **Stand 2026-09-25:** Renovate ist abgeschaltet (Kevin), bis
es mit einer GitHub-App wieder läuft; bis dahin hebt ein Mensch Digests, Locks und Pins per
PR an. **Quelle:** Watchtower-Repo (archiviert 2025-12-17);
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
der Digest wird per PR gepinnt (von Hand, solange Renovate abgeschaltet ist). **Begründung:** kostenlos, integriert sauber mit GitHub-CI +
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
weglassen — das Per-SSID-Rollen-Gate braucht die AD-Gruppen. stunnel verifiziert das
DC-Zertifikat gegen die gemountete CA (`LDAP_CA`) — **seit 7.3.1 für neue Instanzen
Pflicht** (`--ldap-ca`/`--ldap-ca-tofu`, Kampagnen-Befund 8.5: bis 7.3.0 setzte die
Control Plane `LDAP_CA` nie, die Verbindung war unverifiziert; Details in
radius-and-ad.md § 3 und threat-model.md). **Quelle:** FreeRADIUS-Wiki „Rlm_ldap"
(GnuTLS-vs-OpenSSL-Warnung); reproduziert + behoben im Live-E2E (references.md).

### ADR-016 — Lieferkette: Python-Abhängigkeiten nur aus Lockfiles mit Hashes
**Status:** Accepted (Stufe A „Lieferkette", 2026-09-23). **Entscheidung:** Das venv im
`.deb` entsteht nur aus zwei Lockfiles, die `uv pip compile --generate-hashes
--python-version=3.12 --exclude-newer=P7D` erzeugt (nur Fassungen, die mindestens eine Woche
auf PyPI liegen; in diesem Fenster fallen kompromittierte Uploads meist auf): `controlplane/requirements.lock` (Laufzeit, aus
`pyproject.toml`) und `controlplane/build-requirements.lock` (pip selbst und setuptools,
aus `build-requirements.in`). `packaging/build-venv.sh` installiert sie mit `--require-hashes
--only-binary :all: --no-deps`, baut das eigene Paket offline zum Wheel (`--no-index
--no-build-isolation`) und installiert es per Namen (kein Build-Pfad in `direct_url.json`),
prüft mit `pip check`, entfernt setuptools wieder und bricht ab, wenn `pip freeze --all`
nicht exakt den Lockfiles entspricht.
Seit 7.3.4 (kalte Prüfung von Stufe A, Befund F2) liest der Bau die Lockfiles nicht mehr
ungeprüft: `scripts/lockfile_gate.py` lässt vor pip nur Zeilen zu, die uv schreibt (leer,
Kommentar, `name==version \`, `    --hash=sha256:<64 hex>`, nur druckbares ASCII) — pip liest
auch eingerückte Zeilen, URL-Anforderungen (`name @ url#sha256=…` erfüllt `--require-hashes`)
und Optionszeilen wie `--extra-index-url`, und `str.splitlines()` trennt auch an CR/FF/U+2028;
eine eingerückte URL-Zeile hatte die alte Prüfung (nur Spalte 1) passiert und landete im
`.deb`. Jede Prüfsumme muss eine sein, die PyPI für genau diese Fassung veröffentlicht (pip
prüft nur die der geladenen Datei). Nach der Installation vergleicht der Bau `pip freeze
--all` Zeile für Zeile mit den Lockfiles (jede Zeile ein schlichtes `name==version`, jeder
Fehler bricht ab, keine Prozess-Substitution mehr, die eine Ausnahme verschluckt) und
verlangt, dass jede installierte Distribution von `lmnradius` oder pip gebraucht wird (ein
zusätzlicher Pin mit echten Hashes fiele sonst durch). `scripts/tests/lock_gates.sh`
(Fast-Tier) hält die Fälle dauerhaft fest.
Weiter nachgehärtet (7.3.4, Nachbesserung K1 und Runde 3): ein Wheel aus einer Sperrdatei
kann `bin/`-Skripte und eine `.pth` mitbringen; würde es vor der Mengen-/Hüllen-Prüfung
installiert, liefe sein Code (die `.pth` beim Bau des eigenen Wheels, im CI-Fast-Tier seine
`bin/`-Werkzeuge im PATH). Darum ist `scripts/check-lockfiles.sh` **das** Lock-Tor, und jeder
Verbraucher ruft es auf, bevor irgendetwas aus einer Sperrdatei installiert wird: der
Fast-Tier und der CI-Job `lock-gates-build` (je erster Schritt), der Job `lockfile` in ci.yml
und release.yml (nur das Tor), `packaging/build-venv.sh` (also `make deb`, CI `package`,
Release-`build`) und `scripts/tests/run.sh` (zuerst; scheitert es, bricht run.sh ab).
`scripts/tests/lock_gates.sh` bricht ab, wenn die committeten Sperrdateien das Tor nicht
bestehen, und seine Gegenproben (Installationswege ohne Tor) installieren nur Fixture-
Sperrdateien mit dem Test-Wheel, nie die des Checkouts (kalte Prüfung r3, F1). Es prüft alle drei Sperrdateien: Grammatik; die uv-Sperrdatei hält
genau das `uv==` aus `uv-requirements.in` mit von PyPI veröffentlichten Hashes (nur stdlib);
erst dann installiert es dieses uv in ein eigenes, isoliertes venv und ruft es per absolutem
Pfad auf; uv löst `pyproject.toml`/`build-requirements.in` neu auf, die Pins müssen genau
diese Hülle sein, und jeder Hash muss von PyPI für genau diese Fassung stammen. Aus der
Umgebung des Aufrufers nehmen die Tore weder Programme noch Paketquellen: fester PATH ohne
venv-`bin/`, `/usr/bin/python3 -I`, `VIRTUAL_ENV`/`PYTHON*`/`UV_*`/`PIP_*` entfernt (auch
`PIP_REQUIREMENT`/`PIP_CONSTRAINT`), keine pip-/uv-Konfigurationsdateien, uv mit `--python
/usr/bin/python3 --no-config` (kein Projekt-venv, keine umgelenkte Paketquelle). **Grenze:**
nicht neutralisiert sind `BASH_ENV` (bash führt es vor der ersten Skriptzeile aus),
exportierte Shell-Funktionen und Proxy-/CA-Variablen (`HTTPS_PROXY`, `SSL_CERT_FILE`,
`REQUESTS_CA_BUNDLE`, …), die bestimmen, wem das Tor als PyPI vertraut; wer die Umgebung des
Aufrufers so setzt, führt ohnehin Code als dieser aus. Ein
zusätzlicher Pin mit echten Hashes wird so abgewiesen, bevor sein Code läuft (nachgewiesen:
die K1-, R1- und R2-Fälle in `scripts/tests/lock_gates.sh`, die Marker entstehen nie; die
Gegenproben ohne Tor erzeugen sie).
`scripts/check-lockfiles.sh` beweist außerdem, dass die Lockfiles zu ihren
Quellen passen (eine Fassung jünger als sieben Tage fällt dabei durch) und jede Fassung ein
Wheel für die Zielplattform hat (CPython 3.12, glibc 2.39, x86_64). **Grenze:** Die Pins
werden nach Namen mit der Hülle verglichen, die Auflösung bevorzugt die gepinnten Fassungen.
Eine andere echte Fassung eines gepinnten Pakets (älter **oder neuer**, mindestens eine Woche
alt, mit ihren echten Hashes), die die deklarierten Anforderungen erfüllt, besteht darum jedes
Tor; nur die Review des Sperrdatei-Diffs fängt sie. Neue Fassungen kommen per PR, ohne
Automerge (von Hand, solange Renovate abgeschaltet ist). **Begründung:** ohne Pins zog jeder Release-Bau die
neueste PyPI-Fassung ohne Prüfsumme; Bauten waren nicht reproduzierbar und eine
kompromittierte Fassung wäre unbemerkt in ein root-installiertes Paket gelangt.
**Verworfene Alternativen:** Pins ohne Hashes (schützen nicht gegen eine ausgetauschte
Datei); pip-tools (gleiches Format, langsamer; uv ist das Werkzeug, das Renovate für
dieses Format ausführt); `--python-platform`/`--only-binary` im Lockfile-Kopf (Renovate
lehnt beide Optionen ab, deshalb prüft der CI-Job die Zielplattform in einer zweiten
Auflösung). **Quelle:** pip-Doku „Secure installs" (hash-checking mode); Renovate-Quelltext
`lib/modules/manager/pip-compile/common.ts` (erlaubte uv-Optionen, 44.93.5).

### ADR-017 — Build-Eingaben unveränderlich referenziert, Release erst als Entwurf
**Status:** Accepted (Stufe A „Lieferkette", 2026-09-23). **Entscheidung:** Das Build-Image
steht überall als `ghcr.io/linuxmuster/lmndev-runner:<tag>@sha256:<digest>` (ci.yml,
release.yml und der Build-Befehl im Makefile, derselbe Digest). Neue Digests kommen per
PR, ein Mensch merged (von Hand, solange Renovate abgeschaltet ist); den Tag ändert ein
Bump nie (`24.04 → 26.04` wäre
eine neue linuxmuster-Linie, kein Update). Jede GitHub Action steht per vollständigem
Commit-SHA mit `# vN`-Kommentar (`helpers:pinGitHubActionDigests`). Das Release legt die
`gh`-CLI des Runners an (keine Dritt-Action neben `contents: write`): erst als Entwurf, dann
die Assets, dann der Abgleich der von GitHub berechneten sha256 mit den gebauten Dateien,
erst danach wird veröffentlicht. **Begründung:** beide Tags baut eine fremde Org
wöchentlich neu, der Build läuft darin als root; ein still geändertes Image änderte jedes
künftige `.deb`. Ein Action-Tag lässt sich verschieben, ein SHA nicht. Ein unveränderliches
Release („immutable release") lässt sich nach dem Veröffentlichen weder um Assets ergänzen
noch austauschen, deshalb die Reihenfolge Entwurf → Assets → Veröffentlichen.
**Verworfene Alternativen:** eigenes Build-Image oder `ubuntu:24.04@sha256` mit
`apt-get build-dep` (Stufe C im Hub-Plan, setzt den debian/-Umbau voraus);
`softprops/action-gh-release` per SHA pinnen (bliebe Fremdcode mit Schreibrecht neben dem
`.deb`). **Quelle:** Hub `work/plans/paketarchiv.md` §2; GitHub-Doku „Immutable releases"
(„Create the release as a draft. Attach all associated assets to the draft release. Publish
the draft release.").

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
