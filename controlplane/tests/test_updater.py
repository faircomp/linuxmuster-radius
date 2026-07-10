# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Updater: digest-pinned update with health-check auto-rollback (fake docker).

Updating a running RADIUS instance is risky: a bad image means every WLAN client
at that school fails 802.1X. So an update records the known-good image, applies
the new one, waits for *healthy*, and rolls back automatically otherwise. The
rollback paths below are the mandatory negative tests for this module.
"""

from __future__ import annotations

from typing import Any

import pytest

from lmnradius.models import DEFAULT_IMAGE, Instance
from lmnradius.reconciler import Reconciler
from lmnradius.store import Store
from lmnradius.updater import Updater

# The FakeDockerService (conftest) reports a container as "unhealthy" when the
# image name contains "bad", raises on "unpullable", otherwise "healthy".
GOOD_V2 = "ghcr.io/faircomp/linuxmuster-radius:0.2.0"
BAD = "ghcr.io/faircomp/linuxmuster-radius:bad"
UNPULLABLE = "ghcr.io/faircomp/linuxmuster-radius:unpullable"


def _updater(store: Store, docker: Any, reconciler: Reconciler) -> Updater:
    return Updater(store, docker, reconciler, health_timeout=1.0, poll_interval=0.0)


def test_update_success_pins_new_image(
    store: Store, docker: Any, reconciler: Reconciler, instance: Instance
) -> None:
    reconciler.apply(instance)  # baseline (healthy)
    up = _updater(store, docker, reconciler)

    res = up.update(instance.name, GOOD_V2)

    assert res["updated"] is True
    assert res["image"] == GOOD_V2
    assert store.get(instance.name).image == GOOD_V2  # type: ignore[union-attr]


def test_update_bad_image_auto_rolls_back(
    store: Store, docker: Any, reconciler: Reconciler, instance: Instance
) -> None:
    # NEGATIVE: new image comes up unhealthy -> health-gated auto-rollback.
    reconciler.apply(instance)
    good = instance.image
    up = _updater(store, docker, reconciler)

    res = up.update(instance.name, BAD)

    assert res["updated"] is False
    assert res["rolled_back_to"] == good
    assert store.get(instance.name).image == good  # type: ignore[union-attr]
    assert docker.status(instance.name)["health"] == "healthy"


def test_update_rolls_back_when_apply_raises(
    store: Store, docker: Any, reconciler: Reconciler, instance: Instance
) -> None:
    # NEGATIVE: 'unpullable' makes the fake raise from ensure_running AFTER removing
    # the old container -> the Updater must restore the known-good image, not leave
    # the school offline pinned to a broken image.
    reconciler.apply(instance)
    good = instance.image
    up = _updater(store, docker, reconciler)

    res = up.update(instance.name, UNPULLABLE)

    assert res["updated"] is False
    assert res["rolled_back_to"] == good
    assert store.get(instance.name).image == good  # type: ignore[union-attr]
    assert docker.status(instance.name)["health"] == "healthy"


def test_update_unknown_instance_raises(store: Store, docker: Any, reconciler: Reconciler) -> None:
    # NEGATIVE: updating a name that was never created.
    up = _updater(store, docker, reconciler)
    with pytest.raises(KeyError):
        up.update("nonexistent", GOOD_V2)


def test_explicit_rollback(
    store: Store, docker: Any, reconciler: Reconciler, instance: Instance
) -> None:
    reconciler.apply(instance)
    good = instance.image
    up = _updater(store, docker, reconciler)
    up.update(instance.name, GOOD_V2)  # records prev=good, pins v2

    res = up.rollback(instance.name)

    assert res["rolled_back_to"] == good
    assert store.get(instance.name).image == good  # type: ignore[union-attr]


def test_rollback_without_recorded_previous_raises(
    store: Store, docker: Any, reconciler: Reconciler, instance: Instance
) -> None:
    # NEGATIVE: no prior update -> no known-good recorded -> refuse.
    reconciler.apply(instance)
    up = _updater(store, docker, reconciler)
    with pytest.raises(FileNotFoundError):
        up.rollback(instance.name)


def test_update_all_lifts_stale_and_skips_current(
    store: Store, docker: Any, reconciler: Reconciler, instance: Instance
) -> None:
    # stale instance on an old image
    reconciler.apply(
        instance.model_copy(update={"image": "ghcr.io/faircomp/linuxmuster-radius:0.0.9"})
    )
    up = _updater(store, docker, reconciler)

    results = {r["name"]: r for r in up.update_all(DEFAULT_IMAGE)}

    assert results[instance.name]["updated"] is True
    assert store.get(instance.name).image == DEFAULT_IMAGE  # type: ignore[union-attr]

    # a second run is a no-op: already current -> skipped (no recreate)
    docker.ensure_calls.clear()
    results2 = {r["name"]: r for r in up.update_all(DEFAULT_IMAGE)}
    assert results2[instance.name].get("skipped") is True
    assert docker.ensure_calls == []


def test_update_all_bad_target_rolls_back_each_and_does_not_abort(
    store: Store, docker: Any, reconciler: Reconciler
) -> None:
    # NEGATIVE: an unpullable batch target must roll BOTH back and not abort midway.
    def _inst(name: str) -> Instance:
        return Instance(
            name=name,
            realm="LINUXMUSTER.LAN",
            workgroup="LINUXMUSTER",
            server_fqdn=f"{name}.linuxmuster.lan",
            ldap_server="ldaps://dc.linuxmuster.lan",
            ldap_base_dn="DC=linuxmuster,DC=lan",
            ldap_bind_dn="CN=global-binduser,OU=Management,DC=linuxmuster,DC=lan",
            client_subnets=["10.0.0.0/16"],
            ssids=[{"name": f"{name}-lehrer", "allowed_group": "teachers", "vlan": 20}],
            join_secret=f"{name}-join.secret",
            ldap_bind_secret=f"{name}-ldap.secret",
            radius_secret=f"{name}-radius.secret",
            image="ghcr.io/faircomp/linuxmuster-radius:0.0.9",
        )

    reconciler.apply(_inst("aaa"))
    reconciler.apply(_inst("bbb"))
    up = _updater(store, docker, reconciler)

    results = {r["name"]: r for r in up.update_all(UNPULLABLE)}

    assert set(results) == {"aaa", "bbb"}
    for name in ("aaa", "bbb"):
        assert results[name]["updated"] is False
        assert store.get(name).image == "ghcr.io/faircomp/linuxmuster-radius:0.0.9"  # type: ignore[union-attr]
        assert docker.status(name)["health"] == "healthy"


def test_update_endpoint(
    client: Any, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    client.post("/v1/instances", json=instance_data, headers=auth_headers)
    resp = client.post(
        f"/v1/instances/{instance_data['name']}/update",
        json={"image": GOOD_V2},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] is True


def test_update_endpoint_bad_image_reports_rollback(
    client: Any, auth_headers: dict[str, str], instance_data: dict[str, Any]
) -> None:
    # NEGATIVE (HTTP): a bad image returns 200 with updated=False (rollback happened),
    # NOT a 500 — the school stays online on the known-good image.
    client.post("/v1/instances", json=instance_data, headers=auth_headers)
    resp = client.post(
        f"/v1/instances/{instance_data['name']}/update",
        json={"image": BAD},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] is False
    assert body["rolled_back_to"] == instance_data["image"]
