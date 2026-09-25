#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# `make deb` calls this. It runs dpkg-buildpackage on an export of exactly the files git
# tracks, as they are in the working tree -- never an untracked or .gitignored file a working
# checkout carries (a venv, a cache, deploy/secrets/krb5.conf, deploy/**/ssl_db/, .env, a token
# under .claude/; linuxmusterDEV work/tasks/nachbesserung-kalte-pruefung-umbau.md, K3, R3).
# The .deb, .changes, .buildinfo, .dsc and source tarball land one level ABOVE the checkout.
#
# What is built, and what is said about it (R4):
#   * the tracked files (the git index) with their working-tree content: uncommitted edits
#     and staged new files ARE built, a tracked file deleted in the working tree is left out;
#   * untracked files are NOT built, even if they are not ignored (git add them first);
#   * whenever that differs from HEAD, a WARNING lists every difference before and after the
#     build: the package still carries the version of debian/changelog, so such a .deb is not
#     the release of that version;
#   * modes as git records them (0644/0755, directories 0755), whatever the umask of the
#     checkout or of this build; mtimes = the changelog date; tracked symlinks stay symlinks.
# Deliberate exclusions from the published source: .github (workflows) and .claude
# (developer tooling with internal endpoints/token ids); dpkg-source drops .gitignore files.
#
# git only reads here, and nothing configured in the checkout runs: git's ownership guard stays
# on (no safe.directory waiver -- a checkout owned by another user is refused, as git refuses
# it; CI marks its own workspace safe by its exact path), core.fsmonitor and hooks are switched
# off, and the files are copied from the working tree instead of through a git command that
# would run a clean/smudge filter (stash, archive, status). A .git that git cannot use -- a
# worktree whose repository is not mounted into the container, a refused owner -- stops the
# build: packing the tree as it is would pack everything in it. Only without any .git (an
# unpacked source package) is the tree built in place.
set -euo pipefail

# Nothing from the caller's environment decides which programs run (R1).
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset VIRTUAL_ENV CONDA_PREFIX
for v in $(compgen -e); do
    case "$v" in PYTHON* | UV_* | PIP_* | GIT_*) unset "$v" ;; esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PKG=linuxmuster-radius
cd "$ROOT"
# -tc cleans the build tree afterwards; -I keeps dpkg-source's default ignore list (.git,
# editor backups, ...); the two -I patterns drop the deliberate exclusions.
DPKG_ARGS=(-us -uc -tc -I -I.github -I.claude)
say() { echo "make-deb.sh: $*"; }
die() { echo "make-deb.sh: $*" >&2; exit 1; }
git_() { git --no-pager -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "$ROOT" "$@"; }

if [ ! -e "$ROOT/.git" ] && [ ! -L "$ROOT/.git" ]; then
    say "no .git here (an unpacked source package), building in place"
    exec dpkg-buildpackage "${DPKG_ARGS[@]}"
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
if ! top="$(git_ rev-parse --show-toplevel 2> "$work/git.err")"; then
    sed 's/^/  git: /' "$work/git.err" >&2
    die "$ROOT/.git exists, but git cannot use it. Not building: without git the source" \
        "package would pack every file in the tree. A git worktree needs its repository's" \
        "common .git directory at the same path (docker: add -v \"\$C\":\"\$C\" with" \
        "C=\$(git rev-parse --path-format=absolute --git-common-dir), see the Makefile); a" \
        "checkout owned by another user is refused by git's ownership guard -- build as its" \
        "owner, or mark exactly this path safe (git config --global --add safe.directory PATH)" \
        "if you trust its .git/config."
fi
[ "$top" = "$ROOT" ] || die "git's top level is $top, not $ROOT"
head="$(git_ rev-parse --verify --short HEAD)" || die "no commit to build from"
VERSION="$(dpkg-parsechangelog -S Version)"
STAMP="@$(dpkg-parsechangelog -S Timestamp)"

# ------------------------------------------------------------------ what is built
# The index: "<mode> <object> <stage>\t<path>", NUL-separated.
git_ ls-files -s -z > "$work/index"
: > "$work/files"   # paths to copy, NUL-separated
: > "$work/modes"   # "<mode> <path>", NUL-separated
: > "$work/check"   # "<object> <path>" of present regular files, compared below
declare -a changes=()
while IFS= read -r -d '' entry; do
    meta="${entry%%$'\t'*}" path="${entry#*$'\t'}"
    read -r mode obj stage <<< "$meta"
    [ "$stage" = 0 ] || die "unmerged path (resolve the conflict first): $path"
    case "$path" in *$'\n'*) die "a tracked path contains a newline: ${path@Q}" ;; esac
    if [ ! -e "$path" ] && [ ! -L "$path" ]; then
        changes+=("D  $path   (deleted in the working tree: left out)")
        continue
    fi
    case "$mode" in
        100644 | 100755)
            [ -f "$path" ] && [ ! -L "$path" ] \
                || die "tracked as a file, but not a regular file in the working tree: $path"
            if [ "$mode" = 100755 ] && [ ! -x "$path" ]; then
                changes+=("M  $path   (not executable in the working tree; built 0755 as git records)")
            elif [ "$mode" = 100644 ] && [ -x "$path" ]; then
                changes+=("M  $path   (executable in the working tree; built 0644 as git records)")
            fi
            printf '%s %s\n' "$obj" "$path" >> "$work/check"
            ;;
        120000)
            [ -L "$path" ] || die "tracked as a symlink, but not one in the working tree: $path"
            [ "$(printf '%s' "$(readlink "$path")" | git_ hash-object --stdin)" = "$obj" ] \
                || changes+=("M  $path   (symlink target changed)")
            ;;
        *) die "unsupported tracked entry (mode $mode, a submodule?): $path" ;;
    esac
    printf '%s\0' "$path" >> "$work/files"
    printf '%s %s\0' "$mode" "$path" >> "$work/modes"
done < "$work/index"
# Working-tree content vs index, raw bytes (--no-filters: no clean filter runs).
if [ -s "$work/check" ]; then
    cut -d' ' -f2- "$work/check" | git_ hash-object --no-filters --stdin-paths > "$work/wt"
    while IFS=' ' read -r obj path <&3 && read -r now <&4; do
        [ "$obj" = "$now" ] || changes+=("M  $path   (changed in the working tree)")
    done 3< "$work/check" 4< "$work/wt"
fi
# Index vs HEAD (staged, not committed; no working tree involved).
git_ diff-index --cached --no-renames --name-status -z HEAD > "$work/staged"
while IFS= read -r -d '' st && IFS= read -r -d '' path; do
    case "$st" in
        A) changes+=("A  $path   (staged, not committed)") ;;
        D) changes+=("D  $path   (removed from the index, not committed)") ;;
        *) changes+=("$st  $path   (staged, not committed)") ;;
    esac
done < "$work/staged"
git_ ls-files --others --exclude-standard -z > "$work/untracked"

warn() {
    local u
    if [ "${#changes[@]}" -gt 0 ]; then
        say "WARNING: this build is NOT commit $head, it carries uncommitted changes:"
        printf 'make-deb.sh:   %s\n' "${changes[@]}"
    fi
    if [ -s "$work/untracked" ]; then
        say "WARNING: untracked files are NOT in this build (git add them to build them):"
        while IFS= read -r -d '' u; do say "  ?? $u"; done < "$work/untracked"
    fi
    if [ "${#changes[@]}" -gt 0 ] || [ -s "$work/untracked" ]; then
        say "WARNING: the package version stays $VERSION (debian/changelog): do not take this" \
            ".deb for the release $VERSION."
    fi
}
warn

# ------------------------------------------------------------------ the export
src="$work/$PKG-$VERSION"   # canonical dir name -> stable tarball top level
mkdir -m 0755 "$src"
tar -C "$ROOT" --null --no-recursion -T "$work/files" -cf - | tar -C "$src" --no-same-owner -xf -
find "$src" -type d -exec chmod 0755 {} +
while IFS= read -r -d '' entry; do
    mode="${entry%% *}" path="${entry#* }"
    case "$mode" in
        100755) chmod 0755 "$src/$path" ;;
        100644) chmod 0644 "$src/$path" ;;
    esac
done < "$work/modes"
find "$src" -exec touch -h -d "$STAMP" {} +
tr '\0' '\n' < "$work/files" | LC_ALL=C sort > "$work/tracked"
say "building $(wc -l < "$work/tracked") tracked files (HEAD $head) in $src"
( cd "$src" && dpkg-buildpackage "${DPKG_ARGS[@]}" )

# The source tarball must hold exactly the exported files minus the deliberate exclusions:
# never an untracked or ignored file, and prove it here rather than trust the export.
tar="$work/${PKG}_${VERSION}.tar.xz"
[ -f "$tar" ] || die "no source tarball at $tar"
# Members that are not directories (files and symlinks; directory entries end in '/'); strip
# the canonical top-level dir. Expected = exported minus .github, .claude and every .gitignore
# (dpkg-source drops those by its default ignore list; a documented tool exclusion).
grep -vE '^(\.github|\.claude)(/|$)' "$work/tracked" | grep -vE '(^|/)\.gitignore$' \
    | LC_ALL=C sort -u > "$work/expected"
tar -tJf "$tar" | grep -v '/$' | sed -e 's|^\./||' -e "s|^$PKG-$VERSION/||" \
    | grep -v '^$' | LC_ALL=C sort -u > "$work/intar"
leaked="$(comm -13 "$work/expected" "$work/intar")"
missing="$(comm -23 "$work/expected" "$work/intar")"
if [ -n "$leaked" ]; then
    printf '  %s\n' "$leaked" >&2
    die "in the source tarball but not an exported tracked file (above)"
fi
if [ -n "$missing" ]; then
    printf '  %s\n' "$missing" >&2
    die "tracked but missing from the source tarball (dpkg-source default ignores?) (above)"
fi
bad_modes="$(tar -tvJf "$tar" | awk '$1 !~ /^(-rw-r--r--|-rwxr-xr-x|drwxr-xr-x|lrwxrwxrwx)$/')"
[ -z "$bad_modes" ] || { printf '  %s\n' "$bad_modes" >&2; die "unexpected modes in the source tarball"; }
say "source tarball holds exactly the tracked files minus .github/.claude ($(wc -l < "$work/expected")), modes 0644/0755"

# dpkg-buildpackage wrote the artefacts one level above the build tree (in $work); move them
# next to the checkout, where CI and the developer expect them.
mv "$work/${PKG}_"* "$ROOT/.."
say "artefacts in $(cd "$ROOT/.." && pwd)"
warn
