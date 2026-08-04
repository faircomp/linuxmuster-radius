<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Produktions-Deployment: Netzwerk, Client-Steuerung & Abnahme

Statusdokument. Mit jeder wesentlichen Änderung aktuell halten (siehe `CLAUDE.md`
→ Dokumentationspflege). Ziel dieses Dokuments: den **produktiven** Weg vom
gejointen RADIUS-Member zum funktionierenden Enterprise-WLAN beschreiben —
**Netzwerk** (UniFi + OPNsense), **Client-Trust** (EAP-CA + gesperrtes WLAN-Profil
per GPO/MDM) und die **Abnahme durch einen Menschen**. Die AD-Seite (Member-
Registrierung, Bind-User, `wifi`-Gruppe) liegt in [`radius-and-ad.md`](radius-and-ad.md);
CA/Pinning-Details in [`certs-and-ca.md`](certs-and-ca.md); die Einordnung ins
Gesamtbild in [`architecture.md`](architecture.md).

> **Sicherheitsmodell (tragend, mirror-Prinzip von linuxmuster-squid):** GPO und
> WLAN-Profil **steuern nur**, welche SSID ein Client sieht und welcher EAP-CA er
> vertraut. Die **Rollentrennung wird server-seitig erzwungen** — der RADIUS
> antwortet mit **Access-Reject**, wenn der User nicht in der für die SSID
> geforderten Gruppe (bzw. nicht in `wifi`) ist (§ 4). Ein Schüler, der sich
> manuell mit der Lehrer-SSID verbindet, wird **vom RADIUS abgelehnt — unabhängig
> vom ausgerollten Profil.** Das Profil ist Komfort, nicht die Grenze.

Beispiel durchgehend: SSID `<schule>-lehrer` → Gruppe `<schule>-teachers` → **VLAN 20**;
SSID `<schule>-schueler` → Gruppe `<schule>-students` → **VLAN 10** (default-school:
Gruppen `teachers`/`students` ohne Präfix).

## 1. Netzwerk — UniFi (Controller + Access Points)

**RADIUS-Profil — EINMAL anlegen** (Settings → Profiles → RADIUS). Ein Profil
bedient **mehrere** SSIDs (references.md § UniFi):

- **Auth-Server:** IP der RADIUS-VM, **Port 1812**; **Accounting-Server:** dieselbe
  IP, **Port 1813**.
- **Shared Secret:** der `RADIUS_SECRET` — genau das Secret, das die Control Plane
  über `--radius-secret` in `clients.conf` rendert. Pro AP-Subnetz **ein starkes,
  langes** Secret (ADR-014); kein Wörterbuchwort.

**Pro SSID ein WPA2/WPA3-Enterprise-WLAN** (Settings → WiFi), das genau dieses
Profil referenziert — **nicht** WPA2-Personal/PSK:

- `<schule>-lehrer` → WPA2/WPA3-**Enterprise**, RADIUS-Profil von oben.
- `<schule>-schueler` → dito.

**VLAN: statisch je SSID (Default).** Die SSID **ist** die Rolle **ist** das VLAN.
Im WLAN die feste VLAN-ID hinterlegen: Lehrer-SSID → **VLAN 20**, Schüler-SSID →
**VLAN 10** (ADR-008). Das ist der einfache, robuste Default: der AP taggt jeden
Client dieser SSID statisch.

- **Alternative — RADIUS-assigned VLAN** (optional, RFC 2868): der Accept trägt
  `Tunnel-Type=13`, `Tunnel-Medium-Type=6`, `Tunnel-Private-Group-Id=<vlan>`. Dafür
  muss der UniFi-Toggle **„RADIUS assigned VLAN" aktiviert** sein **und das
  VLAN/Network in UniFi existieren** (sonst Fallback aufs Default-VLAN). Der
  optionale VLAN-Wert im Instanzmodell (`--ssid name:group:<vlan>`) speist genau
  diesen Pfad. *NICHT VERIFIZIERT — die exakte UI-Verortung des Toggles variiert je
  Controller-Version (references.md); im crabbox-E2E (P6) zu beweisen.*

**Die Access Points sind die RADIUS-Clients (NAS), nicht der Controller.** Jeder AP
sendet den `Access-Request` aus **seiner eigenen IP** — der UniFi-Controller ist
**kein** Proxy. Deshalb steht in `clients.conf` das **AP-Management-Subnetz als
CIDR**, nie die Controller-IP (references.md § UniFi, ADR-009). Genau deshalb nimmt
`lmnradius create` das **AP-Subnetz** entgegen, **wiederholbar** je Subnetz:

```bash
lmnradius create --name meineschule \
  --server-fqdn radius.linuxmuster.lan \
  --realm LINUXMUSTER.LAN --workgroup LINUXMUSTER \
  --ldap-server ldaps://dc.linuxmuster.lan \
  --ldap-base-dn OU=SCHOOLS,DC=linuxmuster,DC=lan \
  --ldap-bind-dn CN=global-binduser,OU=Management,OU=GLOBAL,DC=linuxmuster,DC=lan \
  --client-subnet 10.0.0.0/16 \
  --client-subnet 10.1.0.0/16 \
  --ssid meineschule-lehrer:meineschule-teachers:20 \
  --ssid meineschule-schueler:meineschule-students:10 \
  --join-secret join.authfile --ldap-bind-secret ldap-bind.secret --radius-secret radius.secret
```

> Nur die **Controller-IP** einzutragen erzeugt „unknown client" und stille Rejects
> — die Requests kommen von den AP-IPs, nicht vom Controller.

**`Called-Station-Id` = `<AP-MAC>:<SSID>`** (RFC 3580) — daraus parst FreeRADIUS die
SSID (`Called-Station-SSID`) und verzweigt auf die geforderte Gruppe (§ 4). Kein
UniFi-seitiges Zutun nötig; die APs setzen das Attribut von selbst.

**RadSec zurückgestellt** (UniFi Network ≥ 8.4, **TCP/2083**, Shared Secret konstant
`radsec`, gegenseitige Zert-Auth). Im MVP: UDP 1812/1813 + starkes Per-Subnetz-Secret
(ADR-014). Später ohne Client-Neurollout aktivierbar.

## 2. Netzwerk — OPNsense (Firewall + Routing)

Der RADIUS-Verkehr ist schmal und gerichtet:

- **RADIUS-VM ins Management-VLAN** stellen (dort, wo Controller/APs verwaltet
  werden). Der Member spricht von dort LDAPS + winbind mit dem DC und empfängt
  Access-Requests von den APs.
- **Firewall-Regel:** **UDP 1812–1813** von **den AP-Management-Subnetz(en)**
  (dieselben CIDRs wie `--client-subnet`) → **RADIUS-VM-IP** erlauben. Sonst nichts.

  | Aktion | Proto | Quelle | Ziel | Port |
  |---|---|---|---|---|
  | pass | UDP | AP-Mgmt-Subnetz(e) | RADIUS-VM | 1812 (Auth) |
  | pass | UDP | AP-Mgmt-Subnetz(e) | RADIUS-VM | 1813 (Accounting) |

- **Die Rollen-VLANs (10/20/…) terminieren und routen wie die Schule es bereits
  tut** — Gateway, DHCP, Internet-Uplink/Filter je VLAN sind bestehende
  Infrastruktur (OPNsense oder L3-Switch), **kein** Bestandteil dieses Projekts.
  Dieses Deployment erzeugt nur die **Zuordnung** Client → VLAN (statisch je SSID
  bzw. RADIUS-assigned); die VLANs selbst müssen vorhanden und geroutet sein.

## 3. Client-Trust — EAP-CA + gesperrtes WLAN-Profil ausrollen

Ohne gepinntes Serverzertifikat ist PEAP-MSCHAPv2 gegen Evil-Twin/Rogue-RADIUS
schutzlos (der innere MSCHAPv2-Faktor ist kryptografisch schwach) — die
Serverzert-Prüfung am Client ist deshalb **tragend, nicht optional**
([`certs-and-ca.md`](certs-and-ca.md) § 1, threat-model.md). Ausgerollt werden **zwei
Dinge zusammen**: (a) die exportierte EAP-Root als Trust-Anker und (b) ein
**gesperrtes** WLAN-Profil, das die Pinning-Triade erzwingt.

CA exportieren:

```bash
lmnradius ca export --out eap-ca.pem   # NUR das öffentliche CA-Cert, nie ca.key.pem
```

**Windows — per GPO** (Schwesterprojekt **`linuxmuster-gpo-template`**), zwei
GPO-Bausteine:

1. **EAP-Root als Trusted-Root-CA** in den **Computer-Zertifikatsspeicher** importieren
   (Computer Configuration → Policies → Windows Settings → Security Settings → Public
   Key Policies → **Trusted Root Certification Authorities**). Maschinen-Store, damit
   die Prüfung schon vor dem User-Login greift.
2. **Wireless Network (IEEE 802.11) Policy** ausrollen, die **PEAP-Eigenschaften** je
   SSID setzt: WPA2/WPA3-Enterprise, **PEAP-MSCHAPv2**, Serverzert-Prüfung **AN**, als
   Trust-Anker **die EAP-Root** (Punkt 1), **„Connect to these servers" = `SERVER_FQDN`**
   und **kein** Nutzer-Prompt für neue Server/CAs (Trust-on-first-use aus).

**Apple / Android — per MDM/EMM:** dieselbe CA + WLAN-Konfiguration als
**`.mobileconfig`** (Apple, MDM) bzw. per **EMM-WLAN-Policy** (Android). Das Profil
setzt Trust-Anker **und** Servername in einem Zug — der Nutzer kann die Prüfung nicht
wegklicken.

**Pinning-Pflichten (nicht verhandelbar, aus [`certs-and-ca.md`](certs-and-ca.md) § 4).**
Jedes Client-Profil MUSS **alle vier** Punkte setzen; ein fehlender macht das Pinning
wertlos:

1. **Serverzertifikat-Validierung AN** — „Validate server certificate" eingeschaltet.
2. **Trusted-CA = die eine EAP-Root** (`eap-ca.pem`) — **nicht** die öffentlichen
   System-Trust-Stores, **nicht** die linuxmuster-CA.
3. **Trusted-Server-Name = `SERVER_FQDN` gepinnt** (= SAN/CN des Server-Zerts) —
   ohne diesen Pin würde jedes von der Root signierte Zertifikat akzeptiert.
4. **„Neuem Zertifikat vertrauen" AUS** — kein Trust-on-first-use, keine
   Nutzer-Autorisierung neuer Server/CAs (Windows: „Benutzer zur Autorisierung
   auffordern" deaktiviert; Android: „Do not validate" verboten).

> *NICHT VERIFIZIERT:* die konkreten GPO-Template-Objekte/Import-Schritte liegen im
> Schwesterprojekt `linuxmuster-gpo-template` und werden am **realen** domänen-
> gejointen Windows-Client bewiesen (§ 5). Der P6-crabbox-E2E belegt die
> **server-seitige** Wirkung (Accept/Reject), nicht den Windows-Client.

## 4. Steuerung ≠ Enforcement (das tragende Prinzip)

Wie bei linuxmuster-squid gilt die **Trennung von Steuerung und Erzwingung**:

- **Steuerung (Client, weich):** GPO/`.mobileconfig`/EMM legen fest, **welche SSID**
  ein Gerät kennt und **welcher CA** es vertraut. Das ist Komfort und
  Fehlervermeidung — kein Sicherheitsanker.
- **Enforcement (Server, hart):** Die Rollentrennung entscheidet der **RADIUS**. Aus
  `Called-Station-Id` (`<AP-MAC>:<SSID>`) parst er die SSID, verzweigt auf die je SSID
  geforderte Gruppe und prüft per `rlm_ldap` (rekursiv, Bind als `global-binduser`):
  Mitglied der Rollengruppe **und** von `wifi`? Wenn nicht → **Access-Reject**
  (architecture.md § 8, ADR-007).

Daraus folgt die Kernaussage: **Ein Schüler, der sich manuell mit `<schule>-lehrer`
verbindet, wird abgelehnt — egal, welches Profil auf dem Gerät liegt.** Der
`--ssid`-gebundene Gruppen-Check ist die Grenze; das Profil steuert nur den
Normalfall. Ebenso hält das **CA-Pinning** einen Evil-Twin ab, selbst wenn der Client
zur richtigen SSID gelockt wurde: ohne gepinnte Root kein TLS-Tunnel, keine
Preisgabe der MSCHAPv2-Challenge.

## 5. Produktions-Abnahme (human gate)

> Diese Schritte kann **nur ein Mensch** auf realer Hardware (Windows/iOS/Android)
> ausführen. Das **server-seitige Äquivalent (Accept/Reject-Matrix) ist bereits
> automatisiert im P6-crabbox-E2E bewiesen** (test-strategy.md, architecture.md § 9):
> Lehrer @ Lehrer-SSID → Accept (+ VLAN); Schüler @ Lehrer-SSID → Reject; falsches
> Passwort → Reject; Nicht-`wifi` → Reject. Diese Abnahme belegt, dass Netzwerk,
> Client-Rollout und Pinning **am echten Gerät** zusammenwirken.

Pro Gerät protokollieren (User, SSID, Ergebnis, zugeteiltes VLAN):

- [ ] **Lehrer** verbindet sich mit `<schule>-lehrer` → **online**, landet in **VLAN 20**.
- [ ] **Schüler** auf `<schule>-lehrer` → **abgelehnt** (RADIUS-Reject; Profil irrelevant).
- [ ] **Schüler** auf `<schule>-schueler` → **online**, landet in **VLAN 10**.
- [ ] **Falsches Passwort** (beliebige SSID) → **abgelehnt**.
- [ ] **Rogue-AP / Gerät ohne gepinnte EAP-CA** → **abgelehnt** (Pinning hält, kein
      Trust-on-first-use; der Client verbindet sich gar nicht erst zum Fake-Server).
- [ ] *(optional, aus Proof-Matrix)* **Nicht-`wifi`-User** (aus `wifi` entfernt) → **abgelehnt**.

Ergebnis (Client/OS/SSID/Codes) hier oder im Ticket dokumentieren. Erst dann gilt das
Deployment als produktiv abgenommen.

---

**Siehe auch:** AD-Seite & Member-Registrierung → [`radius-and-ad.md`](radius-and-ad.md);
CA, Serverzertifikat & Pinning → [`certs-and-ca.md`](certs-and-ca.md); Control/Data
Plane, Auth-Pfad & ehrliche Grenzen → [`architecture.md`](architecture.md) (§ 7 + § 8);
Assets, Evil-Twin & Non-Goals → [`threat-model.md`](threat-model.md); verifizierte
Fakten mit Quellen → [`references.md`](references.md) (§ UniFi, § EAP-Zertifikate).
