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

from typing import Any

import pytest
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

    def remove(self, name: str) -> None:
        self.removed.append(name)
        self.containers.pop(name, None)

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
def instance_data() -> dict[str, Any]:
    """A complete, valid Instance body for POST /v1/instances."""
    return {
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
