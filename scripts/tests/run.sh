#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Test aggregator for linuxmuster-radius. See docs/test-strategy.md and the
# /test skill. Modes: lint | unit | quick (default) | e2e | all.
# Each step is dependency-gated and skips cleanly when a toolchain is missing.
# e2e/all refuse without LMNRADIUS_ALLOW_REAL=1 (protection against accidental runs).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1

# Prefer control-plane tools from the venv (created by crabbox_bootstrap)
[ -x "$ROOT/.venv/bin/ruff" ] && export PATH="$ROOT/.venv/bin:$PATH"

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
  lint)  lint ;;
  unit)  unit ;;
  quick) lint; unit ;;
  e2e)   e2e ;;
  all)   lint; unit; e2e ;;
  *) echo "usage: run.sh [lint|unit|quick|e2e|all]" >&2; exit 2 ;;
esac

echo
echo "$PASS passed, $FAIL failed, $SKIP skipped"
[ "$FAIL" -eq 0 ]
