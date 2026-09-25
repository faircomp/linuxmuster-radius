#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Regression test for `make deb` itself (packaging/make-deb.sh; linuxmusterDEV
# work/tasks/nachbesserung-kalte-pruefung-umbau.md, "Runde 3" R1, R3, R4). Run it in the build
# image after `apt-get build-dep .` (CI job lock-gates-build):
#
#   make_deb_checks.sh          as the build user: one complete `make deb` of a dirty git
#                               checkout, under a poisoned caller environment, with everything
#                               a checkout's .git can configure to run planted; then a git
#                               worktree whose repository is out of reach.
#   make_deb_checks.sh guard U  as root: `make deb` in a checkout that belongs to user U (a
#                               foreign checkout) is refused by git's ownership guard, and
#                               nothing its .git configures runs as root.
#
# The dirty build must succeed and
#   R4  warn, before and after the build, about every difference from HEAD: a modified, a
#       deleted, a staged new and an exec-bit-flipped tracked file, and every untracked file
#       (NOT built), and say that the version stays the changelog's;
#   R3  pack exactly the tracked files as they are in the working tree (the modified one with
#       its change, the staged one, not the deleted one, nothing untracked or ignored), a
#       tracked symlink as a symlink, and only 0644/0755 modes although the checkout was made
#       with umask 002 and one file is 0600;
#   R3  run nothing the checkout's .git configures: core.fsmonitor, a clean/smudge filter for
#       every path (.git/info/attributes), hooks, git config from the environment;
#   R1  run nothing from these parts of the caller's environment: an activated venv with the
#       K1 wheel (scripts/tests/k1_wheel.py: bin/ tools and a .pth, all writing the marker)
#       first on PATH, VIRTUAL_ENV, PYTHONPATH with a marker sitecustomize, PYTHONHOME, UV_*/PIP_*
#       redirections, and a poisoned .venv in the checkout.
# Any marker line fails the test. Needs git, dpkg-dev, debhelper, python3-venv and PyPI.
set -uo pipefail

export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset VIRTUAL_ENV CONDA_PREFIX
for v in $(compgen -e); do
    case "$v" in PYTHON* | UV_* | PIP_* | GIT_*) unset "$v" ;; esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
chmod 0755 "$TMP"
PASS=0
FAIL=0
MARK="$TMP/marker"
VERSION="$(cd "$ROOT" && dpkg-parsechangelog -S Version)"
ok() { echo "ok    $*"; PASS=$((PASS + 1)); }
wrong() { echo "WRONG $*"; FAIL=$((FAIL + 1)); }
check() {  # <label> <command...>: ok if the command succeeds
    local label="$1"
    shift
    if "$@"; then ok "$label"; else wrong "$label"; fi
}
no_marker() {
    if [ -e "$MARK" ]; then
        wrong "$1: something planted ran ($(wc -l < "$MARK") marker lines):"
        sed 's/^/      /' "$MARK" | head -20
    else
        ok "$1: nothing planted ran"
    fi
    rm -f "$MARK"
}
# git with the same switches as make-deb.sh: no pager, no fsmonitor, no hooks (nothing a
# checkout's .git configures runs through the harness either).
GIT=(git --no-pager -c core.fsmonitor=false -c core.hooksPath=/dev/null)
marker_script() {  # <path> <label>: an executable that appends "<label> ran" to the marker
    printf '#!/bin/sh\necho "%s ran: $*" >> %s\ncat\n' "$2" "$MARK" > "$1"
    chmod 0755 "$1"
}

# checkout <dir>: a git repository of the tracked files, one commit (works from a shallow CI
# checkout as well, where a clone would not).
checkout() {
    mkdir -p "$1"
    # (the harness reads its own, trusted checkout; as root in `guard` mode that belongs to the
    # build user, hence the exact safe.directory -- make-deb.sh itself never waives the guard)
    "${GIT[@]}" -c safe.directory="$ROOT" -C "$ROOT" ls-files -z \
        | tar -C "$ROOT" --null -T - -cf - | tar -C "$1" -xf -
    G=("${GIT[@]}" -C "$1" -c user.name=test -c user.email=test@localhost -c commit.gpgsign=false)
    "${G[@]}" init -q
    "${G[@]}" add -A
    "${G[@]}" commit -q -m base
}
# plant_git <repo>: everything a .git can make git run -- after the last harness git call.
plant_git() {
    marker_script "$TMP/fsmonitor" "core.fsmonitor"
    marker_script "$TMP/filter" "filter"
    "${GIT[@]}" -C "$1" config core.fsmonitor "$TMP/fsmonitor"
    "${GIT[@]}" -C "$1" config filter.evil.clean "$TMP/filter clean %f"
    "${GIT[@]}" -C "$1" config filter.evil.smudge "$TMP/filter smudge %f"
    "${GIT[@]}" -C "$1" config filter.evil.required true
    mkdir -p "$1/.git/info" "$1/.git/hooks"
    echo '* filter=evil' > "$1/.git/info/attributes"
    for h in post-index-change pre-commit post-checkout post-commit reference-transaction; do
        marker_script "$1/.git/hooks/$h" "hook $h"
    done
}

if [ "${1:-}" = guard ]; then
    owner="${2:?usage: make_deb_checks.sh guard <owner>}"
    [ "$(id -u)" = 0 ] || { echo "make_deb_checks.sh guard: run as root"; exit 1; }
    parent="$TMP/foreign"
    checkout "$parent/linuxmuster-radius"
    plant_git "$parent/linuxmuster-radius"
    echo "# a change, so a status-like git call would consult fsmonitor and the filter" \
        >> "$parent/linuxmuster-radius/README.md"
    chown -R "$owner" "$parent"
    rm -f "$MARK"
    (cd "$parent/linuxmuster-radius" && make deb) > "$TMP/out" 2>&1
    rc=$?
    if [ "$rc" != 0 ] && grep -q 'dubious ownership' "$TMP/out" \
        && grep -q 'exists, but git cannot use it' "$TMP/out"; then
        ok "guard: root's make deb in a checkout of $owner is refused by git (exit $rc)"
    else
        wrong "guard: expected a refusal for dubious ownership (exit $rc)"
        tail -15 "$TMP/out" | sed 's/^/      /'
    fi
    check "guard: no source package or .deb was written" \
        test -z "$(find "$parent" -maxdepth 1 -name 'linuxmuster-radius_*' -print -quit)"
    no_marker "guard"
    echo
    echo "make deb checks: $PASS ok, $FAIL wrong"
    [ "$FAIL" = 0 ]
    exit
fi

# ------------------------------------------------------------ the dirty build (R1, R3, R4)
umask 002
parent="$TMP/co"
REPO="$parent/linuxmuster-radius"
checkout "$REPO"
ln -s ../README.md "$REPO/docs/README-link.md"
"${G[@]}" add docs/README-link.md
"${G[@]}" commit -q -m "a tracked symlink"
HEAD_SHORT="$("${G[@]}" rev-parse --short HEAD)"
# a worktree for the unreachable-repository case below (created before git is poisoned)
"${G[@]}" worktree add -q --detach "$TMP/wt/linuxmuster-radius" HEAD
# the dirty state
echo "# uncommitted line (R4 test)" >> "$REPO/README.md"
rm "$REPO/docs/references.md"
echo "staged, not committed" > "$REPO/docs/staged-note.md"
"${G[@]}" add docs/staged-note.md
echo "untracked" > "$REPO/docs/untracked-note.md"
echo "TOKEN=secret" > "$REPO/.env"
mkdir -p "$REPO/deploy/secrets" "$REPO/.claude"
echo "SECRET" > "$REPO/deploy/secrets/krb5.conf"
echo '{"token": "x"}' > "$REPO/.claude/settings.local.json"
chmod 0600 "$REPO/docs/architecture.md"
chmod 0755 "$REPO/docs/install.md"
# the caller's environment (R1): an activated venv and a .venv, both with the K1 wheel
WHEEL="$(/usr/bin/python3 -I "$ROOT/scripts/tests/k1_wheel.py" "$TMP" "$MARK")"
for v in "$TMP/activated" "$REPO/.venv"; do
    /usr/bin/python3 -I -m venv "$v"
    "$v/bin/python" -I -m pip install -q --no-index --no-deps "$WHEEL"
done
mkdir -p "$TMP/pythonpath"
printf "open(%s, 'a').write('sitecustomize ran\\\\n')\n" "'$MARK'" > "$TMP/pythonpath/sitecustomize.py"
plant_git "$REPO"
POISON=(env "PATH=$TMP/activated/bin:/usr/sbin:/usr/bin:/sbin:/bin" "VIRTUAL_ENV=$TMP/activated"
    "PYTHONPATH=$TMP/pythonpath" "PYTHONHOME=$TMP/activated" "UV_PYTHON=$TMP/activated/bin/python3"
    "UV_INDEX_URL=http://127.0.0.1:9/simple" "PIP_INDEX_URL=http://127.0.0.1:9/simple"
    "PIP_FIND_LINKS=$TMP" "GIT_CONFIG_PARAMETERS='core.fsmonitor'='$TMP/fsmonitor'"
    "GIT_DIR=$TMP/nonexistent")
rm -f "$MARK"
(cd "$REPO" && "${POISON[@]}" make deb) > "$TMP/out" 2>&1
rc=$?
if [ "$rc" = 0 ] && [ -f "$parent/linuxmuster-radius_${VERSION}_amd64.deb" ]; then
    ok "dirty checkout, poisoned environment: make deb builds the .deb"
else
    wrong "dirty checkout: make deb failed (exit $rc)"
    tail -30 "$TMP/out" | sed 's/^/      /'
fi
no_marker "dirty build (fsmonitor, filter, hooks, git env, activated venv, .venv, PYTHONPATH)"

# R4: the warnings, before AND after the build (so they are seen after dpkg's output)
warns() {  # <label> <fixed string>: printed twice
    local n
    n="$(grep -cF -- "$2" "$TMP/out")"
    if [ "$n" -ge 2 ]; then ok "R4 warns: $1"; else wrong "R4 warns: $1 ($n times: $2)"; fi
}
warns "not the commit" "WARNING: this build is NOT commit $HEAD_SHORT"
warns "modified file" "M  README.md   (changed in the working tree)"
warns "deleted file" "D  docs/references.md   (deleted in the working tree: left out)"
warns "staged new file" "A  docs/staged-note.md   (staged, not committed)"
warns "exec bit flipped" "M  docs/install.md   (executable in the working tree; built 0644"
warns "untracked files not built" "WARNING: untracked files are NOT in this build"
warns "untracked file" "?? docs/untracked-note.md"
warns "untracked .env" "?? .env"
warns "version stays" "the package version stays $VERSION (debian/changelog)"
if grep -qE '\?\? (\.venv|deploy/secrets|\.claude)' "$TMP/out"; then
    wrong "R4: an ignored path is listed as untracked"
else
    ok "R4: ignored paths are not listed"
fi

# R3: the source tarball
TAR="$parent/linuxmuster-radius_$VERSION.tar.xz"
if [ -f "$TAR" ]; then
    tar -tvJf "$TAR" > "$TMP/tarv"
    tar -tJf "$TAR" | sed "s|^linuxmuster-radius-$VERSION/||" > "$TMP/tarl"
    has() { grep -qx -- "$1" "$TMP/tarl"; }
    check "R3 tarball: the modified file with its change" \
        bash -c "tar -xOJf '$TAR' linuxmuster-radius-$VERSION/README.md | grep -q 'uncommitted line'"
    check "R3 tarball: the staged new file" has docs/staged-note.md
    check "R3 tarball: not the deleted file" bash -c "! grep -qx docs/references.md '$TMP/tarl'"
    for p in docs/untracked-note.md .env .venv/ deploy/secrets/krb5.conf .claude/settings.local.json; do
        check "R3 tarball: not $p" bash -c "! grep -q '^${p//./\\.}' '$TMP/tarl'"
    done
    check "R3 tarball: the tracked symlink as a symlink" \
        grep -qE "^l.* linuxmuster-radius-$VERSION/docs/README-link.md -> \.\./README\.md$" "$TMP/tarv"
    check "R3 tarball: only 0644/0755 modes (umask 002 checkout, a 0600 file)" \
        bash -c "! awk '{print \$1}' '$TMP/tarv' | grep -vxE -- '-rw-r--r--|-rwxr-xr-x|drwxr-xr-x|lrwxrwxrwx'"
    check "R3 tarball: the 0600 file as 0644" \
        grep -qE "^-rw-r--r-- .* linuxmuster-radius-$VERSION/docs/architecture.md$" "$TMP/tarv"
    check "R3 tarball: the exec-flipped file as 0644" \
        grep -qE "^-rw-r--r-- .* linuxmuster-radius-$VERSION/docs/install.md$" "$TMP/tarv"
    check "R3 tarball: debian/rules 0755" \
        grep -qE "^-rwxr-xr-x .* linuxmuster-radius-$VERSION/debian/rules$" "$TMP/tarv"
else
    wrong "R3: no source tarball at $TAR"
fi

# ------------------------------------------------------------ R3: a worktree out of reach
# As in a container that mounts only the worktree: its .git file points into a repository git
# cannot reach. make deb must stop before dpkg-buildpackage, not pack the tree as it is.
mv "$REPO/.git" "$REPO/.git.away"
echo "untracked secret" > "$TMP/wt/linuxmuster-radius/.env"
rm -f "$MARK"
(cd "$TMP/wt/linuxmuster-radius" && make deb) > "$TMP/out" 2>&1
rc=$?
mv "$REPO/.git.away" "$REPO/.git"
if [ "$rc" != 0 ] && grep -q 'exists, but git cannot use it' "$TMP/out" \
    && grep -q 'common .git directory at the same path' "$TMP/out"; then
    ok "R3 worktree without its repository: make deb refuses (exit $rc)"
else
    wrong "R3 worktree without its repository: expected a refusal (exit $rc)"
    tail -15 "$TMP/out" | sed 's/^/      /'
fi
check "R3 worktree: no source package or .deb was written" \
    test -z "$(find "$TMP/wt" -maxdepth 1 -name 'linuxmuster-radius_*' -print -quit)"
check "R3 worktree: dpkg-buildpackage never started" \
    bash -c "! grep -q 'dpkg-buildpackage' '$TMP/out'"
no_marker "R3 worktree"

echo
echo "make deb checks: $PASS ok, $FAIL wrong"
[ "$FAIL" = 0 ]
