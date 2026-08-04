#!/usr/bin/env bash
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Member-Registrierung + Secret-Bau — run ON THE linuxmuster SAMBA-DC (as root).
# lmn-konform (ADR-006), OHNE von Hand ein Maschinenkonto/Gruppen/Bind-User anzulegen:
#
#   (a) Trägt die RADIUS-VM als Device mit Rolle 'server' in die devices.csv ein
#       (idempotent: bereits vorhanden -> übersprungen; Backup vor jedem Append) und
#       zeigt die exakte Zeile + den `linuxmuster-import-devices`-Aufruf.
#   (b) Verweist für den LDAP-Bind auf den BESTEHENDEN global-binduser (legt NICHTS an).
#   (c) Baut die Secret-Dateien für den Control-Plane secrets_dir mit umask 077:
#       - join.authfile   (Samba -A Format: username/password/domain) für den net-ads-join
#       - ldap-bind.secret (global-binduser-Passwort)
#       - radius.secret    (AP-Shared-Secret, identisch im UniFi-RADIUS-Profil)
#       Es wird NIE ein Secret ausgegeben.
#
# JOIN-KONTO — TEILVERIFIZIERT (Live-E2E gegen echte linuxmuster, 2026-07-12):
# Ein EINFACHER Benutzer kann NICHT joinen ('Insufficient access'); mit dem
# linuxmuster-Administrator-Konto (bzw. einem delegierten Konto mit dem Recht,
# Maschinenkonten anzulegen/zurückzusetzen) läuft `net ads join` durch. Das gehört
# also in das join.authfile. WEITERHIN OFFEN: ob ein via devices.csv Rolle 'server'
# + linuxmuster-import-devices VORAB angelegtes Computerkonto vom späteren Join
# sauber ADOPTIERT wird (im E2E legte der Join das Konto selbst an) — siehe
# ../docs/radius-and-ad.md und ../docs/decisions.md (ADR-006 „Offen (E2E)").
#
# Usage:  provision-radius-account.sh <hostname> <mac> <ip> [out-secrets-dir]
# Env:    WORKGROUP=... (sonst via testparm/smb.conf), DEVICES_CSV=...,
#         DRY_RUN=1 (devices.csv nur anzeigen, nicht anhängen),
#         RUN_IMPORT=1 (linuxmuster-import-devices direkt starten)
#
# Siblings: discover-ad-facts.sh (read-only Discovery),
#           ../controlplane/lmnradius/cli.py (--join-secret/--ldap-bind-secret/--radius-secret).
set -euo pipefail
umask 077                                    # Secret-Dateien entstehen mit 0600 (kein 0644-Fenster)

RADIUS_HOST="${1:?Usage: provision-radius-account.sh <hostname> <mac> <ip> [out-secrets-dir]}"
RADIUS_MAC="${2:?fehlende MAC-Adresse}"
RADIUS_IP="${3:?fehlende IP-Adresse}"
OUT_DIR="${4:-${SECRETS_STAGE:-${PWD}/radius-secrets}}"

SMB_CONF="${SMB_CONF:-/etc/samba/smb.conf}"
DEVICES_CSV="${DEVICES_CSV:-/etc/linuxmuster/sophomorix/default-school/devices.csv}"
BINDUSER_SECRET="${BINDUSER_SECRET:-/etc/linuxmuster/.secret/global-binduser}"
IMPORT_CMD="linuxmuster-import-devices"

have() { command -v "$1" >/dev/null 2>&1; }

# WORKGROUP for the join.authfile 'domain =' line (testparm -s -> fallback smb.conf).
detect_workgroup() {
    v=""
    have testparm && v="$(testparm -s --parameter-name workgroup 2>/dev/null | tr -d '[:space:]')"
    if [ -z "$v" ] && [ -r "$SMB_CONF" ]; then
        v="$(grep -iE '^[[:space:]]*workgroup[[:space:]]*=' "$SMB_CONF" 2>/dev/null \
            | tail -n1 | cut -d= -f2- | tr -d '[:space:]')"
    fi
    printf '%s' "$v"
}
WORKGROUP="${WORKGROUP:-$(detect_workgroup)}"
WORKGROUP="$(printf '%s' "$WORKGROUP" | tr '[:lower:]' '[:upper:]')"
[ -n "$WORKGROUP" ] || WORKGROUP="<FILL: WORKGROUP, z. B. LINUXMUSTER>"

# =============================================================== (a) devices.csv
echo "== (a) devices.csv — RADIUS-VM als Device mit Rolle 'server' =="
# Feld-Layout nach devices.csv(5) — 15 Felder, semikolon-getrennt:
#   1 room · 2 hostname · 3 device group (hardwareclass) · 4 mac · 5 ip ·
#   6 msoffice-key · 7 windows-key · 8 dhcp-options · 9 sophomorixRole ·
#   10 reserviert · 11 pxe-flag (0=kein pxe) · 12-14 reserviert · 15 sophomorixComment
# WICHTIG: die ROLLE gehört in Feld 9, NICHT in Feld 3 (Feld 3 ist die
# Hardwareklasse, bei Servern typischerweise 'nopxe'). Nur Feld 9 entscheidet,
# ob überhaupt ein Computerkonto angelegt wird (devices.csv(5), sophomorixRole).
# Gültige Rollennamen: `sophomorix-samba --show-roletype`.
# (Gegen eine echte linuxmuster-7-devices.csv verifiziert, 2026-08-04.)
DEVICE_ROOM="${DEVICE_ROOM:-server}"        # Raum-Feld; bei Servern konventionell 'server'
DEVICE_GROUP="${DEVICE_GROUP:-nopxe}"       # Hardwareklasse: ein Server bootet nicht per PXE
DEVICE_ROLE="${DEVICE_ROLE:-server}"        # sophomorixRole -> legt das Maschinenkonto an
DEVICE_LINE="${DEVICE_ROOM};${RADIUS_HOST};${DEVICE_GROUP};${RADIUS_MAC};${RADIUS_IP};;;;${DEVICE_ROLE};;0;;;;linuxmuster-radius"

if [ ! -f "$DEVICES_CSV" ]; then
    echo "   ${DEVICES_CSV} nicht gefunden. Kandidaten:"
    found=0
    for f in /etc/linuxmuster/sophomorix/*/devices.csv; do
        [ -e "$f" ] || continue
        echo "     ${f}"; found=1
    done
    [ "$found" = 1 ] || echo "     (keine gefunden — Skript auf dem DC ausführen)"
    echo "   -> DEVICES_CSV=<pfad> setzen und erneut starten. Vorgesehene Zeile:"
    echo "     ${DEVICE_LINE}"
elif grep -qiE "(^|;)${RADIUS_HOST}(;|\$)" "$DEVICES_CSV"; then
    echo "   '${RADIUS_HOST}' steht bereits in ${DEVICES_CSV} — übersprungen (idempotent)."
else
    echo "   Vorgesehene Zeile:"
    echo "     ${DEVICE_LINE}"
    # Layout-Abgleich gegen die BESTEHENDEN Zeilen: schreibt eine Installation mehr/weniger
    # Felder (linuxmuster-setup hängt z. B. Marker-Spalten an), passt unser Satz nicht und
    # der Import bekäme eine verrutschte Zeile. Nur warnen, nicht abbrechen — die
    # Feldbedeutung 1-15 ist laut devices.csv(5) stabil, Zusatzfelder sind erlaubt.
    ref_nf="$(awk -F';' '!/^[[:space:]]*#/ && NF > 1 { print NF; exit }' "$DEVICES_CSV" 2>/dev/null || true)"
    our_nf="$(printf '%s' "$DEVICE_LINE" | awk -F';' '{print NF}')"
    if [ -n "${ref_nf:-}" ] && [ "$ref_nf" != "$our_nf" ]; then
        echo "   [WARN] bestehende Zeilen haben ${ref_nf} Felder, unsere ${our_nf}."
        echo "          Die Felder 1-15 sind laut devices.csv(5) fest (Rolle = Feld 9);"
        echo "          zusätzliche Spalten sind erlaubt. Zeile vor dem Import gegenprüfen."
    fi
    if [ "${DRY_RUN:-0}" = "1" ]; then
        echo "   DRY_RUN=1 -> nicht angehängt. Zum Anhängen ohne DRY_RUN erneut ausführen."
    else
        cp -a "$DEVICES_CSV" "${DEVICES_CSV}.bak.$(date +%Y%m%d%H%M%S)"
        printf '%s\n' "$DEVICE_LINE" >> "$DEVICES_CSV"
        echo "   angehängt an ${DEVICES_CSV} (Backup: ${DEVICES_CSV}.bak.*)."
    fi
fi

if [ "${RUN_IMPORT:-0}" = "1" ] && have "$IMPORT_CMD"; then
    echo "   starte ${IMPORT_CMD} ..."
    "$IMPORT_CMD"
else
    echo "   Danach auf dem DC ausführen (legt das Maschinenkonto an):  ${IMPORT_CMD}"
fi
echo

# =============================================================== (b) LDAP-Bind
echo "== (b) LDAP-Bind — bestehender global-binduser (es wird NICHTS angelegt) =="
echo "   --ldap-bind-dn CN=global-binduser,OU=Management,OU=GLOBAL,<base-dn>"
echo "   Passwort: ${BINDUSER_SECRET} (auf dem lmn-Server; wird in (c) kopiert)."
echo

# =============================================================== (c) Secret-Dateien
echo "== (c) Secret-Dateien für den Control-Plane secrets_dir (umask 077) =="
mkdir -p "$OUT_DIR"
JOIN_AUTHFILE="${OUT_DIR}/join.authfile"
LDAP_BIND_FILE="${OUT_DIR}/ldap-bind.secret"
RADIUS_SECRET_FILE="${OUT_DIR}/radius.secret"

# --- join.authfile (Samba -A: username/password/domain) ---
echo "-- join.authfile (--join-secret) --"
if [ -e "$JOIN_AUTHFILE" ]; then
    echo "   ${JOIN_AUTHFILE} existiert bereits — übersprungen (wird nie überschrieben)."
elif [ -t 0 ]; then
    printf '   Join-Username (Join-berechtigtes AD-Konto): '
    read -r JOIN_USER
    printf '   Join-Passwort (Eingabe verborgen): '
    read -rs JOIN_PASS; echo
    {
        printf 'username = %s\n' "$JOIN_USER"
        printf 'password = %s\n' "$JOIN_PASS"
        printf 'domain = %s\n' "$WORKGROUP"
    } > "$JOIN_AUTHFILE"
    unset JOIN_PASS
    chmod 0600 "$JOIN_AUTHFILE"
    echo "   geschrieben: ${JOIN_AUTHFILE} (0600) — Inhalt NICHT angezeigt."
else
    echo "   kein TTY — nicht interaktiv erstellt. Manuell anlegen (umask 077, Format):"
    echo "     username = <join-berechtigtes-konto>"
    echo "     password = <passwort>"
    echo "     domain = ${WORKGROUP}"
fi

# --- ldap-bind.secret (global-binduser-Passwort; bevorzugt kopiert) ---
echo "-- ldap-bind.secret (--ldap-bind-secret) --"
if [ -e "$LDAP_BIND_FILE" ]; then
    echo "   ${LDAP_BIND_FILE} existiert bereits — übersprungen."
elif [ -r "$BINDUSER_SECRET" ]; then
    cp -- "$BINDUSER_SECRET" "$LDAP_BIND_FILE"
    chmod 0600 "$LDAP_BIND_FILE"
    echo "   kopiert aus ${BINDUSER_SECRET} -> ${LDAP_BIND_FILE} (0600) — Inhalt NICHT angezeigt."
elif [ -t 0 ]; then
    printf '   global-binduser-Passwort (Eingabe verborgen): '
    read -rs BIND_PASS; echo
    printf '%s' "$BIND_PASS" > "$LDAP_BIND_FILE"
    unset BIND_PASS
    chmod 0600 "$LDAP_BIND_FILE"
    echo "   geschrieben: ${LDAP_BIND_FILE} (0600) — Inhalt NICHT angezeigt."
else
    echo "   ${BINDUSER_SECRET} nicht lesbar und kein TTY — ${LDAP_BIND_FILE} manuell befüllen"
    echo "   (eine Zeile, kein Umbruch)."
fi

# --- radius.secret (AP-Shared-Secret) ---
echo "-- radius.secret (--radius-secret) --"
if [ -e "$RADIUS_SECRET_FILE" ]; then
    echo "   ${RADIUS_SECRET_FILE} existiert bereits — übersprungen."
elif [ -t 0 ]; then
    printf '   RADIUS-Shared-Secret (identisch im UniFi-RADIUS-Profil; verborgen): '
    read -rs RAD_PASS; echo
    printf '%s' "$RAD_PASS" > "$RADIUS_SECRET_FILE"
    unset RAD_PASS
    chmod 0600 "$RADIUS_SECRET_FILE"
    echo "   geschrieben: ${RADIUS_SECRET_FILE} (0600) — Inhalt NICHT angezeigt."
else
    echo "   kein TTY — ${RADIUS_SECRET_FILE} manuell befüllen (ein-zeiliges Shared Secret)."
fi
echo

echo "== Nächste Schritte =="
echo "  1. Secret-Dateien aus ${OUT_DIR} in den Control-Plane secrets_dir übertragen"
echo "     (Namen unverändert lassen: sie sind die --join-secret/--ldap-bind-secret/"
echo "      --radius-secret-Referenzen in 'lmnradius create')."
echo "  2. Auf dem DC ${IMPORT_CMD} laufen lassen (falls oben nicht via RUN_IMPORT=1 erfolgt)."
echo "  3. Join-Konto: ein einfacher Benutzer kann NICHT joinen (verifiziert) — ins"
echo "     join.authfile gehört der Administrator bzw. ein delegiertes Join-Konto."
echo "     Offen bleibt nur die Vorab-Adoption via import-devices (Kopf-Kommentar/ADR-006)."
