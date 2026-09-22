# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTP-level tests for the FastAPI control-plane API.

Uses the Starlette/httpx TestClient with the in-memory FakeDockerService, so no
real Docker daemon is required. The negative tests cover the auth boundary
(401/403), unknown/traversal names (404/422), invalid bodies (422) and a down
Docker daemon (503).
"""

from __future__ import annotations

from importlib.metadata import version as dist_version
from typing import Any

from starlette.testclient import TestClient

NAME = "default-school"


# --------------------------------------------------------------------- health/auth


def test_health_needs_no_auth(client: TestClient) -> None:
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_version(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.get("/v1/version", headers=auth_headers)
    assert resp.status_code == 200
    # Single version source: what the package metadata says (fed from debian/changelog).
    assert resp.json() == {"version": dist_version("lmnradius")}


def test_missing_token_is_401(client: TestClient) -> None:
    resp = client.get("/v1/instances")
    assert resp.status_code == 401


def test_wrong_token_is_403(client: TestClient) -> None:
    resp = client.get("/v1/instances", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 403


def test_create_requires_auth(client: TestClient, instance_data: dict[str, Any]) -> None:
    resp = client.post("/v1/instances", json=instance_data)
    assert resp.status_code == 401


# ------------------------------------------------------------------- happy lifecycle


def test_happy_path_lifecycle(
    client: TestClient,
    auth_headers: dict[str, str],
    instance_data: dict[str, Any],
) -> None:
    # create -> 201
    resp = client.post("/v1/instances", json=instance_data, headers=auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["instance"]["name"] == NAME
    assert body["status"]["exists"] is True
    assert body["status"]["running"] is True

    # list
    resp = client.get("/v1/instances", headers=auth_headers)
    assert resp.status_code == 200
    listing = resp.json()
    assert isinstance(listing, list)
    assert any(i["name"] == NAME for i in listing)

    # get
    resp = client.get(f"/v1/instances/{NAME}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == NAME

    # status
    resp = client.get(f"/v1/instances/{NAME}/status", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["running"] is True

    # stop
    resp = client.post(f"/v1/instances/{NAME}/stop", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["running"] is False

    # start
    resp = client.post(f"/v1/instances/{NAME}/start", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["running"] is True

    # restart
    resp = client.post(f"/v1/instances/{NAME}/restart", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["running"] is True

    # logs
    resp = client.get(f"/v1/instances/{NAME}/logs", headers=auth_headers)
    assert resp.status_code == 200
    assert "logs" in resp.json()

    # delete -> 200 with the domain-leave result (never a silent 204)
    resp = client.delete(f"/v1/instances/{NAME}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["removed"] is True
    assert resp.json()["domain_leave"]["ok"] is True

    # gone
    resp = client.get(f"/v1/instances/{NAME}", headers=auth_headers)
    assert resp.status_code == 404


# ------------------------------------------------------------------------- negatives


def test_get_unknown_is_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.get("/v1/instances/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404


def test_api_rejects_traversal_name(client: TestClient, auth_headers: dict[str, str]) -> None:
    # {name} flows into Store filenames + docker names -> must reject traversal/injection.
    for bad in ("..%2f..%2fetc%2fpasswd", "a%2fb", "..;bad", "-leading"):
        resp = client.get(f"/v1/instances/{bad}", headers=auth_headers)
        assert resp.status_code in (404, 422), (bad, resp.status_code)


def test_create_invalid_body_is_422(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    # NEGATIVE: a lowercase realm must be rejected at the pydantic boundary (422),
    # never reach the reconciler/docker.
    bad = {**instance_data, "realm": "linuxmuster.lan"}
    resp = client.post("/v1/instances", json=bad, headers=auth_headers)
    assert resp.status_code == 422


def test_create_bare_image_is_422(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    # NEGATIVE: a bare repo image (pull-all-tags DoS) is rejected at the boundary.
    bad = {**instance_data, "image": "ubuntu"}
    resp = client.post("/v1/instances", json=bad, headers=auth_headers)
    assert resp.status_code == 422


def test_patch_merges_onto_existing(
    client: TestClient,
    auth_headers: dict[str, str],
    instance_data: dict[str, Any],
) -> None:
    created = client.post("/v1/instances", json=instance_data, headers=auth_headers)
    assert created.status_code == 201

    resp = client.patch(
        f"/v1/instances/{NAME}",
        json={"wifi_group": "wlan"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["instance"]["wifi_group"] == "wlan"
    # untouched field preserved from the stored instance
    assert body["instance"]["realm"] == instance_data["realm"]

    stored = client.get(f"/v1/instances/{NAME}", headers=auth_headers).json()
    assert stored["wifi_group"] == "wlan"


def test_patch_cannot_change_identity(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    # NEGATIVE: 'name' is not a patchable field -> ignored; identity stays the same.
    client.post("/v1/instances", json=instance_data, headers=auth_headers)
    resp = client.patch(
        f"/v1/instances/{NAME}",
        json={"name": "other", "wifi_group": "wlan"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["instance"]["name"] == NAME
    # the phantom "other" name was never created
    assert client.get("/v1/instances/other", headers=auth_headers).status_code == 404


def test_dockerd_down_returns_503(
    client: TestClient,
    auth_headers: dict[str, str],
    docker: Any,
    instance_data: dict[str, Any],
    monkeypatch: Any,
) -> None:
    # NEGATIVE: a DockerException from the daemon must surface as 503, not a raw 500.
    from docker.errors import DockerException

    client.post("/v1/instances", json=instance_data, headers=auth_headers)

    def boom(*_a: Any, **_k: Any) -> None:
        raise DockerException("daemon down")

    monkeypatch.setattr(docker, "status", boom)
    resp = client.get(f"/v1/instances/{NAME}/status", headers=auth_headers)
    assert resp.status_code == 503
    assert "docker daemon unreachable" in resp.json()["detail"]


def test_reconcile_endpoint(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    client.post("/v1/instances", json=instance_data, headers=auth_headers)
    resp = client.post("/v1/reconcile", headers=auth_headers)
    assert resp.status_code == 200
    names = [s["name"] for s in resp.json()["reconciled"]]
    assert NAME in names
    # auth required
    assert client.post("/v1/reconcile").status_code == 401


def test_log_query_endpoint_filters(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    client.post("/v1/instances", json=instance_data, headers=auth_headers)
    resp = client.get(
        f"/v1/instances/{NAME}/logs", params={"grep": "radiusd"}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert "radiusd" in resp.json()["logs"]
    assert "winbindd" not in resp.json()["logs"]


def test_log_tail_bounds_are_422(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    # NEGATIVE: out-of-range tail is rejected before touching docker.
    client.post("/v1/instances", json=instance_data, headers=auth_headers)
    for bad_tail in (0, 20000):
        r = client.get(
            f"/v1/instances/{NAME}/logs", params={"tail": bad_tail}, headers=auth_headers
        )
        assert r.status_code == 422, bad_tail


def test_insecure_bind_warns(caplog: Any) -> None:
    import logging as _logging

    from lmnradius.main import _warn_if_insecure_bind

    with caplog.at_level(_logging.WARNING, logger="lmnradius"):
        _warn_if_insecure_bind("0.0.0.0")
    assert "cleartext" in caplog.text
    caplog.clear()
    with caplog.at_level(_logging.WARNING, logger="lmnradius"):
        _warn_if_insecure_bind("127.0.0.1")
    assert "cleartext" not in caplog.text


def test_missing_cert_precondition_is_409_with_detail(
    client: TestClient,
    auth_headers: dict[str, str],
    instance_data: dict[str, Any],
    docker: Any,
    monkeypatch: Any,
) -> None:
    """The fail-closed apply path raises FileNotFoundError with an actionable message
    (e.g. create before 'cert issue'); the API must surface it as 409 + detail, not
    swallow it into a bare 500 (seen on a real first install)."""

    def _boom(_inst: Any) -> dict[str, Any]:
        raise FileNotFoundError("EAP cert material missing: /x/ca.pem (run 'lmnradius cert issue')")

    monkeypatch.setattr(docker, "ensure_running", _boom)
    resp = client.post("/v1/instances", json=instance_data, headers=auth_headers)
    assert resp.status_code == 409
    assert "cert issue" in resp.json()["detail"]


def test_unreadable_secret_precondition_is_409(
    client: TestClient,
    auth_headers: dict[str, str],
    instance_data: dict[str, Any],
    docker: Any,
    monkeypatch: Any,
) -> None:
    def _boom(_inst: Any) -> dict[str, Any]:
        raise PermissionError("/etc/linuxmuster-radius/secrets/radius.secret")

    monkeypatch.setattr(docker, "ensure_running", _boom)
    resp = client.post("/v1/instances", json=instance_data, headers=auth_headers)
    assert resp.status_code == 409
    assert "permission denied" in resp.json()["detail"]


def _create(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    assert client.post("/v1/instances", json=instance_data, headers=auth_headers).status_code < 300


def test_test_endpoint_trust_only(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    _create(client, auth_headers, instance_data)
    resp = client.post(f"/v1/instances/{instance_data['name']}/test", json={}, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["container_running"] is True
    assert body["trust"]["ok"] is True
    assert "login" not in body  # no user -> trust-only


def test_test_endpoint_login_and_gates(
    client: TestClient,
    auth_headers: dict[str, str],
    instance_data: dict[str, Any],
    docker: Any,
) -> None:
    _create(client, auth_headers, instance_data)
    grp = instance_data["ssids"][0]["allowed_group"]
    docker.test_members = {f"alice:{instance_data['wifi_group']}", f"alice:{grp}"}
    resp = client.post(
        f"/v1/instances/{instance_data['name']}/test",
        json={"user": "alice", "password": "good"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["login"]["ok"] is True
    assert any(g["member"] for g in body["gates"])


def test_test_endpoint_wrong_password(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    _create(client, auth_headers, instance_data)
    resp = client.post(
        f"/v1/instances/{instance_data['name']}/test",
        json={"user": "alice", "password": "nope"},
        headers=auth_headers,
    )
    assert resp.json()["login"]["code"] == "NT_STATUS_WRONG_PASSWORD"


def test_test_endpoint_user_without_password_is_422(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    _create(client, auth_headers, instance_data)
    resp = client.post(
        f"/v1/instances/{instance_data['name']}/test", json={"user": "alice"}, headers=auth_headers
    )
    assert resp.status_code == 422


def test_test_endpoint_rejects_bad_username(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    _create(client, auth_headers, instance_data)
    resp = client.post(
        f"/v1/instances/{instance_data['name']}/test",
        json={"user": "a)(uid=*", "password": "x"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


# ------------------------------------------------------- LDAPS CA pinning (7.3.1)


def test_create_ldaps_without_ca_is_422(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    # NEGATIVE + security: an ldaps:// instance without a CA to verify the DC against
    # is refused (strict by default); the detail tells the operator both options.
    body = {k: v for k, v in instance_data.items() if k != "ldap_ca_pem"}
    resp = client.post("/v1/instances", json=body, headers=auth_headers)
    assert resp.status_code == 422
    assert "--ldap-ca" in resp.json()["detail"]
    assert "--ldap-ca-tofu" in resp.json()["detail"]
    assert client.get(f"/v1/instances/{NAME}", headers=auth_headers).status_code == 404


def test_create_plain_ldap_needs_no_ca(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    body = {k: v for k, v in instance_data.items() if k != "ldap_ca_pem"}
    body["ldap_server"] = "ldap://dc.linuxmuster.lan"
    resp = client.post("/v1/instances", json=body, headers=auth_headers)
    assert resp.status_code == 201
    assert resp.json()["instance"]["ldap_ca"] is None


def test_create_with_ca_pins_it(
    client: TestClient,
    auth_headers: dict[str, str],
    instance_data: dict[str, Any],
    settings: Any,
    docker: Any,
    dc_ca_pem: str,
) -> None:
    import os

    resp = client.post("/v1/instances", json=instance_data, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["instance"]["ldap_ca"] == "ldap-ca.pem"
    assert body["ldap_ca"]["trust_anchor"] is True
    assert body["ldap_ca"]["certificates"][0]["subject"] == "CN=test DC CA"
    assert len(body["ldap_ca"]["certificates"][0]["sha256_fingerprint"]) == 64
    # stored under certs_dir/<name>/ with 0600 in a 0700 directory
    path = os.path.join(settings.certs_dir, NAME, "ldap-ca.pem")
    assert open(path, encoding="utf-8").read() == dc_ca_pem
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(os.path.dirname(path)).st_mode & 0o777) == "0o700"
    # the persisted record references the file and the container got LDAP_CA
    assert (
        client.get(f"/v1/instances/{NAME}", headers=auth_headers).json()["ldap_ca"] == "ldap-ca.pem"
    )
    assert "ldap_ca_pem" not in client.get(f"/v1/instances/{NAME}", headers=auth_headers).json()


def test_create_with_garbage_pem_is_422(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    resp = client.post(
        "/v1/instances", json={**instance_data, "ldap_ca_pem": "not a pem"}, headers=auth_headers
    )
    assert resp.status_code == 422
    assert "ldap_ca_pem" in resp.json()["detail"]


def test_set_ldap_ca_on_legacy_instance(
    client: TestClient,
    auth_headers: dict[str, str],
    instance_data: dict[str, Any],
    store: Any,
    docker: Any,
    dc_ca_pem: str,
) -> None:
    from lmnradius.models import Instance

    # A record written by 7.3.0 (no ldap_ca) is listed as unverified ...
    store.put(Instance(**instance_data))
    listed = client.get("/v1/instances", headers=auth_headers).json()
    assert listed[0]["ldap_ca"] is None
    # ... and the upgrade step pins the CA and re-applies the instance.
    resp = client.put(
        f"/v1/instances/{NAME}/ldap-ca", json={"pem": dc_ca_pem}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["instance"]["ldap_ca"] == "ldap-ca.pem"
    assert resp.json()["status"]["running"] is True
    assert docker.ensure_calls == [NAME]
    assert (
        client.get(f"/v1/instances/{NAME}", headers=auth_headers).json()["ldap_ca"] == "ldap-ca.pem"
    )
    # unknown instance / garbage pem
    assert (
        client.put(
            "/v1/instances/nope/ldap-ca", json={"pem": dc_ca_pem}, headers=auth_headers
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"/v1/instances/{NAME}/ldap-ca", json={"pem": "junk"}, headers=auth_headers
        ).status_code
        == 422
    )
    assert client.put(f"/v1/instances/{NAME}/ldap-ca", json={"pem": dc_ca_pem}).status_code == 401


def test_delete_reports_failed_domain_leave(
    client: TestClient, auth_headers: dict[str, str], instance_data: dict[str, Any], docker: Any
) -> None:
    client.post("/v1/instances", json=instance_data, headers=auth_headers)
    docker.leave_ok = False
    resp = client.delete(f"/v1/instances/{NAME}", headers=auth_headers)
    # removed locally, but the failed leave is reported, not hidden
    assert resp.status_code == 200
    assert resp.json()["removed"] is True
    assert resp.json()["domain_leave"]["ok"] is False
    assert "unreachable" in resp.json()["domain_leave"]["detail"]
    assert client.get(f"/v1/instances/{NAME}", headers=auth_headers).status_code == 404


def test_store_failure_is_500_with_detail(
    client: TestClient,
    auth_headers: dict[str, str],
    instance_data: dict[str, Any],
    store: Any,
    monkeypatch: Any,
) -> None:
    from lmnradius.store import StoreError

    def _boom(_inst: Any) -> None:
        raise StoreError("instance change log: git commit exited 128: fatal: not a git repository")

    monkeypatch.setattr(store, "put", _boom)
    resp = client.post("/v1/instances", json=instance_data, headers=auth_headers)
    assert resp.status_code == 500
    assert "change log" in resp.json()["detail"]
