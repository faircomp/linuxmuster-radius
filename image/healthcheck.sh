#!/bin/sh

# SPDX-FileCopyrightText: Kevin Stenzel
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Container healthcheck. Returns 0 only if BOTH backends are proven up -- the
# analogue of linuxmuster-squid's "a 407 proves Squid is alive AND enforcing":
#
#   1. `wbinfo -t` succeeds  -> the winbind secure channel to the AD DC is
#      healthy, i.e. the NT-hash auth backend that PEAP-MSCHAPv2 depends on is
#      joined and reachable (mschap -> ntlm_auth -> winbindd -> DC).
#   2. A RADIUS Status-Server probe to 127.0.0.1:1812 gets a reply -> radiusd is
#      actually listening and answering.
#
# Neither check alone is sufficient: a live winbind with a dead radiusd (or vice
# versa) still cannot authenticate a real WLAN client. Only the pair does.
set -eu

# Shared secret for the loopback Status-Server probe. The entrypoint renders a
# matching `client 127.0.0.1/32 { secret = ... }` and enables Status-Server on
# the auth listener from the SAME env default, so probe and config always agree.
# This traffic never leaves the container's own network namespace.
: "${HEALTHCHECK_SECRET:=lmnradius-loopback}"

# 1) Domain trust / winbind secure channel to the DC.
wbinfo -t >/dev/null 2>&1 || exit 1

# 2) RADIUS listener. A Status-Server request (RFC 5997) must get a reply that
# verifies against the shared secret. Message-Authenticator is mandatory in
# Status-Server packets; "= 0x00" makes radclient add and sign it. A dead
# listener or a mismatched secret yields no verified reply -> no "Received"
# line -> non-zero exit.
echo "Message-Authenticator = 0x00" \
    | radclient -t 3 -r 1 -x 127.0.0.1:1812 status "${HEALTHCHECK_SECRET}" 2>/dev/null \
    | grep -q 'Received'
