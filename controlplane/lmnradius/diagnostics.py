# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Interpret winbind/ntlm_auth output for the ``lmnradius test`` diagnostics.

Pure functions (no Docker, no I/O), so the tricky bit — turning a tool's exit
code + stderr into a plain operator verdict — is directly unit-testable. The
raw commands run in :meth:`lmnradius.docker_service.DockerService.test`.
"""

from __future__ import annotations

from typing import Any


def interpret_trust(exit_code: int, output: str) -> dict[str, Any]:
    """`wbinfo -t` — is the winbind secure channel to the DC alive?

    Success prints "... RPC calls succeeded" (exit 0). A dead channel (machine
    account deleted, DC unreachable) prints WBC_ERR_WINBIND_NOT_AVAILABLE or a
    check failure — the operator's cue to ``lmnradius restart <name>`` (re-join).
    """
    ok = exit_code == 0 and "succeeded" in output.lower()
    if ok:
        detail = "winbind trust to the DC is up (RPC calls succeeded)"
    elif "not_available" in output.lower() or "not available" in output.lower():
        detail = (
            "winbind is not answering — the container may still be starting, or the trust is broken"
        )
    else:
        detail = "winbind trust check FAILED — machine account or DC connection is broken; try 'lmnradius restart'"
    return {"ok": ok, "detail": detail.strip()}


def interpret_ntlm(exit_code: int, output: str) -> dict[str, Any]:
    """`ntlm_auth --require-membership-of` — the domain-login core.

    This is exactly the mschap-module path the server uses per WLAN request:
    exit 0 (``NT_STATUS_OK``) means the password is correct AND the account is a
    member of the required group. The status strings distinguish the failure
    reasons so the operator sees *why*, not just that it failed.
    """
    up = output.upper()
    if exit_code == 0 or "NT_STATUS_OK" in up:
        return {
            "ok": True,
            "code": "NT_STATUS_OK",
            "detail": "password correct and group membership satisfied",
        }
    if "WRONG_PASSWORD" in up:
        return {"ok": False, "code": "NT_STATUS_WRONG_PASSWORD", "detail": "wrong password"}
    if "NO_SUCH_USER" in up:
        return {
            "ok": False,
            "code": "NT_STATUS_NO_SUCH_USER",
            "detail": "no such user in the domain",
        }
    if "LOGON_FAILURE" in up:
        return {
            "ok": False,
            "code": "NT_STATUS_LOGON_FAILURE",
            "detail": "password correct but NOT a member of the required group (or bad credentials)",
        }
    if "WINBIND_NOT_AVAILABLE" in up or "COULD NOT OBTAIN WINBIND" in up:
        return {
            "ok": False,
            "code": "WINBIND_UNAVAILABLE",
            "detail": "winbind not answering — check trust first",
        }
    first = output.strip().splitlines()[0] if output.strip() else "unknown ntlm_auth error"
    return {"ok": False, "code": "ERROR", "detail": first[:200]}
