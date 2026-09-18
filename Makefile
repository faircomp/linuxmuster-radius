# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# `make deb` is the uniform build entry point of Kevin's linuxmuster.net packages
# (linuxmusterDEV/docs/paket-konventionen.md). Until the debian/ conversion it wraps
# packaging/build-deb.sh, which needs root (the venv is built at its target path
# /opt/linuxmuster-radius/venv). Build like CI does, in the lmndev-runner container:
#   docker run --rm -u root -v "$PWD":/src -w /src ghcr.io/linuxmuster/lmndev-runner:24.04 \
#     bash -c 'apt-get update -qq && apt-get install -y -qq python3-venv && make deb'
# The version comes from debian/changelog (dpkg-parsechangelog); VERSION=<x> overrides it.

.PHONY: deb clean

deb:
	bash packaging/build-deb.sh

clean:
	rm -f linuxmuster-radius_*.deb
	rm -rf controlplane/build controlplane/*.egg-info
