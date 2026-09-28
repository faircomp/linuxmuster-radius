#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Builds the hermetic Python venv of linuxmuster-radius into $1. debian/rules passes
# debian/linuxmuster-radius/opt/linuxmuster-radius/venv (no root needed) and then runs
# debian/venv-relocate, which makes the shebangs, activate scripts, pyvenv.cfg and .pyc files
# correct for /opt/linuxmuster-radius/venv. The version of the control plane is the top entry
# of debian/changelog (controlplane/setup.py reads it).

# debian/rules calls this script with a bare `bash`, and dpkg-buildpackage may be started by hand,
# so it starts itself again in a clean environment (R1, A3, P1: the block below), and reads no pip
# configuration file.
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
set -euo pipefail
export PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1

die() { echo "build-venv.sh: $*" >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="${1:?usage: build-venv.sh <venv directory>}"
# The venv path is removed with `rm -rf` further down. Refuse, before anything else, a path that
# could name something else: only an absolute path without whitespace and without . or ..
# components, ending in /opt/linuxmuster-radius/venv -- debian/rules passes
# <build dir>/debian/linuxmuster-radius/opt/linuxmuster-radius/venv. (A build directory with a
# space once split that path, and the `rm -rf` hit a directory outside the build; T2.)
case "$VENV" in
    *[[:space:]]*) die "refusing venv path with whitespace: '$VENV'" ;;
    */./* | */../* | */. | */.. | *//*) die "refusing venv path with . or .. or //: '$VENV'" ;;
    /*/opt/linuxmuster-radius/venv | /opt/linuxmuster-radius/venv) ;;
    *) die "refusing venv path '$VENV': it must be absolute and end in /opt/linuxmuster-radius/venv" ;;
esac
GATE="$ROOT/scripts/lockfile_gate.py"
CP="$ROOT/controlplane"
LOCKS=("$CP/build-requirements.lock" "$CP/requirements.lock")
WHEELS="$(mktemp -d)"
trap 'rm -rf "$WHEELS"' EXIT
# The system interpreter, isolated (-I: no PYTHON* variables, no user site, neither the script's
# directory nor the working directory on sys.path) -- never one from a venv this script
# populates from a lockfile.
PY=/usr/bin/python3

# K1 (linuxmusterDEV work/tasks/nachbesserung-kalte-pruefung-umbau.md): no program or
# interpreter from a venv populated by requirements.lock / build-requirements.lock runs, and
# no such bin/ is on PATH, until ALL locks are fully verified -- grammar, every hash published
# by PyPI, and the pin set equal to the closure of the declared inputs. A wheel from the build
# lock ships bin/ scripts and a .pth; installing it before the set/closure check would run
# that .pth while lmnradius's own wheel is built (a lock-only build-lock pin ran a .pth 18
# times on the pre-K1 head, though the extra pin was caught afterwards). So the venv below is
# created only after step 3.
echo "== lockfiles: grammar and published hashes =="
# 1. Grammar (stdlib only): every line is one uv writes. pip would also honour an indented
#    URL requirement, an --extra-index-url line and every other option line.
"$PY" -I -B "$GATE" lint "${LOCKS[@]}" || die "a lockfile holds a line uv does not write"
# 2. Every hash is one PyPI publishes for that exact version (pip only checks the file it
#    downloads, so a replaced sdist or other-platform-wheel hash would pass pip).
prc=0
"$PY" -I -B "$GATE" pypi "${LOCKS[@]}" || prc=$?
[ "$prc" != 3 ] || die "PyPI gave no answer (offline?): the lockfiles are NOT verified," \
    "nothing is built"
[ "$prc" = 0 ] || die "a lockfile pin or hash is not what PyPI publishes (FAIL lines above)"

echo "== lockfiles: the uv lock, uv, and pin set == closure of the declared inputs =="
# 3. scripts/check-lockfiles.sh, the one lock gate: the grammar of all three locks, the uv
#    lock is exactly the published uv of uv-requirements.in, uv from it into an isolated venv
#    (called by absolute path, never put on PATH), and uv re-resolves pyproject.toml /
#    build-requirements.in: the committed pins must be exactly that closure -- an extra pin
#    (even with real PyPI hashes and a shipped wheel) is rejected HERE, before any lock wheel
#    is unpacked.
bash "$ROOT/scripts/check-lockfiles.sh" \
    || die "a lockfile is not the closure of its declared inputs (extra/other pins)"

echo "== venv @ $VENV =="
rm -rf "$VENV"
mkdir -p "$(dirname "$VENV")"
"$PY" -I -m venv "$VENV"
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
"$PY" -I -B "$GATE" freeze --own lmnradius --drop setuptools "$FREEZE" "${LOCKS[@]}" \
    || die "the venv differs from the lockfiles"
"$VENV/bin/python" -I -B "$GATE" closure --root lmnradius --root pip \
    || die "the venv holds a distribution that nothing requires"
