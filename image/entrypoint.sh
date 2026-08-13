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
: "${LDAP_CA:=}"                         # optional: PEM CA to verify the DC's LDAPS cert (stunnel)
: "${LDAP_STUNNEL_PORT:=3890}"           # loopback port where stunnel exposes plaintext LDAP

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

# alive PID — true while process PID exists, WITHOUT signalling it. `kill -0` is unusable
# for daemon liveness here: radiusd drops to ${SERVICE_USER}, and the hardened run profile
# drops CAP_KILL, so the root entrypoint's `kill -0` on the freerad-owned radiusd returns
# EPERM (indistinguishable from "dead") even while it is running — which made the
# supervisor tear a perfectly healthy container down. Reading /proc/<pid> needs no signal
# and no capability. (Verified under --cap-drop ALL against a real Samba AD member,
# 2026-07-12.)
alive() { [ -e "/proc/$1" ]; }

# ---- writable directories (tmpfs on a read-only rootfs) ----
mkdir -p "${RUN}/eap" "${RADDB}" "${RUN}/log/radacct" "${RUN}/run" \
         /run/samba "${PRIV_DIR}" "${STATEDIR}/private"

# ---- Kerberos + LDAP client env ----
# rdns / dns_canonicalize_hostname off so the DC principal is taken literally (via SRV),
# not via reverse DNS. ccache + replay cache onto tmpfs so a read-only rootfs cannot
# break the join. SASL_NOCANON keeps the `net ads` GSSAPI bind (during the join) from
# reverse-DNS'ing the DC name. (rlm_ldap itself binds simple over the stunnel loopback.)
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

# ---- LDAP transport: route ldaps:// through a local stunnel (GnuTLS/OpenSSL fix) ----
# On Ubuntu, libldap is built against GnuTLS while FreeRADIUS links OpenSSL; when
# rlm_ldap opens an LDAPS/StartTLS connection in the THREADED server the two TLS stacks
# collide and radiusd segfaults seconds after "Ready to process requests" (reproduced
# against a real Samba AD DC, 2026-07-12). So rlm_ldap ALWAYS talks plaintext LDAP, and
# when LDAP_SERVER is ldaps:// we terminate TLS in a local stunnel on the loopback and
# point rlm_ldap at it. libldap never touches TLS -> the conflict cannot happen.
STUNNEL_LDAP_CONF=""                       # non-empty => a tunnel must be started + supervised
LDAP_SERVER_EFFECTIVE="${LDAP_SERVER}"
_scheme="$(printf '%s' "${LDAP_SERVER}" | sed -n 's|^\([A-Za-z][A-Za-z0-9+.-]*\)://.*|\1|p' | tr '[:upper:]' '[:lower:]')"
case "${_scheme}" in
  ldaps)
    # Only a single ldaps:// URL is wrapped (linuxmuster is a single DC); a
    # space-separated server list is not supported by the TLS shim.
    if printf '%s' "${LDAP_SERVER}" | grep -q ' '; then
        echo "FATAL: LDAP_SERVER lists multiple servers; the stunnel TLS shim supports a single ldaps:// DC only." >&2
        exit 1
    fi
    _hostport="$(printf '%s' "${LDAP_SERVER}" | sed -e 's|^[A-Za-z][A-Za-z0-9+.-]*://||' -e 's|/.*$||')"
    _dchost="${_hostport%%:*}"
    _dcport="${_hostport##*:}"
    [ "${_dcport}" = "${_hostport}" ] && _dcport=636   # no explicit port -> LDAPS default
    [ -n "${_dchost}" ] || { echo "FATAL: could not parse a host from LDAP_SERVER='${LDAP_SERVER}'." >&2; exit 1; }
    STUNNEL_LDAP_CONF="${RUN}/stunnel-ldap.conf"
    LDAP_SERVER_EFFECTIVE="ldap://127.0.0.1:${LDAP_STUNNEL_PORT}"
    # stunnel client config on tmpfs. Verify the DC cert only when a CA is mounted
    # (LDAP_CA); otherwise connect without verification, matching the prior rlm_ldap
    # 'require_cert = allow' posture on the trusted RADIUS<->DC link. Empty pid = no
    # pidfile (read-only rootfs); foreground + syslog=no => logs go to stderr.
    {
        printf 'foreground = yes\n'
        printf 'pid =\n'
        printf 'syslog = no\n'
        printf 'sslVersionMin = TLSv1.2\n'
        printf '[ldap]\n'
        printf 'client = yes\n'
        printf 'accept = 127.0.0.1:%s\n' "${LDAP_STUNNEL_PORT}"
        printf 'connect = %s:%s\n' "${_dchost}" "${_dcport}"
        if [ -n "${LDAP_CA}" ] && [ -r "${LDAP_CA}" ]; then
            printf 'CAfile = %s\n' "${LDAP_CA}"
            printf 'verifyChain = yes\n'
            printf 'checkHost = %s\n' "${_dchost}"
        elif [ -n "${LDAP_CA}" ]; then
            echo "WARN: LDAP_CA='${LDAP_CA}' is set but not readable; connecting to the DC WITHOUT certificate verification." >&2
        fi
    } > "${STUNNEL_LDAP_CONF}"
    echo "linuxmuster-radius: LDAPS to ${_dchost}:${_dcport} is tunnelled via stunnel on 127.0.0.1:${LDAP_STUNNEL_PORT}." >&2
    ;;
  ldap|"")
    : # plaintext LDAP straight through; libldap does no TLS, so there is no crash
    ;;
  *)
    echo "WARN: unrecognised LDAP_SERVER scheme '${_scheme}'; passing it through unchanged (no stunnel)." >&2
    ;;
esac

# ---- render env-driven config from templates ----
export INSTANCE REALM WORKGROUP SERVER_FQDN DNS_DOMAIN SMB_CONF
export LDAP_SERVER_EFFECTIVE LDAP_BASE_DN LDAP_BIND_DN LDAP_BIND_PW WIFI_GROUP
export EAP_CA EAP_CERT EAP_KEY
# The single quotes are intentional: envsubst needs the literal ${VAR} tokens as its
# allow-list argument, so the shell must NOT expand them here.
# shellcheck disable=SC2016
ALLOW='${INSTANCE} ${REALM} ${WORKGROUP} ${SERVER_FQDN} ${DNS_DOMAIN} ${SMB_CONF} ${LDAP_SERVER_EFFECTIVE} ${LDAP_BASE_DN} ${LDAP_BIND_DN} ${LDAP_BIND_PW} ${WIFI_GROUP} ${EAP_CA} ${EAP_CERT} ${EAP_KEY}'

render "${TPL}/smb.conf.template"  "${SMB_CONF}"
render "${TPL}/krb5.conf.template" "${KRB5_CONFIG}"

# ---- assemble the FreeRADIUS config tree on tmpfs ----
# -dR --preserve=mode (NOT `cp -a`): the hardened run profile drops CAP_FOWNER, so a
# preserve-all copy that chowns files to freerad can no longer chmod/utime them
# ("Operation not permitted"). We keep symlinks + mode only; the chown to the service
# user happens once, below. (Verified against a real Samba AD member, 2026-07-12.)
cp -dR --preserve=mode "${BAKED}/." "${RADDB}/"
rm -rf "${RADDB}/templates"          # not needed inside the running tree
# Repoint FreeRADIUS' writable dirs at tmpfs so a read-only rootfs cannot break it and
# so ${confdir}/${raddbdir} resolve to the assembled tree (not the read-only /etc copy).
sed -i \
    -e "s|^raddbdir = .*|raddbdir = ${RADDB}|" \
    -e "s|^logdir = .*|logdir = ${RUN}/log|" \
    -e "s|^run_dir = .*|run_dir = ${RUN}/run|" \
    "${RADDB}/radiusd.conf"
# Log every auth DECISION ("Login OK"/"Login incorrect" + reason + username): the stock
# default `auth = no` leaves the operator blind — on a real first deployment the admin
# could not tell from `lmnradius logs` whether a WLAN attempt was accepted or why it was
# rejected. Usernames are operational data, not secrets; the password knobs
# (auth_badpass/auth_goodpass) stay `no` — this sed matches ONLY the exact `auth = no`.
sed -i -E 's|^([[:space:]]*)auth = no$|\1auth = yes|' "${RADDB}/radiusd.conf"
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
# mounted AP clients. We OVERWRITE (not append to) the assembled clients.conf: the stock
# file already defines a `client localhost` on 127.0.0.1, and FreeRADIUS refuses a second
# client on the same IP ("Failed to add duplicate client"). The stock localhost/example
# clients are not needed — the real NAS clients come from instance.d/clients.conf.
# (Verified against a real Samba AD member, 2026-07-12.)
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
} > "${RADDB}/clients.conf"

# Local realms for identity canonicalisation. Windows-SSO sends 'DOMAIN\user', UPN
# clients 'user@realm'; the inner-tunnel ntdomain/suffix policies split those into
# Stripped-User-Name ONLY if the parsed realm is defined here as a LOCAL realm (empty
# block = strip, keep local, never proxy — rlm_realm docs). Bare sAMAccountNames are
# untouched, so both qualified and unqualified logins work. The DNS-domain realm is
# skipped when it equals the workgroup (single-label domains) — a duplicate realm
# would fail the config check.
{
    printf 'realm %s {\n}\n' "${WORKGROUP}"
    if [ "$(printf '%s' "${DNS_DOMAIN}" | tr '[:lower:]' '[:upper:]')" != "${WORKGROUP}" ]; then
        printf 'realm %s {\n}\n' "${DNS_DOMAIN}"
    fi
} >> "${RADDB}/proxy.conf"

# The rendered ldap mod carries the bind password — restrict it BEFORE the chown, while
# it is still root-owned: the hardened profile drops CAP_FOWNER, so root cannot chmod a
# file once chown has handed it to freerad. radiusd reads config as root then drops to
# ${SERVICE_USER}, which must own the tmpfs config + the writable log/run dirs.
chmod 0640 "${RADDB}/mods-enabled/ldap"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${RADDB}" "${RUN}/log" "${RUN}/run"

# ---- domain join (member) — only if not already joined ----
# net ads testjoin verifies the machine secret + secure channel WITHOUT winbindd. A
# populated /var/lib/samba volume => already joined => never re-join. 'net ads join'
# joins as a MEMBER by default; the -A authfile supplies a join-capable account. A
# pre-created computer account (the linuxmuster devices.csv path) is adopted by name —
# there is NO 'MEMBER' positional for 'net ads' (that is 'net rpc join'), and passing it
# makes net treat MEMBER as the domain. (Verified against a real Samba AD, 2026-07-12.)
if net ads testjoin --configfile="${SMB_CONF}" >/dev/null 2>&1; then
    echo "linuxmuster-radius: already joined to ${REALM} (machine secret present)." >&2
else
    echo "linuxmuster-radius: joining ${REALM} as a member (one-time)..." >&2
    net ads join --configfile="${SMB_CONF}" -A "${JOIN_AUTH_FILE}"
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
    if ! alive "${WB}"; then
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

# ---- start the LDAP TLS stunnel (only for ldaps://), BEFORE radiusd instantiates ----
# rlm_ldap opens its connection pool the moment radiusd (or -XC) instantiates the module,
# so the loopback tunnel must already be listening or those binds fail.
STUN=""
if [ -n "${STUNNEL_LDAP_CONF}" ]; then
    stunnel4 "${STUNNEL_LDAP_CONF}" &
    STUN=$!
    if command -v ss >/dev/null 2>&1; then
        _sw=0
        until ss -Hltn 2>/dev/null | grep -qE "127\.0\.0\.1:${LDAP_STUNNEL_PORT}([^0-9]|\$)"; do
            if ! alive "${STUN}"; then
                echo "FATAL: stunnel exited before the LDAP tunnel on 127.0.0.1:${LDAP_STUNNEL_PORT} came up." >&2
                kill "${WB}" 2>/dev/null || true
                exit 1
            fi
            _sw=$((_sw + 1))
            [ "${_sw}" -ge 15 ] && break
            sleep 1
        done
    else
        sleep 2
        if ! alive "${STUN}"; then
            echo "FATAL: stunnel exited immediately after start." >&2
            kill "${WB}" 2>/dev/null || true
            exit 1
        fi
    fi
    echo "linuxmuster-radius: LDAP stunnel is listening on 127.0.0.1:${LDAP_STUNNEL_PORT}." >&2
fi

# ---- FreeRADIUS config check ----
if ! "${RADIUSD}" -XC -d "${RADDB}" >/dev/null 2>&1; then
    echo "FATAL: FreeRADIUS configuration check ('radiusd -XC') failed; details:" >&2
    "${RADIUSD}" -XC -d "${RADDB}" >&2 || true
    kill "${WB}" ${STUN} 2>/dev/null || true
    exit 1
fi

echo "linuxmuster-radius: instance='${INSTANCE}' realm='${REALM}' fqdn='${SERVER_FQDN}' wifi-group='${WIFI_GROUP}'" >&2

# ---- run radiusd + supervise the daemons ----
# Long-running daemons: winbindd, radiusd, and (for ldaps://) the LDAP stunnel. If ANY of
# them dies, tear the container down and exit non-zero so the orchestrator's restart policy
# (unless-stopped) recreates it — a dead auth backend or a dead LDAP tunnel must not linger
# behind a live listener. Liveness is by /proc/<pid> (see alive()), never `kill -0`, because
# radiusd runs as ${SERVICE_USER} and capless root cannot signal it.
#
# TEARDOWN model: we can signal winbindd + stunnel (root-owned), but NOT the freerad-owned
# radiusd (no CAP_KILL). That is fine: exiting makes tini (PID 1) leave, the PID namespace
# collapses, and the kernel SIGKILLs every remaining process (radiusd included). So we never
# `wait` on radiusd — a kill we are not permitted to send would otherwise hang the teardown.
# ${STUN} is empty when no tunnel is used, so it drops out of the unquoted expansions.
"${RADIUSD}" -f -l stdout -d "${RADDB}" &
RD=$!

trap 'kill "${WB}" ${STUN} 2>/dev/null || true; exit 0' TERM INT

while alive "${WB}" && alive "${RD}" && { [ -z "${STUN}" ] || alive "${STUN}"; }; do
    sleep 5
done

echo "linuxmuster-radius: a core daemon exited; taking the container down for a restart." >&2
kill "${WB}" ${STUN} 2>/dev/null || true
exit 1
