#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# THE lock gate. It proves that the hash-pinned lockfiles under controlplane/ are exactly what
# they claim to be, and it is the first thing every consumer runs, before anything from a
# lockfile is installed: the CI fast tier, the CI and release `lockfile` jobs,
# packaging/build-venv.sh (so `make deb`, the CI `package` job and the release build) and
# scripts/tests/run.sh (linuxmusterDEV work/tasks/nachbesserung-kalte-pruefung-umbau.md, K1,
# R1, R2). packaging/build-venv.sh installs the locks with --no-deps, so nothing else would
# notice a dependency added to pyproject.toml without re-locking.
#   0. Every line of all three lockfiles (requirements, build-requirements, uv-requirements)
#      is one pip reads exactly the way uv wrote it: empty, a comment, a `name==version \` pin
#      or a `    --hash=sha256:<64 hex>` line continuing it, in printable ASCII
#      (scripts/lockfile_gate.py lint). pip also reads indented lines, URL requirements and
#      option lines such as --extra-index-url; an indented `name @ file:///...whl#sha256=...`
#      passed the column-1 greps this script used up to 7.3.3. Nothing else is checked until
#      this passes. `check-lockfiles.sh --lint [LOCK...]` runs only this step (no network).
#   1. The resolver's own lock, uv-requirements.lock, holds exactly `uv==<the version
#      uv-requirements.in pins>` and every hash in it is a file PyPI publishes for that version
#      (stdlib only). Only then is uv installed from it, --require-hashes, into an isolated
#      venv of its own (the stdlib/ensurepip pip of /usr/bin/python3), and called by absolute
#      path. No other uv ever runs here, whatever the caller has on PATH.
#   2. The header records the canonical command (the one that regenerates the lockfile).
#      --exclude-newer=P7D: only releases that have been on PyPI for at least a week, the
#      window in which a compromised upload is usually noticed and yanked.
#   3. Re-resolving with that command, preferring the locked versions, yields the same pins:
#      the pin set is the closure of pyproject.toml / build-requirements.in, nothing more and
#      nothing less (so an extra pin, even one with PyPI's genuine hashes, fails here, and so
#      does a pin younger than seven days).
#   4. Resolving strictly for the target -- CPython 3.12 on Ubuntu 24.04 (glibc 2.39,
#      x86_64), wheels only, as build-venv.sh installs -- yields the same pins, so every
#      locked version has a wheel the build can use. (Renovate refuses --python-platform
#      and --only-binary in the header, hence this second resolution.)
#   5. Every hash in a lockfile is one PyPI publishes for that exact version. This needs a
#      resolution WITHOUT the lockfile in place: uv carries the hashes of kept pins over
#      from an existing output file instead of fetching them again. Hashes PyPI gained
#      since (a wheel added to an existing release) are fine: that file is not in the
#      lock, so pip would refuse it.
# Limit: the pins are compared by NAME with the closure; the resolution prefers the locked
# versions. So another real release of a pinned package (older or newer, at least a week old,
# with its genuine hashes) that still satisfies the declared requirements passes every gate;
# only the review of the lockfile diff catches it.
#
# Independent of the caller's environment (R1): a fixed PATH without any venv bin/, no
# VIRTUAL_ENV, no PYTHON*/UV_*/PIP_* variables and no pip/uv configuration
# files; Python is /usr/bin/python3 -I, uv gets that interpreter explicitly (--python) and
# reads no uv.toml (--no-config), so it neither discovers a project venv nor a redirected index.
# Needs /usr/bin/python3 with venv/ensurepip (python3-venv) and access to PyPI. To regenerate
# a lockfile, run the command in its header inside controlplane/.
set -euo pipefail

export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset VIRTUAL_ENV CONDA_PREFIX
for v in $(compgen -e); do
    case "$v" in PYTHON* | UV_* | PIP_*) unset "$v" ;; esac
done
export PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
GATE="$ROOT/scripts/lockfile_gate.py"
PY=(/usr/bin/python3 -I -B)
LOCKS=("$ROOT/controlplane/requirements.lock" "$ROOT/controlplane/build-requirements.lock")
UVLOCK="$ROOT/controlplane/uv-requirements.lock"

if [ "${1:-}" = --lint ]; then
    shift
    [ "$#" -gt 0 ] || set -- "${LOCKS[@]}" "$UVLOCK"
    exec "${PY[@]}" "$GATE" lint "$@"
fi
if ! "${PY[@]}" "$GATE" lint "${LOCKS[@]}" "$UVLOCK"; then
    echo "FAIL lockfile grammar (above); nothing else is checked until it is fixed"
    exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 1. uv, verified before it is installed, installed before anything else from a lock.
if ! "${PY[@]}" "$GATE" only uv "$UVLOCK" --input "$ROOT/controlplane/uv-requirements.in" \
    || ! "${PY[@]}" "$GATE" pypi "$UVLOCK"; then
    echo "FAIL uv-requirements.lock is not exactly the published uv of uv-requirements.in"
    exit 1
fi
/usr/bin/python3 -I -m venv "$TMP/uv"
"$TMP/uv/bin/python" -I -m pip install --quiet --require-hashes --only-binary :all: --no-deps \
    -r "$UVLOCK"
UV=("$TMP/uv/bin/uv" pip compile --quiet --no-config --python /usr/bin/python3)

# Exact after step 0: a pin starts in column 1, its hashes follow on indented lines.
pins() { grep -E '^[A-Za-z0-9]' "$1" | cut -d' ' -f1 | sort; }
hashes() { awk '/^[A-Za-z0-9]/ { pin = $1 } /--hash=/ { print pin, $1 }' "$1" | sort; }

LOCK_ARGS=(--generate-hashes --python-version=3.12 --exclude-newer=P7D)
rc=0
fail() { echo "FAIL $*"; bad=1; rc=1; }
for pair in pyproject.toml:requirements.lock build-requirements.in:build-requirements.lock; do
    src="${pair%%:*}"
    lock="${pair##*:}"
    committed="$ROOT/controlplane/$lock"
    canonical="uv pip compile ${LOCK_ARGS[*]} --output-file=$lock $src"
    bad=0

    if [ "$(sed -n '2p' "$committed")" != "#    $canonical" ]; then
        fail "$lock: header command is not: $canonical"
    fi

    for mode in header target; do
        dir="$TMP/$mode"
        mkdir -p "$dir"
        cp "$ROOT/controlplane/$src" "$committed" "$dir/"
        extra=()
        if [ "$mode" = target ]; then
            extra=(--python-platform=x86_64-manylinux_2_39 --only-binary=:all:)
        fi
        (cd "$dir" && "${UV[@]}" "${LOCK_ARGS[@]}" "${extra[@]}" --output-file="$lock" "$src")
        if ! diff -u <(pins "$committed") <(pins "$dir/$lock") > "$TMP/pins.diff"; then
            fail "$lock: pins differ from a fresh $mode resolution"
            cat "$TMP/pins.diff"
        fi
    done

    mkdir -p "$TMP/fresh"
    pins "$committed" > "$TMP/fresh/$lock.in"
    (cd "$TMP/fresh" && "${UV[@]}" --generate-hashes --python-version=3.12 \
        --no-deps --output-file="$lock" "$lock.in")
    unknown="$(comm -23 <(hashes "$committed") <(hashes "$TMP/fresh/$lock"))"
    if [ -n "$unknown" ]; then
        fail "$lock: hashes PyPI does not publish for the pinned version:"
        echo "$unknown"
    fi
    if [ "$bad" = 0 ]; then
        echo "ok   $lock: $(pins "$committed" | wc -l) pins, $(hashes "$committed" | wc -l) hashes"
    fi
done
[ "$rc" = 0 ] && echo "ok   the lockfiles are verified: grammar, uv, published hashes, closure"
exit "$rc"
