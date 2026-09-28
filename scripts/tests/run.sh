#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Test aggregator for linuxmuster-radius. See docs/test-strategy.md and the
# /test skill. Modes: gate | lint | unit | quick (default) | locks | e2e | all.
# Each step is dependency-gated: a missing toolchain SKIPS the step, and skipped is not passed.
# Exit status: 0 only if every step of the mode ran and passed; 1 if a step failed (or the gate
# stopped the run); 77 if nothing failed but a step was skipped. The last line names every step
# that was not checked. LMNRADIUS_ALLOW_SKIP=1 accepts skips on purpose (exit 0; the last line
# still names them). e2e/all refuse without LMNRADIUS_ALLOW_REAL=1 (protection against
# accidental runs): e2e is then skipped, so they end with 77 unless skips are allowed.
#
# The lock gate (scripts/check-lockfiles.sh) runs FIRST in every mode but e2e, before anything
# that could run a package from a lockfile (mypy plugins, pytest, a venv's tools, the lock
# regression test), started as /bin/bash -p. run.sh itself, the gate and the lock regression
# test start again in a clean environment first (the block below: an allowlist under env -i,
# nothing else of the caller's, no function; CLAUDE.md, "Python dependencies"), so neither an
# activated venv nor the checkout's .venv takes part in the gate (K1/R1/P1). If it fails, run.sh
# stops right there: nothing else runs, and the last line says what did not run. Only after the
# gate passed do lint, unit and e2e get the caller's PATH back (kept aside as
# LMNRADIUS_CALLER_PATH), with the control-plane tools of .venv (created by crabbox_bootstrap)
# first: they use the caller's tools on purpose. What the caller's shell does before the restart
# is the caller's (the limit in CLAUDE.md): with SHELLOPTS=noexec it runs nothing and ends 0
# without the summary line -- a run without that line has checked nothing.
# The caller's PATH, kept aside for the steps after the gate (lint, unit, e2e use the caller's
# tools on purpose); nothing before the gate uses it.
LMNRADIUS_CALLER_PATH="${LMNRADIUS_CALLER_PATH-$PATH}"
# ---- clean environment (P1): the same block in every entry script (test_packaging.py) ----
# Unless this is the clean run already, start again under `env -i` with exactly this allowlist,
# through /bin/bash -p, which imports no function and reads no BASH_ENV, ENV, SHELLOPTS or
# BASHOPTS: PATH=/usr/sbin:/usr/bin:/sbin:/bin, LANG and LC_ALL C.UTF-8, and, where set, HOME,
# TMPDIR, http_proxy, https_proxy, no_proxy, HTTP_PROXY, HTTPS_PROXY, NO_PROXY, SSL_CERT_FILE,
# SSL_CERT_DIR, REQUESTS_CA_BUNDLE, PIP_CERT, and the switches the repository's scripts pass
# each other: LMNRADIUS_ALLOW_REAL, LMNRADIUS_ALLOW_SKIP, LMNRADIUS_CALLER_PATH (run.sh),
# LOCK_GATES_DEB, LOCK_GATES_VERBOSE (lock_gates.sh). Every other variable and every function of
# the caller is gone. Up to the exec only keywords, assignments and one command by absolute path
# run: POSIXLY_CORRECT puts bash into POSIX mode, where the special builtin `exec` comes before
# any function the caller exported. The clean run is told by its first argument.
if [[ "${1-}" != --lmnradius-clean-env ]]; then
    POSIXLY_CORRECT=1
    exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
        ${HOME+"HOME=$HOME"} ${TMPDIR+"TMPDIR=$TMPDIR"} \
        ${http_proxy+"http_proxy=$http_proxy"} ${https_proxy+"https_proxy=$https_proxy"} \
        ${no_proxy+"no_proxy=$no_proxy"} ${HTTP_PROXY+"HTTP_PROXY=$HTTP_PROXY"} \
        ${HTTPS_PROXY+"HTTPS_PROXY=$HTTPS_PROXY"} ${NO_PROXY+"NO_PROXY=$NO_PROXY"} \
        ${SSL_CERT_FILE+"SSL_CERT_FILE=$SSL_CERT_FILE"} ${SSL_CERT_DIR+"SSL_CERT_DIR=$SSL_CERT_DIR"} \
        ${REQUESTS_CA_BUNDLE+"REQUESTS_CA_BUNDLE=$REQUESTS_CA_BUNDLE"} ${PIP_CERT+"PIP_CERT=$PIP_CERT"} \
        ${LMNRADIUS_ALLOW_REAL+"LMNRADIUS_ALLOW_REAL=$LMNRADIUS_ALLOW_REAL"} \
        ${LMNRADIUS_ALLOW_SKIP+"LMNRADIUS_ALLOW_SKIP=$LMNRADIUS_ALLOW_SKIP"} \
        ${LMNRADIUS_CALLER_PATH+"LMNRADIUS_CALLER_PATH=$LMNRADIUS_CALLER_PATH"} \
        ${LOCK_GATES_DEB+"LOCK_GATES_DEB=$LOCK_GATES_DEB"} \
        ${LOCK_GATES_VERBOSE+"LOCK_GATES_VERBOSE=$LOCK_GATES_VERBOSE"} \
        /bin/bash -p "$0" --lmnradius-clean-env "$@"
fi
shift
# ---- end of the clean environment block ----
CALLER_PATH="$LMNRADIUS_CALLER_PATH"
unset LMNRADIUS_CALLER_PATH
set -uo pipefail

ROOT="$(cd "$(/usr/bin/dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1

PASS=0; FAIL=0; SKIP=0
SKIPPED=()   # "<step> (<why>)" of every skipped step, for the last line
NOT_RUN=""   # the steps the failed gate kept from running
pass(){ PASS=$((PASS + 1)); printf '  [PASS] %s\n' "$1"; }
fail(){ FAIL=$((FAIL + 1)); printf '  [FAIL] %s\n' "$1"; }
skip(){ SKIP=$((SKIP + 1)); SKIPPED+=("$1 ($2)"); printf '  [SKIP] %s (%s)\n' "$1" "$2"; }
have(){ command -v "$1" >/dev/null 2>&1; }

# run_step <name> <required-tool> <command...>
run_step(){
  local name="$1" tool="$2"; shift 2
  if ! have "$tool"; then skip "$name" "$tool not installed"; return; fi
  if "$@"; then pass "$name"; else fail "$name"; fi
}

# The last line: the counts, then everything that was not checked; the exit status (see above).
summary(){
  local line="$PASS passed, $FAIL failed, $SKIP skipped" skipped rc=0
  echo
  if [ -n "$NOT_RUN" ]; then line+="; NOT run (the lock gate failed): $NOT_RUN"; fi
  if [ "$SKIP" -gt 0 ]; then
    skipped="$(printf '%s, ' "${SKIPPED[@]}")"
    line+="; NOT checked: ${skipped%, }"
  fi
  if [ "$FAIL" -gt 0 ]; then
    rc=1
  elif [ "$SKIP" -gt 0 ] && [ "${LMNRADIUS_ALLOW_SKIP:-0}" = 1 ]; then
    line+=" (skips accepted: LMNRADIUS_ALLOW_SKIP=1)"
  elif [ "$SKIP" -gt 0 ]; then
    rc=77
    line+=" -> INCOMPLETE, exit 77 (LMNRADIUS_ALLOW_SKIP=1 accepts skips)"
  fi
  echo "$line"
  return "$rc"
}

# gate <steps that follow>: they run only if the gate passes.
gate(){
  echo "== lock gate =="
  if /bin/bash -p scripts/check-lockfiles.sh; then
    pass "lock gate"
    # Only now the caller's tools, and control-plane tools from the venv (created by
    # crabbox_bootstrap) before them.
    export PATH="$CALLER_PATH"
    if [ -x "$ROOT/.venv/bin/ruff" ]; then export PATH="$ROOT/.venv/bin:$PATH"; fi
  else
    fail "lock gate"
    NOT_RUN="$*"
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
    mapfile -t sh < <(git --no-pager -c core.fsmonitor=false -c core.hooksPath=/dev/null \
      ls-files '*.sh' 2>/dev/null)
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
  # docker compose and the caller's tools; e2e runs nothing from a lockfile
  export PATH="$CALLER_PATH"
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
  gate)  steps=() ;;
  lint)  steps=(lint) ;;
  unit)  steps=(unit) ;;
  quick) steps=(lint unit locks) ;;
  locks) steps=(locks) ;;
  e2e)   steps=(e2e) ;;
  all)   steps=(lint unit locks e2e) ;;
  *) echo "usage: run.sh [gate|lint|unit|quick|locks|e2e|all]" >&2; exit 2 ;;
esac
[ "$mode" = e2e ] || gate "${steps[@]}"
for step in "${steps[@]}"; do "$step"; done

summary
