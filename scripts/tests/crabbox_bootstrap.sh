#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Hydrates a freshly leased crabbox box for the heavy tier (the FreeRADIUS E2E):
# install Docker + compose, ensure openssl (the E2E generates EAP certs on the
# host), pre-pull the E2E images, build the data-plane image and set up the
# Python venv for the control-plane fast tier. Idempotent -- safe to run
# repeatedly. Docker is invoked via sudo if the current user cannot yet reach the
# socket (a fresh SSH login does not have the docker group).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

echo "== crabbox bootstrap =="

# 1. Docker engine (bundles the compose v2 plugin) — install only if missing.
if ! command -v docker >/dev/null 2>&1; then
  echo "-- installing docker --"
  curl -fsSL https://get.docker.com | sh
fi
# Add the current user to the docker group (takes effect only in later sessions) — best effort.
sudo usermod -aG docker "$(id -un)" >/dev/null 2>&1 || true

# 2. openssl — the E2E runner generates the EAP CA + server cert on the host.
if ! command -v openssl >/dev/null 2>&1; then
  echo "-- installing openssl --"
  sudo apt-get install -y -q openssl >/dev/null 2>&1 || true
fi

# Choose the Docker invocation: directly, else via sudo (fresh login lacks the group).
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then DOCKER="sudo docker"; fi
echo "-- docker via: $DOCKER --"
$DOCKER version >/dev/null
$DOCKER compose version >/dev/null

# 3. Pre-pull the base image used by the E2E stack (best effort). The Samba AD DC
#    and the eapol_test client are BUILT (deploy/e2e/dc, deploy/e2e/client), not pulled.
$DOCKER pull ubuntu:24.04 || true   # base of the DC, the radius image and the client

# 4. Build the data-plane image (also validates the Dockerfile / entrypoint).
if [ -f image/Dockerfile ]; then
  echo "-- building linuxmuster-radius:dev --"
  $DOCKER build -t linuxmuster-radius:dev image/
else
  echo "-- skipping image build: image/Dockerfile missing --"
fi

# 5. Python toolchain (venv) for the control-plane fast tier, if code is present.
if [ -f controlplane/pyproject.toml ]; then
  echo "-- Python venv + control-plane deps --"
  sudo apt-get install -y -q python3-venv python3-pip >/dev/null 2>&1 || true
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  # ruff/mypy/pytest drive run.sh's lint+unit; reuse checks SPDX headers.
  .venv/bin/pip install --quiet ruff mypy pytest pytest-asyncio types-PyYAML reuse
  .venv/bin/pip install --quiet -e ./controlplane
fi

echo "== bootstrap done =="
