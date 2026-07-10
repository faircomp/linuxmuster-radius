#!/usr/bin/env bash

# SPDX-FileCopyrightText: Kevin Stenzel
#
# SPDX-License-Identifier: GPL-3.0-or-later

# The 5-case PEAP-MSCHAPv2 E2E matrix, driven with eapol_test (from
# wpa_supplicant). Runs as the `client` service ENTRYPOINT. Exit 0 iff all five
# cases behave as expected; non-zero otherwise (that exit code IS the E2E result
# under `docker compose up --exit-code-from client`).
#
# HOW A CASE IS DRIVEN
#   eapol_test -c <peap-conf> -a <server-ip> -p 1812 -s <secret> \
#              -N30:s:<AP-MAC>:<SSID> -ddd
#   * -c <peap-conf>  selects the user (identity/password) + server-cert pinning.
#   * -N30:s:MAC:SSID sets Called-Station-Id in RFC 3580 "<MAC>:<SSID>" form
#     (attr 30). The server's rewrite_called_station_id splits it into
#     &Called-Station-SSID, which the ssid-policy branches on. This is how the
#     SAME server sees different SSIDs per case.
#   * -ddd raises verbosity to MSG_MSGDUMP so eapol_test dumps the received
#     Access-Accept attributes — required to read Tunnel-Private-Group-Id.
#
# VERDICT
#   Access-Accept => eapol_test prints "SUCCESS" and exits 0.
#   Access-Reject => eapol_test prints "FAILURE"/dumps code=3 and exits non-zero.
#   The VLAN is asserted by finding RADIUS attribute 81 (Tunnel-Private-Group-Id)
#   in the dump and decoding its value bytes.
#
# HONEST LIMIT: cases 2 and 5 are BOTH Access-Reject, for different reasons
# (case 2 = role gate in ssid-policy, case 5 = base 'wifi' gate in ntlm_auth).
# eapol_test only observes "reject" in both; the distinction is by construction
# (different user/SSID), not from the wire. See docs/radius-and-ad.md.
set -uo pipefail

RADIUS_IP="${RADIUS_IP:-172.29.0.10}"
RADIUS_PORT="${RADIUS_PORT:-1812}"
AP_MAC="${AP_MAC:-00-11-22-33-44-55}"
SECRET_FILE="${RADIUS_SECRET_FILE:-/run/secrets/radius.secret}"
CFG_DIR="/etc/eap"

SSID_TEACHER="EXAMPLE-lehrer"
SSID_STUDENT="EXAMPLE-schueler"

SECRET="$(cat "${SECRET_FILE}")"
fail=0
LOG=""

# run_eapol <peap-conf> <ssid> — run one auth; leaves output in $LOG, returns rc.
run_eapol() {
	local conf="$1" ssid="$2"
	LOG="$(mktemp)"
	eapol_test -c "${conf}" -a "${RADIUS_IP}" -p "${RADIUS_PORT}" -s "${SECRET}" \
		-N30:s:"${AP_MAC}:${ssid}" -ddd >"${LOG}" 2>&1
	return $?
}

# accepted <rc> — true iff the server returned Access-Accept.
accepted() {
	[ "$1" -eq 0 ] && grep -q 'SUCCESS' "${LOG}"
}

# rejected — true iff the server actively returned an Access-Reject (RADIUS
# code=3). We deliberately do NOT match a bare "FAILURE": eapol_test also prints
# FAILURE on a timeout / unreachable server, which must NOT count as a valid
# reject (that would let cases 2/4/5 "pass" on a broken data plane).
rejected() {
	grep -qE 'Access-Reject|code=3' "${LOG}"
}

# vlan_ok <expected> — true iff RADIUS attr 81 (Tunnel-Private-Group-Id) in the
# Access-Accept dump decodes to (or contains) the expected VLAN id. Substring
# match on the ASCII-hex tolerates an optional RFC 2868 tag byte prefix.
vlan_ok() {
	local want="$1" want_hex bytes
	want_hex="$(printf '%s' "${want}" | od -An -tx1 | tr -d ' \n')"
	bytes="$(awk '
		/Attribute 81[ (]/ { grab = 1; next }
		grab && /Value/    { print; grab = 0 }
	' "${LOG}" | sed -E 's/.*hexdump\([^)]*\): *//' | tr -d ' ' | tr 'A-F' 'a-f')"
	case "${bytes}" in
		*"${want_hex}"*) return 0 ;;
		*) return 1 ;;
	esac
}

dump_tail() {
	echo "      ---- eapol_test tail ----"
	tail -n 20 "${LOG}" | sed 's/^/      | /'
}

# assert_case <desc> <peap-conf> <ssid> <accept|reject> [expected-vlan]
assert_case() {
	local desc="$1" conf="$2" ssid="$3" want="$4" vlan="${5:-}" rc
	run_eapol "${conf}" "${ssid}"
	rc=$?
	if [ "${want}" = "accept" ]; then
		if ! accepted "${rc}"; then
			echo "  [FAIL] ${desc}: expected Access-Accept, got reject/failure (rc=${rc})"
			dump_tail
			fail=1
		elif [ -n "${vlan}" ] && ! vlan_ok "${vlan}"; then
			echo "  [FAIL] ${desc}: Access-Accept but Tunnel-Private-Group-Id != ${vlan}"
			dump_tail
			fail=1
		else
			echo "  [PASS] ${desc}${vlan:+ (VLAN ${vlan})}"
		fi
	else # reject
		if accepted "${rc}"; then
			echo "  [FAIL] ${desc}: expected Access-Reject, got Access-Accept"
			dump_tail
			fail=1
		elif rejected; then
			echo "  [PASS] ${desc}"
		else
			echo "  [FAIL] ${desc}: no Access-Reject seen (server unreachable/timeout? rc=${rc})"
			dump_tail
			fail=1
		fi
	fi
	rm -f "${LOG}"
}

echo "== linuxmuster-radius PEAP-MSCHAPv2 E2E matrix (server ${RADIUS_IP}:${RADIUS_PORT}) =="

# 1) teacher on the teacher SSID  -> Accept + VLAN 20
assert_case "1 teacher on teacher SSID -> Accept + VLAN 20" \
	"${CFG_DIR}/peap-teacher1.conf" "${SSID_TEACHER}" accept 20

# 2) student on the teacher SSID  -> Reject (right SSID, wrong role)
assert_case "2 student on teacher SSID -> Reject (wrong role)" \
	"${CFG_DIR}/peap-student1.conf" "${SSID_TEACHER}" reject

# 3) student on the student SSID  -> Accept + VLAN 10
assert_case "3 student on student SSID -> Accept + VLAN 10" \
	"${CFG_DIR}/peap-student1.conf" "${SSID_STUDENT}" accept 10

# 4) teacher with a WRONG password -> Reject. Derive a bad-password config from
#    teacher1's (keeps the file set to the three requested peap configs).
BADPW="$(mktemp)"
sed -E 's/^([[:space:]]*password=).*/\1"WrongPassw0rd!"/' \
	"${CFG_DIR}/peap-teacher1.conf" >"${BADPW}"
assert_case "4 teacher with WRONG password -> Reject" \
	"${BADPW}" "${SSID_TEACHER}" reject
rm -f "${BADPW}"

# 5) user NOT in the wifi group -> Reject (base gate, before the SSID policy)
assert_case "5 user not in wifi group -> Reject (base gate)" \
	"${CFG_DIR}/peap-nowifi1.conf" "${SSID_TEACHER}" reject

echo
if [ "${fail}" -eq 0 ]; then
	echo "E2E: ALL 5 CASES OK"
else
	echo "E2E: FAILED"
fi
exit "${fail}"
