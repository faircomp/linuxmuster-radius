#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Regression test for the gates in front of the shipped venv (linuxmusterDEV
# work/verification/cold-stage-a.md, finding F2). Up to 7.3.3 an indented
#   zzzevil @ file:///...whl#sha256=...
# line in controlplane/requirements.lock passed scripts/check-lockfiles.sh (it read only
# lines starting in column 1, pip reads indented ones too), and the freeze == lock check of
# the build crashed inside a process substitution that `set -e` never saw: `make deb`
# shipped the foreign package. Now every tampered lockfile of the matrix below must be
# refused, for the expected reason, by BOTH
#   check  scripts/check-lockfiles.sh (the CI job `lockfile`), and
#   build  packaging/build-venv.sh, which debian/rules runs: its failure fails `make deb`.
# Plus cases for each gate of scripts/lockfile_gate.py on its own (lint, pypi, freeze,
# closure), including the variants pip reads that the old check did not.
# Needs uv (the version pinned in ci.yml), python3 >= 3.12 with venv and pip, and PyPI. It
# never skips: a missing tool fails. LOCK_GATES_VERBOSE=1 prints every refusal's reason.
# LOCK_GATES_DEB=1 also runs the whole `make deb` (dpkg-buildpackage) on each tampered tree
# and requires it to fail without a .deb; run that inside the build image, as a user who
# may write the checkout, after `apt-get build-dep .` (the command is in the Makefile).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GATE="$ROOT/scripts/lockfile_gate.py"
LOCK="$ROOT/controlplane/requirements.lock"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS=0
FAIL=0
H64="$(printf 'e%.0s' $(seq 64))"
EVIL_URL="file:///tmp/evil/zzzevil-1.0-py3-none-any.whl#sha256=$H64"

for tool in uv python3; do
    command -v "$tool" > /dev/null || { echo "lock_gates.sh: $tool is required"; exit 1; }
done
python3 -c 'import sys, venv, pip; sys.exit(sys.version_info < (3, 12))' \
    || { echo "lock_gates.sh: python3 >= 3.12 with venv and pip is required"; exit 1; }

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
    python3 - "$1" "$2" <<'PY' || { echo "lock_gates.sh: case edit failed: $2"; exit 1; }
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
expect ok "pypi: every committed hash is published" "" python3 -I -B "$GATE" pypi \
    "$ROOT/controlplane/build-requirements.lock" "$LOCK"
# The full set-closure of the COMMITTED files, not only their grammar: an extra real pin that
# an author (or an attacker with commit access) left in a lock is caught here in the fast tier,
# not only by the ci `lockfile` job and the build (cold verification of the debian umbau,
# finding 4). Needs uv and PyPI.
expect ok "closure: the committed lockfiles are the closure of their inputs" "" \
    bash "$ROOT/scripts/check-lockfiles.sh"

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
# build-venv.sh now runs the set-closure BEFORE it populates the venv (K1), so an extra pin is
# rejected there, earlier than the post-install closure gate.
BUILD_WHY[extra-pin]="pins differ from a fresh|not the closure of its declared inputs"
EDIT[pin-without-hash]="lines.insert(-1, 'six==1.16.0')"
CHECK_WHY[pin-without-hash]="not an empty line, a comment"
BUILD_WHY[pin-without-hash]="not an empty line, a comment"

for c in indented-url extra-index-url hashes-removed hash-changed extra-pin pin-without-hash; do
    dir="$TMP/$c"
    mkdir -p "$dir/debian"
    cp -r "$ROOT/controlplane" "$ROOT/packaging" "$ROOT/scripts" "$dir/"
    cp "$ROOT/debian/changelog" "$dir/debian/"
    edit "$dir/controlplane/requirements.lock" "${EDIT[$c]}"
    expect fail "check: $c" "${CHECK_WHY[$c]}" bash "$dir/scripts/check-lockfiles.sh"
    expect fail "build: $c" "${BUILD_WHY[$c]}" bash "$dir/packaging/build-venv.sh" "$dir/venv"
    if [ -n "${LOCK_GATES_DEB:-}" ]; then
        mkdir -p "$TMP/deb-$c/src"
        tar -C "$ROOT" --exclude=./.git -cf - . | tar -C "$TMP/deb-$c/src" -xf -
        cp "$dir/controlplane/requirements.lock" "$TMP/deb-$c/src/controlplane/"
        expect fail "make deb: $c" "${BUILD_WHY[$c]}" make -C "$TMP/deb-$c/src" deb
        if compgen -G "$TMP/deb-$c/*.deb" > /dev/null; then
            echo "WRONG make deb: $c left a .deb behind"
            FAIL=$((FAIL + 1))
        fi
    fi
done

# ------------------------------------------------ K1: a lock wheel must not run before the gate
# The attack of the cold verification (work/verification/cold-debian-squid.md F1,
# nachbesserung-kalte-pruefung-umbau.md K1): a wheel whose distribution ships bin/ scripts
# (diff, comm, ... that could shadow the check tools if a lock venv's bin/ were on PATH) and a
# `.pth` that runs in every interpreter of the venv it is installed into. Pinned in the BUILD
# lock (variant 1) or in both locks (variant 2) with a REAL name and hash, it passes grammar
# and the PyPI-hash check; only the set==closure check can stop it, and it must stop it BEFORE
# any lock wheel is unpacked, so the .pth never runs while lmnradius's own wheel is built.
# The wheel is crafted here (self-contained, permanent) and its "publication on PyPI" is
# simulated only inside the throwaway copy, by monkeypatching that copy's lockfile_gate
# published(); the repo's real build path is untouched, so real builds are not weakened.
MARK="$TMP/k1-marker"
WHEELDIR="$TMP/k1-wheel"
mkdir -p "$WHEELDIR"
python3 - "$WHEELDIR" "$MARK" <<'PY'
import base64, hashlib, sys, zipfile
out, marker = sys.argv[1:3]
pth = ("import os; open(%r,'a').write('pth ran in pid %%d\\n' %% os.getpid())\n" % marker)
files = {"zzzk1/__init__.py": "", "zzzk1.pth": pth,
         "zzzk1-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: zzzk1\nVersion: 1.0\n",
         "zzzk1-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
for t in "diff comm sort awk grep cut cp python3 uv".split():
    files["zzzk1-1.0.data/scripts/" + t] = "#!/bin/sh\necho \"bin/%s ran: $*\" >> %s\nexit 0\n" % (t, marker)
rec = []
for n, txt in files.items():
    d = txt.encode(); h = base64.urlsafe_b64encode(hashlib.sha256(d).digest()).rstrip(b"=").decode()
    rec.append("%s,sha256=%s,%d" % (n, h, len(d)))
rec.append("zzzk1-1.0.dist-info/RECORD,,")
files["zzzk1-1.0.dist-info/RECORD"] = "\n".join(rec) + "\n"
p = out + "/zzzk1-1.0-py3-none-any.whl"
with zipfile.ZipFile(p, "w") as z:
    for n, txt in files.items():
        info = zipfile.ZipInfo(n, (2026, 1, 1, 0, 0, 0))
        info.external_attr = (0o755 if "/scripts/" in n else 0o644) << 16
        z.writestr(info, txt)
print(hashlib.sha256(open(p, "rb").read()).hexdigest())
PY
K1_HASH="$(sha256sum "$WHEELDIR"/*.whl | cut -d' ' -f1)"

# k1_case <label> <build|both>: pin zzzk1 in the build lock (and, for "both", the runtime lock)
# of a copy, simulate its PyPI publication in that copy, then assert the set-closure check
# rejects it. Under LOCK_GATES_DEB, also assert `make deb` fails, ships no .deb and, above all,
# leaves NO marker: the .pth never ran.
k1_case() {
    local label="$1" where="$2"
    local dir="$TMP/k1-$label"
    rm -rf "$dir"; mkdir -p "$dir/debian"
    cp -r "$ROOT/controlplane" "$ROOT/packaging" "$ROOT/scripts" "$dir/"
    cp "$ROOT/debian/changelog" "$dir/debian/"
    local block="zzzk1==1.0 \\
    --hash=sha256:$K1_HASH
    # via -r build-requirements.in"
    printf '%s\n' "$block" >> "$dir/controlplane/build-requirements.lock"
    [ "$where" = both ] && printf '%s\n' "${block/-r build-requirements.in/lmnradius (pyproject.toml)}" \
        >> "$dir/controlplane/requirements.lock"
    # simulate "zzzk1 1.0 is on PyPI with this hash" in the copy only
    python3 - "$dir/scripts/lockfile_gate.py" "$K1_HASH" <<'PY'
import sys
p, h = sys.argv[1:3]
s = open(p).read(); i = s.index('if __name__ == "__main__":')
s = s[:i] + ("_real_pub = published\n\n\ndef published(pin):\n"
             "    return {%r} if pin.name == 'zzzk1' else _real_pub(pin)\n\n\n" % h) + s[i:]
open(p, "w").write(s)
PY
    # the set-closure check (grammar and PyPI-hash pass; only closure can stop it)
    expect fail "K1 check: $label" "pins differ from a fresh" \
        bash "$dir/scripts/check-lockfiles.sh"
    if [ -n "${LOCK_GATES_DEB:-}" ]; then
        local src="$TMP/k1-deb-$label/src"
        mkdir -p "$src"
        tar -C "$ROOT" --exclude=./.git -cf - . | tar -C "$src" -xf -
        cp "$dir/controlplane/build-requirements.lock" "$src/controlplane/"
        cp "$dir/controlplane/requirements.lock" "$src/controlplane/"
        cp "$dir/scripts/lockfile_gate.py" "$src/scripts/"
        rm -f "$MARK"
        expect fail "K1 make deb: $label" "not the closure of its declared inputs" \
            env PIP_FIND_LINKS="$WHEELDIR" make -C "$src" deb
        if compgen -G "$TMP/k1-deb-$label/"'*.deb' > /dev/null; then
            echo "WRONG K1 make deb: $label left a .deb behind"; FAIL=$((FAIL + 1))
        fi
        if [ -e "$MARK" ]; then
            echo "WRONG K1 make deb: $label ran the wheel's .pth/bin (marker: $(wc -l < "$MARK") lines)"
            FAIL=$((FAIL + 1))
        else
            echo "ok    K1 make deb: $label built no .deb and the .pth never ran"
            PASS=$((PASS + 1))
        fi
    fi
}
k1_case build-lock-only build
k1_case both-locks both

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
closure() { python3 -I -B "$GATE" closure --path "$SP" --root lmnradius --root pip; }
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
