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

> **Reifegrad (ehrlich):** Die **Control-Plane ist bewiesen** (114 Tests), und **genau das
> Data-Plane-Image, das dieses `.deb` gepinnt ausliefert** (das `:0.1.2`-Release-Image),
> wurde gegen einen produktiven
> linuxmuster-DC verifiziert — mit **frischem Zustandsvolume**, also dem Pfad einer
> Neuinstallation: Member-Join, `healthy` nach 6 s, und die **Auth-Matrix 7/7** (Lehrer →
> Accept in VLAN 20, Schüler → VLAN 10; falsche Rolle / unbekannte SSID / kein `wifi` /
> falsches Passwort → je Reject). Protokoll in [`references.md`](references.md).
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
VER=0.1.3
curl -fsSLo lmnradius.deb \
  https://github.com/faircomp/linuxmuster-radius/releases/download/v${VER}/linuxmuster-radius_${VER}_all.deb
sudo apt install -y ./lmnradius.deb
lmnradius health                                            # {"status":"ok"}
```
Der `postinst` legt den System-User `lmnradius`, `/etc/linuxmuster-radius/{config.yml (0600,
zufälliges Token),secrets,certs}`, das git-initialisierte State-Verzeichnis an und startet den
Dienst. Das Image (`ghcr.io/faircomp/linuxmuster-radius`, **public**) wird bei `reconcile`
gezogen.
*(Optionale Härtung: `deploy/docker-socket-proxy.yml` starten und `docker_host: "tcp://127.0.0.1:2375"` in `config.yml` setzen.)*

## 2. Auf dem linuxmuster-DC — AD vorbereiten
```bash
sudo bash scripts/discover-ad-facts.sh        # read-only: erkennt realm/wg/Base-DN/Gruppen -> fertige create-Vorlage
sudo bash scripts/provision-radius-account.sh # RADIUS-Server als Device (Rolle 'server') + Secret-Vorlagen
```
> In der **DC-`/etc/samba/smb.conf`** muss `ntlm auth = mschapv2-and-ntlmv2-only` stehen
> (sonst schlägt jeder WLAN-Login fehl; Paket-Updates entfernen die Zeile gern).
> **Nutzer/Gruppen/`wifi`/`global-binduser` bleiben reine Sophomorix-Welt** — nichts von Hand anlegen.

## 3. Secrets ablegen (RADIUS-VM, `/etc/linuxmuster-radius/secrets/`, `0600`)
- `radius-join` — Domänen-Beitritts-Authfile (samba `-A`: `username=`/`password=`/`domain=`)
- `global-binduser` — das Passwort aus `/etc/linuxmuster/.secret/global-binduser` vom DC
- `ap-secret` — das WLAN-Shared-Secret (identisch zum UniFi-RADIUS-Profil)

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

```bash
sudo lmnradius create --name meineschule \
  --server-fqdn radius.linuxmuster.lan \
  --realm LINUXMUSTER.LAN --workgroup LINUXMUSTER \
  --ldap-server ldaps://dc.linuxmuster.lan \
  --ldap-base-dn OU=SCHOOLS,DC=linuxmuster,DC=lan \
  --ldap-bind-dn CN=global-binduser,OU=Management,OU=GLOBAL,DC=linuxmuster,DC=lan \
  --client-subnet 10.0.0.0/16 \
  --ssid lehrer-wlan:role-teacher:20 \
  --ssid schueler-wlan:role-student:10 \
  --join-secret radius-join --ldap-bind-secret global-binduser --radius-secret ap-secret

sudo lmnradius cert issue meineschule         # Server-Cert (serverAuth + eapOverLAN, SAN=FQDN)
sudo lmnradius reconcile                       # Container starten/abgleichen
sudo lmnradius status meineschule              # exists/running/health
sudo lmnradius logs meineschule --tail 60
```

## 6. Netz — UniFi + OPNsense
- **UniFi:** ein **RADIUS-Profil** (Server = RADIUS-VM-IP, Ports 1812/1813, Secret = `ap-secret`);
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

> Diese Matrix lief gegen einen echten DC bereits **7/7 durch** — mit genau den
> `role-teacher`/`role-student`-Gates aus Schritt 5 und den zurückgelieferten VLANs 20/10.
> Hier prüfst du also nicht die Mechanik, sondern **deine** Werte: Gruppennamen,
> SSID-Schreibweise, VLAN-IDs, AP-Subnetz und die `devices.csv`-Adoption in deiner Domäne.

## 9. Updates (alles über den `.deb`)
```bash
sudo apt upgrade                               # neues .deb -> postinst: try-restart + 'lmnradius update-all'
```
`update-all` hebt **jede Instanz auf das im `.deb` gepinnte Image**, pro Instanz mit Health-Check und
**automatischem Rollback**. Neue Images kommen via **Renovate**: ein neues GHCR-Image → Renovate
öffnet einen **Digest-Bump-PR** (`DEFAULT_IMAGE`), ein Mensch merged → neuer `v*`-Tag → neues `.deb`
→ `apt upgrade`. *(Für CI auf den Renovate-PRs ein `RENOVATE_TOKEN`-PAT als Repo-Secret hinterlegen;
sonst läuft Renovate mit dem `GITHUB_TOKEN`, dann triggern die PRs keine Folge-Workflows.)*
