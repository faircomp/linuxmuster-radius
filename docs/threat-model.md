<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Threat Model — linuxmuster-radius

> **Status: Gerüst (P0).** Wird ausgearbeitet, sobald Image (P1) und Control-Plane
> (P2) stehen. Struktur wie bei `linuxmuster-squid`:
> **Assets → Risiken & Gegenmaßnahmen (mit Verifikations-Spalte) → bewusste Non-Goals.**
> Entscheidungen dazu in [`decisions.md`](decisions.md), Architektur in
> [`architecture.md`](architecture.md).

## Assets (vorläufig)

- **Maschinen-Account-Secret / Domänen-Beitritt** (`/var/lib/samba`-Volume) — winbind-
  Secure-Channel zum DC.
- **EAP-CA-Privatschlüssel** (Root, idealerweise offline) und **Server-Cert/-Key**.
- **LDAP-Bind-Credential** (`global-binduser`).
- **API-Token** der Control-Plane; **RADIUS-Shared-Secrets** (UniFi).

## Risiken (Auszug, zu vervollständigen)

- **Evil-Twin / Rogue-RADIUS** → erzwungene Server-Cert-Validierung (CA- + Server-Name-
  Pinning) auf allen Clients; ist wegen der MSCHAPv2-Schwäche **tragend**.
- **Control-Plane-RCE = Host-Root** (Docker-Socket) → API nur `127.0.0.1` + Token
  (`hmac.compare_digest`), gehärtete systemd-Unit, Socket-Proxy — reduziert die Fläche,
  bleibt aber root-äquivalent (ehrliche Grenze, s. ADR-012).
- **Secrets-Leak** (Keytab/Bind-PW/CA-Key) → tmpfs/`:ro`, `0600`, nie im Log/Env.
- **Personenbezug der Auth-/Accounting-Logs (DSGVO)** → Retention, Zugriff nur via Token.
- **On-Path-Angreifer zwischen RADIUS-VM und DC (LDAPS)** — die MSCHAPv2-Prüfung selbst
  läuft über den winbind-Secure-Channel (signiert/versiegelt, nicht betroffen), aber das
  **Rollen-Gate und die VLAN-Zuweisung** kommen aus der `rlm_ldap`-Gruppenabfrage über
  LDAPS. Bis 7.3.0 prüfte stunnel das DC-Zertifikat **nicht** (Kampagne 2026-09-22,
  Befund 8.5): wer sich zwischen RADIUS und DC setzt, kann einen Schüler als Lehrer
  ausgeben oder ein anderes VLAN liefern. **Gegenmaßnahme (7.3.1):** das DC-Zertifikat
  wird gegen eine gepinnte CA geprüft — für neue Instanzen Pflicht (`lmnradius create
  --ldap-ca <cacert.pem>`; die API lehnt `ldaps://` ohne CA ab), für bestehende
  Instanzen der Upgrade-Schritt `lmnradius set-ldap-ca`; bis dahin warnen `list`/`health`
  mit „LDAPS unverified". Falsche/fremde CA ⇒ der Container startet **nicht** (fail-closed,
  negativ getestet: `rlm_ldap` bekommt keine Verbindung). **Bewusste Restlücke:**
  `--ldap-ca-tofu` pinnt, was der DC beim ersten Kontakt liefert (Trust on first use);
  ein Angreifer, der genau in diesem Moment on-path ist, wird gepinnt — deshalb warnt die
  CLI laut, druckt die SHA-256-Fingerprints und die Doku verlangt den Abgleich mit
  `openssl x509 -fingerprint` auf dem DC. Klartext-`ldap://` bleibt möglich, ist aber
  nicht verifizierbar und nicht empfohlen. **Verifikation:** Unit-Tests (API lehnt ohne
  CA ab, `set-ldap-ca`), Lab-Negativtest mit falscher CA (`work/campaign/radius-fix.md`).
- **Falscher DNS-Eintrag durch den Join** — `net ads join` registrierte bis 7.3.0 die
  Container-Bridge-IP als A-Record des RADIUS-FQDN (kein Angriff, aber ein Integritäts-
  problem für alles, was den Namen über das AD-DNS auflöst). Seit 7.3.1: `--no-dns-updates`
  und Registrierung der Host-LAN-Adresse mit dem Maschinenkonto bei jedem Start.
- **Leichen im AD nach `rm`** — bis 7.3.0 blieben Computerkonto, DNS-Record und
  Maschinen-Secret-Volume zurück (verwaiste Anmeldeidentität). Seit 7.3.1 verlässt `rm`
  die Domäne (Konto + Record gelöscht) und entfernt das Volume.

## Non-Goals (vorläufig)

- Kein EAP-TLS-Client-Zertifikats-PKI in 1.0 (spätere Phase).
- Kein Schutz gegen einen bösartigen Domänen-Admin (AD ist der Trust-Anker).

_Details folgen in P1/P2._
