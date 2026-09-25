# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# `make deb` is the uniform build entry point of Kevin's linuxmuster.net packages
# (linuxmusterDEV/docs/paket-konventionen.md): dpkg-buildpackage, no root needed
# (Rules-Requires-Root: no; debian/rules builds the venv inside debian/linuxmuster-radius and
# relocates it to /opt/linuxmuster-radius/venv). packaging/make-deb.sh builds from an export of
# the git-tracked files as they are in the working tree: uncommitted edits and staged new files
# ARE built, untracked files are NOT (git add them), and every such difference from HEAD is
# printed as a WARNING -- the .deb still carries the changelog's version. An untracked or
# .gitignored file (a venv, caches, deploy/secrets, .env, a token under .claude/) never
# reaches the source tarball (K3). A .git that git cannot use stops the build. It writes the
# .deb, .changes, .buildinfo, .dsc and the source tarball one level ABOVE this tree.
# Build like CI does, in the same digest-pinned lmndev-runner container (keep the digest equal
# to ci.yml and release.yml; raised by hand while Renovate is disabled). Mount the checkout
# one level down, so the artefacts land in a writable parent (../out here), and the
# repository's common git directory at its own path, so git works in a git worktree too (in a
# plain clone that is the checkout's .git, mounted twice); the image's user `build`
# (uid 1000) builds, root only installs the build dependencies:
#   C="$(git rev-parse --path-format=absolute --git-common-dir)"
#   mkdir -p ../out && docker run --rm -u root -v "$(realpath ../out)":/b -v "$PWD":/b/src \
#     -v "$C":"$C" -w /b/src \
#     ghcr.io/linuxmuster/lmndev-runner:24.04@sha256:6b0c8ac994cf1d2da44b0b36ebd7b3b125094886e78293e04b9db5e515b8209e \
#     bash -c 'apt-get update -qq && apt-get build-dep -y -qq . && runuser -u build -- make deb'
# git's ownership guard stays on: the checkout must belong to uid 1000 (the image's `build`).
.PHONY: deb clean

deb:
	/bin/bash packaging/make-deb.sh

clean:
	debian/rules clean
