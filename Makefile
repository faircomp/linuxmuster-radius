# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# `make deb` is the uniform build entry point of Kevin's linuxmuster.net packages
# (linuxmusterDEV/docs/paket-konventionen.md): dpkg-buildpackage, no root needed
# (Rules-Requires-Root: no; debian/rules builds the venv inside debian/linuxmuster-radius and
# relocates it to /opt/linuxmuster-radius/venv). It writes the .deb, .changes, .buildinfo, .dsc
# and the source tarball one level ABOVE this tree; -tc cleans the tree afterwards. The
# version is the top entry of debian/changelog.
# Build like CI does, in the same digest-pinned lmndev-runner container (keep the digest equal
# to ci.yml and release.yml; pins are raised by hand). Mount the checkout one level down, so
# the artefacts land in a writable parent (../out here); the image's user `build` (uid 1000)
# builds, root only installs the build dependencies:
#   mkdir -p ../out && docker run --rm -u root -v "$(realpath ../out)":/b -v "$PWD":/b/src -w /b/src \
#     ghcr.io/linuxmuster/lmndev-runner:24.04@sha256:6b0c8ac994cf1d2da44b0b36ebd7b3b125094886e78293e04b9db5e515b8209e \
#     bash -c 'apt-get update -qq && apt-get build-dep -y -qq . && runuser -u build -- make deb'
# The bare -I keeps dpkg-source's default ignore list (.git, .gitignore, ...); an -I<pattern>
# alone would replace it. The other patterns keep untracked local state of a working checkout
# (virtualenvs, tool caches, crabbox state, the token in .claude/settings.local.json, test
# key material) out of the source tarball; they match at any depth.
DPKG_SOURCE_IGNORE := -I -I.github -I.venv -I__pycache__ -I'*.egg-info' -I.mypy_cache \
	-I.ruff_cache -I.pytest_cache -I.crabbox -I'settings.local.json' -I'*.keytab' \
	-I'deploy/e2e/certs/out'

.PHONY: deb clean

deb:
	dpkg-buildpackage -us -uc -tc $(DPKG_SOURCE_IGNORE)

clean:
	debian/rules clean
