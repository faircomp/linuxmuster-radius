<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/) · [SemVer](https://semver.org/).

## [Unreleased]

_Noch keine Änderungen._

## [0.1.0] - 2026-07-10

**Projekt-Gerüst / P0** — Scaffold, Konventionen und die Architektur-/
Entscheidungs-Docs. Noch **kein lauffähiger Code**: Control-/Data-Plane,
Image und E2E folgen; die menschlichen Gates stehen in der
[`README.md`](README.md).

### Added
- **Repository-Scaffold:** Verzeichnislayout für Control Plane
  (`controlplane/lmnradius/`), Data-plane-Image (`image/`), Deployment/E2E
  (`deploy/`), Packaging (`packaging/debian/`, `packaging/systemd/`) und Tests
  (`scripts/tests/`); dazu `LICENSES/GPL-3.0-or-later.txt`, `renovate.json`,
  `.gitignore` und durchgängig REUSE-3.3-konforme SPDX-Header.
- **`CLAUDE.md`:** Arbeitsweise, Sicherheits-Leitplanken und Code-/Docs-
  Konventionen (Conventional Commits, SemVer + Keep a Changelog, ruff + mypy
  clean, pydantic v2, deutsche Prosa mit englischen Code-Identifiern) sowie die
  crabbox-Heavy-Tier-Regeln.
- **`docs/architecture.md`:** Control-/Data-Plane-Split, **eine** FreeRADIUS-
  Instanz pro Server mit **SSIDs als Config** (statt Container pro SSID),
  PEAP-MSCHAPv2 via winbind (AD-Member-Join), Pro-SSID-Gating über `rlm_ldap`,
  das VLAN-Modell (statisch pro SSID in UniFi, dynamisch per RFC 2868 optional)
  und das UniFi-NAS-/CIDR-Client-Modell.
- **`docs/decisions.md`:** die gesperrten ADRs samt verworfener Alternativen und
  ehrlicher Grenzen — Ein-Instanz-Topologie, PEAP + AD-Join statt EAP-TTLS-PAP,
  **dedizierte EAP-CA** mit load-bearing Client-Pinning (keine linuxmuster-CA,
  kein Let’s Encrypt), statisches vs. dynamisches VLAN und die AD-Member-
  Registrierung des Servers via `devices.csv`/Sophomorix.
- **`docs/references.md`:** verifizierte Quellen (FreeRADIUS-, Samba-/winbind-,
  docs.linuxmuster.net- sowie UniFi-/RADIUS-RFC-Belege) für die tragenden
  Architekturannahmen.
