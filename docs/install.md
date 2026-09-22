<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Installation & Einrichtung — linuxmuster-radius

Statusdokument. Diese Anleitung führt **von der leeren VM bis zum funktionierenden
WPA2/WPA3-Enterprise-WLAN** mit Rollen-VLANs. Für die Tiefe je Thema:
[`operations.md`](operations.md) (Day-2), [`radius-and-ad.md`](radius-and-ad.md) (AD),
[`certs-and-ca.md`](certs-and-ca.md) (Zertifikate), [`deployment-gpo.md`](deployment-gpo.md)
(UniFi/OPNsense/GPO), [`architecture.md`](architecture.md) (Gesamtbild).

> **Reifegrad (ehrlich):** Die **Control-Plane ist bewiesen** (116 Tests), und **genau das
> Data-Plane-Image, das dieses `.deb` gepinnt ausliefert**, wurde gegen einen produktiven
> linuxmuster-DC verifiziert (zuletzt 2026-08-05) — mit **frischem Zustandsvolume**, also dem
> Pfad einer Neuinstallation: Member-Join, `healthy` nach 18 s, und die **Auth-Matrix 7/7**
> (Lehrer → Accept in VLAN 20, Schüler → VLAN 10; falsche Rolle / unbekannte SSID / kein
> `wifi` / falsches Passwort → je Reject), inklusive Mehrschul-Basis-Gate `all-wifi`.
> Protokoll in [`references.md`](references.md). Zusätzlich ist das Setup in einer realen
> Schul-Testumgebung (echte APs, echte Domänen-Clients, Samba-AD) im Pilotbetrieb erprobt.
> **Nicht** an deiner Umgebung bewiesen sind deine konkreten Fakten (Realm, Gruppen, SSIDs,
> VLANs, AP-Subnetze) und der `devices.csv`-Rolle-`server`-Pfad — deshalb bleibt die Abnahme
> in **Schritt 8** Pflicht. Behandle die Erstinstallation als **kontrollierte Inbetriebnahme
> mit Abnahme**.

## Überblick
- **Control-Plane** (`.deb`): FastAPI-Dienst + `lmnradius`-CLI auf der RADIUS-VM, an
  `127.0.0.1:8080`. Verwaltet die Container über die Docker-Engine.
- **Data-Plane** (Docker-Image von GHCR): **eine** FreeRADIUS-Instanz, die die Domäne als
  Member joint und PEAP-MSCHAPv2 gegen das AD prüft; **SSIDs sind Config**, VLAN pro Rolle.

## 0. Voraussetzungen
- Eigene **VM (Ubuntu 24.04)**, statische IP im Management-VLAN, **Docker**.
- Zugriff auf **linuxmuster-DC**, **UniFi-Controller** und **OPNsense**.
- Fakten bereitlegen (liefert `discover-ad-facts.sh`, Schritt 2): `realm`, `workgroup`,
  Base-DN, `wifi`-Gruppe, VLAN-IDs (Lehrer/Schüler), AP-Management-Subnetz(e), RADIUS-FQDN.

## 1. RADIUS-VM — Docker + `.deb`
```bash
curl -fsSL https://get.docker.com | sh                      # Docker
# neueste Release-Version (Tag v7.3.N) von GitHub holen — oder VER=7.3.N von Hand setzen
VER=$(curl -fsSL https://api.github.com/repos/faircomp/linuxmuster-radius/releases/latest \
      | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p')
: "${VER:?konnte die neueste Version nicht von der GitHub-API lesen - VER=7.3.N von Hand setzen}"
curl -fsSLo lmnradius.deb \
  https://github.com/faircomp/linuxmuster-radius/releases/download/v${VER}/linuxmuster-radius_${VER}_all.deb
sudo apt install -y ./lmnradius.deb
lmnradius health                                            # {"status":"ok"}
```
Der `postinst` legt den System-User `lmnradius`, `/etc/linuxmuster-radius/{config.yml (0600,
zufälliges Token),secrets,certs}` und das State-Verzeichnis als Git-Repo an (jede Instanz-
Änderung ein Commit, `git -C /var/lib/linuxmuster-radius/instances log`) und startet den
Dienst. Das Image (`ghcr.io/faircomp/linuxmuster-radius`, **public**) wird bei `reconcile`
gezogen.
*(Optionale Härtung: `deploy/docker-socket-proxy.yml` starten und `docker_host: "tcp://127.0.0.1:2375"` in `config.yml` setzen.)*

## 2. Auf dem linuxmuster-DC — AD vorbereiten
Die beiden Helferskripte direkt aus dem Release laden (auf dem DC gibt es kein Repo-Checkout):
```bash
VER=$(curl -fsSL https://api.github.com/repos/faircomp/linuxmuster-radius/releases/latest \
      | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p')   # dieselbe Version wie in Schritt 1
: "${VER:?konnte die neueste Version nicht von der GitHub-API lesen - VER=7.3.N von Hand setzen}"
BASE=https://raw.githubusercontent.com/faircomp/linuxmuster-radius/v${VER}/scripts
curl -fsSLO ${BASE}/discover-ad-facts.sh
curl -fsSLO ${BASE}/provision-radius-account.sh

sudo bash discover-ad-facts.sh                            # read-only: Fakten -> fertige create-Vorlage
sudo bash provision-radius-account.sh radius <MAC> <IP>   # <hostname> <mac> <ip> der RADIUS-VM!
```
`provision-radius-account.sh` **braucht die drei Argumente** (Hostname/MAC/IP der RADIUS-VM),
trägt sie als Device (Rolle `server`) in die `devices.csv` ein, fragt interaktiv das
Join-Konto und das AP-Shared-Secret ab und legt die drei Secret-Dateien plus die
linuxmuster-CA (`ldap-ca.pem`, für die LDAPS-Prüfung in Schritt 5) unter
`./radius-secrets/` an — es zeigt dabei **nie** ein Secret an. Es arbeitet bewusst
vorsichtig: Backup der `devices.csv` vor jedem Anhängen, idempotent (vorhandener Host wird
übersprungen), und mit `DRY_RUN=1` zeigt es die Zeile nur an, ohne zu schreiben.

> **`linuxmuster-import-devices` startet das Skript absichtlich NICHT selbst** — den Import
> stößt du danach bewusst an. Er verarbeitet die **gesamte** `devices.csv` (DHCP/DNS/AD für
> **alle** Geräte, nicht nur das neue) — wie bei jeder Geräteaufnahme also am besten in einem
> ruhigen Moment ausführen und die neue Zeile vorher kurz gegenlesen.
> In der **DC-`/etc/samba/smb.conf`** muss `ntlm auth = mschapv2-and-ntlmv2-only` stehen
> (sonst schlägt jeder WLAN-Login fehl; Paket-Updates entfernen die Zeile gern).
> **Nutzer/Gruppen/`wifi`/`global-binduser` bleiben reine Sophomorix-Welt** — nichts von Hand anlegen.

## 3. Secrets auf die RADIUS-VM übertragen (`/etc/linuxmuster-radius/secrets/`)
Die Dateien aus Schritt 2 (Namen **unverändert lassen** — sie sind die
`--…-secret`-Referenzen in Schritt 5):

| Datei | Inhalt |
|---|---|
| `join.authfile` | Domänen-Beitritts-Authfile (samba `-A`) — **Administrator- bzw. delegiertes Konto** (ein einfacher Benutzer kann nicht joinen, verifiziert) |
| `ldap-bind.secret` | das `global-binduser`-Passwort (kopiert das Skript automatisch vom DC) |
| `radius.secret` | das WLAN-Shared-Secret (identisch zum UniFi-RADIUS-Profil, Schritt 6) |
| `ldap-ca.pem` | die linuxmuster-CA `/etc/linuxmuster/ssl/cacert.pem` (kein Secret; sie signiert das LDAPS-Zertifikat des DC und wird in Schritt 5 mit `--ldap-ca` gepinnt) |

```bash
# auf dem DC: übertragen, Besitz/Rechte setzen, Kopie auf dem DC löschen
scp radius-secrets/* root@<radius-vm>:/etc/linuxmuster-radius/secrets/
ssh root@<radius-vm> 'chown lmnradius:lmnradius /etc/linuxmuster-radius/secrets/* \
  && chmod 600 /etc/linuxmuster-radius/secrets/*'
rm -rf radius-secrets
```
> Der `chown lmnradius:` ist nötig: der Dienst läuft als `lmnradius` und muss die Dateien
> lesen können — nach `scp` als root gehören sie sonst root.

## 4. EAP-CA anlegen
```bash
sudo lmnradius ca init                        # dedizierte EAP-Root (Passphrase!)
```
> **Empfehlung:** den Root-Key nach dem Ausstellen **offline** nehmen (siehe `certs-and-ca.md`).

## 5. Instanz + Server-Zertifikat

**Welche Gruppe je SSID?** Das ist die eine Entscheidung, die du hier bewusst treffen musst:

| Ziel | `--ssid`-Gruppe | Hinweis |
|---|---|---|
| **Lehrer/Schüler ALLER Schulen** in je einer SSID (Regelfall) | `role-teacher` / `role-student` | schulunabhängig, von Sophomorix aus `sophomorixRole` befüllt und **direkt** am Nutzer hinterlegt |
| Nur eine bestimmte Schule | `<schule>-teachers` / `<schule>-students` (Default-Schule: `teachers`/`students`) | pro Schule eine eigene SSID |

> **Nicht `all-teachers` verwenden.** Die `all-*`-Gruppen sind **verschachtelt** (`all-teachers`
> enthält die Gruppe `teachers`, nicht die Nutzer), und AD führt `memberOf` nur **direkt**, nicht
> transitiv. Das Gate prüft über `memberOf` — ein Gate auf `all-teachers` weist deshalb **jeden
> Lehrer ab**. Verifiziert an einer echten linuxmuster (siehe [`references.md`](references.md)).
> **`role-teacher` erfüllt denselben Zweck** und funktioniert.

> **Randfall:** Schuladministratoren sind in `role-schooladministrator`, **nicht** in
> `role-teacher` — sollen sie ins Lehrer-WLAN, brauchen sie eine eigene SSID/Gruppe.

> **Mehrere Schulen? Zusätzlich `--wifi-group all-wifi` setzen.** Das WLAN-Grund-Gate läuft
> über das winbind-**Token** und ist — anders als das SSID-Gate — **transitiv** (am echten DC
> verifiziert): `all-wifi` deckt damit `wifi`, `<schule>-wifi` und jede künftige Schule ab.
> Mit dem Default `--wifi-group wifi` kämen nur Nutzer der Default-Schule durch das Grund-Gate.

```bash
sudo lmnradius create --name meineschule \
  --server-fqdn radius.linuxmuster.lan \
  --realm LINUXMUSTER.LAN --workgroup LINUXMUSTER \
  --ldap-server ldaps://dc.linuxmuster.lan \
  --ldap-base-dn OU=SCHOOLS,DC=linuxmuster,DC=lan \
  --ldap-bind-dn CN=global-binduser,OU=Management,OU=GLOBAL,DC=linuxmuster,DC=lan \
  --ldap-ca /etc/linuxmuster-radius/secrets/ldap-ca.pem \
  --client-subnet 10.0.0.0/16 \
  --ssid lehrer-wlan:role-teacher:20 \
  --ssid schueler-wlan:role-student:10 \
  --join-secret join.authfile --ldap-bind-secret ldap-bind.secret --radius-secret radius.secret

sudo lmnradius cert issue meineschule         # Server-Cert (serverAuth + eapOverLAN, SAN=FQDN)
sudo lmnradius reconcile                       # Container starten/abgleichen
sudo lmnradius status meineschule              # exists/running/health/crash_looping
sudo lmnradius logs meineschule --tail 60
```

> **`--ldap-ca` ist Pflicht** (seit 7.3.1): über die LDAPS-Verbindung zum DC laufen das
> Rollen-Gate und die VLAN-Zuweisung; ohne Prüfung des DC-Zertifikats könnte ein Angreifer
> zwischen RADIUS-VM und DC die Gruppenabfrage fälschen. Die API lehnt `ldaps://` ohne CA ab.
> Die CA ist auf dem linuxmuster-Server `/etc/linuxmuster/ssl/cacert.pem` (Schritt 2 legt sie
> als `ldap-ca.pem` bei). Kommst du nicht an die Datei, ist **`--ldap-ca-tofu`** die
> dokumentierte Alternative: die CLI holt die Zertifikatskette per `openssl s_client`, druckt
> die SHA-256-Fingerprints und pinnt sie („trust on first use") — den Fingerprint danach auf
> dem DC gegenprüfen (`openssl x509 -in /etc/linuxmuster/ssl/cacert.pem -noout -fingerprint
> -sha256`). Liefert der DC nur sein eigenes Zertifikat (linuxmuster tut das), wird genau
> dieses gepinnt; nach einer Erneuerung des DC-Zertifikats scheitert die Prüfung dann
> fail-closed, bis `lmnradius set-ldap-ca` neu pinnt. Details:
> [`radius-and-ad.md`](radius-and-ad.md) § 3.

> **`--client-subnet`:** nur die AP-Management-Subnetze. `127.0.0.0/8` (und jedes CIDR, das
> `127.0.0.1` enthält) wird abgelehnt — die Adresse ist für den Healthcheck-Client des Images
> reserviert und ein solcher Client ließ die Instanz bis 7.3.0 crash-loopen. Für einen Test
> von der VM selbst `eapol_test` gegen die **LAN-IP** der VM richten (Schritt 8); Pakete vom
> Host kommen im Container ohnehin von der Docker-Bridge, nie von loopback.

> **Erwartetes Verhalten beim allerersten `create`:** es speichert die Instanz **und
> versucht sofort zu starten** — das Zertifikat aus der nächsten Zeile existiert da noch
> nicht, also meldet es `EAP cert material missing … run 'lmnradius cert issue'`
> (bis v0.1.4 leider nur als `error 500`; der Klartext steht dann im
> `journalctl -u linuxmuster-radius`). Kein Problem: die Instanz **ist** gespeichert —
> einfach mit `cert issue` + `reconcile` fortfahren.

## 6. Netz — UniFi + OPNsense
- **UniFi:** ein **RADIUS-Profil** (Server = RADIUS-VM-IP, Ports 1812/1813, Secret = Inhalt von `radius.secret`);
  pro SSID ein WPA2/WPA3-Enterprise-WLAN mit diesem Profil; SSID **fest ans VLAN** (Lehrer→20,
  Schüler→10). **RADIUS-Clients = AP-Management-Subnetz** (nicht die Controller-IP!).
- **OPNsense:** **`1812-1813/udp`** vom AP-Subnetz zur RADIUS-VM freigeben.

## 7. Clients pinnen (Pflicht — sonst ist WPA2-Enterprise unsicher)
```bash
sudo lmnradius ca export --out eap-ca.pem     # EAP-CA zum Verteilen
```
Windows via **GPO** (`linuxmuster-gpo-template`), Apple/Android via **MDM**. **Pinning-Pflichten:**
Server-Cert-Validierung AN + Trusted-CA = *diese* EAP-Root + Server-Name = FQDN gepinnt + „neuem
Zertifikat vertrauen" AUS. Vollständig: `deployment-gpo.md` + `certs-and-ca.md`.

## 8. Abnahme — der Laufzeit-Beweis
Vor Produktion die **5-Fälle-Matrix** fahren, mit echten Testkonten am WLAN **oder** dem crabbox-E2E:
```bash
LMNRADIUS_ALLOW_REAL=1 bash scripts/tests/run.sh e2e   # auf einer Docker-VM / crabbox
```
Erwartung: Lehrer @ Lehrer-SSID → online in VLAN 20 · Schüler @ Lehrer-SSID → abgewiesen ·
Schüler @ Schüler-SSID → online in VLAN 10 · falsches Passwort → abgewiesen · Gerät ohne die
gepinnte CA → abgewiesen.

**Ohne echten Client — `eapol_test` von einem Rechner im AP-Subnetz (oder von der RADIUS-VM
selbst gegen ihre LAN-IP).** Ubuntu liefert es im Paket `eapoltest` (universe):
```bash
sudo apt install -y eapoltest
sudo lmnradius ca export --out /root/eap-ca.pem
cat > /root/peap-lehrer.conf <<'EOF'
network={
  key_mgmt=WPA-EAP
  eap=PEAP
  identity="lehrer1"
  password="geheim"
  phase2="auth=MSCHAPV2"
  ca_cert="/root/eap-ca.pem"                        # die EAP-Root pinnen ...
  domain_suffix_match="radius.linuxmuster.lan"      # ... und den Servernamen (wie die Clients)
}
EOF
eapol_test -c /root/peap-lehrer.conf -a <RADIUS-LAN-IP> -p 1812 \
  -s "$(sudo cat /etc/linuxmuster-radius/secrets/radius.secret)" \
  -N30:s:00-11-22-33-44-55:lehrer-wlan -t 12          # -N30 = Called-Station-Id "<AP-MAC>:<SSID>"
```
`SUCCESS` = Access-Accept; im Dump zeigen die Attribute 64/65/81 das VLAN
(`Tunnel-Private-Group-Id` als Hex-ASCII, `3230` = „20"). Access-Reject erscheint als
`code=3`/`FAILURE`. Die SSID kommt **nur** über `-N30` — so prüfst du dieselben Konten
gegen jede SSID. Ubuntus Build kennt kein `-d` (die Ausgabe ist ohne bereits vollständig).

> Diese Matrix lief gegen einen echten DC bereits **7/7 durch** — mit genau den
> `role-teacher`/`role-student`-Gates aus Schritt 5 und den zurückgelieferten VLANs 20/10.
> Hier prüfst du also nicht die Mechanik, sondern **deine** Werte: Gruppennamen,
> SSID-Schreibweise, VLAN-IDs, AP-Subnetz und die `devices.csv`-Adoption in deiner Domäne.

## 9. Updates (alles über den `.deb`)
```bash
sudo apt upgrade                               # neues .deb -> postinst: try-restart + 'lmnradius update-all'
```

> **Upgrade von 7.3.0 oder älter — ein Pflichtschritt:** Instanzen aus diesen Versionen
> prüfen das DC-Zertifikat der LDAPS-Verbindung **nicht** (Sicherheitsbefund, siehe
> Schritt 5). Sie laufen nach dem Upgrade unverändert weiter, aber `lmnradius list` und
> `lmnradius health` warnen mit **„LDAPS unverified"**, bis die CA gepinnt ist:
> ```bash
> scp root@<dc>:/etc/linuxmuster/ssl/cacert.pem /root/ldap-ca.pem
> sudo lmnradius set-ldap-ca meineschule --ldap-ca /root/ldap-ca.pem   # oder --ldap-ca-tofu
> sudo lmnradius list                                                   # Warnung weg
> ```
> Der Befehl pinnt die CA und startet den Container mit Prüfung neu (kurze Unterbrechung).
> Das Upgrade selbst hebt die Instanz per `update-all` auf das neue Image, das den
> A-Record des RADIUS-FQDN auf die LAN-IP der VM korrigiert (bis 7.3.0 stand dort die
> Docker-Bridge-Adresse) und die Instanz-Historie (`git log` im State-Verzeichnis)
> repariert.
`update-all` hebt **jede Instanz auf das im `.deb` gepinnte Image**, pro Instanz mit Health-Check und
**automatischem Rollback**. Neue Images kommen via **Renovate**: ein neues GHCR-Image → Renovate
öffnet einen **Digest-Bump-PR** (`DEFAULT_IMAGE`), ein Mensch merged → neuer `v*`-Tag → neues `.deb`
→ `apt upgrade`. *(Für CI auf den Renovate-PRs ein `RENOVATE_TOKEN`-PAT als Repo-Secret hinterlegen;
sonst läuft Renovate mit dem `GITHUB_TOKEN`, dann triggern die PRs keine Folge-Workflows.)*
