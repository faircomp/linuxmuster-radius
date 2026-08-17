# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the pure ntlm_auth/wbinfo output interpreters."""

from __future__ import annotations

from lmnradius import diagnostics


def test_trust_ok() -> None:
    r = diagnostics.interpret_trust(
        0, "checking the trust secret for domain X via RPC calls succeeded"
    )
    assert r["ok"] is True


def test_trust_unavailable() -> None:
    r = diagnostics.interpret_trust(
        1, "could not obtain winbind interface details: WBC_ERR_WINBIND_NOT_AVAILABLE"
    )
    assert r["ok"] is False
    assert "not answering" in r["detail"]


def test_trust_broken_nonzero() -> None:
    r = diagnostics.interpret_trust(1, "checking the trust secret ... failed")
    assert r["ok"] is False
    assert "restart" in r["detail"].lower()


def test_ntlm_ok_by_exit() -> None:
    assert (
        diagnostics.interpret_ntlm(0, "NT_STATUS_OK: The operation completed successfully. (0x0)")[
            "ok"
        ]
        is True
    )


def test_ntlm_ok_zero_only() -> None:
    # ntlm_auth --request-nt-key can print the key line first; exit 0 is authoritative.
    assert diagnostics.interpret_ntlm(0, "NT_KEY: 0011AABB\n")["ok"] is True


def test_ntlm_wrong_password() -> None:
    r = diagnostics.interpret_ntlm(1, "NT_STATUS_WRONG_PASSWORD: ... (0xc000006a)")
    assert r["ok"] is False and r["code"] == "NT_STATUS_WRONG_PASSWORD"


def test_ntlm_logon_failure_is_group() -> None:
    r = diagnostics.interpret_ntlm(
        1, "NT_STATUS_LOGON_FAILURE: The attempted logon is invalid. (0xc000006d)"
    )
    assert r["ok"] is False and r["code"] == "NT_STATUS_LOGON_FAILURE"
    assert "member" in r["detail"]


def test_ntlm_no_such_user() -> None:
    assert (
        diagnostics.interpret_ntlm(1, "NT_STATUS_NO_SUCH_USER (0xc0000064)")["code"]
        == "NT_STATUS_NO_SUCH_USER"
    )


def test_ntlm_winbind_down() -> None:
    assert (
        diagnostics.interpret_ntlm(1, "could not obtain winbind separator!")["code"]
        == "WINBIND_UNAVAILABLE"
    )


def test_ntlm_unknown_error_first_line() -> None:
    r = diagnostics.interpret_ntlm(1, "some weird failure\nmore detail")
    assert r["ok"] is False and r["code"] == "ERROR" and r["detail"] == "some weird failure"
