#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Test aggregator for linuxmuster-radius. See docs/test-strategy.md and the
# /test skill. Modes: gate | lint | unit | quick (default) | locks | e2e | all.
# Each step is dependency-gated and skips cleanly when a toolchain is missing.
# e2e/all refuse without LMNRADIUS_ALLOW_REAL=1 (protection against accidental runs).
#
# The lock gate (scripts/check-lockfiles.sh) runs FIRST in every mode but e2e, before anything
# that could run a package from a lockfile (mypy plugins, pytest, a venv's tools, the lock
# regression test), started as /bin/bash; the gate fixes its own PATH and drops VIRTUAL_ENV,
# PYTHON*, UV_* and PIP_*, so neither an activated venv nor the checkout's .venv takes part in
# it (K1/R1). If it fails, run.sh stops right there: nothing else runs. Only after it passed
# are the control-plane tools of .venv (created by crabbox_bootstrap) put first on PATH for
# lint and unit; the lock regression test (`locks`) cleans its environment like the gate.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1

PASS=0; FAIL=0; SKIP=0
pass(){ PASS=$((PASS + 1)); printf '  [PASS] %s\n' "$1"; }
fail(){ FAIL=$((FAIL + 1)); printf '  [FAIL] %s\n' "$1"; }
skip(){ SKIP=$((SKIP + 1)); printf '  [SKIP] %s (%s)\n' "$1" "$2"; }
have(){ command -v "$1" >/dev/null 2>&1; }

# run_step <name> <required-tool> <command...>
run_step(){
  local name="$1" tool="$2"; shift 2
  if ! have "$tool"; then skip "$name" "$tool not installed"; return; fi
  if "$@"; then pass "$name"; else fail "$name"; fi
}

summary(){
  echo
  echo "$PASS passed, $FAIL failed, $SKIP skipped"
  [ "$FAIL" -eq 0 ]
}

gate(){
  echo "== lock gate =="
  if /bin/bash scripts/check-lockfiles.sh; then
    pass "lock gate"
    # Prefer control-plane tools from the venv (created by crabbox_bootstrap), only now.
    if [ -x "$ROOT/.venv/bin/ruff" ]; then export PATH="$ROOT/.venv/bin:$PATH"; fi
  else
    fail "lock gate"
    echo "lock gate failed: run.sh stops here, nothing else runs (lint, unit, the lock" \
      "regression test and e2e could all run code from a lockfile)"
    summary
    exit 1
  fi
}

locks(){
  echo "== lock gates regression test =="
  # Tampered lockfiles must fail the lockfile check AND the venv build; needs /usr/bin/python3
  # with venv and PyPI (CI runs it too, where it never skips).
  run_step "lock gates" /usr/bin/python3 /bin/bash scripts/tests/lock_gates.sh
}

lint(){
  echo "== lint =="
  if have ruff; then
    run_step "ruff check"        ruff ruff check .
    run_step "ruff format check" ruff ruff format --check .
  else
    skip "ruff" "not installed"
  fi
  # mypy uses controlplane/pyproject.toml so its docker-ignore override is picked up.
  if [ -f controlplane/pyproject.toml ]; then
    run_step "mypy" mypy mypy --config-file controlplane/pyproject.toml controlplane/lmnradius
  else
    skip "mypy" "no control-plane code yet"
  fi
  if have shellcheck; then
    local sh=()
    mapfile -t sh < <(git ls-files '*.sh' 2>/dev/null)
    if [ "${#sh[@]}" -gt 0 ]; then
      # Warning level only: the info tier is noise here (SC2317 unreachable in
      # trap-cleanup helpers, SC2016 intentional envsubst SHELL-FORMAT quotes).
      run_step "shellcheck" shellcheck shellcheck --severity=warning "${sh[@]}"
    else
      skip "shellcheck" "no .sh files"
    fi
  else
    skip "shellcheck" "not installed"
  fi
  # REUSE: every file needs an SPDX header or a .license sidecar.
  run_step "reuse" reuse reuse lint
}

unit(){
  echo "== unit =="
  if [ -f controlplane/pyproject.toml ]; then
    run_step "pytest" pytest pytest -q controlplane/tests
  else
    skip "unit" "no control-plane code yet"
  fi
}

e2e(){
  echo "== e2e (heavy tier) =="
  # Heavy tier lives on crabbox (Samba AD DC + joined FreeRADIUS + eapol_test
  # supplicant); it refuses to run unless explicitly allowed.
  if [ "${LMNRADIUS_ALLOW_REAL:-0}" != "1" ]; then
    skip "freeradius-e2e" "LMNRADIUS_ALLOW_REAL!=1"
    return
  fi
  if ! have docker; then skip "freeradius-e2e" "docker not installed"; return; fi
  if [ -x scripts/tests/e2e_radius.sh ]; then
    # e2e_radius.sh brings up deploy/e2e, runs the 5-case PEAP-MSCHAPv2 matrix and
    # tears the stack down; it self-gates on LMNRADIUS_ALLOW_REAL=1 as well.
    run_step "freeradius-e2e" docker bash scripts/tests/e2e_radius.sh
  else
    skip "freeradius-e2e" "scripts/tests/e2e_radius.sh missing"
  fi
}

mode="${1:-quick}"
case "$mode" in
  gate)  gate ;;
  lint)  gate; lint ;;
  unit)  gate; unit ;;
  quick) gate; lint; unit; locks ;;
  locks) gate; locks ;;
  e2e)   e2e ;;
  all)   gate; lint; unit; locks; e2e ;;
  *) echo "usage: run.sh [gate|lint|unit|quick|locks|e2e|all]" >&2; exit 2 ;;
esac

summary
