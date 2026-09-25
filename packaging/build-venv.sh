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
WHEELS="$(mktemp -d)"
trap 'rm -rf "$WHEELS"' EXIT

echo "== venv @ $VENV =="
rm -rf "$VENV"
mkdir -p "$(dirname "$VENV")"
python3 -m venv "$VENV"
# Supply chain: every distribution in the venv comes from a lockfile that pins its
# version AND sha256 (controlplane/*.lock, `uv pip compile --generate-hashes`, bumped by
# Renovate PRs, checked by scripts/check-lockfiles.sh). --require-hashes: pip installs a
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
# The venv holds exactly the lockfiles, nothing else (names PEP 503-normalised on both sides).
norm() {
    "$VENV/bin/python" -c 'import re, sys
for line in sys.stdin:
    name, version = line.split()[0].split("==")
    print(re.sub(r"[-_.]+", "-", name).lower() + "==" + version)' | LC_ALL=C sort
}
diff -u <(grep -hE '^[A-Za-z0-9]' "$ROOT/controlplane/requirements.lock" \
              "$ROOT/controlplane/build-requirements.lock" | grep -v '^setuptools==' | norm) \
        <("$VENV/bin/pip" freeze --all --exclude lmnradius | norm)

