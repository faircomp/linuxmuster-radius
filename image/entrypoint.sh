#!/bin/sh

# SPDX-FileCopyrightText: Kevin Stenzel
#
# SPDX-License-Identifier: GPL-3.0-or-later

# linuxmuster-radius entrypoint: assemble the FreeRADIUS config on tmpfs, join the
# Samba AD as a member, then run winbindd + radiusd under a small supervisor.
#
# WHY ASSEMBLE ON TMPFS: the rootfs is read-only, so we cannot edit /etc/freeradius
# in place. We copy the baked config tree to ${RADDB} (tmpfs), repoint FreeRADIUS'
# writable dirs (raddbdir/logdir/run_dir) at tmpfs, overlay the rendered per-instance
# mods/sites, and run `radiusd -d ${RADDB}`. Nothing under /etc is written at runtime;
# the only persistent state is the /var/lib/samba volume (the machine-account secret
# from the domain join).
#
# Pitfall (baked in): the container hostname MUST equal ${SERVER_FQDN} and its forward
# DNS must resolve inside the container (the control plane sets both), or the Kerberos
# SPN canonicalisation and the AD join fail.
set -eu

# ---- required per-instance configuration (abort if missing) ----
# Note: keep the :? messages free of apostrophes/parentheses so shellcheck's quote
# tracking is not thrown off by the default-word.
: "${INSTANCE:?INSTANCE is required - instance name, e.g. default-school}"
: "${REALM:?REALM is required - UPPERCASE Kerberos realm, e.g. LINUXMUSTER.SCHULE.DE}"
: "${WORKGROUP:?WORKGROUP is required - NetBIOS short domain, e.g. LINUXMUSTER}"
: "${SERVER_FQDN:?SERVER_FQDN is required - this server FQDN, MUST equal the container hostname}"
: "${LDAP_SERVER:?LDAP_SERVER is required - e.g. ldaps://dc.fqdn}"
: "${LDAP_BASE_DN:?LDAP_BASE_DN is required - e.g. ou=SCHOOLS,dc=...}"
: "${LDAP_BIND_DN:?LDAP_BIND_DN is required - e.g. cn=global-binduser,ou=Management,ou=GLOBAL,dc=...}"

# Secret FILE paths (mounted read-only; these hold PATHS, never the secrets themselves).
: "${LDAP_BIND_SECRET:?LDAP_BIND_SECRET is required - path to the global-binduser password file}"
: "${JOIN_SECRET:?JOIN_SECRET is required - path to the domain-join authfile in samba -A format}"
: "${EAP_CA:?EAP_CA is required - path to the EAP CA certificate PEM}"
: "${EAP_CERT:?EAP_CERT is required - path to the EAP server certificate PEM}"
: "${EAP_KEY:?EAP_KEY is required - path to the EAP server private key PEM}"

# ---- optional, with sane defaults ----
: "${WIFI_GROUP:=wifi}"                  # base WLAN gate group (default-school: 'wifi')
: "${SERVICE_USER:=freerad}"             # the user radiusd drops to (Ubuntu default)
: "${WINBIND_WAIT:=60}"                  # bounded winbind-trust wait, in seconds
: "${HEALTHCHECK_SECRET:=lmnradius-loopback}"  # loopback Status-Server probe secret

RUN=/run/lmnradius
BAKED=/etc/freeradius/3.0
TPL="${BAKED}/templates"
RADDB="${RUN}/raddb"
# Control-plane-mounted per-instance config (clients.conf + ssid-policy), read-only.
# The P2 control plane renders these from the instance's client_subnets[] and ssids[].
MOUNT_D=/etc/lmnradius/instance.d
SMB_CONF="${RUN}/smb.conf"
STATEDIR=/var/lib/samba                  # persistent volume: machine secret (secrets.tdb)
# winbindd's privileged pipe lives under the smb.conf 'state directory' (= /var/lib/samba)
# as <statedir>/winbindd_privileged (Samba winbindd.8). ntlm_auth (run by radiusd as
# ${SERVICE_USER}) needs group access; winbindd self-provisions it root:winbindd_priv 0750
# and the entrypoint re-asserts that below.
PRIV_DIR="${STATEDIR}/winbindd_privileged"

# ---- helpers ----

# copy_secret SRC DST MODE OWNER — copy a mounted secret onto tmpfs with tight perms.
# chmod BEFORE chown: with CAP_FOWNER dropped, root can change the mode only while it
# still owns the copy. Uses cp (never cat) so no secret content ever reaches a log.
copy_secret() {
    _src="$1"; _dst="$2"; _mode="$3"; _owner="$4"
    if [ ! -r "${_src}" ]; then
        echo "FATAL: secret '${_src}' is missing or not readable (mount it read-only)." >&2
        exit 1
    fi
    cp "${_src}" "${_dst}"
    chmod "${_mode}" "${_dst}"
    chown "${_owner}" "${_dst}"
}

# render SRC DST — envsubst SRC to DST with the explicit ${ALLOW} allow-list, so that
# FreeRADIUS %{...} expansions and unlang ${...} config references survive untouched.
render() { envsubst "${ALLOW}" < "$1" > "$2"; }

# ---- writable directories (tmpfs on a read-only rootfs) ----
mkdir -p "${RUN}/eap" "${RADDB}" "${RUN}/log/radacct" "${RUN}/run" \
         /run/samba "${PRIV_DIR}" "${STATEDIR}/private"

# ---- Kerberos + LDAP client env ----
# rdns / dns_canonicalize_hostname off so the DC principal is taken literally (via SRV),
# not via reverse DNS. ccache + replay cache onto tmpfs so a read-only rootfs cannot
# break the join. SASL_NOCANON keeps rlm_ldap's GSSAPI/LDAPS bind from reverse-DNS'ing
# the DC name.
export KRB5_CONFIG="${RUN}/krb5.conf"
export KRB5CCNAME="FILE:${RUN}/krb5cc_${INSTANCE}"
export KRB5RCACHEDIR="${RUN}"
export LDAPCONF="${RUN}/ldap.conf"
printf 'SASL_NOCANON on\n' > "${LDAPCONF}"

# ---- copy EAP cert material onto tmpfs, then point the module vars at the copies ----
# (mods/eap.template references ${EAP_CA} ${EAP_CERT} ${EAP_KEY} directly.)
copy_secret "${EAP_CA}"   "${RUN}/eap/ca.pem"     0644 "${SERVICE_USER}:${SERVICE_USER}"
copy_secret "${EAP_CERT}" "${RUN}/eap/server.pem" 0644 "${SERVICE_USER}:${SERVICE_USER}"
copy_secret "${EAP_KEY}"  "${RUN}/eap/server.key" 0600 "${SERVICE_USER}:${SERVICE_USER}"
EAP_CA="${RUN}/eap/ca.pem"
EAP_CERT="${RUN}/eap/server.pem"
EAP_KEY="${RUN}/eap/server.key"

# ---- LDAP bind password: read the literal into a var (rlm_ldap needs the value, not a
# path). It is only ever written into the rendered ldap mod on tmpfs, never echoed. ----
if [ ! -r "${LDAP_BIND_SECRET}" ]; then
    echo "FATAL: LDAP_BIND_SECRET '${LDAP_BIND_SECRET}' is missing or not readable." >&2
    exit 1
fi
LDAP_BIND_PW="$(cat "${LDAP_BIND_SECRET}")"

# ---- domain-join credential: a samba authfile (username=/password=/domain=), used ONCE
# by this script as root, never by radiusd -> stays root-only. ----
JOIN_AUTH_FILE="${RUN}/join.authfile"
copy_secret "${JOIN_SECRET}" "${JOIN_AUTH_FILE}" 0600 root:root

# lowercase DNS form of the realm (krb5.conf [realms]/[domain_realm]).
DNS_DOMAIN="$(printf '%s' "${REALM}" | tr '[:upper:]' '[:lower:]')"

# ---- render env-driven config from templates ----
export INSTANCE REALM WORKGROUP SERVER_FQDN DNS_DOMAIN SMB_CONF
export LDAP_SERVER LDAP_BASE_DN LDAP_BIND_DN LDAP_BIND_PW WIFI_GROUP
export EAP_CA EAP_CERT EAP_KEY
# The single quotes are intentional: envsubst needs the literal ${VAR} tokens as its
# allow-list argument, so the shell must NOT expand them here.
# shellcheck disable=SC2016
ALLOW='${INSTANCE} ${REALM} ${WORKGROUP} ${SERVER_FQDN} ${DNS_DOMAIN} ${SMB_CONF} ${LDAP_SERVER} ${LDAP_BASE_DN} ${LDAP_BIND_DN} ${LDAP_BIND_PW} ${WIFI_GROUP} ${EAP_CA} ${EAP_CERT} ${EAP_KEY}'

render "${TPL}/smb.conf.template"  "${SMB_CONF}"
render "${TPL}/krb5.conf.template" "${KRB5_CONFIG}"

# ---- assemble the FreeRADIUS config tree on tmpfs ----
cp -a "${BAKED}/." "${RADDB}/"
rm -rf "${RADDB}/templates"          # not needed inside the running tree
# Repoint FreeRADIUS' writable dirs at tmpfs so a read-only rootfs cannot break it and
# so ${confdir}/${raddbdir} resolve to the assembled tree (not the read-only /etc copy).
sed -i \
    -e "s|^raddbdir = .*|raddbdir = ${RADDB}|" \
    -e "s|^logdir = .*|logdir = ${RUN}/log|" \
    -e "s|^run_dir = .*|run_dir = ${RUN}/run|" \
    "${RADDB}/radiusd.conf"
# Fail loudly if the sed did not match: a future radiusd.conf format change would else
# silently leave raddbdir/confdir pointing at the read-only /etc tree.
grep -qx "raddbdir = ${RADDB}" "${RADDB}/radiusd.conf" || {
    echo "FATAL: could not repoint 'raddbdir' in radiusd.conf (unexpected format)." >&2
    exit 1
}

# Overlay the rendered per-instance mods (replace the stock symlinks with real files).
for _m in eap ldap mschap; do
    rm -f "${RADDB}/mods-enabled/${_m}"
    render "${TPL}/mods/${_m}.template" "${RADDB}/mods-enabled/${_m}"
done
# Overlay the rendered virtual servers (replace the stock symlinks).
for _s in default inner-tunnel; do
    rm -f "${RADDB}/sites-enabled/${_s}"
    render "${TPL}/sites/${_s}.template" "${RADDB}/sites-enabled/${_s}"
done

# Per-instance data (clients.conf = AP subnets, ssid-policy = per-SSID gate). The
# control plane mounts these read-only at ${MOUNT_D}; fall back to the shipped default
# ssid-policy so 'radiusd -XC' passes on the image alone. inner-tunnel's
# '$-INCLUDE ${confdir}/instance.d/ssid-policy' and the clients include below both
# resolve into this assembled tree.
mkdir -p "${RADDB}/instance.d"
if [ -f "${MOUNT_D}/ssid-policy" ]; then
    cp "${MOUNT_D}/ssid-policy" "${RADDB}/instance.d/ssid-policy"
else
    cp "${TPL}/instance.d/ssid-policy" "${RADDB}/instance.d/ssid-policy"
fi
if [ -f "${MOUNT_D}/clients.conf" ]; then
    cp "${MOUNT_D}/clients.conf" "${RADDB}/instance.d/clients.conf"
fi

# Loopback client for the healthcheck Status-Server probe + optional include of the
# mounted AP clients. Appended to the assembled clients.conf (loaded by radiusd.conf).
{
    printf 'client healthcheck-loopback {\n'
    printf '    ipaddr = 127.0.0.1\n'
    printf '    secret = %s\n' "${HEALTHCHECK_SECRET}"
    printf '    proto = udp\n'
    printf '    require_message_authenticator = auto\n'
    printf '    shortname = healthcheck\n'
    printf '}\n'
    # shellcheck disable=SC2016
    printf '$-INCLUDE ${confdir}/instance.d/clients.conf\n'
} >> "${RADDB}/clients.conf"

# radiusd reads config as root then drops to ${SERVICE_USER}; it must own the tmpfs
# config + the writable log/run dirs. The rendered ldap mod carries the bind password.
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${RADDB}" "${RUN}/log" "${RUN}/run"
chmod 0640 "${RADDB}/mods-enabled/ldap"

# ---- domain join (member) — only if not already joined ----
# net ads testjoin verifies the machine secret + secure channel WITHOUT winbindd. A
# populated /var/lib/samba volume => already joined => never re-join. 'net ads join
# MEMBER' adopts the pre-created computer account (the linuxmuster devices.csv +
# linuxmuster-import-devices path) instead of creating a fresh one.
if net ads testjoin --configfile="${SMB_CONF}" >/dev/null 2>&1; then
    echo "linuxmuster-radius: already joined to ${REALM} (machine secret present)." >&2
else
    echo "linuxmuster-radius: joining ${REALM} as a member (one-time)..." >&2
    net ads join MEMBER --configfile="${SMB_CONF}" -A "${JOIN_AUTH_FILE}"
    echo "linuxmuster-radius: domain join completed." >&2
fi

# ---- finalize the winbind privileged pipe perms ----
# The dir must stay root-owned; group 'winbindd_priv' + mode 0750 grants ${SERVICE_USER}
# access. That user's membership in winbindd_priv is a BUILD-TIME step (usermod -aG),
# because /etc is read-only here — we only re-assert the dir perms (on the /var/lib/samba
# volume; winbindd normally provisions it correctly itself).
mkdir -p "${PRIV_DIR}"
if getent group winbindd_priv >/dev/null 2>&1; then
    chmod 0750 "${PRIV_DIR}"
    chown root:winbindd_priv "${PRIV_DIR}"
    if ! getent group winbindd_priv | cut -d: -f4 | tr ',' '\n' | grep -qx "${SERVICE_USER}"; then
        echo "WARN: '${SERVICE_USER}' is not in group 'winbindd_priv'; PEAP-MSCHAPv2 will fail until it is added at build time." >&2
    fi
else
    echo "WARN: group 'winbindd_priv' is missing; the winbind package must create it at build time." >&2
fi

# ---- resolve the radiusd binary ----
# On Ubuntu 24.04 the binary is 'freeradius'; there is NO 'radiusd' symlink. Accept
# either for portability across distros.
RADIUSD="$(command -v freeradius || command -v radiusd || true)"
[ -n "${RADIUSD}" ] || { echo "FATAL: neither 'freeradius' nor 'radiusd' is on PATH." >&2; exit 1; }

# ---- start winbindd (foreground child), wait for the secure channel ----
# Only winbindd is needed for ntlm_auth — smbd/nmbd (file sharing) stay off.
winbindd -F --configfile="${SMB_CONF}" &
WB=$!

_waited=0
until wbinfo -t >/dev/null 2>&1; do
    if ! kill -0 "${WB}" 2>/dev/null; then
        echo "FATAL: winbindd exited before the trust to ${REALM} came up." >&2
        exit 1
    fi
    _waited=$((_waited + 1))
    if [ "${_waited}" -ge "${WINBIND_WAIT}" ]; then
        echo "FATAL: winbind trust ('wbinfo -t') not established within ${WINBIND_WAIT}s." >&2
        kill "${WB}" 2>/dev/null || true
        exit 1
    fi
    sleep 1
done
echo "linuxmuster-radius: winbind trust to ${REALM} is up." >&2

# ---- FreeRADIUS config check ----
if ! "${RADIUSD}" -XC -d "${RADDB}" >/dev/null 2>&1; then
    echo "FATAL: FreeRADIUS configuration check ('radiusd -XC') failed; details:" >&2
    "${RADIUSD}" -XC -d "${RADDB}" >&2 || true
    kill "${WB}" 2>/dev/null || true
    exit 1
fi

echo "linuxmuster-radius: instance='${INSTANCE}' realm='${REALM}' fqdn='${SERVER_FQDN}' wifi-group='${WIFI_GROUP}'" >&2

# ---- run radiusd + supervise both daemons ----
# Two long-running daemons: if EITHER winbindd or radiusd dies, tear the other down and
# exit non-zero so the orchestrator's restart policy (unless-stopped) recreates the
# container — a dead auth backend must not linger behind a live listener. tini (PID 1)
# forwards SIGTERM to this script; the trap forwards it to both daemons for a clean stop.
"${RADIUSD}" -f -l stdout -d "${RADDB}" &
RD=$!

trap 'kill "${WB}" "${RD}" 2>/dev/null || true; exit 0' TERM INT

while kill -0 "${WB}" 2>/dev/null && kill -0 "${RD}" 2>/dev/null; do
    sleep 5
done

echo "linuxmuster-radius: a core daemon exited; taking the container down for a restart." >&2
kill "${WB}" "${RD}" 2>/dev/null || true
wait "${RD}" 2>/dev/null || true
exit 1
