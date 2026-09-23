# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# `make deb` is the uniform build entry point of Kevin's linuxmuster.net packages
# (linuxmusterDEV/docs/paket-konventionen.md). Until the debian/ conversion it wraps
# packaging/build-deb.sh, which needs root (the venv is built at its target path
# /opt/linuxmuster-radius/venv). Build like CI does, in the same digest-pinned
# lmndev-runner container (keep the digest equal to ci.yml; Renovate bumps both):
#   docker run --rm -u root -v "$PWD":/src -w /src \
#     ghcr.io/linuxmuster/lmndev-runner:24.04@sha256:6b0c8ac994cf1d2da44b0b36ebd7b3b125094886e78293e04b9db5e515b8209e \
#     bash -c 'apt-get update -qq && apt-get install -y -qq python3-venv && make deb'
# The version comes from debian/changelog (dpkg-parsechangelog); VERSION=<x> overrides it.

.PHONY: deb clean

deb:
	bash packaging/build-deb.sh

clean:
	rm -f linuxmuster-radius_*.deb
	rm -rf controlplane/build controlplane/*.egg-info
