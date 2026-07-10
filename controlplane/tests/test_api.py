# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTP-level tests for the FastAPI control-plane API.

Uses the Starlette/httpx TestClient with the in-memory FakeDockerService, so no
real Docker daemon is required. The negative tests cover the auth boundary
(401/403), unknown/traversal names (404/422), invalid bodies (422) and a down
Docker daemon (503).
"""

from __future__ import annotations

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
    assert "version" in resp.json()


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

    # delete -> 204
    resp = client.delete(f"/v1/instances/{NAME}", headers=auth_headers)
    assert resp.status_code == 204

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
