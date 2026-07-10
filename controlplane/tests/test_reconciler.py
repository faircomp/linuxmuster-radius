# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for :class:`lmnradius.reconciler.Reconciler` against the fake backend."""

from __future__ import annotations

from typing import Any

import pytest

from lmnradius.models import Instance
from lmnradius.reconciler import Reconciler
from lmnradius.store import Store

# ``docker`` is the FakeDockerService instance from conftest; annotated as Any
# to avoid a cross-module import of a test helper.


def _second() -> Instance:
    return Instance(
        name="schuleB",
        realm="LINUXMUSTER.LAN",
        workgroup="LINUXMUSTER",
        server_fqdn="radius-b.linuxmuster.lan",
        ldap_server="ldaps://dc.linuxmuster.lan",
        ldap_base_dn="DC=linuxmuster,DC=lan",
        ldap_bind_dn="CN=global-binduser,OU=Management,DC=linuxmuster,DC=lan",
        client_subnets=["10.9.0.0/16"],
        ssids=[{"name": "b-lehrer", "allowed_group": "schuleB-teachers", "vlan": 40}],
        join_secret="schuleB-join.secret",
        ldap_bind_secret="schuleB-ldap.secret",
        radius_secret="schuleB-radius.secret",
        image="ghcr.io/faircomp/linuxmuster-radius:0.1.0",
    )


def test_apply_persists_and_ensures_running(
    reconciler: Reconciler,
    store: Store,
    docker: Any,
    instance: Instance,
) -> None:
    status = reconciler.apply(instance)

    # persisted to the store (round-trips through YAML)
    persisted = store.get(instance.name)
    assert persisted is not None
    assert persisted.name == instance.name
    assert persisted.ssids[0].name == instance.ssids[0].name

    # ensure_running was invoked and reported a running container
    assert docker.ensure_calls == [instance.name]
    assert status["exists"] is True
    assert status["running"] is True


def test_remove_stops_docker_and_deletes_store(
    reconciler: Reconciler,
    store: Store,
    docker: Any,
    instance: Instance,
) -> None:
    reconciler.apply(instance)
    assert store.get(instance.name) is not None

    reconciler.remove(instance.name)

    assert instance.name in docker.removed
    assert store.get(instance.name) is None
    assert instance.name not in docker.containers


def test_reconcile_all_ensures_every_stored_instance(
    reconciler: Reconciler,
    store: Store,
    docker: Any,
    instance: Instance,
) -> None:
    second = _second()
    store.put(instance)
    store.put(second)

    docker.ensure_calls.clear()
    results = reconciler.reconcile_all()

    assert len(results) == 2
    assert set(docker.ensure_calls) == {instance.name, second.name}
    assert all(r["running"] is True for r in results)


def test_apply_propagates_ensure_running_failure(
    reconciler: Reconciler,
    store: Store,
    docker: Any,
    instance: Instance,
) -> None:
    # NEGATIVE: an unpullable image makes the fake remove the old container and
    # raise from ensure_running; the reconciler must not swallow it (the Updater,
    # not the Reconciler, is responsible for rollback).
    broken = instance.model_copy(update={"image": "ghcr.io/faircomp/linuxmuster-radius:unpullable"})
    with pytest.raises(RuntimeError):
        reconciler.apply(broken)
    # It still persisted the desired state before attempting the container op.
    assert store.get(instance.name) is not None
    assert instance.name not in docker.containers
