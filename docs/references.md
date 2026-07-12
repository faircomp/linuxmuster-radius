<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Referenzen & verifizierte Fakten

Dokumentierte Grundlagen samt Quellen. Vor der Implementierung eines Formats/Verhaltens
die jeweilige **offizielle** Quelle erneut ziehen (siehe `CLAUDE.md`). Erhoben via
Research-Workflows am **2026-07-10** (Konfidenz in Klammern).

## linuxmuster.net 7 — Authentifizierung (Samba AD + Sophomorix)

- Identitäten kommen aus Samba AD DC + Sophomorix; RADIUS ist reiner Consumer, legt
  weder User, `wifi`-Gruppe, Rollengruppen noch Bind-User selbst an. (high)
  — https://github.com/linuxmuster/sophomorix4/wiki/objectClasses
- **Dokumentierter Default ist `ntlm_auth`/winbind**, nicht LDAP-Bind: `mschap` ruft
  `ntlm_auth` auf, der NT-Hash wird vom AD via winbind geprüft. Voraussetzung ist die
  Zeile `ntlm auth = mschapv2-and-ntlmv2-only` in der `smb.conf` sowie `freerad` in der
  Gruppe `winbindd_priv` (`usermod -a -G winbindd_priv freerad`). (high)
  — https://docs.linuxmuster.net/de/latest/systemadministration/network/radius/index.html
  — https://ask.linuxmuster.net/t/anleitung-tipp-radius-peap-mschapv2-mit-ntlm-auth-fuer-wpa2-enterprise/3956
- `ntlm_auth` selbst gruppengated nur über **`--require-membership-of` = genau EINE
  Gruppe** (dokumentiert für `DOMÄNE\wifi`) → Rollen-/Multi-Gruppen-Prüfung braucht
  zusätzlich `rlm_ldap`. (high)
  — https://docs.linuxmuster.net/de/latest/systemadministration/network/radius/index.html
- LDAP-Modul liefert Gruppen-/VLAN-Authorization: `ldaps://<server>`, post-auth
  `if (Ldap-Group == "teachers") { ... }`; VLAN-Zuweisung über LDAP-Gruppe. (high)
  — https://docs.linuxmuster.net/de/latest/systemadministration/network/radius/index.html
  — https://wiki.linuxmuster.net/community/anwenderwiki:freeradius
- WLAN-Zugang wird über die Gruppe **`wifi`** gesteuert (Default: neue User sind in
  `wifi`; Steuerung via `sophomorix-managementgroup`). (high)
  — https://docs.linuxmuster.net/de/latest/systemadministration/network/radius/index.html
- Dynamische VLANs bereits dokumentiert (`Tunnel-Type := VLAN`, `Tunnel-Medium-Type := 6`,
  `Tunnel-Private-Group-Id := <vlan>`); Community-Diskussion zu 802.1X „next level“. (high)
  — https://ask.linuxmuster.net/t/freeradius-dynamische-vlans-802-1x-next-level/9933
- Rollen im Attribut `sophomorixRole`; Gruppen `teachers`/`<schule>-teachers`,
  `students`/`<schule>-students` (default-school ohne Präfix). (high)
  — https://github.com/linuxmuster/sophomorix4/wiki/objectClasses

## linuxmuster.net 7 — CA & Zertifikate

- Interne CA unter `/etc/linuxmuster/ssl` (`cacert.pem`, `cakey.pem`); Erneuerung via
  `linuxmuster-renew-certs` (`-c ca,server,firewall`), Default `--days 7305` ≈ **20 Jahre**. (high)
  — https://docs.linuxmuster.net/de/latest/systemadministration/certificates/index.html
- Fehlende/kaputte CA-Certs blockieren die AD-Anbindung — Beleg für die zentrale Rolle
  von `/etc/linuxmuster/ssl`. (high)
  — https://github.com/linuxmuster/linuxmuster-base7/issues/52
- ⚠️ Diese linuxmuster-CA wird **nicht** als EAP-CA wiederverwendet (breite Trust-Basis,
  in jedes Schülergerät gepinnt) → dediziertes EAP-CA, siehe unten (ADR-005).

## linuxmuster.net 7 — LDAP-Bind-User

- Bind-Account = existierender **`global-binduser`**, DN
  `cn=global-binduser,ou=Management,ou=GLOBAL,dc=…`; Passwort auf dem lmn-Server unter
  `/etc/linuxmuster/.secret/global-binduser` (eine Zeile, kein Zeilenumbruch;
  `ldapsearch -x -y /etc/linuxmuster/.secret/global-binduser`). (high)
  — https://wiki.linuxmuster.net/community/anwenderwiki:scripting:ldapsearch
  — https://github.com/linuxmuster/sophomorix4/wiki/objectClasses
- Es existieren zusätzlich **eigene Bind-User pro Dienst/Schule** (Konzept dokumentiert;
  z. B. WebUI mit eigenem Passwort). (medium)
  — https://ask.linuxmuster.net/t/global-bindadmin-global-binduser-weitere-bind-user/9485
  — https://ask.linuxmuster.net/t/extra-bind-user-fuer-die-verschiedenen-ldap-anwendungen/8070

## linuxmuster.net 7 — Member-Registrierung (linuxmuster-conform)

- RADIUS-Server wird als **Device mit Rolle `server`** in `devices.csv` eingetragen +
  `linuxmuster-import-devices` auf dem DC → ein Maschinenkonto, sophomorix-konform
  (identischer Pfad wie ein zusätzlicher Fileserver). (high)
  — https://docs.linuxmuster.net/de/latest/setup/setup-file-server.html
- ⚠️ Ein Samba-**Member** betreibt (anders als DCs) kein periodisches DDNS; der
  konkrete Parameter `dns update = no` ist **NICHT verifiziert** (siehe unten).

## FreeRADIUS 3.2 + Samba AD — PEAP-MSCHAPv2

- PEAP-MSCHAPv2 erfordert winbind/`ntlm_auth`: das AD gibt den **NT-Hash nicht über
  LDAP** heraus, MSCHAPv2 braucht ihn → Container joint als AD-Member; `mschap` →
  `ntlm_auth --request-nt-key --allow-mschapv2`. (high)
  — https://wiki.samba.org/index.php/Authenticating_Freeradius_against_Active_Directory
  — https://deployingradius.com/documents/configuration/active_directory.html
- `rlm_ldap` für Authorization/VLAN (nicht für die MSCHAPv2-Authentifizierung selbst). (high)
  — https://wiki.samba.org/index.php/Authenticating_Freeradius_against_Active_Directory
- Per-SSID-Branching: Policy **`rewrite_called_station_id`** zerlegt `Called-Station-Id`
  (`<AP-MAC>:<SSID>`, RFC 3580) in **`Called-Station-SSID`** → auf SSID verzweigen
  (virtual-server). (high)
  — https://github.com/FreeRADIUS/freeradius-server/issues/982
- Enterprise-WiFi/802.1X-EAP-Referenz + arbeitendes Samba-Member-Muster (Docker,
  `entrypoint-member.sh`). (high / medium bzgl. Übertragbarkeit)
  — https://wiki.freeradius.org/guide/Enterprise-WiFi
  — https://github.com/robinrosenberger/freeradius4samba4

## EAP-Server-/Client-Zertifikate (ADR-005)

- Best Practice: **dediziertes privates CA nur für EAP**, sehr lange Laufzeit
  (Rollover-Problem vermeiden), `basicConstraints CA:TRUE`, gibt ausschließlich das
  Server-Cert aus. (high)
  — https://eduroam.ac.za/faq/certificates/
  — https://wiki.geant.org/spaces/H2eduroam/pages/121346323/EAP+Server+Certificate+considerations
  — https://www.freeradius.org/documentation/freeradius-server/4.0.0/reference/raddb/certs/index.html
  — https://www.freeradius.org/documentation/freeradius-server/4.0.0/howto/os/letsencrypt.html
- Server-Cert braucht EKU **serverAuth `1.3.6.1.5.5.7.3.1`**; zusätzlich
  eapOverLAN `1.3.6.1.5.5.7.3.14`. `SAN:DNS` = FQDN (= CN), **kein Wildcard**. (high /
  medium bzgl. Pflicht des eapOverLAN-OID — Microsoft verlangt nur serverAuth)
  — https://learn.microsoft.com/en-us/troubleshoot/windows-server/networking/certificate-requirements-eap-tls-peap
  — https://learn.microsoft.com/en-us/windows-server/networking/technologies/nps/nps-manage-cert-requirements
- PEAP-MSCHAPv2 ist kryptographisch schwach → **erzwungene Server-Cert-Validierung
  (CA-Pinning) ist tragend**, nicht optional; ohne sie MITM/Credential-Diebstahl. (high)
  — https://securew2.com/blog/peap-mschapv2-vulnerability
- Client-Pinning verpflichtend (vier Punkte): Server-Cert-Validierung AN + Trusted-CA = das EINE
  EAP-Root + Server-Name gepinnt + „neues Zertifikat akzeptieren“ AUS. (high)
  — https://eduroam.ac.za/faq/certificates/
  — https://support.apple.com/guide/deployment/connect-to-8021x-networks-depabc994b84/web
- Verteilung: Windows-GPO drückt Trusted-Root-CA + Wireless Network Policy
  (Mutual-Auth erzwungen) auf Clients; Apple via `.mobileconfig` (MDM). (high)
  — https://learn.microsoft.com/en-us/windows-server/networking/core-network-guide/cncg/server-certs/deploy-server-certificates-for-802.1x-wired-and-wireless-deployments
  — https://learn.microsoft.com/en-us/windows-server/identity/ad-fs/deployment/distribute-certificates-to-client-computers-by-using-group-policy
  — https://support.apple.com/guide/deployment/connect-to-8021x-networks-depabc994b84/web
- Root offline/passphrasengeschützt halten (eduroam-Empfehlung, kein Rollover-Zwang). (high)
  — https://eduroam.ac.za/faq/certificates/

## UniFi — RADIUS-Clients, VLAN, RadSec

- **Die Access Points sind der NAS** und senden Access-Requests aus der **eigenen IP**
  (der Controller ist kein Proxy) → `clients.conf` nutzt das **AP-Subnetz als CIDR**
  (ggf. mehrere Subnetze), nicht die Controller-IP. (high)
  — https://dannyda.com/2021/05/21/what-is-the-client-ip-address-for-freeradius-windows-radius-server-when-configuring-ubiquiti-unifi-controller-with-external-radius-server-non-unifi-radius-server/
  — https://help.ui.com/hc/en-us/articles/360015268353-Configuring-a-RADIUS-Server-in-UniFi
- **Ein** RADIUS-Profil kann von **mehreren SSIDs** genutzt werden; pro SSID
  WPA2/WPA3-Enterprise + gemeinsames Profil. (high)
  — https://evanmccann.net/blog/2021/11/unifi-advanced-wi-fi-settings
- `Called-Station-Id` = `<AP-MAC>:<SSID>` (RFC 3580) → SSID im RADIUS parsebar. (high)
  — https://community.ui.com/questions/free-radius-auth-using-Calling-Station-Id/ed94d0c2-3b5e-4eb3-85f8-2f7641cf0f98
  — https://github.com/FreeRADIUS/freeradius-server/issues/982
- VLAN: statisch pro SSID (Default) **oder** RADIUS-assigned via RFC 2868
  (`Tunnel-Type=13`, `Tunnel-Medium-Type=6`, `Tunnel-Private-Group-Id=<vlan>`); dafür muss
  der **„RADIUS assigned VLAN“-Toggle aktiviert** sein **und die VLAN/das Network in UniFi
  existieren** (sonst Fallback auf Default-VLAN). (high)
  — https://neilzone.co.uk/2021/09/using-freeradius-to-assign-vlans-for-unifi-wi-fi/
  — https://dannyda.com/2021/11/15/how-to-have-vlan-trunk-for-unifi-ubnt-ubiquiti-access-point-unifi-ap-on-unifi-controller-use-radius-freeradius-assigned-vlan-for-unifi-ap-etc-general-ideas/
  — https://www.ironwifi.com/blogs/ubiquiti-radius-setup-guide/
- MAC-Auth-Muster (`Calling-Station-Id`) als Referenz für Attribut-Handling. (high)
  — https://wiki.freeradius.org/guide/Mac-Auth
- **RadSec** (RADIUS over TLS) ab **UniFi Network 8.4**, **TCP/2083**, Shared Secret
  konstant **`radsec`** (per RadSec-RFC), gegenseitige Zert-Auth (AP-Client-Cert +
  RADIUS-Server-Cert). Im Projekt **zurückgestellt**. (high)
  — https://help.ui.com/hc/en-us/articles/360015268353-Configuring-a-RADIUS-Server-in-UniFi
  — https://securew2.com/radius/radsec-deployment

## NICHT VERIFIZIERT / offene Punkte

Diese Punkte sind bewusst offen und werden **im crabbox-E2E** bzw. am realen System
belegt, nicht angenommen:

- **`sophomorix-cleanup` vs. Fremd-/Computerkonten:** ob `sophomorix-cleanup` ein per
  `linuxmuster-import-devices` angelegtes bzw. ein „fremdes“ Computerkonto (RADIUS-Member)
  anfasst/entfernt, ist unbestätigt. (NICHT VERIFIZIERT)
- **Exakte Device-/Computer-OU:** in welcher OU das Member-Maschinenkonto konkret landet
  (`OU=…,OU=…,OU=SCHOOLS`/`GLOBAL`), ist nicht quellenbelegt. (NICHT VERIFIZIERT)
- **`dns update = no`:** dass der Member dank dieses `smb.conf`-Parameters kein DDNS
  betreibt, ist plausibel, aber der exakte Parameter/Default ist unbestätigt. (NICHT VERIFIZIERT)
- **Exakte Position des UniFi-„RADIUS assigned VLAN“-Toggles:** Existenz belegt, die genaue
  UI-Verortung (Network/WLAN-Advanced vs. RADIUS-Profil) variiert je Controller-Version. (NICHT VERIFIZIERT)
- **Per-Schule-Binduser-DN:** dass es je Schule einen eigenen Bind-User gibt, ist als
  Konzept belegt; der exakte DN pro Schule ist nicht bestätigt. (NICHT VERIFIZIERT)

## Ehrliche Grenzen (Projekt-Kontext)

- Separate-VM-Member ist offiziell nur dünn dokumentiert → im crabbox-E2E bewiesen, nicht
  angenommen. (medium)
- Container ist **stateful** (winbind-Maschinenkonto-Secret in `/var/lib/samba`-Volume;
  Re-Join bei Verlust) — Abweichung vom stateless Keytab-Modell von linuxmuster-squid. (high)
- ⚠️ Die Zeile `ntlm auth = mschapv2-and-ntlmv2-only` lebt in der **DC-`smb.conf`** und
  wird von Paket-Updates gelegentlich entfernt → nach Updates prüfen. (medium)

## Empirisch verifiziert an echter linuxmuster (2026-07-12, `EVSVBZ.ORG`, linuxmuster-base7 7.3.36)

Read-only gegen einen produktiven linuxmuster-Server geprüft (noch ohne Container/Join):
- `discover-ad-facts.sh` erkennt realm/workgroup/Base-DN/Bind-DN/Gruppen korrekt; der
  `[OK]`-Check für `ntlm auth = mschapv2-and-ntlmv2-only` griff (war gesetzt). (verifiziert)
- **LDAPS-Bind als `global-binduser` funktioniert** (extern, `require_cert=allow`); die
  Gruppe `teachers` liegt unter `OU=Teachers,OU=default-school,OU=SCHOOLS,DC=…`. (verifiziert)
- **Gruppenmodell der WLAN-User:** ein Lehrer ist memberOf `teachers` **und** `role-teacher`
  (+ `sophomorixRole=teacher`); ein `schooladministrator` ist in `wifi`, aber **weder**
  `teachers` **noch** `students` (memberOf `role-schooladministrator`, `admins`, …). → Das
  Per-SSID-Gate auf `Ldap-Group == teachers/students` trifft Lehrer/Schüler korrekt, **lässt
  aber schooladministrator/examuser durchfallen** (würde abgewiesen). (verifiziert)
- **Design-Konsequenz (offen):** vollständigere Gate-Signale sind die `role-<rolle>`-Gruppen
  bzw. das Attribut `sophomorixRole` — je nachdem, ob Admins/Examuser WLAN bekommen sollen.
  Pro Einsatz zu entscheiden. (offen)
- `all-*`/`global-*` sind **schulübergreifende Aggregatgruppen**, keine Schulen (discover-Skript
  korrigiert). (verifiziert)

### Voller Laufzeit-Test: Container-Join + PEAP-MSCHAPv2 gegen den echten DC (2026-07-12)

In einer Wegwerf-VM mit DC-Sicht (Docker, `--dns` auf den DC) wurde das gebaute Image mit
temporären Test-Konten end-to-end geprüft. Der Test deckte fünf Laufzeit-Bugs auf, die
`radiusd -XC` und statische Reviews **nicht** finden konnten — alle behoben und re-verifiziert:

- **Join als Member funktioniert**, aber nur mit einem **Admin-/delegierten Konto** (`JOIN_SECRET`):
  ein einfacher Benutzer scheitert mit `Insufficient access`. Ausserdem: `net ads join` darf
  **kein** `MEMBER`-Positional bekommen (das ist `net rpc join`), und `kerberos method = secrets
  only` ist nötig, sonst schlägt der Join am schreibgeschützten `/etc/krb5.keytab` fehl. (verifiziert)
- **`rlm_ldap` über `ldaps://` bringt den *threaded* Server zum Absturz** (libldap=GnuTLS vs.
  FreeRADIUS=OpenSSL) — Sekunden nach „Ready to process requests". Fix: LDAP-TLS in einem lokalen
  **stunnel** terminieren (`rlm_ldap` → Klartext `127.0.0.1` → stunnel/OpenSSL → DC:636). (verifiziert)
- **Supervisor riss gesunde Container ab:** `kill -0` auf den `freerad`-eigenen radiusd gibt unter
  `--cap-drop ALL` (kein `CAP_KILL`) `EPERM` = „tot". Fix: Liveness über `/proc/<pid>`. (verifiziert)
- **`Called-Station-SSID` erreichte den PEAP-Tunnel nicht** (interne Attribute werden von
  `copy_request_to_tunnel` nicht übertragen). Fix: `rewrite_called_station_id` im *inner-tunnel*
  ausführen (das volle `Called-Station-Id` wird kopiert). (verifiziert)
- **`wbinfo -t` gegen den echten DC: „RPC calls succeeded".** (verifiziert)

Auth-Matrix (winbind `ntlm_auth`, echte Konten): Lehrer in `wifi` + richtiges PW → `NT_STATUS_OK`;
falsches PW → `NT_STATUS_WRONG_PASSWORD`; Konto **nicht** in `wifi` → `NT_STATUS_LOGON_FAILURE`
(wifi-Gate greift). (verifiziert)

Voller PEAP-Handshake (`eapol_test`, echter Lehrer): Server-Cert validiert, inner MSCHAPv2 gegen
den DC erfolgreich, und auf SSID `…-Lehrer` → **Access-Accept mit `Tunnel-Private-Group-Id=20`**
(Tunnel-Type=VLAN, Medium=IEEE-802). Lehrer auf `…-Schueler` (Rollen-Gate) und unbekannte SSID →
**Reject**. Die komplette Kette EAP-Cert → PEAP → winbind → `rlm_ldap`-Gruppen-Gate → VLAN ist
damit gegen einen echten linuxmuster-DC bestätigt. (verifiziert)
