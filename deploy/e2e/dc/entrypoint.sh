#!/bin/sh

# SPDX-FileCopyrightText: Kevin Stenzel
#
# SPDX-License-Identifier: GPL-3.0-or-later

# E2E Samba AD DC entrypoint: provision the domain on first start, patch the
# DC-side 'ntlm auth' line, launch the AD DC daemon in the foreground, and kick
# off the one-shot fixture bootstrap in the background. NOT production code.
set -eu

: "${REALM:?REALM is required, e.g. EXAMPLE.LMN}"
: "${WORKGROUP:?WORKGROUP is required, e.g. EXAMPLE}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is required}"

DNS_DOMAIN="$(printf '%s' "${REALM}" | tr '[:upper:]' '[:lower:]')"
# The container's primary IPv4 (hostname -I lists real addresses, loopback excluded).
IP="$(hostname -I | awk '{print $1}')"

# Provision wants the DC's own FQDN to resolve locally; Docker only writes the
# short hostname into /etc/hosts, so add the FQDN too.
if ! grep -q "dc.${DNS_DOMAIN}" /etc/hosts; then
    printf '%s dc.%s dc\n' "${IP}" "${DNS_DOMAIN}" >> /etc/hosts
fi

if [ ! -f /var/lib/samba/private/sam.ldb ]; then
    echo "dc: provisioning realm=${REALM} workgroup=${WORKGROUP} ..." >&2
    # The stock Ubuntu smb.conf is a standalone/member config; provision refuses
    # to overwrite it, so remove it first.
    rm -f /etc/samba/smb.conf
    samba-tool domain provision \
        --server-role=dc \
        --dns-backend=SAMBA_INTERNAL \
        --realm="${REALM}" \
        --domain="${WORKGROUP}" \
        --adminpass="${ADMIN_PASSWORD}" \
        --host-name=dc \
        --host-ip="${IP}"          # pin dc.<realm> A-record to the container IP

    # PEAP-MSCHAPv2 needs this on the DC or the DC refuses the MSCHAPv2 NT-response
    # and every WLAN login fails (docs/radius-and-ad.md §4). The member sets it too.
    python3 - <<'PY'
path = "/etc/samba/smb.conf"
with open(path, "r", encoding="utf-8") as fh:
    text = fh.read()
if "ntlm auth" not in text:
    text = text.replace(
        "[global]",
        "[global]\n\tntlm auth = mschapv2-and-ntlmv2-only",
        1,
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
PY
    echo "dc: provisioning done." >&2
fi

# samba-tool / net use this Kerberos config (provision emits a matching one).
cp -f /var/lib/samba/private/krb5.conf /etc/krb5.conf 2>/dev/null || true

echo "dc: starting the AD DC daemon ..." >&2
# -i = interactive: stay in the foreground and log to stdout, so `docker compose
# logs dc` shows everything and the shell can supervise the PID. Uses the default
# config path /etc/samba/smb.conf that provision wrote.
samba -i &
SAMBA_PID=$!

# Seed the fixtures once the DC is up; it writes the /var/lib/samba/.e2e-ready
# marker last, which the compose healthcheck watches for.
/usr/local/bin/bootstrap.sh &

# Supervise the DC daemon; if it dies, the container exits (compose surfaces it).
wait "${SAMBA_PID}"
