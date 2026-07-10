#!/bin/sh

# SPDX-FileCopyrightText: Kevin Stenzel
#
# SPDX-License-Identifier: GPL-3.0-or-later

# One-shot fixture seeder for the E2E Samba AD DC. Runs in the background from
# dc/entrypoint.sh, waits for the DC to come fully up, then creates the exact
# users/groups/accounts/DNS record the matrix needs. Idempotent-ish: it guards
# each create so a container restart (with a persisted volume) does not error.
#
# NOT the linuxmuster way. In production Sophomorix owns the users/groups/wifi
# gate and the RADIUS machine account is registered via devices.csv (role=server)
# + linuxmuster-import-devices. Here we provision everything directly; the join
# account is a plain Domain Admin so `net ads join` CREATES the computer account
# (the devices.csv ADOPTION path is NICHT VERIFIZIERT — see docs/radius-and-ad.md §2).
set -eu

: "${REALM:?}"
: "${ADMIN_PASSWORD:?}"
: "${USER_PASSWORD:?}"
: "${JOIN_USER:?}"
: "${JOIN_PASSWORD:?}"
: "${BIND_USER:?}"
: "${BIND_PASSWORD:?}"
: "${RADIUS_FQDN:?}"
: "${RADIUS_IP:?}"

DNS_DOMAIN="$(printf '%s' "${REALM}" | tr '[:upper:]' '[:lower:]')"
ADMIN="Administrator%${ADMIN_PASSWORD}"
RADIUS_HOST="${RADIUS_FQDN%%.*}"          # "radius" from "radius.example.lmn"

# ---- wait until the DC answers RPC + DNS (proves it is fully up) ----
i=0
until samba-tool dns query 127.0.0.1 "${DNS_DOMAIN}" @ SOA -U "${ADMIN}" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "${i}" -ge 120 ]; then
        echo "dc-bootstrap: DC did not become ready in time." >&2
        exit 1
    fi
    sleep 2
done
echo "dc-bootstrap: DC is up; seeding fixtures ..." >&2

# ---- relax the password policy so the scripted test passwords are accepted ----
samba-tool domain passwordsettings set \
    --complexity=off --min-pwd-length=1 --min-pwd-age=0 --max-pwd-age=0 >/dev/null

# ---- groups: the base WLAN gate + the two role groups ----
for g in wifi teachers students; do
    if ! samba-tool group list | grep -qxF "${g}"; then
        samba-tool group add "${g}" >/dev/null
    fi
done

# ---- users ----
add_user() {
    if ! samba-tool user list | grep -qxF "$1"; then
        samba-tool user create "$1" "$2" >/dev/null
    fi
}
add_user teacher1        "${USER_PASSWORD}"
add_user student1        "${USER_PASSWORD}"
add_user nowifi1         "${USER_PASSWORD}"
add_user "${JOIN_USER}"  "${JOIN_PASSWORD}"
add_user "${BIND_USER}"  "${BIND_PASSWORD}"

# ---- memberships (addmembers is a no-op error if already a member -> ignore) ----
#   teacher1: wifi + teachers   student1: wifi + students   nowifi1: neither
samba-tool group addmembers wifi     teacher1,student1 >/dev/null 2>&1 || true
samba-tool group addmembers teachers teacher1          >/dev/null 2>&1 || true
samba-tool group addmembers students student1          >/dev/null 2>&1 || true

# ---- join account: Domain Admin so `net ads join` may create RADIUS$ ----
samba-tool group addmembers "Domain Admins" "${JOIN_USER}" >/dev/null 2>&1 || true

# ---- forward A-record for the RADIUS member's FQDN ----
if ! samba-tool dns query 127.0.0.1 "${DNS_DOMAIN}" "${RADIUS_HOST}" A -U "${ADMIN}" >/dev/null 2>&1; then
    samba-tool dns add 127.0.0.1 "${DNS_DOMAIN}" "${RADIUS_HOST}" A "${RADIUS_IP}" -U "${ADMIN}" >/dev/null
fi

# ---- readiness marker (healthcheck watches for it) ----
touch /var/lib/samba/.e2e-ready
echo "dc-bootstrap: fixtures ready (groups=wifi/teachers/students, users=teacher1/student1/nowifi1, ${RADIUS_HOST}.${DNS_DOMAIN} -> ${RADIUS_IP})." >&2
