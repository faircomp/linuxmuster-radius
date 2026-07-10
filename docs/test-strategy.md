<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Test Strategy — linuxmuster-radius

> **Status: Gerüst (P0).** Zwei-Tier-Modell wie bei `linuxmuster-squid`. Der Katalog
> wächst pro Roadmap-Phase; Negativ-Tests sind Pflicht.

## Fast-Tier (lokal / CI)

- `ruff check` · `ruff format --check` · `mypy` · `pytest` · `shellcheck` · `reuse lint`
- Aggregat: `bash scripts/tests/run.sh quick` (entsteht in P1/P2).

## Heavy-Tier (crabbox, Docker)

Der Dev-Rechner hat **kein** Docker — der schwere Tier läuft auf **crabbox** (ephemere
Proxmox-VM). Stack: **Samba-AD-DC + gejointe FreeRADIUS-Instanz + `eapol_test`-Supplicant**.

**Proof-Matrix (Ziel):**

| Fall | Erwartung |
|---|---|
| Lehrer @ `<schule>-lehrer` | Access-Accept (+ korrektes VLAN) |
| Schüler @ `<schule>-lehrer` | Access-Reject |
| falsches Passwort | Access-Reject |
| Nicht-`wifi`-User | Access-Reject |

Aufruf: `LMNRADIUS_ALLOW_REAL=1 bash scripts/tests/run.sh e2e`.

## Negativ-Test-Katalog (wächst je Phase)

- Reject bei falscher Gruppe/SSID-Kombination · Reject ohne `wifi` · Reject bei
  ungültigem Cert-Pinning-Szenario · unbekannter RADIUS-Client (falsches Subnetz) →
  ignoriert · API 401/403 · … _(zu vervollständigen)_

_Details folgen in P1/P2._
