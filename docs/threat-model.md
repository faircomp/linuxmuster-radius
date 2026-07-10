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

## Non-Goals (vorläufig)

- Kein EAP-TLS-Client-Zertifikats-PKI in 1.0 (spätere Phase).
- Kein Schutz gegen einen bösartigen Domänen-Admin (AD ist der Trust-Anker).

_Details folgen in P1/P2._
