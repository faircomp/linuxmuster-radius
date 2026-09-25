#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Proves that the hash-pinned lockfiles under controlplane/ still match their sources and
# the target platform. packaging/build-venv.sh installs them with --no-deps, so nothing
# else would notice a dependency added to pyproject.toml without re-locking.
#   0. Every line is one pip reads exactly the way uv wrote it: empty, a comment, a
#      `name==version \` pin or a `    --hash=sha256:<64 hex>` line continuing it, in
#      printable ASCII (scripts/lockfile_gate.py lint). pip also reads indented lines, URL
#      requirements and option lines such as --extra-index-url; an indented
#      `name @ file:///...whl#sha256=...` passed the column-1 greps this script used up to
#      7.3.3. Nothing else is checked (and no uv runs on the file) until this passes.
#      `check-lockfiles.sh --lint [LOCK...]` runs only this step, without uv or network;
#      build-venv.sh runs it before pip reads a lockfile.
#   1. The header records the canonical command (the one Renovate re-runs on a bump).
#      --exclude-newer=P7D: only releases that have been on PyPI for at least a week, the
#      window in which a compromised upload is usually noticed and yanked.
#   2. Re-resolving with that command, preferring the locked versions, yields the same pins
#      (so a pin younger than seven days fails here).
#   3. Resolving strictly for the target -- CPython 3.12 on Ubuntu 24.04 (glibc 2.39,
#      x86_64), wheels only, as build-venv.sh installs -- yields the same pins, so every
#      locked version has a wheel the build can use. (Renovate refuses --python-platform
#      and --only-binary in the header, hence this second resolution.)
#   4. Every hash in a lockfile is one PyPI publishes for that exact version. This needs a
#      resolution WITHOUT the lockfile in place: uv carries the hashes of kept pins over
#      from an existing output file instead of fetching them again. Hashes PyPI gained
#      since (a wheel added to an existing release) are fine: that file is not in the
#      lock, so pip would refuse it.
# Needs uv and access to PyPI. To regenerate a lockfile, run the command in its header
# inside controlplane/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATE="$ROOT/scripts/lockfile_gate.py"
LOCKS=("$ROOT/controlplane/requirements.lock" "$ROOT/controlplane/build-requirements.lock")

if [ "${1:-}" = --lint ]; then
    shift
    [ "$#" -gt 0 ] || set -- "${LOCKS[@]}"
    exec python3 -I -B "$GATE" lint "$@"
fi
if ! python3 -I -B "$GATE" lint "${LOCKS[@]}"; then
    echo "FAIL lockfile grammar (above); nothing else is checked until it is fixed"
    exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

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
        (cd "$dir" && uv pip compile --quiet "${LOCK_ARGS[@]}" "${extra[@]}" \
            --output-file="$lock" "$src")
        if ! diff -u <(pins "$committed") <(pins "$dir/$lock") > "$TMP/pins.diff"; then
            fail "$lock: pins differ from a fresh $mode resolution"
            cat "$TMP/pins.diff"
        fi
    done

    mkdir -p "$TMP/fresh"
    pins "$committed" > "$TMP/fresh/$lock.in"
    (cd "$TMP/fresh" && uv pip compile --quiet --generate-hashes --python-version=3.12 \
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
exit "$rc"
