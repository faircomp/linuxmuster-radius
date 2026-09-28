<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# Test Strategy — linuxmuster-radius

> **Status: Gerüst (P0).** Zwei-Tier-Modell wie bei `linuxmuster-squid`. Der Katalog
> wächst pro Roadmap-Phase; Negativ-Tests sind Pflicht.

## Fast-Tier (lokal / CI)

- `ruff check` · `ruff format --check` · `mypy` · `pytest` · `shellcheck` · `reuse lint`
- Aggregat: `bash scripts/tests/run.sh quick` = Lock-Tor zuerst, dann lint, unit und der
  Lock-Regressionstest; scheitert das Tor, bricht run.sh ab und nichts weiter läuft (lint, unit
  und der Lock-Test könnten Code aus einer Sperrdatei ausführen). `run.sh gate` ist nur das Tor.
  Übersprungen ist nicht grün: fehlt ein Werkzeug (oder `LMNRADIUS_ALLOW_REAL=1` für e2e), endet
  run.sh mit Exit 77 statt 0, bei einem Fehlschlag mit 1; die Schlusszeile nennt alles, was nicht
  geprüft wurde (übersprungen oder nach rotem Tor nicht gelaufen). Wer Überspringen bewusst
  zulässt, setzt `LMNRADIUS_ALLOW_SKIP=1` (Exit 0, die Schlusszeile nennt die Lücken trotzdem).
  Vor dem Tor läuft nichts vom Aufrufer: run.sh startet sich zuerst unter `env -i` mit der
  Positivliste neu (siehe unten), ruft `/usr/bin/dirname` mit absolutem Pfad und das Tor mit
  `/bin/bash -p`; erst nach dem Tor bekommen lint, unit und e2e den PATH des Aufrufers zurück.
- Lock-Tor: `bash scripts/check-lockfiles.sh` — **das** Tor vor allem, was aus einer
  Sperrdatei installiert wird (CI-Fast-Tier und CI-Job `lock-gates-build` als erster Schritt,
  Job `lockfile` in ci.yml und release.yml, `packaging/build-venv.sh`, `run.sh`). Prüft alle drei Sperrdateien: Grammatik,
  uv-Sperrdatei = genau das uv aus `uv-requirements.in` mit PyPI-Hashes, dann uv aus ihr in
  ein isoliertes venv (absoluter Pfad), Pins = Hülle von `pyproject.toml`/
  `build-requirements.in`, jede Prüfsumme von PyPI, jede Fassung hat ein Wheel für CPython
  3.12/glibc 2.39/x86_64. Braucht `/usr/bin/python3` mit venv (python3-venv) und PyPI, kein uv
  vorab. Unabhängig von der Umgebung des Aufrufers (P1): Tor, `build-venv.sh`, `make-deb.sh`,
  `run.sh` und die beiden Test-Gerüste beginnen mit demselben Block und starten sich unter
  `env -i` über `/bin/bash -p` neu, nur mit der Positivliste aus CLAUDE.md ("Python
  dependencies"; fester PATH, C.UTF-8, `HOME`, `TMPDIR`, Proxy- und CA-Variablen, die Schalter
  des Repos); keine Funktion, keine andere Variable. Grenze: nur, was vor dem Neustart läuft (die
  Shell des Aufrufers mit `BASH_ENV`/`SHELLOPTS`, `LD_PRELOAD`, das eigene `make` des Aufrufers),
  und die bewusst durchgelassenen Proxy-/CA-Variablen. Ohne Netz scheitert es nach Sekunden (kurze PyPI-Timeouts, Abbruch beim
  ersten unlesbaren Pin) und sagt das ("PyPI gave no answer (offline?)"), statt einen Hash zu
  beschuldigen; ebenso `build-venv.sh`/`make deb`.
  **Grenze:** eine andere echte Fassung (älter oder neuer, echte Hashes), die die deklarierten
  Anforderungen erfüllt, besteht das Tor; nur die Review fängt sie. `pytest` und `mypy` laufen
  in CI gegen die gelockten Fassungen, installiert erst nach dem Tor.
- Lock-Tore: `bash scripts/tests/lock_gates.sh` (CI-Job `fast`; braucht PyPI, überspringt
  nie; bricht sofort ab, wenn die committeten Sperrdateien das Tor nicht bestehen, denn jeder
  Fall arbeitet auf Kopien davon) — manipulierte Lockfiles müssen **sowohl** `check-lockfiles.sh` **als auch**
  `packaging/build-venv.sh` (also den Paketbau) mit dem erwarteten Grund scheitern lassen:
  eingerückte URL-Zeile (`    zzzevil @ file:///…#sha256=…`), `--extra-index-url`, entfernte
  Hashes eines Pins, geänderter Hash (auch der eines nie geladenen sdist), zusätzlicher Pin mit
  echten PyPI-Hashes, Pin ohne Hash; dazu je Tor (`scripts/lockfile_gate.py lint|pypi|freeze|
  closure`) die Varianten, die pip anders liest als sie aussehen (`-i/-f/-e/-r/-c`, CR/FF/U+2028
  in Kommentaren, Tab, NUL, Marker, Großschreibung, …). Dazu:
  - **K1:** das Wheel aus `scripts/tests/k1_wheel.py` (ausführbare `bin/`-Skripte `diff`,
    `comm`, `sort`, `awk`, `grep`, `cut`, `cp`, `python3`, `python`, `pip`, `uv`, `bash`, … und
    eine `.pth`, alle schreiben eine Markerdatei), als echter Pin (Name und Hash echt,
    Veröffentlichung nur in der Wegwerfkopie simuliert) in der Build-, der Laufzeit-, in beiden
    oder in der uv-Sperrdatei: Tor und `build-venv.sh` weisen es ab, der Marker entsteht nie.
  - **R6:** das Wheel ist scharf — ohne Tor installiert, sind seine `bin/`-Skripte ausführbare
    Dateien und schreiben den Marker, ebenso die `.pth`; Gegenprobe: `build-venv.sh` ohne den
    Tor-Aufruf erzeugt den Marker aus einem `bin/`-Skript. Die Gegenproben (hier und bei R2)
    installieren nur Fixture-Sperrdateien, die allein das Test-Wheel pinnen, nie die
    Sperrdateien des Checkouts (kalte Prüfung r3, F1).
  - **R1/P3:** unter vergifteter Umgebung (aktiviertes venv mit dem Wheel vorn im PATH, darin
    auch `dirname`, `VIRTUAL_ENV`, `PYTHONPATH` mit Marker-`sitecustomize`, `PYTHONHOME`,
    `UV_*`/`PIP_*` samt Konfigurationsdateien, die Paketquelle und Interpreter umlenken, `.venv`
    im Checkout; exportierte Funktionen `set`, `export`, `.`, `builtin`, `exec`, `exit`, `shift`,
    `cd`, `unset`, `compgen`, `mapfile`, `dirname`; `PERL5OPT`/`PERL5LIB`, `CDPATH`,
    `GCONV_PATH` mit einem gconv-Modul und `LD_LIBRARY_PATH` mit einer `libz.so.1`, beide
    markerschreibend (mit gcc gebaut), `TAR_OPTIONS` mit Checkpoint-Aktion; für direkt
    gestartete Skripte `MAKEFILES`, `MAKEFLAGS`/`GNUMAKEFLAGS` mit `-i` und fremder `SHELL`,
    `MAKEOVERRIDES`) gleiche Urteile und kein Marker: `check-lockfiles.sh`, `build-venv.sh`,
    `run.sh gate`/`lint`, mit `LOCK_GATES_DEB=1` auch `make deb`, `make-deb.sh` direkt,
    `make -i deb`, `GNUMAKEFLAGS=-i make deb` und `SHELLOPTS=noexec make deb` (kein .deb). Der
    Neustart-Block selbst unter demselben Gift sieht nur die Positivliste, keine Funktion, und
    läuft als `bash -p`. Jeder Teil des Gifts ist nachweislich scharf (Gegenproben: eine bash,
    die die Funktionen behält, `make`, `dpkg-parsechangelog`, `iconv`, `python3`, `tar`,
    `dirname` aus dem PATH, ein relatives `cd` unter `CDPATH`). Die Grenze wird gezeigt:
    `SHELLOPTS=noexec` bei direktem `bash`-Start lässt das Tor nichts tun (Exit 0, keine
    Urteilszeile).
  - **T2:** `build-venv.sh` weist einen venv-Pfad ab, der nicht absolut ist, Leerraum oder
    `.`/`..`/`//` enthält oder nicht auf `/opt/linuxmuster-radius/venv` endet, bevor irgendetwas
    läuft; das benannte Verzeichnis bleibt.
  - **R2:** die Lock-Schritte von ci.yml (`fast`, `lockfile`) und release.yml (`lockfile`),
    per `scripts/tests/ci_step.py` wörtlich ausgelesen und wie in Actions nachgefahren, bleiben
    mit einem gepflanzten Paket in jeder Sperrdatei am Tor stehen; nichts wird installiert, kein
    Marker. Gegenprobe: die Sperrdatei-Zeilen des Install-Schritts ohne Tor-Schritt davor
    installieren es, der Marker entsteht aus einem `bin/`-Skript. `controlplane/tests/
    test_packaging.py` hält die Reihenfolge der Workflow-Schritte zusätzlich statisch fest.
  Mit `LOCK_GATES_DEB=1` im Build-Image zusätzlich der ganze `make deb` je Fall (CI-Job
  `lock-gates-build`): muss ohne `.deb` und ohne Marker scheitern, auch unter der vergifteten
  Umgebung.
- `make deb` selbst: `bash scripts/tests/make_deb_checks.sh` (CI-Job `lock-gates-build`, im
  Build-Image): ein vollständiger Bau eines schmutzigen git-Checkouts (geänderte, gelöschte,
  gestagte, nicht hinzugefügte Datei, Modus 0600 und umask 002, versionierter Symlink,
  ignorierte Geheimnisse) unter der vergifteten Umgebung und mit fsmonitor, Filter und Hooks in
  `.git` und mit `MAKEFILES`/`MAKEFLAGS`/`GNUMAKEFLAGS`, `PERL5OPT`/`PERL5LIB`, `CDPATH`,
  `TAR_OPTIONS`, `SHELLOPTS=noexec` und Funktionen, die in jedem make-Lauf von `debian/rules`
  bzw. jedem Perl-Programm oder tar den Marker schreiben würden, als `make deb DEST=…`:
  baut, warnt vor und nach dem Bau über jede Abweichung von HEAD (und dass die Version
  die des Changelogs bleibt), packt genau die versionierten Dateien mit 0644/0755 und den
  Symlink, führt nichts aus `.git` oder der Umgebung aus. Ein Worktree ohne erreichbares
  Repository bricht ab, ohne etwas zu packen; `make_deb_checks.sh guard build` als root: ein
  Checkout eines anderen Nutzers wird von git's Eigentümerschutz abgewiesen, nichts läuft.
  **T1:** mit einem `TMPDIR`, der ein Leerzeichen enthält (der Bau läuft in einem Verzeichnis
  darunter), bricht `debian/rules` ab, bevor etwas läuft; ein Verzeichnis, das wie der Teil vor
  dem Leerzeichen heißt, bleibt (mit den alten, ungequoteten Pfaden in `debian/rules` löschte
  `build-venv.sh` es per `rm -rf`).

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
