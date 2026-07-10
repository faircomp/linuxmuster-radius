#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# READ-ONLY discovery helper — run ON THE linuxmuster SAMBA-DC.
# Detects realm/workgroup (testparm -s, fallback: smb.conf), derives the base DN and
# the existing global-binduser bind DN, lists the 'wifi' gate group plus the
# teacher/student role groups (grouped per school), and prints a ready-to-run
# `lmnradius create` skeleton per school. Touches nothing, joins nothing, prints NO
# secret — the counterpart to linuxmuster-squid's discover-ad-facts.sh.
#
# Optional overrides:
#   REALM=LINUXMUSTER.LAN WORKGROUP=LINUXMUSTER WIFI_GROUP=wifi \
#     SMB_CONF=/etc/samba/smb.conf ./discover-ad-facts.sh
#
# Siblings: provision-radius-account.sh (Member-Registrierung + Secrets),
#           ../docs/radius-and-ad.md, ../docs/references.md (verifizierte Fakten),
#           ../controlplane/lmnradius/cli.py (die exakten `lmnradius create`-Flags).
set -uo pipefail

REALM="${REALM:-}"
WORKGROUP="${WORKGROUP:-}"
WIFI_GROUP="${WIFI_GROUP:-wifi}"
SMB_CONF="${SMB_CONF:-/etc/samba/smb.conf}"
BINDUSER_SECRET="${BINDUSER_SECRET:-/etc/linuxmuster/.secret/global-binduser}"
NTLM_REQUIRED="mschapv2-and-ntlmv2-only"

have() { command -v "$1" >/dev/null 2>&1; }

# --- realm/workgroup detection (defensive) -------------------------------------------
# Primary: testparm -s emits ONLY the effective value on stdout; diagnostics go to
# stderr (suppressed). Fallback: last matching 'key = value' line in smb.conf.
detect_smb_param() {
    have testparm || return 0
    testparm -s --parameter-name "$1" 2>/dev/null | tr -d '[:space:]'
}
detect_param_grep() {
    [ -r "$SMB_CONF" ] || return 0
    grep -iE "^[[:space:]]*$1[[:space:]]*=" "$SMB_CONF" 2>/dev/null \
        | tail -n1 | cut -d= -f2- | tr -d '[:space:]'
}
resolve_param() {
    v="$(detect_smb_param "$1")"
    [ -n "$v" ] || v="$(detect_param_grep "$1")"
    printf '%s' "$v"
}

# LINUXMUSTER.LAN -> DC=linuxmuster,DC=lan (deterministic AD convention).
base_dn_from_realm() {
    [ -n "$1" ] || return 0
    printf '%s\n' "$1" | tr '[:upper:]' '[:lower:]' | awk -F. '{
        out=""
        for (i = 1; i <= NF; i++) { out = out (i > 1 ? "," : "") "DC=" $i }
        print out
    }'
}

[ -n "$REALM" ]     || REALM="$(resolve_param realm)"
[ -n "$WORKGROUP" ] || WORKGROUP="$(resolve_param workgroup)"
REALM="$(printf '%s' "$REALM" | tr '[:lower:]' '[:upper:]')"
WORKGROUP="$(printf '%s' "$WORKGROUP" | tr '[:lower:]' '[:upper:]')"

if [ -n "$REALM" ]; then
    BASE_DN="$(base_dn_from_realm "$REALM")"
else
    REALM="<FILL: REALM, z. B. LINUXMUSTER.LAN>"
    BASE_DN="<FILL: base-dn, z. B. DC=linuxmuster,DC=lan>"
    echo "WARNUNG: realm nicht ermittelbar (testparm/smb.conf) — Platzhalter gesetzt." >&2
fi
[ -n "$WORKGROUP" ] || WORKGROUP="<FILL: WORKGROUP, z. B. LINUXMUSTER>"

BIND_DN="CN=global-binduser,OU=Management,OU=GLOBAL,${BASE_DN}"

DC_FQDN="$(hostname -f 2>/dev/null || true)"
[ -n "${DC_FQDN:-}" ] || DC_FQDN="<dc-fqdn>"
LDAP_SERVER="ldaps://${DC_FQDN}"

# --- group detection (defensive) -----------------------------------------------------
list_groups_raw() {
    have samba-tool || return 0
    samba-tool group list 2>/dev/null
}
# Role groups: exactly 'teachers'/'students' (default-school) OR '<school>-teachers'/
# '<school>-students' (per RFC of the task and linuxmuster's naming). 'wifi' is the
# separate WLAN gate group.
role_groups() {
    list_groups_raw | grep -E '^(teachers|students)$|-(teachers|students)$' | sort -u
}

ROLE_GROUPS="$(role_groups)"

# Unique schools derived from the role-group prefixes.
schools_of() {
    printf '%s\n' "$ROLE_GROUPS" | while IFS= read -r g; do
        [ -n "$g" ] || continue
        case "$g" in
            *-teachers) printf '%s\n' "${g%-teachers}" ;;
            *-students) printf '%s\n' "${g%-students}" ;;
            teachers|students) printf '%s\n' "default-school" ;;
        esac
    done | sort -u
}

group_present() { printf '%s\n' "$ROLE_GROUPS" | grep -Fxq "$1"; }

# --- report --------------------------------------------------------------------------
echo "== Erkannte AD-Fakten (read-only) =="
echo "  realm (--realm)              : ${REALM}"
echo "  workgroup (--workgroup)      : ${WORKGROUP}"
echo "  base DN (--ldap-base-dn)     : ${BASE_DN}"
echo "  bind DN (--ldap-bind-dn)     : ${BIND_DN}"
echo "  ldap server (--ldap-server)  : ${LDAP_SERVER}  (aus 'hostname -f'; ggf. anpassen)"
echo "  wifi gate group (--wifi-group): ${WIFI_GROUP}"
echo

if ! have samba-tool; then
    echo "WARNUNG: samba-tool nicht gefunden — Gruppen-Erkennung übersprungen." >&2
    echo "         Dieses Skript gehört auf den Samba-DC. Realm/DN oben stammen aus smb.conf." >&2
fi

echo "== WLAN-Gate-Gruppe =="
if ! have samba-tool; then
    echo "  (samba-tool fehlt — nicht prüfbar)"
elif list_groups_raw | grep -Fxq "$WIFI_GROUP"; then
    echo "  '${WIFI_GROUP}' vorhanden — jeder WLAN-User muss darin sein (--wifi-group ${WIFI_GROUP})."
else
    echo "  WARNUNG: Gruppe '${WIFI_GROUP}' NICHT gefunden — via Sophomorix prüfen"
    echo "           (sophomorix-managementgroup); RADIUS legt sie nie selbst an."
fi
echo

echo "== Rollengruppen (group-Teil von --ssid je SSID), gruppiert pro Schule =="
if [ -z "$ROLE_GROUPS" ]; then
    echo "  (keine teachers/students-Gruppen gefunden — Schul-/Gruppennamen prüfen)"
else
    schools_of | while IFS= read -r school; do
        [ -n "$school" ] || continue
        echo "  Schule '${school}':"
        printf '%s\n' "$ROLE_GROUPS" | while IFS= read -r g; do
            [ -n "$g" ] || continue
            case "$g" in
                teachers|students) [ "$school" = "default-school" ] && echo "    - ${g}" ;;
                "${school}-teachers"|"${school}-students") echo "    - ${g}" ;;
            esac
        done
    done
fi
echo

echo "== Vorlage: 'lmnradius create' pro Schule =="
echo "   (FILL: --server-fqdn, --client-subnet und die Secret-Datei-Namen ergänzen;"
echo "    Secrets baut provision-radius-account.sh — siehe REMINDERS unten.)"
echo
if [ -z "$ROLE_GROUPS" ]; then
    echo "   (keine Rollengruppen — kein Skeleton erzeugt)"
else
    schools_of | while IFS= read -r school; do
        [ -n "$school" ] || continue
        if [ "$school" = "default-school" ]; then
            tgroup="teachers";           sgroup="students"
            tssid="lehrer";              sssid="schueler"
        else
            tgroup="${school}-teachers"; sgroup="${school}-students"
            tssid="${school}-lehrer";    sssid="${school}-schueler"
        fi
        echo "# --- Schule: ${school} ---"
        echo "lmnradius create \\"
        echo "  --name ${school} \\"
        echo "  --realm ${REALM} \\"
        echo "  --workgroup ${WORKGROUP} \\"
        echo "  --server-fqdn <FILL: FQDN == Container-Hostname == EAP-Cert-CN/SAN> \\"
        echo "  --ldap-server ${LDAP_SERVER} \\"
        echo "  --ldap-base-dn ${BASE_DN} \\"
        echo "  --ldap-bind-dn ${BIND_DN} \\"
        echo "  --wifi-group ${WIFI_GROUP} \\"
        echo "  --client-subnet <FILL: AP-Management-Subnetz als CIDR, z. B. 10.0.0.0/24> \\"
        if group_present "$tgroup"; then
            echo "  --ssid ${tssid}:${tgroup}:20 \\"
        fi
        if group_present "$sgroup"; then
            echo "  --ssid ${sssid}:${sgroup}:10 \\"
        fi
        echo "  --join-secret <FILL: join.authfile> \\"
        echo "  --ldap-bind-secret <FILL: ldap-bind.secret> \\"
        echo "  --radius-secret <FILL: radius.secret>"
        echo
    done
fi

echo "== REMINDERS =="
# Active read-only check of the DC's ntlm-auth line (must be present for MSCHAPv2).
ntlm_effective="$(detect_smb_param 'ntlm auth')"
if [ -z "$ntlm_effective" ]; then
    echo "  [ ? ] 'ntlm auth' nicht ermittelbar — 'ntlm auth = ${NTLM_REQUIRED}' MUSS in der"
    echo "        DC-smb.conf (${SMB_CONF}) stehen, sonst schlägt jeder MSCHAPv2-Login fehl."
elif [ "$ntlm_effective" = "$NTLM_REQUIRED" ]; then
    echo "  [ OK ] DC-smb.conf: ntlm auth = ${NTLM_REQUIRED}."
else
    echo "  [WARN] DC-smb.conf: ntlm auth = '${ntlm_effective}', erwartet '${NTLM_REQUIRED}'."
    echo "         MSCHAPv2 schlägt sonst fehl; Paket-Updates entfernen die Zeile gelegentlich."
fi
echo "  [ i ] global-binduser-Passwort liegt auf dem lmn-Server unter"
echo "        ${BINDUSER_SECRET} (eine Zeile, kein Umbruch) — provision-radius-account.sh"
echo "        legt daraus die 'ldap-bind.secret' an. Dieses Skript zeigt KEIN Secret."
echo "  [ i ] Rollengruppen/'wifi'/Bind-User sind rein Sophomorix — nie von Hand anlegen."
