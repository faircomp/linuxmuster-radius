<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Zertifikate & EAP-CA — linuxmuster-radius

Statusdokument. Mit jeder wesentlichen Änderung aktuell halten (siehe `CLAUDE.md`
→ Dokumentationspflege). Diese Anleitung beschreibt die **dedizierte EAP-CA**: warum
sie existiert, wie die Control Plane sie erzeugt (`lmnradius ca init`), Server-Zertifikate
ausstellt (`lmnradius cert issue`) und exportiert (`lmnradius ca export`), und wie CA
plus WLAN-Profil auf die Clients gepinnt werden. Die Entscheidung mit Begründung und
verworfenen Alternativen: [`decisions.md`](decisions.md) (ADR-005); die verifizierten
Fakten mit Quellen: [`references.md`](references.md) (§ EAP-Server-/Client-Zertifikate);
die Einordnung ins Gesamtbild: [`architecture.md`](architecture.md) (§ 7 + § 8).

## 1. Warum eine dedizierte EAP-CA

PEAP-MSCHAPv2 (ADR-003) baut einen äußeren **TLS-Tunnel** auf und schützt darin einen
**kryptografisch schwachen** inneren MSCHAPv2-Faktor. Der einzige belastbare Schutz gegen
einen gefälschten RADIUS-Server, der die MSCHAPv2-Challenge abgreift und offline knackt,
ist die **erzwungene Serverzertifikat-Prüfung am Client**. Damit ist die CA **kein
Deko-Element, sondern das tragende Sicherheitsfundament** (load-bearing): fällt das
Pinning, fällt die gesamte WLAN-Authentifizierung.

Genau **eine** enge, single-purpose Vertrauensbasis ist deshalb Pflicht. Die EAP-CA
gibt **ausschließlich** das eine EAP-Server-Zertifikat aus und wird auf jedes Client-Gerät
als **einziger** Trust-Anker für dieses WLAN gepinnt. Je schmaler diese Basis, desto
weniger fremde Zertifikate können den RADIUS-Server impersonieren.

**Verworfene Alternativen (siehe ADR-005):**

- **Die linuxmuster-CA (`/etc/linuxmuster/ssl`) wiederverwenden** — verworfen. Diese CA
  signiert breit (Server-, Firewall-, weitere interne Zertifikate) und hat eine Laufzeit
  von ~20 Jahren (`linuxmuster-renew-certs --days 7305`). Sie als EAP-Trust-Anker zu pinnen
  würde eine **breite Vertrauensbasis in jedes Schülergerät** einbrennen: jedes von dieser
  CA signierte Zertifikat könnte dann den RADIUS-Server vortäuschen. Enge Vertrauensbasis
  schlägt Bequemlichkeit.
- **Let's Encrypt / eine öffentliche CA** — verworfen aus **zwei** Gründen. (a) Die
  **90-Tage-Rotation** bricht 802.1X: bei jedem Renewal müsste jedes Gerät der neuen
  Serveridentität wieder vertrauen. (b) Ohne **Server-Name-Pinning** vertraut der Client
  jedem von einer öffentlichen CA signierten Zertifikat — ein Angreifer besorgt sich schlicht
  ein gültiges öffentliches Zertifikat und **impersoniert** den Server. Öffentliche CAs sind
  für EAP das Gegenteil einer engen Vertrauensbasis.

Fazit: eine **private, dedizierte EAP-CA**, sehr lange Root-Laufzeit (Rollover-Problem
vermeiden), von der Control Plane verwaltet, in jedes Gerät gepinnt.

## 2. CA erstellen — `lmnradius ca init`

Die CA wird **einmal** pro RADIUS-VM angelegt:

```bash
lmnradius ca init
```

- **Passphrase:** Der Befehl fragt eine **Passphrase** ab und legt den privaten Root-Key
  **passphrase-verschlüsselt** ab. Ohne Passphrase kein `cert issue`.
- **Ablage:** `certs_dir/ca/` (Default `certs_dir` = `/etc/linuxmuster-radius/certs`; Verzeichnis
  `0700`, Eigentümer `lmnradius`). Es entstehen `ca.cert.pem` (öffentliches CA-Zertifikat,
  `0644`, wird verteilt) und `ca.key.pem` (privater Root-Key, `0600`, passphrase-verschlüsselt,
  **verlässt die Control Plane nie unbeabsichtigt**).
- **Root-Laufzeit ~10 Jahre**, `basicConstraints CA:TRUE`. Lange Laufzeit ist gewollt: eine
  Root-Rotation zwingt jeden Client zum erneuten Pinning (§ 5) — das soll selten passieren.

**Empfehlung — den Root-Key offline nehmen.** Der private Root-Key ist der Generalschlüssel
für die gesamte WLAN-Vertrauenskette. Best Practice (eduroam) ist, ihn **offline** und
**passphrase-geschützt** zu halten und **vom Control-Plane-Host wegzunehmen**: nach der
Init-Phase und dem Ausstellen der benötigten Server-Zertifikate `ca.key.pem` auf einen
Offline-Datenträger (verschlüsselt) verlagern und nur zum Ausstellen/Erneuern eines
Server-Zertifikats temporär zurückspielen. Die Control Plane braucht den Root-Key **nur**
für `cert issue` — für den laufenden Betrieb (RADIUS-Auth, `ca export`) genügt `ca.cert.pem`.

**Ehrliche Grenze (MVP):** Der MVP hält `ca.key.pem` **passphrase-verschlüsselt auf dem
Control-Plane-Host** in `certs_dir/ca/` — bequem, aber kein echtes Air-Gap. Wer den Host
kompromittiert und die Passphrase erlangt, erlangt den Root-Key. Die Offline-Verlagerung
oben ist die empfohlene Härtung; sie ist eine **manuelle Betreiber-Entscheidung**, kein
Automatismus.

## 3. Server-Zertifikat — `lmnradius cert issue`

Für jede Instanz wird unter der EAP-CA ein Server-Zertifikat ausgestellt:

```bash
lmnradius cert issue <instance> [--fqdn radius.<schule>.<tld>]
```

- **Eingabe:** `<instance>` ist der Instanzname (→ Container `lmnradius-<name>`). Ohne
  `--fqdn` wird der `server_fqdn` aus der Instanz-YAML verwendet (siehe `architecture.md` § 5).
- **Passphrase:** Der Befehl braucht die CA-Passphrase (§ 2), um mit dem Root-Key zu signieren.
- **Zertifikat-Eigenschaften (verifiziert, ADR-005 / references.md):**
  - **EKU `serverAuth` `1.3.6.1.5.5.7.3.1`** — Pflicht, Windows verlangt exakt dieses OID.
  - zusätzlich **`eapOverLAN` `1.3.6.1.5.5.7.3.14`** (802.1X). *Ehrliche Grenze:* dass dieses
    OID zwingend nötig ist, ist **nicht** hart belegt — Microsoft verlangt nur `serverAuth`;
    `eapOverLAN` wird als Best-Practice mitgegeben.
  - **`SAN:DNS = FQDN`** (identisch zum CN), **kein Wildcard**. Der SAN ist der Servername,
    den die Clients pinnen (§ 4).
  - **Laufzeit mehrjährig** (kein 90-Tage-Zwang, weil es eine private CA ist).
- **Ablage & Mount:** Das Paar landet in `certs_dir/<name>/` (privater Key `0600`) und wird
  **read-only** nach `/run/secrets/eap/*` in den Container gemountet, wo das FreeRADIUS-
  `eap`-Modul es als Server-Identität lädt.
- **Aktivierung:** Nach dem Ausstellen die Instanz abgleichen bzw. neu starten:

  ```bash
  lmnradius reconcile        # rendert Config + gleicht Mounts ab
  # oder gezielt: lmnradius restart <instance>
  ```

- **Fail-closed:** **Ohne** gültiges Server-Zertifikat im Mount startet der EAP-Dienst
  **nicht** — der Container ist fail-closed. Es gibt kein „unverschlüsseltes PEAP": lieber
  kein WLAN als ein WLAN ohne Serveridentität.

## 4. CA verteilen & Clients pinnen — `lmnradius ca export`

Die öffentliche CA wird exportiert und an die Geräteverwaltung übergeben:

```bash
lmnradius ca export --out eap-ca.pem   # oder ohne --out: Ausgabe nach stdout
```

Der Export enthält **ausschließlich** das öffentliche CA-Zertifikat (`ca.cert.pem`), nie den
privaten `ca.key.pem`.
Verteilung nach Plattform:

- **Windows via GPO** — Schwesterprojekt **`linuxmuster-gpo-template`**: `cacert.pem` als
  **Trusted-Root-CA** in den Maschinen-Zertifikatsspeicher importieren **und** ein
  **gesperrtes WLAN-Profil** (Wireless Network Policy) ausrollen, das die gegenseitige
  Authentisierung erzwingt.
- **Apple / Android via MDM/EMM** — `cacert.pem` plus WLAN-Konfiguration als
  **`.mobileconfig`** (Apple) bzw. per EMM-WLAN-Policy (Android). Das Profil setzt Trust-Anker
  und Servername in einem Zug — der Nutzer kann die Prüfung nicht wegklicken.

**Pinning-Pflichten (nicht verhandelbar).** Weil MSCHAPv2 schwach ist (§ 1), MUSS jedes Client-
WLAN-Profil **alle** der folgenden Punkte setzen — ein fehlender Punkt macht das Pinning wertlos:

1. **Serverzertifikat-Validierung AN** — „Validate server certificate" / „Serverzertifikat
   überprüfen" **eingeschaltet**.
2. **Trusted-CA = die eine EAP-Root** — als Trust-Anker **ausschließlich** die exportierte
   EAP-Root (`cacert.pem`), **nicht** die System-Trust-Stores der Öffentlichkeit, **nicht**
   die linuxmuster-CA.
3. **Trusted-Server-Name = FQDN gepinnt** — der erwartete Servername (= `SAN`/CN des
   Server-Zertifikats, § 3) ist **fest hinterlegt**; ohne diesen Pin würde jedes von der Root
   signierte Zertifikat akzeptiert.
4. **„Neuem Zertifikat vertrauen" AUS** — die Prompt-Option „neues/unbekanntes Zertifikat
   akzeptieren" (Android: „Do not validate" / „Trust on first use"; Windows: „Benutzer zur
   Autorisierung auffordern") **deaktiviert**. Andernfalls schult man Nutzer darauf, jeden
   Fake-Server durchzuwinken.

Erst diese vier Punkte machen aus „TLS irgendwie" ein **echtes** gegenseitiges 802.1X.

## 5. Lebenszyklus & Rotation

- **Server-Zertifikat erneuern (schmerzlos).** Solange **dieselbe Root** signiert, ist die
  Rotation eines ablaufenden Server-Zertifikats client-transparent: `lmnradius cert issue
  <instance>` neu ausstellen, `lmnradius reconcile` / `restart`. **Die Clients brauchen
  nichts** — sie vertrauen weiter der gepinnten Root, und der gepinnte Servername (FQDN)
  bleibt gleich. Deshalb ist die Server-Laufzeit mehrjährig, aber nicht extrem lang.
- **Root erneuern (selten, mit Überlappung).** Die Root ist auf ~10 Jahre ausgelegt, damit
  eine Root-Rotation **selten** nötig ist — denn sie zwingt **jedes** Gerät zum erneuten
  Pinning der neuen CA (§ 4). Wenn doch: die neue Root **rechtzeitig überlappend** ausrollen
  (beide CAs eine Zeitlang als Trust-Anker gepinnt), erst dann Server-Zertifikate auf die
  neue Root umstellen, zuletzt die alte Root aus den Clients entfernen. Nie „hart" umschalten.
- **Ablauf überwachen.** Sowohl Server-Zertifikat als auch Root haben ein Ablaufdatum; ein
  abgelaufenes Server-Zertifikat bricht das WLAN **flottenweit**. Ablaufdaten aktiv
  überwachen (Monitoring/Kalender) und Rotationen **vor** Ablauf mit Puffer einplanen.

---

**Siehe auch:** Entscheidung mit verworfenen Alternativen → [`decisions.md`](decisions.md)
(ADR-005); verifizierte Fakten mit Quellen → [`references.md`](references.md); Einordnung
in Control/Data Plane und Auth-Pfad → [`architecture.md`](architecture.md) (§ 7 + § 8).
