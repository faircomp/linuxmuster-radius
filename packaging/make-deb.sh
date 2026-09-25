#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# `make deb` calls this. It runs dpkg-buildpackage so the source package holds ONLY
# git-tracked files, minus two deliberate exclusions -- never an untracked or .gitignored
# file a working checkout carries (a venv, a cache, deploy/secrets/krb5.conf,
# deploy/**/ssl_db/, .env, a token under .claude/; linuxmusterDEV
# work/tasks/nachbesserung-kalte-pruefung-umbau.md, K3). An enumerated -I ignore list is
# whack-a-mole; instead, in a git checkout the tracked tree is exported with `git archive`
# and built there. `git stash create` captures uncommitted changes to tracked files too (so
# local iteration builds what the developer sees), while untracked and ignored files never
# enter the export. Deliberate exclusions: .github (workflows) and .claude (developer tooling
# with internal endpoints/token ids) -- both tracked, both out of the published source.
# Outside a git checkout (an unpacked .dsc) it builds in place.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# -tc cleans the build tree afterwards; -I keeps dpkg-source's default ignore list (.git,
# editor backups, ...); the two -I patterns drop the deliberate exclusions.
DPKG_ARGS=(-us -uc -tc -I -I.github -I.claude)

if ! git rev-parse --git-dir > /dev/null 2>&1; then
    echo "make-deb.sh: not a git checkout, building in place"
    exec dpkg-buildpackage "${DPKG_ARGS[@]}"
fi

VERSION="$(dpkg-parsechangelog -S Version)"
# A tree object of the tracked files with the working tree's current content (the identity is
# only for the throwaway commit `stash create` writes; nothing is committed to a branch).
tree="$(git -c user.email=build@localhost -c user.name=build stash create || true)"
[ -n "$tree" ] || tree=HEAD
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
src="$work/linuxmuster-radius-$VERSION"   # canonical dir name -> stable tarball top level
mkdir -p "$src"
git archive --format=tar "$tree" | tar -x -C "$src"
tracked="$(cd "$src" && find . -type f | sed 's|^\./||' | LC_ALL=C sort)"
echo "make-deb.sh: building from the git-tracked tree ($(git rev-parse --short "$tree"), $(printf '%s\n' "$tracked" | wc -l) files) in $src"
( cd "$src" && dpkg-buildpackage "${DPKG_ARGS[@]}" )

# The source tarball must hold exactly the tracked files minus the two deliberate exclusions:
# never an untracked or ignored file, and prove it here rather than trust the export.
tar="$work/linuxmuster-radius_${VERSION}.tar.xz"
[ -f "$tar" ] || { echo "make-deb.sh: no source tarball at $tar" >&2; exit 1; }
# Regular-file members only (directory entries end in '/'); strip the canonical top-level dir
# (a 3.0-native tarball roots its files at linuxmuster-radius-<version>/). Expected = tracked
# minus the deliberate exclusions .github and .claude, and minus every .gitignore (dpkg-source
# drops those by its default ignore list; that is a documented tool exclusion, not a leak).
printf '%s\n' "$tracked" \
    | grep -vE '^(\.github|\.claude)(/|$)' | grep -vE '(^|/)\.gitignore$' \
    | LC_ALL=C sort -u > "$work/expected"
tar -tJf "$tar" | grep -v '/$' | sed -e 's|^\./||' -e "s|^linuxmuster-radius-$VERSION/||" \
    | grep -v '^$' | LC_ALL=C sort -u > "$work/intar"
leaked="$(comm -13 "$work/expected" "$work/intar")"
missing="$(comm -23 "$work/expected" "$work/intar")"
if [ -n "$leaked" ]; then
    echo "make-deb.sh: in the source tarball but not a tracked file (minus .github/.claude):" >&2
    printf '  %s\n' "$leaked" >&2
    exit 1
fi
if [ -n "$missing" ]; then
    echo "make-deb.sh: tracked but missing from the source tarball (dpkg-source default ignores?):" >&2
    printf '  %s\n' "$missing" >&2
    exit 1
fi
echo "make-deb.sh: source tarball holds exactly the tracked files minus .github/.claude ($(wc -l < "$work/expected"))"

# dpkg-buildpackage wrote the artefacts one level above the build tree (in $work); move them
# next to the checkout, where CI and the developer expect them.
mv "$work"/linuxmuster-radius_* "$ROOT/.."
echo "make-deb.sh: artefacts in $(cd "$ROOT/.." && pwd)"
