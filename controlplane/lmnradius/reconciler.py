# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reconcile desired instance state (store) with runtime state (Docker)."""

from __future__ import annotations

import logging

from .docker_service import DockerService
from .models import Instance
from .store import Store

logger = logging.getLogger("lmnradius.reconciler")


class Reconciler:
    """Bridge the persistent :class:`Store` and the live :class:`DockerService`."""

    def __init__(self, store: Store, docker: DockerService) -> None:
        self.store = store
        self.docker = docker

    def apply(self, inst: Instance) -> dict:
        """Persist ``inst`` then render its config and (re)create + start its container."""
        self.store.put(inst)
        return self.docker.ensure_running(inst)

    def remove(self, name: str) -> dict:
        """Leave the domain and remove the container, state volume and rendered config,
        then delete the instance from the store. Returns the domain-leave result
        (``{"ok", "attempted", "detail"}``) for the operator."""
        inst = self.store.get(name)
        if inst is None:
            leave: dict = {"ok": True, "attempted": False, "detail": "no instance record"}
        else:
            leave = self.docker.remove(inst)
        self.store.delete(name)
        return leave

    def reconcile_all(self) -> list[dict]:
        """Ensure every stored instance is running; return their statuses."""
        statuses: list[dict] = []
        for inst in self.store.list():
            statuses.append(self.docker.ensure_running(inst))
        return statuses
