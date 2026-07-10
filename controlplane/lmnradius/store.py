# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Git-backed YAML store for :class:`~lmnradius.models.Instance` objects."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from pathlib import Path

import yaml

from .models import Instance

logger = logging.getLogger("lmnradius.store")


class Store:
    """Persist instances as ``<name>.yaml`` files, best-effort git-committed."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def _file(self, name: str) -> Path:
        if not name or "/" in name or "\\" in name or ".." in name:
            raise ValueError(f"unsafe instance name: {name!r}")
        return self.path / f"{name}.yaml"

    def list(self) -> list[Instance]:
        """Return all stored instances, sorted by name."""
        instances: list[Instance] = []
        for file in sorted(self.path.glob("*.yaml")):
            try:
                data = yaml.safe_load(file.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                logger.warning("failed to read instance file %s", file.name)
                continue
            if not isinstance(data, dict):
                continue
            try:
                instances.append(Instance(**data))
            except Exception:  # noqa: BLE001 - skip invalid records, keep listing
                logger.warning("invalid instance record in %s", file.name)
        return instances

    def get(self, name: str) -> Instance | None:
        """Return the instance named ``name`` or ``None`` if absent."""
        file = self._file(name)
        if not file.is_file():
            return None
        try:
            data = yaml.safe_load(file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            logger.warning("failed to read instance file %s", file.name)
            return None
        if not isinstance(data, dict):
            return None
        return Instance(**data)

    def put(self, inst: Instance) -> None:
        """Write ``inst`` to disk and, if inside a git repo, commit it."""
        file = self._file(inst.name)
        # ``name`` is a required INPUT field here (unlike squid, where it is a computed
        # field), so it MUST be persisted — excluding it would make get()/list() fail to
        # reconstruct the Instance. Only the computed ``container_name`` is dropped.
        payload = inst.model_dump(exclude={"container_name"})
        file.write_text(
            yaml.safe_dump(payload, default_flow_style=False, sort_keys=True),
            encoding="utf-8",
        )
        self._git(["add", "--", file.name], f"add {file.name}")
        self._git(
            ["commit", "-m", f"lmnradius: update {inst.name}", "--", file.name],
            f"commit {file.name}",
        )

    def delete(self, name: str) -> None:
        """Remove the instance file and, if inside a git repo, commit removal."""
        file = self._file(name)
        if not file.exists():
            return
        file.unlink()
        self._git(["rm", "--ignore-unmatch", "--", file.name], f"rm {file.name}")
        self._git(
            ["commit", "-m", f"lmnradius: remove {name}", "--", file.name],
            f"commit removal of {file.name}",
        )

    def _in_git_repo(self) -> bool:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=self.path,
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _git(self, args: Sequence[str], what: str) -> None:
        """Run a git command best-effort; never raise on failure."""
        if not self._in_git_repo():
            return
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.path,
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("git %s failed: %s", what, exc)
            return
        if result.returncode != 0:
            logger.debug(
                "git %s exited %d: %s",
                what,
                result.returncode,
                result.stderr.strip(),
            )
