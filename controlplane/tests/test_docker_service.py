# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""DockerService unit tests that need no daemon: the honest status derivation, the
host-IP detection and the env/mount contract with image/entrypoint.sh (LDAP_CA,
HOST_IP). The container lifecycle itself is proven on a real host (docs/references.md).
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from lmnradius.docker_service import DockerService, detect_host_ip, state_view
from lmnradius.models import Instance


def _attrs(**state: Any) -> dict[str, Any]:
    base = {"Running": True, "Restarting": False, "ExitCode": 0, "Health": {"Status": "healthy"}}
    base.update(state.pop("state", {}))
    return {"State": base, **state}


def test_state_view_healthy_running() -> None:
    v = state_view(_attrs(RestartCount=0))
    assert v == {"running": True, "health": "healthy", "restart_count": 0, "crash_looping": False}


def test_state_view_crash_loop_is_not_running() -> None:
    # The campaign case: Running=true between restarts, health stuck at "starting",
    # RestartCount climbing -- was reported as "running".
    v = state_view(_attrs(RestartCount=9, state={"Health": {"Status": "starting"}}))
    assert v["running"] is True and v["crash_looping"] is True and v["restart_count"] == 9
    # while Docker backs off before the next restart
    v = state_view(
        _attrs(RestartCount=2, state={"Running": False, "Restarting": True, "ExitCode": 1})
    )
    assert v["running"] is False and v["crash_looping"] is True and v["exit_code"] == 1


def test_state_view_recovered_after_one_restart_is_fine() -> None:
    v = state_view(_attrs(RestartCount=1))
    assert v["crash_looping"] is False and v["running"] is True


def test_state_view_exited_and_no_healthcheck() -> None:
    v = state_view({"State": {"Running": False, "ExitCode": 137}, "RestartCount": "garbage"})
    assert v == {
        "running": False,
        "health": None,
        "restart_count": 0,
        "crash_looping": False,
        "exit_code": 137,
    }


def test_detect_host_ip_returns_non_loopback_or_none() -> None:
    ip = detect_host_ip("127.0.0.1")  # routes via lo -> falls back to the default route
    assert ip is None or (ip.count(".") == 3 and not ip.startswith("127."))
    ip = detect_host_ip("no-such-host.invalid")  # unresolvable -> default route or None
    assert ip is None or ip.count(".") == 3


@pytest.fixture
def service(tmp_path: Any) -> DockerService:
    return DockerService(
        secrets_dir=str(tmp_path / "secrets"),
        certs_dir=str(tmp_path / "certs"),
        render_dir=str(tmp_path / "instance.d"),
        host_ip="10.250.0.208",
        client=object(),  # never touched by env_for/_mounts
    )


def test_env_for_carries_host_ip_and_ldap_ca(service: DockerService, instance: Instance) -> None:
    env = service.env_for(instance)
    assert env["HOST_IP"] == "10.250.0.208"
    assert "LDAP_CA" not in env  # legacy record: unverified, no mount
    pinned = instance.model_copy(update={"ldap_ca": "ldap-ca.pem"})
    env = service.env_for(pinned)
    assert env["LDAP_CA"] == "/run/secrets/ldap/ca.pem"


def test_mounts_fail_closed_on_missing_ldap_ca(
    service: DockerService, instance: Instance, tmp_path: Any
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name in (instance.join_secret, instance.ldap_bind_secret):
        (secrets / name).write_text("x")
    certs = tmp_path / "certs" / instance.name
    certs.mkdir(parents=True)
    for name in ("ca.pem", "server.pem", "server.key"):
        (certs / name).write_text("x")

    pinned = instance.model_copy(update={"ldap_ca": "ldap-ca.pem"})
    env = service.env_for(pinned)
    with pytest.raises(FileNotFoundError, match="ldap_ca file missing"):
        service._mounts(pinned, env, require_eap=True)

    (certs / "ldap-ca.pem").write_text("x")
    mounts = service._mounts(pinned, env, require_eap=True)
    assert mounts[str(certs / "ldap-ca.pem")] == {"bind": "/run/secrets/ldap/ca.pem", "mode": "ro"}
    assert mounts[f"lmnradius-samba-{instance.name}"]["bind"] == "/var/lib/samba"

    # the leave run tolerates missing EAP material, the instance run does not
    os.remove(certs / "server.key")
    with pytest.raises(FileNotFoundError, match="EAP cert material missing"):
        service._mounts(pinned, env, require_eap=True)
    assert str(certs / "server.pem") in service._mounts(pinned, env, require_eap=False)
