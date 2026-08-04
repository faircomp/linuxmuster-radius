<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Betrieb (Day-2 Operations) — linuxmuster-radius

Kurzreferenz für den laufenden Betrieb. Gesamtbild (Control/Data Plane, Auth-Pfad) →
[`architecture.md`](architecture.md); AD-Member-Registrierung, Bind-User, `wifi`-Gruppe,
DNS/Zeit/Hostname → [`radius-and-ad.md`](radius-and-ad.md); EAP-CA, Zertifikat-Ausstellung
und Client-Pinning → [`certs-and-ca.md`](certs-and-ca.md); Netzwerk (UniFi/OPNsense),
WLAN-Profil per GPO/MDM und die Abnahme → [`deployment-gpo.md`](deployment-gpo.md).

Alle `lmnradius`-Befehle sind ein dünner Client der REST-API (FastAPI, gebunden an
`127.0.0.1:8080`, Bearer-Token) — es gibt **keinen** direkten Docker-Zugriff über die CLI.

## Installation (Control-Plane-Tooling)

```
apt install ./linuxmuster-radius_<version>_all.deb     # oder aus dem lmn73-apt-Repo
systemctl status linuxmuster-radius                    # sollte "active" sein
```

Die `postinst` legt den Systembenutzer `lmnradius` an (in der Gruppe `docker`), erzeugt
ein **zufälliges API-Token** in `/etc/linuxmuster-radius/config.yml` (`0600`) und startet
den Dienst — gebunden an **`127.0.0.1:8080`**. Verzeichnisse: `secrets_dir`
(`/etc/linuxmuster-radius/secrets`, `0700`), `certs_dir` (`/etc/linuxmuster-radius/certs`,
`0700`), `instances_dir` (`/var/lib/linuxmuster-radius/instances`, als Git-Repo = Change-Log).

## Erstinbetriebnahme (einmalig)

Reihenfolge — jeder Schritt setzt den vorigen voraus:

1. **EAP-CA anlegen** (auf der RADIUS-VM). Fragt eine Passphrase ab und legt den privaten
   Root-Key **passphrase-verschlüsselt** ab (`~10 Jahre` Laufzeit):

   ```
   lmnradius ca init
   ```

   Details/Härtung (Root-Key offline nehmen) → [`certs-and-ca.md`](certs-and-ca.md).

2. **Member-Registrierung + Secrets bauen — auf dem Samba-DC** (als root). Trägt die
   RADIUS-VM als Device mit Rolle `server` in die `devices.csv` ein und baut die drei
   Secret-Dateien (`join.authfile`, `ldap-bind.secret`, `radius.secret`):

   ```
   scripts/provision-radius-account.sh <hostname> <mac> <ip> [out-secrets-dir]
   linuxmuster-import-devices          # legt das Maschinenkonto an (falls nicht via RUN_IMPORT=1)
   ```

   > **Join-Konto (verifiziert am echten DC, 2026-07-12):** Ein einfacher Benutzer kann
   > **nicht** joinen (`Insufficient access`) — in das `join.authfile` gehört der
   > linuxmuster-Administrator bzw. ein delegiertes Konto mit Maschinenkonten-Rechten.
   > **Offen bleibt** nur, ob ein via `import-devices` **vorab** angelegtes Computerkonto
   > vom Join sauber adoptiert wird — siehe [`radius-and-ad.md`](radius-and-ad.md) (§ 2)
   > und `decisions.md` (ADR-006). Das Skript zeigt **nie** ein Secret an.

3. **AD-Fakten ermitteln — auf dem Samba-DC** (read-only; berührt/joined nichts). Liefert
   realm/workgroup/base-DN/bind-DN, listet `wifi` + die Rollengruppen und druckt ein
   fertiges `lmnradius create`-Skelett je Schule; prüft zudem die DC-`ntlm auth`-Zeile:

   ```
   scripts/discover-ad-facts.sh
   ```

4. **Secret-Dateien** aus `out-secrets-dir` in den `secrets_dir` der RADIUS-VM übertragen —
   **Dateinamen unverändert** (sie sind die `--join-secret`/`--ldap-bind-secret`/
   `--radius-secret`-Referenzen), Modus `0600`.

5. **Instanz anlegen** (rendert Config + gleicht den Container ab). Werte aus Schritt 3:

   ```
   lmnradius create \
     --name default-school \
     --realm LINUXMUSTER.LAN --workgroup LINUXMUSTER \
     --server-fqdn radius.linuxmuster.lan \
     --ldap-server ldaps://dc.linuxmuster.lan \
     --ldap-base-dn DC=linuxmuster,DC=lan \
     --ldap-bind-dn CN=global-binduser,OU=Management,OU=GLOBAL,DC=linuxmuster,DC=lan \
     --wifi-group wifi \
     --client-subnet 10.0.0.0/16 \
     --ssid lehrer:teachers:20 --ssid schueler:students:10 \
     --join-secret join.authfile \
     --ldap-bind-secret ldap-bind.secret \
     --radius-secret radius.secret
   ```

   - `--server-fqdn` = **Container-Hostname == EAP-Cert-CN/SAN**, vorwärts auflösbar über
     den DC (siehe [`radius-and-ad.md`](radius-and-ad.md) § 5).
   - `--client-subnet` ist **wiederholbar** — die **AP-Management-Subnetze** als CIDR (die
     Access Points sind der NAS und senden ab **eigener** IP; nur die Controller-IP ergibt
     `unknown client`).
   - `--ssid` ist **wiederholbar** — `name:group[:vlan]`.
   - `--image` ist optional (Default: der gepflegte, digest-gepinnte Data-Plane-Image).

6. **Server-Zertifikat ausstellen** (unter der EAP-CA; braucht die CA-Passphrase):

   ```
   lmnradius cert issue default-school            # optional: --fqdn radius.<schule>.<tld>
   ```

7. **Abgleichen** (rendert Config, gleicht Mounts ab, bringt den Container hoch), dann die
   **öffentliche CA exportieren** und an die Geräteverwaltung übergeben:

   ```
   lmnradius reconcile
   lmnradius ca export --out eap-ca.pem           # Trust-Anker fürs Client-Pinning
   ```

   Client-Rollout (gesperrtes WLAN-Profil, Server-Name-Pin) →
   [`deployment-gpo.md`](deployment-gpo.md) und [`certs-and-ca.md`](certs-and-ca.md).

## Instanzen verwalten (Lifecycle)

```
lmnradius list                                  # alle Instanzen
lmnradius show    default-school                # eine Instanz (Spec)
lmnradius status  default-school                # exists/running/health/image
lmnradius start|stop|restart default-school
lmnradius logs    default-school --tail 100 --grep teacher1   # radiusd-Log, optional gefiltert
lmnradius logs    default-school --since 1783000000           # ab Unix-Epoch-Sekunde
lmnradius rm      default-school                # Instanz + Container entfernen
```

## Updates (digest-gepinnt, Health-Auto-Rollback)

```
lmnradius update     default-school                                          # -> gepflegter Default-Digest
lmnradius update     default-school ghcr.io/faircomp/linuxmuster-radius@sha256:<neu>   # expliziter Pin
lmnradius update-all                             # jede Instanz -> Default-Image (per-Instanz-Rollback)
lmnradius rollback   default-school              # auf das letzte bekannt-gute Image
```

Das Update zieht den neuen Digest, ersetzt den Container, wartet auf `healthy` (winbind-
Trust **und** radiusd erreichbar) und **rollt bei Fehler automatisch zurück** — die Schule
bleibt online. Welcher Digest in Produktion gehört, entscheidet ein **gemergter
Renovate-PR** (nie Auto-Merge). Bei einem **`.deb`-Upgrade** ruft die `postinst`
automatisch `update-all` auf (best-effort; Instanzen auf dem Default werden übersprungen,
die apt-Transaktion scheitert daran nie).

## Beobachten

```
systemctl status linuxmuster-radius ; journalctl -u linuxmuster-radius
docker ps --filter name=lmnradius-              # laufende RADIUS-Container
lmnradius status default-school                 # exists/running/health/image
lmnradius logs   default-school --tail 100 --grep Reject   # Access-Accept/Reject je User/SSID
```

- **Audit-Log:** jede API-Mutation (create/update/rollback/cert issue/…) geht an den Logger
  **`lmnradius.audit`** → syslog/journal.
- **Docker-Daemon weg:** die API antwortet mit **`503`** (nicht mit einem rohen `500`) und
  klarer Meldung — Instanzen bleiben unberührt.
- **Health:** `healthcheck.sh` im Container prüft `wbinfo -t` (AD-Trust) **und** einen
  radiusd-Status-Server-Probe (dienst hört) — beides zusammen = „up **und** enforcing".

### Alerting ohne Monitoring-Stack

- **Instanz down/unhealthy melden:** `docker events --filter event=health_status
  --filter event=die` in ein kleines Skript hängen, das bei `unhealthy`/`die` eine
  Mail/Matrix-Nachricht schickt (kein Prometheus nötig).
- **Zertifikat-Ablauf früh erkennen:** ein abgelaufenes EAP-Server-Zertifikat bricht das
  WLAN **flottenweit** → Ablaufdaten aus `lmnradius cert show <instance>` / `ca show`
  aktiv überwachen und **vor** Ablauf rotieren (siehe [`certs-and-ca.md`](certs-and-ca.md) § 5).
- **DC-`ntlm auth`-Zeile überwachen:** Samba-Updates entfernen
  `ntlm auth = mschapv2-and-ntlmv2-only` auf dem DC gelegentlich → nach jedem DC-Update
  prüfen, sonst schlagen alle MSCHAPv2-Logins fehl ([`radius-and-ad.md`](radius-and-ad.md) § 4).

### ⚠️ Datenschutz (DSGVO)

RADIUS-Logs zeigen, **welcher Nutzer sich wann an welcher SSID** authentifiziert (bzw.
abgelehnt) hat = **personenbezogene Daten**. Daher:

- **Log-Zugriff eng halten** (API-Token); Abfragen laufen in das Audit-Log.
- Log-Aufbewahrung kurz halten und dokumentieren; für zentrale Langzeit-Analyse den
  Docker-`syslog`-Log-Driver in das bestehende SIEM/syslog hängen — die Control Plane ist
  **keine** Log-Datenbank.

## Secrets & Zertifikate

- **Drei Betriebs-Secrets** je Instanz im `secrets_dir` (`0600`, Dateiname == die Referenz
  in der Instanz-YAML): `join_secret` (samba-`-A`-Authfile), `ldap_bind_secret`
  (`global-binduser`-Passwort), `radius_secret` (AP-Shared-Secret). Details/Zustellung →
  [`radius-and-ad.md`](radius-and-ad.md) § 6.
- **EAP-CA + Server-Keys** im `certs_dir` (`0700`): `ca/ca.cert.pem` (öffentlich, wird
  verteilt) + `ca/ca.key.pem` (privat, passphrase-verschlüsselt) und je Instanz das
  Server-Zertifikat/-Key. Empfehlung: den Root-Key **offline** nehmen
  ([`certs-and-ca.md`](certs-and-ca.md) § 2).
- **Nie** in Git, **nie** in Logs, **nie** in die Instanz-YAML — dort steht nur der
  Dateiname. Fehlt eine Secret-/Zertifikatsdatei, startet die Instanz **nicht**
  (fail-closed, geprüft **bevor** der laufende Container angefasst wird — kein
  Secret-Problem verursacht Downtime).
- **Maschinenkonto-Secret:** liegt aus dem Join im persistenten Volume
  `lmnradius-samba-<name>` (`/var/lib/samba`) — generierter Zustand, kein gepflegtes
  Secret. Geht das Volume verloren, muss die Instanz **neu joinen** (Re-Join).

## Backup

Zu sichern:

- `/etc/linuxmuster-radius/config.yml` (**API-Token!**),
- `/etc/linuxmuster-radius/secrets/` (die drei Betriebs-Secrets),
- `/etc/linuxmuster-radius/certs/` (**EAP-CA-Key + Server-Keys** — ohne die CA gibt es kein
  neues Server-Zertifikat),
- `instances_dir` (`/var/lib/linuxmuster-radius/instances/*.yaml` — git-versioniert =
  Change-Log; die `postinst` legt das Repo an),
- optional die **`/var/lib/samba`-Volumes** (`lmnradius-samba-<name>`): sichern spart den
  Re-Join, ist aber verzichtbar — geht das Volume verloren, joint die Instanz neu.

## Restore / Disaster Recovery

Frischer Host → laufende Instanzen:

```
apt install ./linuxmuster-radius_<version>_all.deb          # Dienst kommt hoch
# API-Token behalten: config.yml zurückspielen ODER das neue Token akzeptieren
cp -a <backup>/secrets/*   /etc/linuxmuster-radius/secrets/      # Betriebs-Secrets
cp -a <backup>/certs/*     /etc/linuxmuster-radius/certs/        # EAP-CA + Server-Keys
cp -a <backup>/instances/*.yaml /var/lib/linuxmuster-radius/instances/
chown -R lmnradius:lmnradius /etc/linuxmuster-radius/secrets \
      /etc/linuxmuster-radius/certs /var/lib/linuxmuster-radius/instances
lmnradius reconcile      # liest den Soll-Zustand, zieht die gepinnten Digests -> Container laufen
```

- **`lmnradius reconcile`** (`POST /v1/reconcile`) re-appliziert **alle** gespeicherten
  Instanzen — auch zum Beheben von Drift nach einem Vorfall.
- **Reboot** braucht das nicht: `restart_policy: unless-stopped` bringt laufende Container
  zurück.
- Ist das **`/var/lib/samba`-Volume** verloren (kein Backup), **joint die Instanz beim
  ersten Start neu** — dafür muss das `join_secret` im `secrets_dir` liegen.

## Sicherheitslage (kurz)

- API nur auf `127.0.0.1`, Bearer-Token (konstant-zeitiger Vergleich); Docker-Socket-Zugriff
  ist **root-äquivalent** → nicht über localhost hinaus exponieren, `docker-socket-proxy`
  empfohlen (`deploy/docker-socket-proxy.yml`).
- Der Auth-Weg selbst ist gegen unautorisierten Zugriff gehärtet: ohne gültiges
  PEAP-MSCHAPv2 **und** Mitgliedschaft in `wifi` + der geforderten Rollengruppe gibt es
  **Access-Reject**; ohne gepinntes Server-Zertifikat am Client ist PEAP **wertlos** (das
  Pinning ist tragend, siehe [`certs-and-ca.md`](certs-and-ca.md)).
- Data-Plane-Container: read-only rootfs, `cap_drop`, `no-new-privileges`, Secrets/Certs als
  read-only Mounts; der zweite Daemon (`winbindd`) läuft klein-supervidiert neben `radiusd`.
