#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Regression test for the gates in front of the shipped venv and in front of every CI step
# that installs from a lockfile (linuxmusterDEV work/verification/cold-stage-a.md F2,
# work/tasks/nachbesserung-kalte-pruefung-umbau.md K1 and "Runde 3" R1, R2, R6).
# Up to 7.3.3 an indented
#   zzzevil @ file:///...whl#sha256=...
# line in controlplane/requirements.lock passed scripts/check-lockfiles.sh (it read only
# lines starting in column 1, pip reads indented ones too), and the freeze == lock check of
# the build crashed inside a process substitution that `set -e` never saw: `make deb`
# shipped the foreign package. Now every tampered lockfile of the matrix below must be
# refused, for the expected reason, by BOTH
#   check  scripts/check-lockfiles.sh (the lock gate: CI fast tier and `lockfile` jobs), and
#   build  packaging/build-venv.sh, which debian/rules runs: its failure fails `make deb`.
# Plus cases for each gate of scripts/lockfile_gate.py on its own (lint, pypi, freeze,
# closure), including the variants pip reads that the old check did not, and:
#   K1  a published-looking wheel (scripts/tests/k1_wheel.py: a marker-writing .pth and
#       marker-writing bin/ tools) pinned in the build lock, the runtime lock, both, or the uv
#       lock is refused before any of its code runs (no marker);
#   R6  that wheel is sharp: installed without a gate, its bin/ scripts are executable and
#       write the marker, and so does its .pth;
#   R1  the gates are not steered by these parts of the caller's environment: an activated
#       venv (bin/ first on PATH, VIRTUAL_ENV), a .venv in the checkout, PYTHONPATH/PYTHONHOME,
#       UV_*/PIP_* variables and config files that redirect the index or the interpreter -- no
#       marker, same verdicts;
#   R2  the lock-related steps of ci.yml and release.yml (extracted word for word by
#       scripts/tests/ci_step.py) stop at the gate with a planted package, before anything is
#       installed; the counter-probe without the gate step installs it and the marker appears.
# The counter-probes (R6, R2) run install paths WITHOUT the gate, so they install fixture
# locks that pin only the test wheel, never the checkout's lockfiles; and nothing after the
# first cases runs unless the committed lockfiles pass the gate (cold verification r3, F1).
# Needs /usr/bin/python3 >= 3.12 with venv/ensurepip (python3-venv) and PyPI; no uv (the gate
# installs its own). It never skips: a missing tool fails. LOCK_GATES_VERBOSE=1 prints every
# refusal's reason. LOCK_GATES_DEB=1 also runs the whole `make deb` (dpkg-buildpackage) on each
# tampered tree and requires it to fail without a .deb and without a marker; run that inside
# the build image, as a user who may write the checkout, after `apt-get build-dep .` (the
# command is in the Makefile). The harness itself runs in a clean environment too.
set -uo pipefail

export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset VIRTUAL_ENV CONDA_PREFIX
for v in $(compgen -e); do
    case "$v" in PYTHON* | UV_* | PIP_* | GIT_*) unset "$v" ;; esac
done
export PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
GATE="$ROOT/scripts/lockfile_gate.py"
LOCK="$ROOT/controlplane/requirements.lock"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS=0
FAIL=0
H64="$(printf 'e%.0s' $(seq 64))"
EVIL_URL="file:///tmp/evil/zzzevil-1.0-py3-none-any.whl#sha256=$H64"

/usr/bin/python3 -I -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 12))' \
    || { echo "lock_gates.sh: /usr/bin/python3 >= 3.12 with venv/ensurepip is required"; exit 1; }

# expect <ok|fail> <label> <reason (ERE, fail only)> <command...>
# A refusal counts only with the expected reason: a tampered case that fails for some other
# cause (no network, a typo in the case) would otherwise pass without testing anything.
expect() {
    local want="$1" label="$2" why="$3" rc
    shift 3
    "$@" > "$TMP/out" 2>&1
    rc=$?
    if [ "$want" = ok ] && [ "$rc" = 0 ]; then
        echo "ok    $label"
        PASS=$((PASS + 1))
    elif [ "$want" = fail ] && [ "$rc" != 0 ] && grep -qE -- "$why" "$TMP/out"; then
        echo "ok    $label (refused, exit $rc)"
        PASS=$((PASS + 1))
        if [ -n "${LOCK_GATES_VERBOSE:-}" ]; then
            grep -E -- "$why" "$TMP/out" | head -2 | cut -c1-200 | sed 's/^/        /'
        fi
    else
        echo "WRONG $label (expected $want${why:+ with /$why/}, exit $rc)"
        tail -20 "$TMP/out" | sed 's/^/      /'
        FAIL=$((FAIL + 1))
    fi
}

# edit <file> <python statement on the list `lines`>: change a lockfile, and make sure the
# edit really changed it (a case whose edit matched nothing would test the clean file).
edit() {
    /usr/bin/python3 -I - "$1" "$2" <<'PY' || { echo "lock_gates.sh: case edit failed: $2"; exit 1; }
import sys
path, stmt = sys.argv[1:3]
before = open(path).read()
lines = before.split("\n")
exec(stmt)
after = "\n".join(lines)
if after == before:
    sys.exit(f"the edit changed nothing: {stmt}")
open(path, "w").write(after)
PY
}

# ---------------------------------------------------------------- the committed files
expect ok "lint: the committed lockfiles" "" bash "$ROOT/scripts/check-lockfiles.sh" --lint
expect ok "pypi: every committed hash is published" "" /usr/bin/python3 -I -B "$GATE" pypi \
    "$ROOT/controlplane/build-requirements.lock" "$LOCK" "$ROOT/controlplane/uv-requirements.lock"
# The full gate on the COMMITTED files, not only their grammar: an extra real pin that an
# author (or an attacker with commit access) left in a lock is caught here in the fast tier
# (cold verification of the debian umbau, finding 4). Needs PyPI. It is a precondition, not
# just a case: every later case works on copies of these files, so if they fail the gate,
# nothing else runs (cold verification r3, F1). The counter-probes below never install these
# files at all -- they use fixture locks that pin only the test's own wheel.
expect ok "gate: the committed lockfiles pass scripts/check-lockfiles.sh" "" \
    bash "$ROOT/scripts/check-lockfiles.sh"
if [ "$FAIL" != 0 ]; then
    echo "lock_gates.sh: the committed lockfiles fail the lock gate; stopping before any case" \
        "that copies them (fix the lockfiles first)"
    echo
    echo "lock gates: $PASS ok, $FAIL wrong"
    exit 1
fi

# ---------------------------------------------------------------- the F2 matrix
# Each case edits controlplane/requirements.lock in a copy of the files both scripts read.
# The first pin and its hashes are found at run time, so the cases follow lockfile bumps.
PY_FIRST_PIN='i = next(n for n, l in enumerate(lines) if l[:1].isalnum())'
declare -A EDIT CHECK_WHY BUILD_WHY
EDIT[indented-url]="lines.insert(-1, '    zzzevil @ $EVIL_URL')"
CHECK_WHY[indented-url]="not an empty line, a comment"
BUILD_WHY[indented-url]="not an empty line, a comment"
EDIT[extra-index-url]="lines.insert(-1, '--extra-index-url https://evil.example/simple')"
CHECK_WHY[extra-index-url]="not an empty line, a comment"
BUILD_WHY[extra-index-url]="not an empty line, a comment"
# every --hash line of the first pin removed; its trailing backslash stays, as in the
# cold verification (pip then reads the pin without any hash)
EDIT[hashes-removed]="$PY_FIRST_PIN
j = i + 1
while lines[j].startswith('    --hash='): del lines[j]"
CHECK_WHY[hashes-removed]="expected a '    --hash=sha256"
BUILD_WHY[hashes-removed]="expected a '    --hash=sha256"
# the LAST hash of the first pin, one hex digit changed. For annotated-doc 0.0.5 that is the
# sdist, a file pip never downloads with --only-binary, so pip alone would not notice.
EDIT[hash-changed]="$PY_FIRST_PIN
j = i + 1
while lines[j].endswith(' \\\\'): j += 1
lines[j] = lines[j][:-1] + ('0' if lines[j][-1] != '0' else '1')"
CHECK_WHY[hash-changed]="hashes PyPI does not publish"
BUILD_WHY[hash-changed]="is not a file PyPI publishes"
# a well-formed pin with PyPI's genuine hashes that nothing requires (six 1.16.0)
EDIT[extra-pin]="lines[-1:-1] = ['six==1.16.0 \\\\',
    '    --hash=sha256:1e61c37477a1626458e36f7b1d82aa5c9b094fa4802892072e49de9c60c4c926 \\\\',
    '    --hash=sha256:8abb2f1d86890a2dfb989f9a77cfcfd3e47c2a354b01111771326f8aa26e0254']"
CHECK_WHY[extra-pin]="pins differ from a fresh"
# build-venv.sh runs the set-closure BEFORE it populates the venv (K1), so an extra pin is
# rejected there, earlier than the post-install closure gate.
BUILD_WHY[extra-pin]="pins differ from a fresh|not the closure of its declared inputs"
EDIT[pin-without-hash]="lines.insert(-1, 'six==1.16.0')"
CHECK_WHY[pin-without-hash]="not an empty line, a comment"
BUILD_WHY[pin-without-hash]="not an empty line, a comment"

# copy_tree <dir>: the checkout without .git and without local venvs (a fresh test tree).
copy_tree() {
    mkdir -p "$1"
    tar -C "$ROOT" --exclude=./.git --exclude=./.venv --exclude=./venv -cf - . | tar -C "$1" -xf -
}

for c in indented-url extra-index-url hashes-removed hash-changed extra-pin pin-without-hash; do
    dir="$TMP/$c"
    mkdir -p "$dir/debian"
    cp -r "$ROOT/controlplane" "$ROOT/packaging" "$ROOT/scripts" "$dir/"
    cp "$ROOT/debian/changelog" "$dir/debian/"
    edit "$dir/controlplane/requirements.lock" "${EDIT[$c]}"
    expect fail "check: $c" "${CHECK_WHY[$c]}" bash "$dir/scripts/check-lockfiles.sh"
    expect fail "build: $c" "${BUILD_WHY[$c]}" bash "$dir/packaging/build-venv.sh" "$dir/venv"
    if [ -n "${LOCK_GATES_DEB:-}" ]; then
        copy_tree "$TMP/deb-$c/src"
        cp "$dir/controlplane/requirements.lock" "$TMP/deb-$c/src/controlplane/"
        expect fail "make deb: $c" "${BUILD_WHY[$c]}" make -C "$TMP/deb-$c/src" deb
        if compgen -G "$TMP/deb-$c/*.deb" > /dev/null; then
            echo "WRONG make deb: $c left a .deb behind"
            FAIL=$((FAIL + 1))
        fi
    fi
done

# ------------------------------------------------ K1: a lock wheel must not run before the gate
# The attack of the cold verifications (work/verification/cold-debian-squid.md F1,
# nachbesserung-kalte-pruefung-umbau.md K1): scripts/tests/k1_wheel.py builds a wheel whose
# distribution ships executable bin/ scripts (diff, comm, ... python, pip, uv: every tool a gate
# or a venv entry point could run) and a `.pth` that runs in every interpreter of the venv it
# is installed into; all of them append to $MARK. Pinned with a REAL name and hash it passes
# grammar and the PyPI-hash check; only the set==closure check (the "only uv" check for the uv
# lock) can stop it, and it must stop it BEFORE anything from a lock is installed.
# Its "publication on PyPI" is simulated only inside the throwaway copy: the copy's
# lockfile_gate.published() knows its hash, and the copy's pip calls get --find-links to the
# wheel, so pip WOULD install it if a gate let it through. No environment variable is read by
# the real scripts for this (they drop every PIP_*/UV_* variable), so real builds are not
# weakened: the repo's own files are untouched.
MARK="$TMP/k1-marker"
WHEELDIR="$TMP/k1-wheel"
mkdir -p "$WHEELDIR"
WHEEL="$(/usr/bin/python3 -I "$ROOT/scripts/tests/k1_wheel.py" "$WHEELDIR" "$MARK")"
K1_HASH="$(sha256sum "$WHEEL" | cut -d' ' -f1)"

# no_marker <label>: nothing of the wheel ran (and reset the marker for the next case).
no_marker() {
    if [ -e "$MARK" ]; then
        echo "WRONG $1: code of the planted package ran ($(wc -l < "$MARK") marker lines):"
        head -5 "$MARK" | sed 's/^/      /'
        FAIL=$((FAIL + 1))
    else
        echo "ok    $1: no code of the planted package ran"
        PASS=$((PASS + 1))
    fi
    rm -f "$MARK"
}
# marker_from_bin <label>: the counter-probe -- the wheel's code DID run, from a bin/ script.
marker_from_bin() {
    if [ -e "$MARK" ] && grep -q '^bin/' "$MARK"; then
        echo "ok    $1: marker written ($(grep -c '^bin/' "$MARK") from bin/ scripts," \
            "$(grep -c '^pth ran' "$MARK") from the .pth)"
        PASS=$((PASS + 1))
    else
        echo "WRONG $1: expected a marker line from a bin/ script, got:"
        sed 's/^/      /' "$MARK" 2> /dev/null | head -5
        FAIL=$((FAIL + 1))
    fi
    rm -f "$MARK"
}

# plant <dir> <build|runtime|both|uv>: pin zzzk1 in the copy's lock(s) and simulate its
# publication in the copy (published(), --find-links for the copy's pip calls).
plant() {
    local dir="$1" where="$2" cp="$1/controlplane"
    block() { printf 'zzzk1==1.0 \\\n    --hash=sha256:%s\n    # via %s\n' "$K1_HASH" "$1"; }
    case "$where" in
        build) block "-r build-requirements.in" >> "$cp/build-requirements.lock" ;;
        runtime) block "lmnradius (pyproject.toml)" >> "$cp/requirements.lock" ;;
        both)
            block "-r build-requirements.in" >> "$cp/build-requirements.lock"
            block "lmnradius (pyproject.toml)" >> "$cp/requirements.lock"
            ;;
        uv) block "-r uv-requirements.in" >> "$cp/uv-requirements.lock" ;;
    esac
    /usr/bin/python3 -I - "$dir" "$K1_HASH" "$WHEELDIR" <<'PY'
import sys
d, h, wheels = sys.argv[1:4]
p = d + "/scripts/lockfile_gate.py"
s = open(p).read()
i = s.index('if __name__ == "__main__":')
s = s[:i] + ("_real_pub = published\n\n\ndef published(pin):\n"
             "    return {%r} if pin.name == 'zzzk1' else _real_pub(pin)\n\n\n" % h) + s[i:]
open(p, "w").write(s)
for p, old in ((d + "/packaging/build-venv.sh", "--only-binary :all: --no-deps)"),
               (d + "/scripts/check-lockfiles.sh", "--only-binary :all: --no-deps \\")):
    s = open(p).read()
    assert s.count(old) == 1, (p, old)
    open(p, "w").write(s.replace(old, old.replace("--no-deps", "--no-deps --find-links " + wheels)))
PY
}

# fixture_locks <copy>: the counter-probes run a path WITHOUT the gate, so they must never
# install the checkout's own lockfiles (a crafted lock would run there; cold verification r3,
# F1). Both locks of the copy become fixtures that pin only the test's wheel zzzk1.
fixture_locks() {
    local f
    for f in requirements.lock build-requirements.lock; do
        printf '# fixture of scripts/tests/lock_gates.sh: only the test wheel\nzzzk1==1.0 \\\n    --hash=sha256:%s\n' \
            "$K1_HASH" > "$1/controlplane/$f"
    done
}

# k1_case <label> <build|runtime|both|uv>: the gate rejects the planted pin, for the expected
# reason, and nothing of the wheel runs; under LOCK_GATES_DEB also through a whole `make deb`.
k1_case() {
    local label="$1" where="$2" dir="$TMP/k1-$1" why
    copy_tree "$dir"
    plant "$dir" "$where"
    why="pins differ from a fresh"
    [ "$where" = uv ] && why="is not exactly the published uv of uv-requirements.in"
    rm -f "$MARK"
    expect fail "K1 check: $label" "$why" bash "$dir/scripts/check-lockfiles.sh"
    no_marker "K1 check: $label"
    expect fail "K1 build-venv: $label" "$why" bash "$dir/packaging/build-venv.sh" "$dir/v"
    no_marker "K1 build-venv: $label"
    if [ -n "${LOCK_GATES_DEB:-}" ]; then
        expect fail "K1 make deb: $label" "$why" make -C "$dir" deb
        if compgen -G "$TMP/"'*.deb' > /dev/null; then
            echo "WRONG K1 make deb: $label left a .deb behind"; FAIL=$((FAIL + 1))
            rm -f "$TMP/"*.deb
        fi
        no_marker "K1 make deb: $label"
    fi
}
k1_case build-lock-only build
k1_case runtime-lock-only runtime
k1_case both-locks both
k1_case uv-lock uv

# ------------------------------------------------ R6: the wheel is sharp
# Installed without any gate, every bin/ script is an executable file and writes the marker,
# and the .pth runs in the venv's interpreter: both halves of the K1 cases can fire.
R6="$TMP/r6-venv"
/usr/bin/python3 -I -m venv "$R6"
"$R6/bin/python" -I -m pip install -q --no-index --no-deps "$WHEEL"
rm -f "$MARK"
notx=""
for t in $(/usr/bin/python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import k1_wheel; print(*k1_wheel.TOOLS)' "$ROOT/scripts/tests"); do
    { [ -f "$R6/bin/$t" ] && [ ! -L "$R6/bin/$t" ] && [ -x "$R6/bin/$t" ]; } || notx="$notx $t"
done
if [ -z "$notx" ]; then
    echo "ok    R6: every bin/ script of the wheel is installed as an executable file"
    PASS=$((PASS + 1))
else
    echo "WRONG R6: not installed as executable files:$notx"; FAIL=$((FAIL + 1))
fi
"$R6/bin/diff" a b
if grep -qx 'bin/diff ran: a b' "$MARK" 2> /dev/null; then
    echo "ok    R6: an installed bin/ script writes the marker"; PASS=$((PASS + 1))
else
    echo "WRONG R6: bin/diff did not write the marker"; FAIL=$((FAIL + 1))
fi
rm -f "$MARK"
# The wheel replaced bin/python3 (and python/python3.X link to it on Debian/Ubuntu), so start
# the venv's interpreter through a fresh link: sys.prefix is the venv, site runs the .pth.
ln -s /usr/bin/python3 "$R6/bin/real-python"
"$R6/bin/real-python" -c pass
if grep -q '^pth ran' "$MARK" 2> /dev/null; then
    echo "ok    R6: the .pth writes the marker in the venv's interpreter"; PASS=$((PASS + 1))
else
    echo "WRONG R6: the .pth did not write the marker"; FAIL=$((FAIL + 1))
fi
rm -f "$MARK"
# Counter-probe through the real build path: build-venv.sh of a planted copy with only the
# gate call removed installs the build lock -- a fixture holding only zzzk1, never the
# checkout's lock -- and zzzk1's bin/pip then runs in place of pip: the marker appears from a
# bin/ script. (With the gate, above: no marker.)
dir="$TMP/r6-nogate"
copy_tree "$dir"
plant "$dir" build
fixture_locks "$dir"
/usr/bin/python3 -I - "$dir/packaging/build-venv.sh" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
old = 'bash "$ROOT/scripts/check-lockfiles.sh"'
assert s.count(old) == 1
open(p, "w").write(s.replace(old, "true"))
PY
rm -f "$MARK"
bash "$dir/packaging/build-venv.sh" "$dir/v" > "$TMP/out" 2>&1
marker_from_bin "R6 counter-probe: build-venv.sh without the gate"

# ------------------------------------------------ R1: these parts of the caller's environment
# (BASH_ENV, exported shell functions and proxy/CA variables are the documented limit, not
# tested here: whoever sets them already runs code as the caller)
# An activated venv P with the wheel installed: its bin/ (diff, sort, awk, grep, python3, uv,
# pip, bash, ...) first on PATH, VIRTUAL_ENV=P, UV_PYTHON pointing into it; PYTHONPATH with a
# sitecustomize that writes the marker; PYTHONHOME that breaks every non-isolated Python;
# uv and pip variables and config files that send both to a dead index; git config via the
# environment. And a .venv in the checkout copy, poisoned the same way, with a `ruff` so that
# run.sh would put it on PATH. None of it may run or redirect anything: the verdicts stay the
# same and no marker appears.
P="$TMP/r1-activated"
/usr/bin/python3 -I -m venv "$P"
"$P/bin/python" -I -m pip install -q --no-index --no-deps "$WHEEL"
mkdir -p "$TMP/r1-pythonpath"
printf "import os\nopen(%s, 'a').write('sitecustomize ran in pid %%d\\\\n' %% os.getpid())\n" \
    "'$MARK'" > "$TMP/r1-pythonpath/sitecustomize.py"
printf '[global]\nindex-url = http://127.0.0.1:9/simple\nfind-links = %s\n' "$WHEELDIR" \
    > "$TMP/r1-pip.conf"
printf 'index-url = "http://127.0.0.1:9/simple"\n' > "$TMP/r1-uv.toml"
printf '#!/bin/sh\necho "fsmonitor ran" >> %s\n' "$MARK" > "$TMP/r1-fsmonitor"
chmod +x "$TMP/r1-fsmonitor"
POISON=(env "PATH=$P/bin:/usr/sbin:/usr/bin:/sbin:/bin" "VIRTUAL_ENV=$P"
    "PYTHONPATH=$TMP/r1-pythonpath" "PYTHONHOME=$P" "UV_PYTHON=$P/bin/python3"
    "UV_INDEX_URL=http://127.0.0.1:9/simple" "UV_DEFAULT_INDEX=http://127.0.0.1:9/simple"
    "UV_FIND_LINKS=$WHEELDIR" "UV_CONFIG_FILE=$TMP/r1-uv.toml"
    "PIP_INDEX_URL=http://127.0.0.1:9/simple" "PIP_FIND_LINKS=$WHEELDIR"
    "PIP_CONFIG_FILE=$TMP/r1-pip.conf" "GIT_CONFIG_PARAMETERS='core.fsmonitor'='$TMP/r1-fsmonitor'")
poison_dotvenv() {  # <checkout copy>: a poisoned .venv with a ruff, so run.sh would use it
    /usr/bin/python3 -I -m venv "$1/.venv"
    "$1/.venv/bin/python" -I -m pip install -q --no-index --no-deps "$WHEEL"
    printf '#!/bin/sh\necho "bin/ruff ran: $*" >> %s\n' "$MARK" > "$1/.venv/bin/ruff"
    chmod +x "$1/.venv/bin/ruff"
}
# the clean locks pass, untouched by the environment
dir="$TMP/r1-clean"
copy_tree "$dir"
poison_dotvenv "$dir"
rm -f "$MARK"
expect ok "R1 check (poisoned env, clean locks)" "" "${POISON[@]}" /bin/bash "$dir/scripts/check-lockfiles.sh"
no_marker "R1 check (poisoned env, clean locks)"
expect ok "R1 run.sh gate (poisoned env + .venv, clean locks)" "" \
    "${POISON[@]}" /bin/bash "$dir/scripts/tests/run.sh" gate
no_marker "R1 run.sh gate (poisoned env + .venv, clean locks)"
# a planted runtime pin is still refused, for the same reason, and nothing of it runs
dir="$TMP/r1-planted"
copy_tree "$dir"
plant "$dir" runtime
poison_dotvenv "$dir"
rm -f "$MARK"
expect fail "R1 check (poisoned env, K1 pin)" "pins differ from a fresh" \
    "${POISON[@]}" /bin/bash "$dir/scripts/check-lockfiles.sh"
no_marker "R1 check (poisoned env, K1 pin)"
expect fail "R1 build-venv (poisoned env, K1 pin)" "pins differ from a fresh" \
    "${POISON[@]}" /bin/bash "$dir/packaging/build-venv.sh" "$dir/v"
no_marker "R1 build-venv (poisoned env, K1 pin)"
# run.sh lint: the gate runs first and fails, so neither .venv's ruff nor anything else runs
expect fail "R1 run.sh lint (poisoned env + .venv, K1 pin)" "\[FAIL\] lock gate" \
    "${POISON[@]}" /bin/bash "$dir/scripts/tests/run.sh" lint
no_marker "R1 run.sh lint (poisoned env + .venv, K1 pin)"
if [ -n "${LOCK_GATES_DEB:-}" ]; then
    expect fail "R1 make deb (poisoned env + .venv, K1 pin)" "pins differ from a fresh" \
        "${POISON[@]}" make -C "$dir" deb
    if compgen -G "$TMP/"'*.deb' > /dev/null; then
        echo "WRONG R1 make deb: left a .deb behind"; FAIL=$((FAIL + 1)); rm -f "$TMP/"*.deb
    fi
    no_marker "R1 make deb (poisoned env + .venv, K1 pin)"
fi

# ------------------------------------------------ R2: CI verifies before it installs
# The lock-related steps of ci.yml and release.yml, extracted word for word, replayed in a
# planted copy the way Actions runs them: each step its own `bash -eo pipefail`, the job stops
# at the first failing step. T stands in for the setup-python interpreter (a clean venv, its
# bin/ first on PATH); pip there would find the planted wheel (PIP_FIND_LINKS, as if it were
# on PyPI). Expected: the gate step fails with the reason, no later step runs, nothing is
# installed into T, no marker.
# replay <label> <why> <copy> <workflow> <job> <step>...
replay() {
    local label="$1" why="$2" dir="$3" wf="$4" job="$5" step n=0 rc=0 T="$TMP/r2-tool"
    shift 5
    rm -rf "$T"
    /usr/bin/python3 -I -m venv "$T"
    : > "$TMP/r2-out"
    for step in "$@"; do
        n=$((n + 1))
        /usr/bin/python3 -I "$ROOT/scripts/tests/ci_step.py" "$ROOT/.github/workflows/$wf" \
            "$job" "$step" > "$TMP/r2-step.sh" || { rc=99; break; }
        (cd "$dir" && env "PATH=$T/bin:/usr/sbin:/usr/bin:/sbin:/bin" "PIP_FIND_LINKS=$WHEELDIR" \
            /bin/bash --noprofile --norc -eo pipefail "$TMP/r2-step.sh") >> "$TMP/r2-out" 2>&1
        rc=$?
        [ "$rc" = 0 ] || break
    done
    if [ "$rc" != 0 ] && [ "$n" = 1 ] && grep -qE -- "$why" "$TMP/r2-out" \
        && ! compgen -G "$T/lib/python*/site-packages/zzzk1*" > /dev/null; then
        echo "ok    R2 $label: stopped at step 1 ($1), nothing installed (exit $rc)"
        PASS=$((PASS + 1))
    else
        echo "WRONG R2 $label: expected step 1 to fail with /$why/ (failed step $n, exit $rc)"
        tail -15 "$TMP/r2-out" | sed 's/^/      /'
        FAIL=$((FAIL + 1))
    fi
    no_marker "R2 $label"
}
FAST_GATE="Lock gate (every lockfile, before anything from one is installed)"
FAST_INSTALL="Install tools + control plane (locked dependencies)"
LOCKFILE_STEP="Lockfiles match their sources, PyPI and the target platform"
for where in build runtime both uv; do
    dir="$TMP/r2-$where"
    copy_tree "$dir"
    plant "$dir" "$where"
    why="pins differ from a fresh"
    [ "$where" = uv ] && why="is not exactly the published uv of uv-requirements.in"
    rm -f "$MARK"
    replay "ci.yml fast, $where lock" "$why" "$dir" ci.yml fast "$FAST_GATE" "$FAST_INSTALL"
    replay "ci.yml lockfile, $where lock" "$why" "$dir" ci.yml lockfile "$LOCKFILE_STEP"
    replay "release.yml lockfile, $where lock" "$why" "$dir" release.yml lockfile "$LOCKFILE_STEP"
done
# Counter-probe: the install step's lockfile lines WITHOUT the gate step before them install
# the runtime lock into T -- a fixture holding only zzzk1, never the checkout's lock -- and
# zzzk1's bin/pip then runs in place of pip: marker from bin/.
dir="$TMP/r2-counter"
copy_tree "$dir"
plant "$dir" runtime
fixture_locks "$dir"
T="$TMP/r2-tool"
rm -rf "$T"
/usr/bin/python3 -I -m venv "$T"
/usr/bin/python3 -I "$ROOT/scripts/tests/ci_step.py" "$ROOT/.github/workflows/ci.yml" fast \
    "$FAST_INSTALL" | grep -E 'controlplane|pip check' > "$TMP/r2-step.sh"
rm -f "$MARK"
(cd "$dir" && env "PATH=$T/bin:/usr/sbin:/usr/bin:/sbin:/bin" "PIP_FIND_LINKS=$WHEELDIR" \
    /bin/bash --noprofile --norc -eo pipefail "$TMP/r2-step.sh") > "$TMP/out" 2>&1
marker_from_bin "R2 counter-probe: ci.yml fast install lines without the gate step"

# ---------------------------------------------------------------- lint, line by line
# Everything pip would read differently from a plain uv pin. Appended to a copy of the lock.
lint_case() {  # <label> <reason> <printf format> [args]: the bytes to append
    local label="$1" why="$2"
    shift 2
    cp "$LOCK" "$TMP/case.lock"
    # shellcheck disable=SC2059  # the format carries the escapes of the case
    printf "$@" >> "$TMP/case.lock"
    expect fail "lint: $label" "$why" python3 -I -B "$GATE" lint "$TMP/case.lock"
}
NOT="not an empty line, a comment"
lint_case "URL requirement in column 1" "$NOT" 'zzzevil @ %s\n' "$EVIL_URL"
lint_case "URL requirement after a tab" "0x09" '\tzzzevil @ %s\n' "$EVIL_URL"
lint_case "--index-url" "$NOT" -- '--index-url https://evil.example/simple\n'
lint_case "-i" "$NOT" -- '-i https://evil.example/simple\n'
lint_case "-f" "$NOT" -- '-f /tmp/evil\n'
lint_case "--find-links" "$NOT" -- '--find-links=/tmp/evil\n'
lint_case "-e" "$NOT" -- '-e /tmp/evil\n'
lint_case "-r" "$NOT" -- '-r /tmp/evil.txt\n'
lint_case "-c" "$NOT" -- '-c /tmp/evil.txt\n'
lint_case "--trusted-host" "$NOT" -- '--trusted-host evil.example\n'
lint_case "indented option" "$NOT" -- '    --extra-index-url https://evil.example/simple\n'
lint_case "environment variable" "$NOT" '${EVIL_REQUIREMENT}\n'
lint_case "requirement hidden behind CR in a comment" "0x0d" '# note\rzzzevil @ %s\n' "$EVIL_URL"
lint_case "requirement hidden behind a form feed" "0x0c" '# note\fzzzevil @ %s\n' "$EVIL_URL"
lint_case "requirement hidden behind U+2028" "0xe2" '# note\xe2\x80\xa8zzzevil @ %s\n' "$EVIL_URL"
lint_case "NUL byte" "0x00" '# note\x00\n'
lint_case "uppercase name" "$NOT" 'Evil==1.0 \\\n    --hash=sha256:%s\n' "$H64"
lint_case "environment marker" "$NOT" 'evil==1.0 ; python_version >= "3" \\\n    --hash=sha256:%s\n' "$H64"
lint_case "hash on the pin's own line" "$NOT" 'evil==1.0 --hash=sha256:%s\n' "$H64"
lint_case "pin ending in a backslash at the end of the file" "ends inside the pin" 'evil==1.0 \\\n'
lint_case "md5 hash" "expected a '    --hash=sha256" 'evil==1.0 \\\n    --hash=md5:0123456789abcdef0123456789abcdef\n'
lint_case "sha256 with 63 hex digits" "expected a '    --hash=sha256" 'evil==1.0 \\\n    --hash=sha256:%s\n' "${H64:1}"
lint_case "comment inside a pin" "expected a '    --hash=sha256" 'evil==1.0 \\\n    # via nothing\n'
lint_case "trailing space after the hash" "expected a '    --hash=sha256" 'evil==1.0 \\\n    --hash=sha256:%s \n' "$H64"
lint_case "orphan hash line" "$NOT" '    --hash=sha256:%s\n' "$H64"
lint_case "second pin of a pinned package" "pinned twice" 'anyio==1.0 \\\n    --hash=sha256:%s\n' "$H64"
cp "$LOCK" "$TMP/case.lock"
printf '\n# a note\n    # an indented note\n\n' >> "$TMP/case.lock"
expect ok "lint: empty lines and comments stay allowed" "" python3 -I -B "$GATE" lint "$TMP/case.lock"

# ---------------------------------------------------------------- pypi, per hash
cp "$LOCK" "$TMP/case.lock"
edit "$TMP/case.lock" "$PY_FIRST_PIN
lines[i + 1] = lines[i + 1][:-3] + ('0' if lines[i + 1][-3] != '0' else '1') + lines[i + 1][-2:]"
expect fail "pypi: first hash of the first pin changed" "is not a file PyPI publishes" \
    python3 -I -B "$GATE" pypi "$TMP/case.lock"
cp "$LOCK" "$TMP/case.lock"
edit "$TMP/case.lock" "$PY_FIRST_PIN
lines[i] = lines[i].split('==')[0] + '==0.0.0.999999 \\\\'"
expect fail "pypi: a version PyPI does not have" "cannot read PyPI" \
    python3 -I -B "$GATE" pypi "$TMP/case.lock"

# ---------------------------------------------------------------- freeze
# `pip freeze --all` of a correct build: the pins with pip's spelling of some names, pip,
# the control plane, and no setuptools (build-venv.sh uninstalls it).
python3 -I -B - "$ROOT/controlplane/build-requirements.lock" "$LOCK" > "$TMP/freeze.good" <<'PY'
import sys
spell = {"pyyaml": "PyYAML", "typing-extensions": "typing_extensions",
         "pydantic-core": "pydantic_core", "markdown-it-py": "markdown_it_py"}
for path in sys.argv[1:]:
    for line in open(path):
        if line[:1].isalnum():
            name, version = line.split()[0].split("==")
            if name != "setuptools":
                print(f"{spell.get(name, name)}=={version}")
print("lmnradius==7.3.99")
PY
freeze_case() {  # <ok|fail> <label> <reason> <file>
    expect "$1" "freeze: $2" "$3" python3 -I -B "$GATE" freeze --own lmnradius \
        --drop setuptools "$4" "$ROOT/controlplane/build-requirements.lock" "$LOCK"
}
freeze_case ok "the locks plus lmnradius, pip's spelling of the names" "" "$TMP/freeze.good"
{ cat "$TMP/freeze.good"; echo "zzzevil @ $EVIL_URL"; } > "$TMP/f"
freeze_case fail "a URL install next to the locks" "not a plain name==version pin" "$TMP/f"
{ cat "$TMP/freeze.good"; echo "-e git+https://evil.example/x.git@0#egg=x"; } > "$TMP/f"
freeze_case fail "an editable install" "not a plain name==version pin" "$TMP/f"
{ cat "$TMP/freeze.good"; echo "six==1.16.0"; } > "$TMP/f"
freeze_case fail "a plain pin that no lockfile has" "in the venv, not in the lockfiles: six" "$TMP/f"
{ cat "$TMP/freeze.good"; echo "setuptools==84.0.0"; } > "$TMP/f"
freeze_case fail "setuptools left in the venv" "in the venv, not in the lockfiles: setuptools" "$TMP/f"
grep -v '^anyio==' "$TMP/freeze.good" > "$TMP/f"
freeze_case fail "a locked pin missing from the venv" "in the lockfiles, not in the venv: anyio" "$TMP/f"
sed 's/^anyio==.*/anyio==0.0.1/' "$TMP/freeze.good" > "$TMP/f"
freeze_case fail "another version in the venv" "anyio: the venv has 0.0.1" "$TMP/f"
grep -v '^lmnradius==' "$TMP/freeze.good" > "$TMP/f"
freeze_case fail "the control plane missing" "lmnradius is not installed" "$TMP/f"
{ cat "$TMP/freeze.good"; printf 'six==1.16.0\rzzzevil==1.0\n'; } > "$TMP/f"
freeze_case fail "a CR inside a line" "0x0d" "$TMP/f"

# ---------------------------------------------------------------- closure
# A fake site-packages: lmnradius needs alpha and beta[fast]; beta's extra "fast" needs
# gamma, its extra "slow" needs delta (not requested).
dist() {  # <dir> <name> <version> [Requires-Dist...]
    local d="$1/$2-$3.dist-info" r
    mkdir -p "$d"
    printf 'Metadata-Version: 2.1\nName: %s\nVersion: %s\n' "$2" "$3" > "$d/METADATA"
    shift 3
    for r in "$@"; do printf 'Requires-Dist: %s\n' "$r" >> "$d/METADATA"; done
}
SP="$TMP/site"
dist "$SP" lmnradius 7.3.99 alpha 'beta[fast]>=1'
dist "$SP" alpha 1.0
dist "$SP" beta 1.0 'gamma; extra == "fast"' 'delta; extra == "slow"'
dist "$SP" gamma 1.0
dist "$SP" pip 26.0
# closure runs in a venv's interpreter (it imports pip's vendored packaging), as in the build.
/usr/bin/python3 -I -m venv "$TMP/closure-venv"
closure() {
    "$TMP/closure-venv/bin/python" -I -B "$GATE" closure --path "$SP" --root lmnradius --root pip
}
expect ok "closure: everything installed is required" "" closure
dist "$SP" six 1.16.0
expect fail "closure: an extra distribution" "six==1\.16\.0 is installed but required by nothing" closure
rm -r "$SP/six-1.16.0.dist-info"
dist "$SP" delta 1.0
expect fail "closure: needed only by an extra nobody asked for" "delta==1\.0 is installed but required by nothing" closure
rm -r "$SP/delta-1.0.dist-info" "$SP/gamma-1.0.dist-info"
expect fail "closure: a required distribution missing" "gamma is required but not installed" closure

echo
echo "lock gates: $PASS ok, $FAIL wrong"
[ "$FAIL" = 0 ]
