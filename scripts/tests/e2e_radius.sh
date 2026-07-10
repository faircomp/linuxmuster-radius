#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# End-to-end runner for linuxmuster-radius (HEAVY tier -- crabbox / Docker).
#
# AUTHORED on the dev box, RUN by the operator on crabbox: the dev box has no
# Docker. It is a THIN wrapper around the committed deploy/e2e stack, whose
# docker-compose.yml already wires everything: a real Samba AD DC that
# self-provisions its fixtures (dc/bootstrap.sh -- users/groups/join+bind
# accounts/DNS/'ntlm auth'), the P1 FreeRADIUS image joined to it as a MEMBER,
# and an eapol_test supplicant (client/assert.sh) that runs the 5-case matrix.
# With `--abort-on-container-exit --exit-code-from client`, the client's exit
# code IS the E2E result.
#
#   LMNRADIUS_ALLOW_REAL=1 bash scripts/tests/e2e_radius.sh
#
# WHAT IT PROVES (the matrix, driven by deploy/e2e/client/assert.sh):
#   1. teacher on the teacher SSID -> Access-Accept + Tunnel-Private-Group-Id 20
#   2. student on the teacher SSID -> Access-Reject   (right SSID, wrong role)
#   3. student on the student SSID -> Access-Accept + Tunnel-Private-Group-Id 10
#   4. teacher with a WRONG password -> Access-Reject
#   5. a user NOT in the wifi group  -> Access-Reject (base gate)
# Together these exercise the MEMBER domain-join inside the container, winbindd +
# the winbindd_privileged pipe perms, ntlm_auth NT-hash validation, the
# rewrite_called_station_id -> &Called-Station-SSID branch, the rlm_ldap group
# check, and the Tunnel-* VLAN reply. The happy path also pins the server cert
# (ca_cert + domain_suffix_match), proving the EAP trust anchor.
#
# HONEST LIMIT: the DC is a PLAIN Samba AD DC (dc/bootstrap.sh provisions the
# users/groups/join+bind accounts DIRECTLY), NOT the linuxmuster Sophomorix /
# devices.csv role='server' registration path -- that still needs a real
# linuxmuster server (docs/radius-and-ad.md, NICHT VERIFIZIERT).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
E2E="$ROOT/deploy/e2e"
CF="$E2E/docker-compose.yml"

# --- refuse to run without the explicit opt-in (belt-and-suspenders: run.sh
#     already gates the e2e tier, but a direct invocation must refuse too) ---
if [ "${LMNRADIUS_ALLOW_REAL:-0}" != "1" ]; then
  echo "REFUSING: this stands up a real Samba AD DC + a joined FreeRADIUS via Docker." >&2
  echo "Set LMNRADIUS_ALLOW_REAL=1 to run it (crabbox only -- the dev box has no Docker)." >&2
  exit 2
fi

[ -f "$CF" ] || { echo "FATAL: $CF not found -- deploy/e2e is the P6 stack." >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "FATAL: openssl not on PATH (run crabbox_bootstrap.sh)." >&2; exit 1; }

# Docker directly, else via sudo (a fresh crabbox login is not in the docker group yet).
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"
DC="$DOCKER compose -f $CF"

# shellcheck disable=SC2317  # cleanup is invoked via 'trap cleanup EXIT', not directly
cleanup(){ $DC down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== reset any previous stack =="
$DC down -v --remove-orphans >/dev/null 2>&1 || true

# The EAP CA + server cert are minted out-of-band (openssl, mirroring
# 'lmnradius cert issue') into deploy/e2e/certs/out/, which the compose bind-mounts.
echo "== mint the EAP CA + server cert (certs/gen-certs.sh) =="
bash "$E2E/certs/gen-certs.sh" radius.example.lmn || { echo "FATAL: cert generation failed." >&2; exit 1; }

# Bring the whole stack up. compose's service_healthy ordering gates it: the DC
# self-provisions and goes healthy, the radius member joins and goes healthy, then
# the client runs the matrix. --abort-on-container-exit + --exit-code-from client
# makes the client's exit code (0 = all 5 cases pass) this script's result.
echo "== build + run the E2E (DC -> joined member -> eapol_test matrix) =="
$DC up --build --abort-on-container-exit --exit-code-from client
rc=$?

echo
echo "== radius log excerpt =="
$DC logs radius 2>/dev/null | tail -30 || true

echo
if [ "$rc" -eq 0 ]; then echo "E2E: ALL CASES PASSED"; else echo "E2E: FAILED (see per-case output above)"; fi
exit "$rc"
