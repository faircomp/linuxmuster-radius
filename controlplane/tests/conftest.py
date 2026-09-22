# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared pytest fixtures and an in-memory fake Docker backend.

The fake fully satisfies the :class:`lmnradius.docker_service.DockerService`
interface so that :func:`lmnradius.api.create_app` (and the reconciler / updater)
can be exercised without a real Docker daemon. Every test in this suite runs
WITHOUT Docker: the seam is the ``docker`` fixture, an in-memory stand-in.

Sibling control-plane modules (store, reconciler, updater, api, security, main,
docker_service, cli) are written in parallel; this conftest wires them together
following the shape of the linuxmuster-squid templates and the model/render/api
contract in the SPEC (docs/architecture.md).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from starlette.testclient import TestClient

from lmnradius.api import create_app
from lmnradius.config import Settings
from lmnradius.models import Instance
from lmnradius.reconciler import Reconciler
from lmnradius.store import Store
from lmnradius.updater import Updater

TEST_TOKEN = "test-secret-token"


class FakeDockerService:
    """In-memory stand-in for :class:`lmnradius.docker_service.DockerService`.

    Containers are tracked in a dict keyed by instance ``name`` (i.e. the short
    name, not the ``lmnradius-`` prefixed container name). Every method mirrors
    the real service's signature and return shape, and :meth:`status` returns the
    exact five-key dict the real service returns.

    Two image markers drive the failure paths the Updater must handle:

    * an image containing ``"bad"`` comes up **unhealthy** (health-gate rollback);
    * an image containing ``"unpullable"`` makes :meth:`ensure_running` remove the
      old container and then **raise** (pull/run failure -> apply-time rollback).
    """

    def __init__(
        self,
        docker_host: str | None = None,
        secrets_dir: str = "/etc/linuxmuster-radius/secrets",
        certs_dir: str = "/etc/linuxmuster-radius/certs",
        render_dir: str = "/var/lib/linuxmuster-radius/instance.d",
        container_bind_ip: str = "0.0.0.0",
        log_max_size: str = "20m",
        log_max_file: int = 5,
    ) -> None:
        self.docker_host = docker_host
        self.secrets_dir = secrets_dir
        self.certs_dir = certs_dir
        self.render_dir = render_dir
        self.container_bind_ip = container_bind_ip
        self.log_max_size = log_max_size
        self.log_max_file = log_max_file
        self.containers: dict[str, dict[str, Any]] = {}
        # Test-observability hooks.
        self.ensure_calls: list[str] = []
        self.removed: list[str] = []

    def env_for(self, inst: Instance) -> dict[str, str]:
        return {
            "INSTANCE": inst.name,
            "REALM": inst.realm,
            "WORKGROUP": inst.workgroup,
            "SERVER_FQDN": inst.server_fqdn,
            "LDAP_SERVER": inst.ldap_server,
            "LDAP_BASE_DN": inst.ldap_base_dn,
            "LDAP_BIND_DN": inst.ldap_bind_dn,
            "WIFI_GROUP": inst.wifi_group,
            "JOIN_SECRET": f"/run/secrets/{inst.join_secret}",
            "LDAP_BIND_SECRET": f"/run/secrets/{inst.ldap_bind_secret}",
            "EAP_CA": "/run/secrets/eap/ca.pem",
            "EAP_CERT": "/run/secrets/eap/server.pem",
            "EAP_KEY": "/run/secrets/eap/server.key",
        }

    def ensure_running(self, inst: Instance) -> dict[str, Any]:
        self.ensure_calls.append(inst.name)
        if "unpullable" in inst.image:
            # Mirror the real service: the old container is force-removed before the new
            # one is created, so a pull/run failure leaves NO container and raises.
            self.containers.pop(inst.name, None)
            raise RuntimeError("simulated pull failure")
        self.containers[inst.name] = {
            "running": True,
            "image": inst.image,
            "health": "unhealthy" if "bad" in inst.image else "healthy",
            "env": self.env_for(inst),
            "logs": (
                f"started {inst.container_name}\n"
                "winbindd: ready to serve connections\n"
                "radiusd: Ready to process requests\n"
            ),
        }
        return self.status(inst.name)

    def start(self, name: str) -> dict[str, Any]:
        container = self.containers.get(name)
        if container is not None:
            container["running"] = True
        return self.status(name)

    def stop(self, name: str) -> dict[str, Any]:
        container = self.containers.get(name)
        if container is not None:
            container["running"] = False
        return self.status(name)

    def restart(self, name: str) -> dict[str, Any]:
        container = self.containers.get(name)
        if container is not None:
            container["running"] = True
        return self.status(name)

    def remove(self, inst: Instance) -> dict[str, Any]:
        # Mirrors the real service: full teardown incl. the domain leave, whose result
        # is reported (``leave_ok`` lets a test simulate an unreachable DC).
        self.removed.append(inst.name)
        self.containers.pop(inst.name, None)
        ok = getattr(self, "leave_ok", True)
        return {"ok": ok, "attempted": True, "detail": "stub leave" if ok else "DC unreachable"}

    def status(self, name: str) -> dict[str, Any]:
        container = self.containers.get(name)
        if container is None:
            return {
                "name": name,
                "exists": False,
                "running": False,
                "health": None,
                "image": None,
            }
        return {
            "name": name,
            "exists": True,
            "running": bool(container["running"]),
            "health": container["health"],
            "restart_count": 0,
            "crash_looping": False,
            "image": container["image"],
        }

    def logs(
        self,
        name: str,
        tail: int = 100,
        since: int | None = None,
        until: int | None = None,
        grep: str | None = None,
    ) -> str:
        container = self.containers.get(name)
        if container is None:
            return ""
        lines = str(container["logs"]).splitlines()
        if grep:
            lines = [line for line in lines if grep in line]
        return "\n".join(lines[-tail:])

    def test(self, inst: Any, user: str | None, password: str | None) -> dict[str, Any]:
        """In-memory stand-in mirroring the real test() shape. Membership is
        driven by ``self.test_members`` (set of "user:GROUP"); the password
        ``"good"`` authenticates, anything else is a wrong password."""
        container = self.containers.get(inst.name)
        if container is None:
            return {"instance": inst.name, "container_running": False, "detail": "no container"}
        if not container["running"]:
            return {"instance": inst.name, "container_running": False, "detail": "not running"}
        members = getattr(self, "test_members", set())
        out: dict[str, Any] = {"instance": inst.name, "container_running": True}
        out["trust"] = {"ok": getattr(self, "test_trust_ok", True), "detail": "stub"}
        if user is None:
            return out
        pw_ok = password == "good"
        in_wifi = f"{user}:{inst.wifi_group}" in members
        if not pw_ok:
            out["login"] = {
                "ok": False,
                "code": "NT_STATUS_WRONG_PASSWORD",
                "detail": "wrong password",
            }
            out["gates"] = []
            return out
        out["login"] = (
            {"ok": True, "code": "NT_STATUS_OK", "detail": "ok"}
            if in_wifi
            else {"ok": False, "code": "NT_STATUS_LOGON_FAILURE", "detail": "not in wifi"}
        )
        out["gates"] = [
            {
                "ssid": s.name,
                "group": s.allowed_group,
                "vlan": s.vlan,
                "member": f"{user}:{s.allowed_group}" in members,
            }
            for s in inst.ssids
        ]
        return out


def make_cert_pem(common_name: str, ca: bool = True, issuer_key: Any = None) -> str:
    """A throwaway certificate PEM: self-signed (a trust anchor) by default, or an
    end-entity certificate signed by ``issuer_key`` (then ``ca=False``)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    issuer = (
        name
        if issuer_key is None
        else x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test issuer")])
    )
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    cert = builder.sign(issuer_key or key, hashes.SHA256())
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


@pytest.fixture(scope="session")
def dc_ca_pem() -> str:
    """A self-signed 'DC CA' used as the pinned LDAPS trust anchor in the tests."""
    return make_cert_pem("test DC CA")


@pytest.fixture(scope="session")
def leaf_cert_pem() -> str:
    """An end-entity certificate (not self-signed, CA:FALSE) -- what a DC serves when
    it does not include its CA in the chain."""
    issuer_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return make_cert_pem("dc.linuxmuster.lan", ca=False, issuer_key=issuer_key)


@pytest.fixture
def token() -> str:
    return TEST_TOKEN


@pytest.fixture
def settings(tmp_path: Any, token: str) -> Settings:
    return Settings(
        api_token=token,
        instances_dir=str(tmp_path / "instances"),
        secrets_dir=str(tmp_path / "secrets"),
        certs_dir=str(tmp_path / "certs"),
        render_dir=str(tmp_path / "instance.d"),
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    return Store(settings.instances_dir)


@pytest.fixture
def docker(settings: Settings) -> FakeDockerService:
    return FakeDockerService(
        secrets_dir=settings.secrets_dir,
        certs_dir=settings.certs_dir,
        render_dir=settings.render_dir,
    )


@pytest.fixture
def reconciler(store: Store, docker: FakeDockerService) -> Reconciler:
    return Reconciler(store, docker)  # type: ignore[arg-type]


@pytest.fixture
def updater(store: Store, docker: FakeDockerService, reconciler: Reconciler) -> Updater:
    return Updater(store, docker, reconciler, health_timeout=1.0, poll_interval=0.0)  # type: ignore[arg-type]


@pytest.fixture
def app(
    settings: Settings,
    store: Store,
    reconciler: Reconciler,
    docker: FakeDockerService,
    updater: Updater,
) -> Any:
    return create_app(settings, store, reconciler, docker, updater)  # type: ignore[arg-type]


@pytest.fixture
def client(app: Any) -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def instance_data(dc_ca_pem: str) -> dict[str, Any]:
    """A complete, valid body for POST /v1/instances (ldaps:// + the CA to pin;
    ``Instance(**instance_data)`` ignores the write-only ``ldap_ca_pem``)."""
    return {
        "ldap_ca_pem": dc_ca_pem,
        "name": "default-school",
        "realm": "LINUXMUSTER.LAN",
        "workgroup": "LINUXMUSTER",
        "server_fqdn": "radius.linuxmuster.lan",
        "ldap_server": "ldaps://dc.linuxmuster.lan",
        "ldap_base_dn": "DC=linuxmuster,DC=lan",
        "ldap_bind_dn": "CN=global-binduser,OU=Management,OU=GLOBAL,DC=linuxmuster,DC=lan",
        "wifi_group": "wifi",
        "client_subnets": ["10.0.0.0/16"],
        "ssids": [
            {"name": "pgw-lehrer", "allowed_group": "teachers", "vlan": 20},
            {"name": "pgw-schueler", "allowed_group": "students", "vlan": 30},
        ],
        "join_secret": "default-school-join.secret",
        "ldap_bind_secret": "default-school-ldap.secret",
        "radius_secret": "default-school-radius.secret",
        "image": "ghcr.io/faircomp/linuxmuster-radius:0.1.0",
    }


@pytest.fixture
def instance(instance_data: dict[str, Any]) -> Instance:
    return Instance(**instance_data)
