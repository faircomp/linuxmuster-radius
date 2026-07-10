#!/usr/bin/env bash

# SPDX-FileCopyrightText: Kevin Stenzel
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Mint the EAP CA + server certificate for the E2E, with openssl.
#
# This MIRRORS what the control plane's `lmnradius cert issue` emits
# (controlplane/lmnradius/ca.py) so the image under test loads a cert with the
# same shape it will see in production — but it is deliberately a SEPARATE,
# openssl-based path: the control-plane crypto is already unit-tested
# (controlplane/tests/test_ca.py); here we only need cert MATERIAL on disk.
#
# What it reproduces (see ca.py):
#   * CA: RSA-4096, self-signed, basicConstraints CA:TRUE, keyCertSign|cRLSign.
#   * server: RSA-2048, signed by the CA, CN = FQDN, SAN = DNS:FQDN (no wildcard),
#     EKU serverAuth (1.3.6.1.5.5.7.3.1) + id-kp-eapOverLAN (1.3.6.1.5.5.7.3.14).
# Output (mounted read-only into the radius service at /run/secrets/eap/*, and
# ca.pem into the client for server-cert pinning):
#   out/ca.pem      out/server.pem      out/server.key
#
# Run this ONCE before `docker compose up` (the compose bind-mounts out/*).
# Idempotent: re-running overwrites out/ with a fresh CA + cert.
set -euo pipefail

FQDN="${1:-radius.example.lmn}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${HERE}/out"
mkdir -p "${OUT}"

echo "certs: minting EAP CA + server cert for ${FQDN} into ${OUT} (TEST-ONLY) ..." >&2

# ---- CA (RSA-4096, ~10y) -----------------------------------------------------
openssl req -x509 -newkey rsa:4096 -sha256 -days 3652 -nodes \
    -keyout "${OUT}/ca.key" -out "${OUT}/ca.pem" \
    -subj "/CN=linuxmuster-radius EAP CA (E2E TEST)" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null

# ---- server key + CSR (RSA-2048) ---------------------------------------------
openssl req -newkey rsa:2048 -sha256 -nodes \
    -keyout "${OUT}/server.key" -out "${OUT}/server.csr" \
    -subj "/CN=${FQDN}" 2>/dev/null

# ---- sign the server cert with the EAP EKUs + SAN (~3y) ----------------------
# extendedKeyUsage: serverAuth = 1.3.6.1.5.5.7.3.1 (Windows requires exactly this);
#                   1.3.6.1.5.5.7.3.14 = id-kp-eapOverLAN (802.1X, best-practice).
cat > "${OUT}/server-ext.cnf" <<EOF
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth,1.3.6.1.5.5.7.3.14
subjectAltName = DNS:${FQDN}
EOF

openssl x509 -req -in "${OUT}/server.csr" \
    -CA "${OUT}/ca.pem" -CAkey "${OUT}/ca.key" -CAcreateserial \
    -days 1095 -sha256 -extfile "${OUT}/server-ext.cnf" \
    -out "${OUT}/server.pem" 2>/dev/null

chmod 0600 "${OUT}/server.key" "${OUT}/ca.key"
rm -f "${OUT}/server.csr" "${OUT}/server-ext.cnf" "${OUT}/ca.srl"

echo "certs: done." >&2
echo "  CA     : ${OUT}/ca.pem" >&2
echo "  server : ${OUT}/server.pem (CN=SAN=${FQDN}, EKU serverAuth+eapOverLAN)" >&2
echo "  key    : ${OUT}/server.key" >&2
