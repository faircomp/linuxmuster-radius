#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Builds the hermetic Python venv of linuxmuster-radius into $1. debian/rules passes
# debian/linuxmuster-radius/opt/linuxmuster-radius/venv (no root needed) and then runs
# debian/venv-relocate, which makes the shebangs, activate scripts, pyvenv.cfg and .pyc files
# correct for /opt/linuxmuster-radius/venv. The version of the control plane is the top entry
# of debian/changelog (controlplane/setup.py reads it).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${1:?usage: build-venv.sh <venv directory>}"
GATE="$ROOT/scripts/lockfile_gate.py"
LOCKS=("$ROOT/controlplane/build-requirements.lock" "$ROOT/controlplane/requirements.lock")
WHEELS="$(mktemp -d)"
trap 'rm -rf "$WHEELS"' EXIT
die() { echo "build-venv.sh: $*" >&2; exit 1; }

# Before pip reads a lockfile. (1) Every line is one uv writes: pip would also honour an
# indented URL requirement, an --extra-index-url line and every other option line, and a
# check that reads only some lines lets them through. (2) Every hash is one PyPI publishes
# for that exact version: pip compares only the hash of the file it downloads, so a
# replaced hash of any other file (the sdist, another platform's wheel) would pass pip.
echo "== lockfiles =="
python3 -I -B "$GATE" lint "${LOCKS[@]}" || die "a lockfile holds a line uv does not write"
python3 -I -B "$GATE" pypi "${LOCKS[@]}" || die "a lockfile hash is not a file PyPI publishes"

echo "== venv @ $VENV =="
rm -rf "$VENV"
mkdir -p "$(dirname "$VENV")"
python3 -m venv "$VENV"
# Supply chain: every distribution in the venv comes from a lockfile that pins its
# version AND sha256 (controlplane/*.lock, `uv pip compile --generate-hashes`, raised in
# reviewed PRs, checked by scripts/check-lockfiles.sh). --require-hashes: pip installs a
# file only if its hash is in the lock. --only-binary :all:: wheels only, so no sdist is
# built with unhashed build dependencies. --no-deps: the lock is the whole closure, pip
# resolves nothing on its own (pip check below proves it is complete).
LOCKED=("$VENV/bin/pip" install --quiet --require-hashes --only-binary :all: --no-deps)
# pip itself (replaces what ensurepip bootstrapped) and setuptools, the build backend.
"${LOCKED[@]}" -r "$ROOT/controlplane/build-requirements.lock"
# The runtime closure; cryptography (P3) arrives as a manylinux wheel -> no extra apt Depends.
"${LOCKED[@]}" -r "$ROOT/controlplane/requirements.lock"
# The control plane itself: built offline with the locked setuptools, then installed by name
# from that wheel (a path install would record the build directory in direct_url.json).
"$VENV/bin/pip" wheel --quiet --no-index --no-build-isolation --no-deps -w "$WHEELS" "$ROOT/controlplane"
"$VENV/bin/pip" install --quiet --no-index --only-binary :all: --no-deps --find-links "$WHEELS" lmnradius
"$VENV/bin/pip" check
# setuptools was only needed to build the control plane; nothing imports it at runtime.
"$VENV/bin/pip" uninstall --quiet --yes setuptools
# The venv holds exactly the lockfiles plus the control plane, nothing else, and every
# distribution in it is needed by the control plane or pip. Every step writes a file or
# returns its own status, checked right here: no pipeline, no process substitution, so an
# error anywhere (a crash included) fails the build.
FREEZE="$WHEELS/freeze.txt"
"$VENV/bin/pip" freeze --all > "$FREEZE" || die "pip freeze failed"
python3 -I -B "$GATE" freeze --own lmnradius --drop setuptools "$FREEZE" "${LOCKS[@]}" \
    || die "the venv differs from the lockfiles"
"$VENV/bin/python" -I -B "$GATE" closure --root lmnradius --root pip \
    || die "the venv holds a distribution that nothing requires"
