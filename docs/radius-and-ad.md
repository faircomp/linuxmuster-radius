<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# RADIUS & Active Directory — Member-Join, winbind & ntlm_auth

Statusdokument. Mit jeder wesentlichen Änderung aktuell halten (siehe `CLAUDE.md`
→ Dokumentationspflege). Dieses Dokument ist das AD-Pendant zu squids
`keytab-and-dns.md`: es beschreibt, **warum** die RADIUS-VM ein Samba-AD-**Member**
sein muss, **wie** sie linuxmuster-konform als AD-Objekt registriert wird, welche
DNS-/Zeit-/Hostname-Vorbedingungen hart sind und welche drei Secrets die AD-Anbindung
trägt. Die Entscheidungen mit Begründung und verworfenen Alternativen:
[`decisions.md`](decisions.md) (ADR-003 winbind/Join, ADR-006 Member-Registrierung);
die verifizierten Fakten mit Quellen: [`references.md`](references.md); die dedizierte
EAP-CA und das Client-Pinning: [`certs-and-ca.md`](certs-and-ca.md); der Client-Rollout
per GPO/MDM: [`deployment-gpo.md`](deployment-gpo.md).

## 1. Auth-Modell — warum die RADIUS-VM ein AD-Member ist

Die Authentifizierung ist **PEAP-MSCHAPv2** (ADR-003). Der äußere PEAP-TLS-Tunnel
schützt einen inneren **MSCHAPv2**-Faktor, und MSCHAPv2 braucht auf der Serverseite den
**NT-Hash** des Nutzers, um die Challenge/Response zu prüfen. Genau hier liegt der
Zwang zum Member-Join: **das AD gibt den NT-Hash nicht über LDAP heraus** — es prüft ihn
nur selbst. Ein LDAP-Bind (wie ihn squid für seine Gruppen-ACL nutzt) kann MSCHAPv2 also
**nicht** bedienen.

Der Ausweg ist das Samba-Standardmuster: Der Container tritt der Samba-AD als
**Domänen-Mitglied** bei und betreibt einen eigenen `winbindd`. Der Auth-Pfad:

```
mschap  →  ntlm_auth --request-nt-key --allow-mschapv2  →  winbindd  →  AD DC prüft den NT-Hash
```

Auf der RADIUS-VM liegen dabei **keine Passwörter und keine Hashes** — `ntlm_auth`
reicht die MSCHAPv2-Challenge/Response an den DC weiter, der DC antwortet nur mit
„gültig/ungültig". Belegt in SambaWiki „Authenticating FreeRADIUS against Active
Directory" (siehe [`references.md`](references.md)).

**Member-seitige Voraussetzungen** (vom Image gebacken, siehe
`image/templates/smb.conf.template` + `image/entrypoint.sh`):

- `security = ADS`, `realm = ${REALM}` (UPPERCASE Kerberos-Realm),
  `workgroup = ${WORKGROUP}` (NetBIOS-Kurzname).
- `kerberos method = secrets and keytab` — damit das von `net ads join` geschriebene
  Maschinenkonto-Secret (in `secrets.tdb`) tatsächlich genutzt wird.
- `winbind use default domain = yes` — EAP-Identitäten kommen als nackter
  `sAMAccountName` ohne `DOMÄNE\`-Präfix an.
- `ntlm auth = mschapv2-and-ntlmv2-only` — der moderne Samba-Default `ntlmv2-only`
  **verbietet** MSCHAPv2; die Zeile schaltet es frei (auf dem Member **und** auf dem DC,
  siehe § 4).
- Der Service-User **`freerad`** muss in der Gruppe **`winbindd_priv`** sein
  (Build-Zeit-Schritt `usermod -aG winbindd_priv freerad`), damit `ntlm_auth` — von
  `radiusd` als `freerad` gestartet — die privilegierte winbind-Pipe
  `/var/lib/samba/winbindd_privileged` (`root:winbindd_priv`, `0750`) lesen darf. Ohne
  diese Mitgliedschaft scheitert PEAP-MSCHAPv2 stumm. Der Entrypoint re-asserted nur die
  Verzeichnisrechte; die Gruppenmitgliedschaft ist ein `/etc`-Schritt und daher
  build-time (read-only rootfs zur Laufzeit).

**Ehrliche Grenze — der Container ist stateful.** Der Member-Join schreibt ein
**Maschinenkonto-Secret** (`secrets.tdb`), das auf dem **persistenten
`/var/lib/samba`-Volume** liegt und Restarts/Recreates überlebt. Geht das Volume
verloren, muss die Instanz **neu joinen** (Re-Join). Das und der zweite Daemon
(`winbindd` neben `radiusd`, klein-supervidiert) sind eine bewusste **Abkehr vom
zustandslosen Keytab-Modell** des Schwesterprojekts squid (ADR-003).

## 2. Member-Registrierung — der linuxmuster-Weg

Das Maschinenkonto wird **nicht** von Hand mit `samba-tool`/`net` angelegt, sondern
**sophomorix-konform** über den offiziell dokumentierten Member-Server-Pfad (identisch
zu einem zusätzlichen Fileserver), ADR-006:

1. Die RADIUS-VM als **Device mit Rolle `server`** in die **`devices.csv`** eintragen.
2. Auf dem DC **`linuxmuster-import-devices`** laufen lassen → ein Maschinenkonto,
   konsistent mit Sophomorix.

**Sophomorix bleibt Sophomorix.** USERS, die WLAN-Gruppe `wifi`, die Rollengruppen
(`teachers`/`students` bzw. `<schule>-teachers`/`<schule>-students`) und die Bind-User
werden **nie** von Hand erzeugt — RADIUS **konsumiert** sie nur (LDAP-Reads, § 3). Das
einzige AD-Objekt, das dieses Projekt einbringt, ist das eine Maschinenkonto der
RADIUS-VM.

**Der Join-Ablauf im Container** (`image/entrypoint.sh`, einmalig, als root):

- `net ads testjoin` prüft das Maschinenkonto-Secret + den Secure Channel **ohne**
  `winbindd`. Ein bereits befülltes `/var/lib/samba`-Volume ⇒ schon gejoint ⇒ **kein**
  erneuter Join.
- Sonst: `net ads join MEMBER --configfile=<smb.conf> -A <authfile>`. Die Join-Credentials
  liefert das **`JOIN_SECRET`** als **Samba-`-A`-Authfile** (§ 6):

  ```
  username = <join-berechtigtes-konto>
  password = <passwort>
  domain   = <WORKGROUP>
  ```

  Der Entrypoint kopiert dieses Authfile als `root:root 0600` auf tmpfs und nutzt es
  **genau einmal** für den Join — `radiusd`/`freerad` sehen es nie.

**Join-Konto — TEILVERIFIZIERT (Live-E2E gegen eine echte linuxmuster, 2026-07-12,
Protokoll in [`references.md`](references.md)).** Verifiziert ist: ein **einfacher
Benutzer kann nicht joinen** (`Insufficient access … does not have administrator
privileges`); mit dem **linuxmuster-Administrator** (bzw. einem delegierten Konto mit
dem Recht, Maschinenkonten anzulegen/zurückzusetzen) läuft `net ads join` durch —
genau das gehört in das `JOIN_SECRET`-Authfile. Ebenfalls aus dem E2E: `net ads join`
nimmt **kein** `MEMBER`-Positional (das ist `net rpc join`-Syntax). **WEITERHIN OFFEN**
ist allein die Adoptionsfrage: ob `devices.csv` mit Rolle `server` +
`linuxmuster-import-devices` das Computerkonto **vorab** so anlegt, dass der spätere
Join es **sauber adoptiert** (im E2E existierte kein vorab angelegtes Konto — der Join
erzeugte es selbst). Siehe ADR-006 „Offen (E2E)".

## 3. LDAP-Bind — der `global-binduser` (nur Authorization)

Der zweite AD-Kontakt ist **rlm_ldap**, und er ist **strikt von der MSCHAPv2-Prüfung
getrennt**: LDAP liefert **ausschließlich die Gruppen-/VLAN-Authorization** (ist der
User Mitglied der für die SSID geforderten Rollengruppe **und** von `wifi`?), **nie**
die Authentifizierung selbst — die läuft über winbind (§ 1).

- **Bind-Account = der bestehende `global-binduser`**, DN
  `CN=global-binduser,OU=Management,OU=GLOBAL,DC=…` — kein neu angelegter, kein Admin.
- **Passwort:** auf dem lmn-Server unter `/etc/linuxmuster/.secret/global-binduser`
  (eine Zeile, kein Zeilenumbruch). Es wird als Secret **`LDAP_BIND_SECRET`** (§ 6) auf
  die RADIUS-VM übertragen; der Entrypoint liest den Wert in `LDAP_BIND_PW` und rendert
  ihn in das `ldap`-Modul (`0640`, `freerad`) — nie in Env oder Log.
- **Transport:** Der Operator setzt `ldaps://<dc>`; intern spricht `rlm_ldap` jedoch
  Klartext-LDAP zu einem **lokalen stunnel**, das die TLS-Verbindung zum DC terminiert
  (der `libldap`-GnuTLS-vs-OpenSSL-Crash im threaded Server, ADR-015). Die per-SSID-
  Verzweigung (`Called-Station-SSID` → geforderte Gruppe) beschreibt ADR-007 (Ableitung
  im inner-tunnel).
- **DC-Zertifikat wird geprüft (seit 7.3.1 Pflicht für neue Instanzen).** Über genau
  diese Verbindung laufen das Rollen-Gate und die VLAN-Entscheidung; ohne Prüfung könnte
  ein On-Path-Angreifer zwischen RADIUS-VM und DC die Gruppenabfrage beantworten
  ([`threat-model.md`](threat-model.md)). stunnel verifiziert deshalb das DC-Zertifikat
  gegen das mit `--ldap-ca <datei>` gepinnte PEM-Bündel (Instanz-Feld `ldap_ca`,
  Datei `certs/<instanz>/ldap-ca.pem`, `0600`, read-only in den Container gemountet als
  `LDAP_CA`). Auf einem linuxmuster.net-Server ist das die linuxmuster-CA
  `/etc/linuxmuster/ssl/cacert.pem` — sie signiert das DC-Zertifikat aus `tls certfile`
  in der DC-`smb.conf`; `provision-radius-account.sh` legt sie als `ldap-ca.pem` neben
  die Secrets. Zwei Pin-Modi, vom Entrypoint am **Inhalt** der Datei erkannt:
  - **chain** — die Datei enthält einen Trust-Anker (selbstsigniertes CA-Zertifikat):
    Kettenprüfung bis zur CA plus Hostnamen-Check (`checkHost`, CN-Fallback: das
    linuxmuster-DC-Zertifikat trägt keinen SAN). Überlebt eine Erneuerung des
    DC-Zertifikats.
  - **peer** — nur Endzertifikat(e), typisch nach `--ldap-ca-tofu` (der DC liefert die
    CA nicht in der Kette): das DC-Zertifikat selbst ist gepinnt (`verifyPeer`); ein
    erneuertes DC-Zertifikat schlägt **fail-closed** fehl, bis `lmnradius set-ldap-ca`
    neu pinnt.
  Fehlt die Prüfung (Instanzen aus ≤ 7.3.0 ohne `ldap_ca`), läuft LDAPS **unverifiziert**
  weiter; `lmnradius list`/`health` warnen mit „LDAPS unverified", der Container loggt
  `WARN: LDAPS ... is UNVERIFIED`. Upgrade-Schritt: `lmnradius set-ldap-ca <instanz>
  --ldap-ca /pfad/cacert.pem` ([`install.md`](install.md) § 9). Schlägt die Prüfung fehl
  (falsche CA, manipulierter DC), scheitert bereits der `rlm_ldap`-Verbindungsaufbau beim
  Start: der Container geht **nicht** in Betrieb (fail-closed, in der Fix-Runde
  2026-09-22 negativ getestet) statt stumm ohne Prüfung zu authentisieren.

## 4. DC-seitige Voraussetzung — `ntlm auth`

Die Zeile

```
ntlm auth = mschapv2-and-ntlmv2-only
```

muss **auf dem DC** in `/etc/samba/smb.conf` stehen. Der Member setzt sie ebenfalls (das
Image tut das, § 1), aber der DC ist es, der die MSCHAPv2-NT-Response am Ende
akzeptiert — **fehlt die Zeile dort, scheitert jeder WLAN-Login**, obwohl die
Member-/RADIUS-Seite korrekt konfiguriert ist.

**⚠️ Überwachen.** DC-Paket-Updates (Samba) **entfernen diese Zeile gelegentlich**
wieder. Sie liegt außerhalb der Reichweite dieses Containers (fremder Host) → nach jedem
DC-Update prüfen, dass sie noch da ist, sonst brechen alle Anmeldungen flottenweit.
Belegt in SambaWiki „Authenticating FreeRADIUS against Active Directory" und im
Image-Kommentar (`image/templates/smb.conf.template`, „HONEST LIMIT").

## 5. DNS, Zeit & Hostname (hart)

Kerberos und der AD-Join sind gegenüber Namensauflösung und Uhr **unnachgiebig**. Diese
Punkte sind Vorbedingungen, keine Optionen:

- **Container-Hostname == `SERVER_FQDN`.** Die Control Plane setzt den Container-Hostname
  auf `server_fqdn` (docker-py `hostname=…`); Docker trägt ihn zusätzlich in
  `/etc/hosts` ein, damit die **Vorwärts-DNS-Auflösung**, die der Join braucht, **im
  Container aufgeht**. Stimmen Hostname und FQDN nicht überein, scheitern die
  Kerberos-SPN-Kanonisierung und der Join.
- **Vorwärts-DNS muss auflösen.** Für `SERVER_FQDN` (das ist zugleich der `SAN`/CN des
  EAP-Server-Zertifikats und der von Clients gepinnte Servername, siehe
  [`certs-and-ca.md`](certs-and-ca.md)) ein **A-Record**; der DC muss per SRV/Name
  erreichbar sein. **Woher der A-Record kommt:** der Join läuft mit `--no-dns-updates`
  (bis 7.3.0 registrierte `net ads join` die **Docker-Bridge-Adresse** `172.17.0.x` des
  Containers als A-Record — unerreichbar für alles, was den RADIUS-Namen über das AD-DNS
  auflöst). Stattdessen registriert der Container **bei jedem Start** mit dem
  Maschinenkonto (`net ads dns register -P`) die **LAN-Adresse der RADIUS-VM**, die die
  Control Plane als `HOST_IP` übergibt (automatisch: die Adresse, über die der DC
  erreicht wird, sonst das Default-Route-Interface; fest per `host_ip:` in
  `config.yml`). Ein Host, der beim Upgrade von 7.3.0 noch den Bridge-Record trägt,
  korrigiert ihn damit selbst. Auf dem devices.csv-Weg legt `linuxmuster-import-devices`
  den Record an; kann das Maschinenkonto ihn nicht überschreiben, bleibt es bei einer
  `WARN`-Zeile im Container-Log (Record dann von Hand prüfen: `host <fqdn>` auf dem DC).
- **NTP-Skew < 5 min.** Die Uhr der RADIUS-VM muss mit dem DC synchron sein (Kerberos-
  Toleranz), sonst `KRB_AP_ERR_SKEW` beim Join.
- **Kein Reverse-DNS-Zwang.** Das Image backt `/etc/krb5.conf` mit **`rdns` /
  `dns_canonicalize_hostname` aus** und `/etc/ldap/ldap.conf` mit **`SASL_NOCANON on`**
  (Entrypoint), damit der DC-Prinzipal **literal** (per SRV) gebildet wird und nicht über
  einen PTR — das entkoppelt Join und LDAPS-Bind von der Reverse-Zone.

## 6. Secrets — Übersicht

Drei Secrets tragen die AD-Anbindung. Alle liegen als **Datei** im `secrets_dir` (Default
`/etc/linuxmuster-radius/secrets`), Dateiname == die in der Instanz-YAML/`lmnradius
create` angegebene **Secret-Referenz**, Modus **`0600`**. **Nie** in Git, **nie** in
Logs, **nie** in die Instanz-YAML — dort steht nur der Dateiname, nie der Wert.

| Secret (CLI-Flag) | Inhalt | Zustellung an den Container |
| --- | --- | --- |
| **`join_secret`** → `JOIN_SECRET` | Samba-`-A`-Authfile (`username=`/`password=`/`domain=`) für den Domänen-Join | Datei **read-only gemountet** unter `/run/secrets/<name>`; Env `JOIN_SECRET` zeigt darauf. Vom Entrypoint **einmal** als root für `net ads join` genutzt (§ 2). |
| **`ldap_bind_secret`** → `LDAP_BIND_SECRET` | Passwort des `global-binduser` (§ 3) | Datei **read-only gemountet**; Env `LDAP_BIND_SECRET` zeigt darauf. Entrypoint liest den Wert und rendert das `ldap`-Modul. |
| **`radius_secret`** | AP-Shared-Secret für `clients.conf` (NAS ↔ RADIUS) | **Nicht** in den Container gemountet: die **Control Plane** liest den Wert aus `secrets_dir/<name>` und rendert ihn in die `clients.conf`, die read-only nach `/etc/lmnradius/instance.d` gemountet wird. Einzeilig, ohne Zeilenumbruch. |

Ergänzend hält der Container das **Maschinenkonto-Secret** selbst auf dem persistenten
`/var/lib/samba`-Volume (aus dem Join, § 1) — das ist kein vom Betreiber gepflegtes
Secret, sondern generierter Zustand. Die EAP-Zertifikate/-Schlüssel (`EAP_CA`,
`EAP_CERT`, `EAP_KEY`) sind separat und in [`certs-and-ca.md`](certs-and-ca.md)
beschrieben; die gepinnte **DC-CA** für LDAPS (`ldap_ca` → `LDAP_CA`, § 3) liegt bei
ihnen unter `certs/<instanz>/ldap-ca.pem` — kein Secret, aber `0600`.

**Domäne verlassen — `lmnradius rm`.** Der Rückbau ist seit 7.3.1 vollständig: `rm`
stoppt den Container, startet dasselbe Image einmalig mit dem Kommando `leave` (gleiche
Env, Secrets und Volume), das den A-Record von `SERVER_FQDN` entfernt (`net ads dns
unregister`, Maschinenkonto, Fallback Join-Konto) und mit dem Join-Konto `net ads leave`
ausführt (Computerkonto gelöscht), und entfernt danach Container, Samba-Volume, gerenderte
Config und den Instanz-Datensatz. Bleibt `certs/<instanz>/` (EAP-Server-Zert/-Key +
`ldap-ca.pem`) — bewusst, von Hand löschen. Scheitert das Verlassen (DC nicht erreichbar),
wird lokal trotzdem aufgeräumt und die CLI meldet rot, was auf dem DC zu tun ist
(`samba-tool computer delete <NAME>$`, A-Record). Auf dem devices.csv-Weg zusätzlich die
Zeile aus `devices.csv` entfernen, sonst legt der nächste Import das Konto neu an.

Alle Mount-Quellen werden **fail-closed** geprüft: fehlt eine Secret- oder
Zertifikatsdatei, startet die Instanz **nicht** (die Prüfung läuft, bevor der laufende
Container angefasst wird — ein Secret-/Config-Problem verursacht nie Downtime).

---

**Siehe auch:** Entscheidungen mit verworfenen Alternativen →
[`decisions.md`](decisions.md) (ADR-003 winbind/Join, ADR-006 Member-Registrierung,
ADR-007 per-SSID-Gate); verifizierte Fakten mit Quellen → [`references.md`](references.md);
Gesamtbild (Control/Data Plane, Auth-Pfad) → [`architecture.md`](architecture.md)
(§ 6 + § 8); EAP-CA & Client-Pinning → [`certs-and-ca.md`](certs-and-ca.md);
Client-Rollout per GPO/MDM → [`deployment-gpo.md`](deployment-gpo.md).

