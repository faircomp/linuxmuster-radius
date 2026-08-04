#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Builds the linuxmuster-radius .deb with a hermetic Python venv under
# /opt/linuxmuster-radius/venv (built at the target path so the shebangs are correct)
# + systemd unit + maintainer scripts. RUN AS ROOT. VERSION via env.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${VERSION:-0.1.0}"
case "$VERSION" in
    ""|*[!0-9A-Za-z.+~-]*) echo "invalid VERSION: '$VERSION'" >&2; exit 1 ;;
esac
VENV=/opt/linuxmuster-radius/venv
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "== venv @ $VENV =="
rm -rf "$VENV"
mkdir -p /opt/linuxmuster-radius
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
# Pulls in cryptography (P3) as a manylinux wheel into the venv -> no extra apt Depends.
"$VENV/bin/pip" install --quiet "$ROOT/controlplane"

echo "== staging tree =="
mkdir -p "$STAGE/opt/linuxmuster-radius" "$STAGE/lib/systemd/system" "$STAGE/DEBIAN" \
         "$STAGE/usr/bin"
cp -a "$VENV" "$STAGE/opt/linuxmuster-radius/venv"
# Operator CLI onto PATH. The venv keeps the hermetic interpreter, but without this
# packaged symlink `lmnradius` is "command not found" for the admin — the docs' very
# first post-install step. (Found on a real install, 2026-08-04: postinst masked it by
# calling the full venv path itself.) Packaged symlink => dpkg removes it on purge.
ln -s /opt/linuxmuster-radius/venv/bin/lmnradius "$STAGE/usr/bin/lmnradius"
cp "$ROOT/packaging/systemd/linuxmuster-radius.service" \
   "$STAGE/lib/systemd/system/linuxmuster-radius.service"
sed "s/@VERSION@/$VERSION/" "$ROOT/packaging/debian/control" > "$STAGE/DEBIAN/control"
for f in postinst prerm postrm; do
    cp "$ROOT/packaging/debian/$f" "$STAGE/DEBIAN/$f"
    chmod 0755 "$STAGE/DEBIAN/$f"
done

OUT="$ROOT/linuxmuster-radius_${VERSION}_all.deb"
echo "== dpkg-deb -> $OUT =="
dpkg-deb --build --root-owner-group "$STAGE" "$OUT"
echo "== built $OUT =="

# Signing (production): apt does NOT verify individual .deb signatures, but rather the
# signed repo `Release` (InRelease / Release.gpg). So add the .deb into the lmn73 **reprepro**
# repo (deb.linuxmuster.net); reprepro signs the `Release` with the linuxmuster
# GPG key (reprepro `SignWith`). NO `dpkg-sig` per package. Requires the real key + repo access
# (human gate). Verified against wiki.debian.org/DebianRepository/SetupWithReprepro + deb.linuxmuster.net.
