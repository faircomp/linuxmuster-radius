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
- Lockfiles: `bash scripts/check-lockfiles.sh` (CI-Job `lockfile`; braucht `uv` und PyPI) —
  Pins passen zu `pyproject.toml`/`build-requirements.in`, jede Prüfsumme stammt von PyPI,
  jede Fassung hat ein Wheel für CPython 3.12/glibc 2.39/x86_64. `pytest` und `mypy` laufen
  in CI gegen die gelockten Fassungen.
- Lock-Tore: `bash scripts/tests/lock_gates.sh` (CI-Job `fast`; braucht `uv` und PyPI, überspringt
  nie) — manipulierte Lockfiles müssen **sowohl** `check-lockfiles.sh` **als auch**
  `packaging/build-venv.sh` (also den Paketbau) mit dem erwarteten Grund scheitern lassen:
  eingerückte URL-Zeile (`    zzzevil @ file:///…#sha256=…`), `--extra-index-url`, entfernte
  Hashes eines Pins, geänderter Hash (auch der eines nie geladenen sdist), zusätzlicher Pin mit
  echten PyPI-Hashes, Pin ohne Hash; dazu je Tor (`scripts/lockfile_gate.py lint|pypi|freeze|
  closure`) die Varianten, die pip anders liest als sie aussehen (`-i/-f/-e/-r/-c`, CR/FF/U+2028
  in Kommentaren, Tab, NUL, Marker, Großschreibung, …). Dazu die K1-Fälle: ein Wheel mit
  `bin/`-Skripten und einer Marker-schreibenden `.pth`, als echter Pin (Name und Hash echt,
  Veröffentlichung nur in der Wegwerfkopie simuliert) in der Build-Sperrdatei (Variante 1) oder
  in beiden (Variante 2) — die Mengen-/Hüllen-Prüfung muss ihn abweisen, **bevor** ein
  Sperrdatei-Wheel entpackt wird. Mit `LOCK_GATES_DEB=1` im Build-Image zusätzlich der ganze
  `make deb` je Fall (CI-Job `lock-gates-build`): muss ohne `.deb` scheitern, und bei den
  K1-Fällen darf die Markerdatei **nicht** entstehen (die `.pth` lief nie).
- K1-Regel (`work/tasks/nachbesserung-kalte-pruefung-umbau.md`): vor jedem Programm/Interpreter
  aus einem Sperrdatei-venv und bevor dessen `bin/` im PATH steht, sind beide Sperrdateien voll
  geprüft (Grammatik, jeder Hash von PyPI, Pins = Hülle der Eingaben). `packaging/build-venv.sh`
  installiert dazu `uv` hash-gepinnt aus `controlplane/uv-requirements.lock` (nur `uv`, per
  absolutem Pfad, isoliert) und lässt es `check-lockfiles.sh` die Hülle prüfen, alles **vor**
  dem venv. `uv` kommt überall (fast tier, `lockfile`, Bau) aus dieser Datei, nie aus einem
  ungepinnten `pip install uv==…`.

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
- **7.3.1 (Fix-Runde 2026-09-22):** `ldaps://` ohne `--ldap-ca` → API 422 (Unit);
  falsche/fremde DC-CA → Container startet nicht, kein Access-Accept (Lab, negativ);
  `--client-subnet` in 127.0.0.0/8 (oder jedes CIDR, das 127.0.0.1 enthält) → 422 (Unit);
  crash-loopender Container → `create`/`reconcile` Exit 1 mit `last_error` (Unit);
  git-Change-Log ohne konfigurierte Identität → Commit entsteht trotzdem, git-Fehler →
  `StoreError`/HTTP 500 statt stillem Verlust (Unit); `rm` bei nicht erreichbarem DC →
  lokal entfernt, Domänen-Austritt als Fehler gemeldet (Unit).

_Details folgen in P1/P2._
