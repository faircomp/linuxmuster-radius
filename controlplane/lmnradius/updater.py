# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Digest-pinned image updates with health-check driven auto-rollback.

Updating a running RADIUS server at a school is risky: a bad image means every
WLAN client fails 802.1X. So an update always records the previous (known-good)
image, applies the new one, waits for the container to become *healthy* (winbind
trust up AND radiusd answering — see image/healthcheck.sh), and rolls back
automatically if it does not.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .docker_service import DockerService
from .reconciler import Reconciler
from .store import Store

audit = logging.getLogger("lmnradius.audit")


class Updater:
    """Perform health-gated, auto-rolling image updates for an instance."""

    def __init__(
        self,
        store: Store,
        docker: DockerService,
        reconciler: Reconciler,
        health_timeout: float = 120.0,
        poll_interval: float = 3.0,
    ) -> None:
        self.store = store
        self.docker = docker
        self.reconciler = reconciler
        # Longer default than squid: the domain-member container's healthcheck only
        # passes once winbind's secure channel to the DC is up (image start-period 45s).
        self.health_timeout = health_timeout
        self.poll_interval = poll_interval

    def _prev_file(self, name: str) -> Path:
        # Lives beside the yaml files; Store.list() only globs *.yaml so this is ignored there.
        return self.store.path / f"{name}.prev"

    def update(self, name: str, new_image: str) -> dict[str, Any]:
        """Update ``name`` to ``new_image``; auto-rollback if it does not turn healthy."""
        inst = self.store.get(name)
        if inst is None:
            raise KeyError(name)

        previous_image = inst.image
        self._prev_file(name).write_text(previous_image, encoding="utf-8")
        audit.info("update start name=%s from=%s to=%s", name, previous_image, new_image)

        try:
            self.reconciler.apply(inst.model_copy(update={"image": new_image}))
        except Exception as exc:
            # apply() rewrote the store and removed the old container BEFORE the new
            # one could start (e.g. the new image is unpullable). Broad catch on
            # purpose: any failure here must restore the known-good image, not leave
            # the school offline pinned to a broken image.
            audit.warning(
                "update apply failed name=%s (%s) -> rollback to %s", name, exc, previous_image
            )
            self.reconciler.apply(inst)  # inst still carries previous_image
            return {
                "name": name,
                "updated": False,
                "rolled_back_to": previous_image,
                "failed_image": new_image,
                "error": str(exc),
            }
        if self._wait_healthy(name):
            audit.info("update ok name=%s image=%s", name, new_image)
            return {
                "name": name,
                "updated": True,
                "image": new_image,
                "previous_image": previous_image,
            }

        # Not healthy in time -> restore the known-good image.
        audit.warning("update unhealthy name=%s -> rollback to %s", name, previous_image)
        self.reconciler.apply(inst)  # inst still carries previous_image
        return {
            "name": name,
            "updated": False,
            "rolled_back_to": previous_image,
            "failed_image": new_image,
        }

    def update_all(self, target_image: str) -> list[dict[str, Any]]:
        """Update every stored instance to ``target_image``, each with its own
        health-gated auto-rollback. Instances already on the target are skipped
        so a no-op run (e.g. a ``.deb`` upgrade whose default did not move) does
        not needlessly recreate any container."""
        results: list[dict[str, Any]] = []
        for inst in self.store.list():
            if inst.image == target_image:
                results.append({"name": inst.name, "updated": False, "skipped": True})
                continue
            try:
                results.append(self.update(inst.name, target_image))
            except Exception as exc:
                # One instance failing (even during its own rollback) must not abort
                # the whole batch — record it and carry on to the rest.
                audit.error("update-all failed name=%s (%s)", inst.name, exc)
                results.append({"name": inst.name, "updated": False, "error": str(exc)})
        return results

    def rollback(self, name: str) -> dict[str, Any]:
        """Revert ``name`` to the image recorded before the last update."""
        inst = self.store.get(name)
        if inst is None:
            raise KeyError(name)
        prev_file = self._prev_file(name)
        if not prev_file.is_file():
            raise FileNotFoundError(f"no previous image recorded for {name}")
        previous_image = prev_file.read_text(encoding="utf-8").strip()
        audit.info("rollback name=%s to=%s", name, previous_image)
        self.reconciler.apply(inst.model_copy(update={"image": previous_image}))
        return {"name": name, "rolled_back_to": previous_image}

    def _wait_healthy(self, name: str) -> bool:
        deadline = time.monotonic() + self.health_timeout
        while True:
            st = self.docker.status(name)
            health = st.get("health")
            if health == "healthy":
                return True
            if health == "unhealthy":
                return False
            # Exists but crashed/exited: no healthcheck will ever pass -> fail fast.
            if st.get("exists") and not st.get("running") and health is None:
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.poll_interval)
