# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# `make deb` is the uniform build entry point of Kevin's linuxmuster.net packages
# (linuxmusterDEV/docs/paket-konventionen.md): dpkg-buildpackage, no root needed
# (Rules-Requires-Root: no; debian/rules builds the venv inside debian/linuxmuster-radius and
# relocates it to /opt/linuxmuster-radius/venv). packaging/make-deb.sh builds the source
# package from the git-tracked tree only -- an untracked or .gitignored file a working
# checkout carries (a venv, caches, deploy/secrets, .env, a token under .claude/) never
# reaches the source tarball (K3). It writes the .deb, .changes, .buildinfo, .dsc and the
# source tarball one level ABOVE this tree.
# Build like CI does, in the same digest-pinned lmndev-runner container (keep the digest equal
# to ci.yml and release.yml; raised by hand while Renovate is disabled). Mount the checkout
# one level down, so the artefacts land in a writable parent (../out here); the image's user
# `build` (uid 1000) builds, root only installs the build dependencies:
#   mkdir -p ../out && docker run --rm -u root -v "$(realpath ../out)":/b -v "$PWD":/b/src -w /b/src \
#     ghcr.io/linuxmuster/lmndev-runner:24.04@sha256:6b0c8ac994cf1d2da44b0b36ebd7b3b125094886e78293e04b9db5e515b8209e \
#     bash -c 'apt-get update -qq && apt-get build-dep -y -qq . && runuser -u build -- make deb'
.PHONY: deb clean

deb:
	bash packaging/make-deb.sh

clean:
	debian/rules clean
