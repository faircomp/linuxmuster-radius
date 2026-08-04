<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Architecture — linuxmuster-radius

Statusdokument. Mit jeder wesentlichen Änderung aktuell halten (siehe `CLAUDE.md`
→ Dokumentationspflege). Belege für die verifizierten Aussagen: `docs/references.md`;
die Entscheidungen mit Begründung und verworfenen Alternativen: `docs/decisions.md`.

## 1. Kontext: linuxmuster.net 7 & Enterprise-WLAN

- linuxmuster.net **7.x** (Ubuntu 24.04), Identität über **Samba AD DC** auf dem
  linuxmuster-Server; **Sophomorix** als User-/Device-Backend; **UniFi** als
  WLAN-Controller mit den **Access Points** als Edge.
- **Rollen** liegen im AD-Attribut `sophomorixRole`. Nutzbare **Gruppen**:
  `teachers`/`students` für **default-school** (ohne Präfix), sonst
  `<schule>-teachers`/`<schule>-students`. Die Sophomorix-Gruppe **`wifi`** ist die
  allgemeine WLAN-Freigabe. LDAP-Bind über den `global-binduser` unter
  `OU=Management,OU=GLOBAL` — nie als Admin.
- **Lücke:** WPA2/WPA3-**Enterprise** (802.1X) mit rollenbasierten VLANs gibt es
  nicht out-of-the-box. Dieses Projekt liefert genau das: **eine SSID je Rolle →
  eigenes VLAN**, Authentisierung gegen das bestehende AD.
- **Aufstellung:** dieser RADIUS läuft als **Domänen-Mitglied** auf einer
  **separaten RADIUS-VM** (nicht auf dem lmn-Server) — Schwesterprojekt zu
  linuxmuster-squid und in dessen Hausstil.

## 2. Zielbild: Control Plane / Data Plane

```
                       RADIUS-VM (eigener Host) · .deb + hardened systemd
  Admin ─ CLI (lmnradius, Typer/httpx) ─▶ REST-API  (FastAPI · 127.0.0.1 · Bearer)
                                              │   Reconciler · Updater · EAP-CA
                                              │   Git-State: instances/*.yaml
                                              ▼   docker-py ─▶ docker-socket-proxy
                                    ┌────────────────────────────────────────────────┐
   UniFi-APs (NAS)                  │   lmnradius-<name>   ·   EINE Instanz          │
   senden ab EIGENER IP             │   radiusd + winbindd   (Domänen-Mitglied)      │
        │                           │   PEAP-MSCHAPv2 · Called-Station-SSID-Branch   │
        └──── 1812/1813 udp ───────▶│                                                │
                                    └───────────────────────┬────────────────────────┘
                                                            │  winbind (NT-Hash) · LDAPS (Gruppen)
                                                            ▼
                       Samba AD DC (separater Host · „linuxmuster")
                       Sophomorix: users · Gruppe wifi · role-groups · global-binduser
```

- **Data Plane:** **genau eine** FreeRADIUS-Instanz je linuxmuster-Server. Mehrere
  **SSIDs** werden **innerhalb** dieser einen Instanz als Config abgebildet
  (virtual-server / `Called-Station-SSID`-Branching), **nicht** als getrennte
  Container. Weitere Instanzen nur für harte Isolation (z. B. ein separates
  Gäste-RADIUS).
- **Control Plane:** gehärteter systemd-Dienst; **ein** Core-Engine, darauf die
  REST-API; die CLI ist ein dünner Client der API (kein Docker-Zugriff, kein
  dupliziertes Codepfad).

## 3. Data Plane — die eine FreeRADIUS-Instanz

- **Image:** **ein** generisches `FROM ubuntu:24.04` + **FreeRADIUS 3.2** +
  `winbind`/`samba`, env-getrieben; alles Instanz-spezifische kommt aus **Env +
  gerenderter `conf.d` + Secrets**. Verteilt als `ghcr.io/faircomp/linuxmuster-radius`,
  **digest-gepinnt**; Container = `lmnradius-<name>`.
- **Zwei Daemons (Mini-Supervisor):** `radiusd` **und** `winbindd`. Die Instanz ist
  als **Member-Server** in die Samba-AD **gejoint**; `mschap` → `ntlm_auth
  --request-nt-key --allow-mschapv2` lässt die AD (via winbind) den **NT-Hash**
  prüfen. **Ehrliche Grenze:** das macht den Container **stateful** (Maschinenkonto-
  Secret im `/var/lib/samba`-Volume; bei Verlust Re-Join) und fügt `winbindd` als
  zweiten Daemon hinzu — eine Abkehr vom zustandslosen Keytab-Modell von squid.
- **SSIDs als Config:** die Policy `rewrite_called_station_id` zerlegt die
  `Called-Station-Id` in **`Called-Station-SSID`** — **im inner-tunnel**, weil
  `copy_request_to_tunnel` interne Attribute nicht überträgt (ADR-007); darauf wird verzweigt.
- **Per-SSID-Gruppen-Gate:** je SSID eine Zielgruppe, geprüft über `rlm_ldap`
  (Bind als `global-binduser`; dessen LDAP-TLS terminiert ein lokales **stunnel** zum DC,
  ADR-015). Geprüft wird die **direkte** Mitgliedschaft (`memberOf`), **nicht** rekursiv —
  darum schulübergreifend `role-teacher`/`role-student` (direkt zugewiesen) und **nicht** die
  verschachtelten `all-*`-Aggregate; alternativ pro Schule `<schule>-lehrer` →
  `<schule>-teachers`. Sonst Access-Reject. `ntlm_auth --require-membership-of` allein reicht
  nicht — es prüft **genau eine** Gruppe (ADR-007).
- **VLAN:** Default ist das **statische VLAN je SSID** im UniFi-Profil (SSID = Rolle
  = VLAN). Optional dynamisch per RFC 2868: `Tunnel-Type=13`,
  `Tunnel-Medium-Type=6`, `Tunnel-Private-Group-Id=<vlan>` im Access-Accept.
- **UniFi-Clients:** die **Access Points** sind der NAS und senden Access-Requests
  ab ihrer **eigenen IP** (der Controller ist **kein** Proxy). `clients.conf`
  nutzt daher das **AP-Management-CIDR** und **muss mehrere Subnetze** unterstützen
  (ein großes CIDR oder mehrere) über das wiederholbare `--client-subnet` (Liste,
  wie squids `--school-subnets`). Nur die Controller-IP einzutragen erzeugt
  „unknown client".
- **Healthcheck** = **`wbinfo -t`** (Domänen-Trust / Secure Channel) **+** eine
  **`radclient` Status-Server**-Probe auf `127.0.0.1:1812`. Zusammen beweist das:
  RADIUS läuft **und** der Trust ist gesund.
- **Härtung:** non-root; `cap_drop: ALL`; `no-new-privileges`; Logs → stdout/stderr;
  Secrets als ro-Mounts/tmpfs. **Ehrliche Grenze:** die read-only-Rootfs ist für
  den Samba-State (`/var/lib/samba`) **teilweise gelockert**; die DC-Zeile
  `ntlm auth = mschapv2-and-ntlmv2-only` lebt in der `smb.conf` des DC und kann
  durch Paket-Updates entfernt werden (MSCHAPv2 bricht dann).

## 4. Control Plane

- **REST-API (`lmnradius-api`, FastAPI/uvicorn):** CRUD über die Instanz-Definitionen
  + Lifecycle (`start/stop/restart/update/rollback/status/logs`).
  `HTTPBearer(auto_error=False)` + `hmac.compare_digest`; Bind **nur `127.0.0.1`**;
  Token in `config.yml` (`chmod 600`); **Audit-Log** jeder Mutation. **Strikte
  pydantic-v2-Boundary-Validierung** jedes extern gelieferten Strings — er fließt in
  Dateinamen/Container-Namen/Mounts/gerenderte Config.
- **Reconciler:** deklarativer, **git-versionierter** State (`instances/*.yaml`) →
  rendert Config, gleicht Ist gegen Soll ab (docker-py).
- **Updater:** Pull-by-**Digest** (`image@sha256:`), **health-gated**, **Auto-Rollback**
  auf den letzten Known-Good; Renovate (`docker:pinDigests`, `automerge:false`) +
  CI-Publish. Kein Watchtower.
- **EAP-CA / Cert-Manager:** dedizierte, single-purpose EAP-CA
  (`lmnradius ca init` / `cert issue` / `ca export`) — siehe §7.
- **CLI (`lmnradius`, Typer/httpx):** ausschließlich über die REST-API — **ein**
  auditierter Pfad zum Docker-Daemon.
- **Docker-Zugriff:** docker-py; **Socket-Zugriff ist root-äquivalent** → hinter
  `docker-socket-proxy` auf `127.0.0.1`; API nie öffentlich. Paketierung via
  **dh-virtualenv** (hermetisches venv, kein pip-in-postinst); Dienst hinter einer
  gehärteten systemd-Unit. System-User `lmnradius` (Gruppe `docker`);
  Config `/etc/linuxmuster-radius/{config.yml,instances,secrets,certs}`, State
  `/var/lib/linuxmuster-radius/instances`, Env-Präfix `LMNRADIUS_`.

## 5. Instanzmodell & Konfig-Split

Eine Instanz ist eine deklarative, git-versionierte YAML — validiert an der
pydantic-Boundary:

```yaml
name:          <schule>                         # → Container lmnradius-<schule>, State-Ordner
realm:         LINUXMUSTER.<...>                 # Kerberos-Realm (UPPERCASE)
workgroup:     LINUXMUSTER                       # NetBIOS/Workgroup
ldap_base_dn:  DC=linuxmuster,DC=lan
ldap_bind_dn:  CN=global-binduser,OU=Management,OU=GLOBAL,DC=linuxmuster,DC=lan
client_subnets:                                 # LISTE — AP-Management-CIDR(s), wiederholbar
  - 10.0.0.0/16
ssids:                                           # SSIDs = Config, NICHT je ein Container
  # schulübergreifend (Regelfall): direkt zugewiesene Sophomorix-Rollengruppen
  - { name: "lehrer-wlan",   allowed_group: "role-teacher", vlan: 20 }
  - { name: "schueler-wlan", allowed_group: "role-student", vlan: 30 }
  # pro Schule stattdessen: allowed_group "<schule>-teachers" / "<schule>-students"
server_fqdn:   radius.linuxmuster.lan            # SAN des EAP-Server-Zerts
join_secret:   <secret-ref>                      # Secret-Referenz für den Domänen-Join
image:         ghcr.io/faircomp/linuxmuster-radius@sha256:<digest>   # optional, sonst DEFAULT_IMAGE
```

**Konfig-Split (wie squid — Skalare via Env, Listen vorgerendert):**

- **Skalare** (`realm`, `workgroup`, `server_fqdn`, `ldap_base_dn`, `ldap_bind_dn`, …)
  gehen als **Whitelist-Env** in den Container; der **`envsubst`-Entrypoint** rendert
  daraus die Templates (`smb.conf`, `mods-enabled/ldap`, `eap`).
- **Listen** sind mit `envsubst` nicht ausdrückbar → die **Control Plane** rendert
  sie in eine gemountete, read-only **`conf.d/`**: `client_subnets` → `clients.conf`,
  `ssids[]` → die **per-SSID `unlang`/virtual-server-Branches**.
- **Secrets** (`join_secret`, LDAP-Bind-Passwort des `global-binduser`, das
  AP-Shared-Secret für `clients.conf`, das Status-Server-Secret) liegen unter
  `secrets/` (`0600`) und werden als ro-Mounts/tmpfs eingehängt — nie in die YAML.

## 6. AD-Integration (Sophomorix-konform, verifiziert)

- **Member-Registrierung:** die RADIUS-VM wird als **Device mit Rolle `server`** in
  `devices.csv` eingetragen + `linuxmuster-import-devices` auf dem DC ausgeführt →
  **ein** Maschinenkonto, konsistent mit Sophomorix (der offiziell dokumentierte
  Member-Server-Pfad, wie ein zusätzlicher Fileserver). Der Container joint danach
  mit `join_secret`.
- **Sophomorix bleibt Sophomorix:** USERS, die Gruppe `wifi`, die Rollengruppen und
  die Bind-User werden **nie** von Hand erzeugt — RADIUS **konsumiert** sie nur
  (LDAP-Reads).
- **LDAP-Bind** = der bestehende `global-binduser`
  (`CN=global-binduser,OU=Management,OU=GLOBAL,DC=…`); das Passwort liegt auf dem
  lmn-Server unter `/etc/linuxmuster/.secret/global-binduser` und wird als Secret
  auf die RADIUS-VM übertragen.

## 7. Zertifikate (EAP-CA) & Client-Rollout

- **Dedizierte, single-purpose EAP-CA**, verwaltet von der Control Plane
  (`lmnradius ca init` / `cert issue` / `ca export`). Root **~10 Jahre**,
  **OFFLINE**/passphrase-geschützt; Server-Zert mehrjährig, EKU **serverAuth**
  (`1.3.6.1.5.5.7.3.1`) + `eapOverLAN`, **SAN = `server_fqdn`**.
- **Verteilung:** CA + ein **gesperrtes WLAN-Profil** per Windows-**GPO**
  (Schwesterprojekt `linuxmuster-gpo-template`) und MDM-**`.mobileconfig`**. Pflicht-
  **Triade** am Client: Serverzertifikat-Prüfung **AN** + Trust-Anker = **genau
  diese eine EAP-Root** + **Servername gepinnt** + „neues Zertifikat akzeptieren"
  **AUS**.
- **Verworfen:** die linuxmuster-CA wiederverwenden (`/etc/linuxmuster/ssl`, breite
  Trust-Basis in jedem Schülergerät) und Let's Encrypt (90-Tage-Rotation bricht
  802.1X; jedes öffentliche Zert kann ohne Servername-Pinning impersonieren).
- **Load-bearing:** weil der innere MSCHAPv2-Faktor von PEAP kryptografisch schwach
  ist, ist die **erzwungene Serverzert-Prüfung (CA-Pinning) tragend, nicht optional**.

## 8. Datenfluss (Auth-Pfad, PEAP-MSCHAPv2)

1. Client assoziiert sich mit einer SSID; der **AP (NAS)** sendet ab **eigener IP**
   einen `Access-Request` an `udp/1812` (Accounting → `1813`). `clients.conf`
   erkennt ihn über das AP-Management-CIDR (`client_subnets`), nicht über eine
   Controller-IP.
2. **PEAP:** FreeRADIUS baut den äußeren **TLS-Tunnel** auf und präsentiert das
   **EAP-Server-Zertifikat**; der Client validiert es gegen die **gepinnte EAP-CA**
   und den **gepinnten Servernamen** (load-bearing, §7).
3. **Innerer MSCHAPv2:** `mschap` → `ntlm_auth --request-nt-key --allow-mschapv2`
   → `winbindd` → der AD DC prüft den **NT-Hash** (keine Passwörter/Hashes auf der
   RADIUS-VM).
4. **`rewrite_called_station_id`** (im **inner-tunnel**, da `copy_request_to_tunnel`
   das interne `Called-Station-SSID` nicht mitkopiert) zerlegt das kopierte
   `Called-Station-Id` (`<AP-MAC>:<SSID>`) und legt **`Called-Station-SSID`** an.
5. **Branch** auf `Called-Station-SSID` (virtual-server / `unlang`) → geforderte
   Gruppe je SSID (aus `ssids[].allowed_group`).
6. **Gruppen-Gate** via `rlm_ldap` (Bind als `global-binduser`, rekursiv); dessen
   LDAP-TLS terminiert ein lokales **stunnel** zum DC (ADR-015): Mitglied der
   geforderten Rollengruppe **und** der Sophomorix-Gruppe `wifi`?
7. **Ergebnis:** **Access-Accept** (Auth ok **und** Gruppe ok) bzw.
   **Access-Reject** (falsches Passwort, falsche SSID/Rolle, nicht in `wifi`).
8. **VLAN:** Default = statisches VLAN je SSID im UniFi-Profil; optional trägt der
   Accept die RFC-2868-Attribute (`Tunnel-Type=13`, `Tunnel-Medium-Type=6`,
   `Tunnel-Private-Group-Id=<vlan>`).

## 9. Ehrliche Grenzen (state candidly)

- **Separate-VM-Member** ist offiziell nur dünn dokumentiert → in der **crabbox-E2E
  bewiesen**, nicht angenommen. Die Proof-Matrix: Lehrer auf Lehrer-SSID →
  Access-Accept (+korrektes VLAN); Schüler auf Lehrer-SSID → Reject; falsches
  Passwort → Reject; Nicht-`wifi`-User → Reject.
- **Stateful Container:** Maschinenkonto-Secret im `/var/lib/samba`-Volume (Re-Join
  bei Verlust); `winbindd` als zweiter Daemon.
- **DC-`smb.conf`:** `ntlm auth = mschapv2-and-ntlmv2-only` kann durch Paket-Updates
  entfernt werden → überwachen.
- **read-only-Rootfs** für Samba-State teilweise gelockert.
- **RadSec** (UniFi Network ≥ 8.4, `tcp/2083`, Secret `radsec`) ist **zurückgestellt**.

---

**Siehe auch:** Entscheidungen mit verworfenen Alternativen → [`docs/decisions.md`](decisions.md);
verifizierte Fakten mit Quellen → [`docs/references.md`](references.md).
