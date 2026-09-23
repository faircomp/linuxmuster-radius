#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Proves that the hash-pinned lockfiles under controlplane/ still match their sources and
# the target platform. packaging/build-deb.sh installs them with --no-deps, so nothing
# else would notice a dependency added to pyproject.toml without re-locking.
#   1. The header records the canonical command (the one Renovate re-runs on a bump).
#      --exclude-newer=P7D: only releases that have been on PyPI for at least a week, the
#      window in which a compromised upload is usually noticed and yanked.
#   2. Re-resolving with that command, preferring the locked versions, yields the same pins
#      (so a pin younger than seven days fails here).
#   3. Resolving strictly for the target -- CPython 3.12 on Ubuntu 24.04 (glibc 2.39,
#      x86_64), wheels only, as build-deb.sh installs -- yields the same pins, so every
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
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

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
