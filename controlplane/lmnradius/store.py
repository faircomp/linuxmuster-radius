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

# Commit identity, passed per call. The instances directory is the operator-facing
# change log (docs/operations.md), so a commit must never fail on a missing
# user.name/user.email -- which is exactly what happened on 0.1.x/7.3.0: the postinst
# set the identity as root in a directory owned by lmnradius, git refused ("dubious
# ownership"), and every commit died silently with "empty ident name".
_GIT_IDENT = ["-c", "user.name=linuxmuster-radius", "-c", "user.email=lmnradius@localhost"]


class StoreError(RuntimeError):
    """The instance change log (git) rejected an operation."""


class Store:
    """Persist instances as ``<name>.yaml`` files, git-committed when the directory
    is a repository (the postinst creates it). A failing git command raises
    :class:`StoreError` -- a silently broken change log is worse than a loud one."""

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
        """Write ``inst`` to disk and, if inside a git repo, commit it.

        :raises StoreError: the directory is a git repository but the commit failed.
        """
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
        self._commit(f"lmnradius: update {inst.name}", file.name)

    def delete(self, name: str) -> None:
        """Remove the instance file and, if inside a git repo, commit removal.

        :raises StoreError: the directory is a git repository but the commit failed.
        """
        file = self._file(name)
        # The updater's rollback pointer (<name>.prev) belongs to the removed instance;
        # untracked by git, so a plain unlink (found left behind in the lab, 2026-09-22).
        file.with_suffix(".prev").unlink(missing_ok=True)
        if not file.exists():
            return
        file.unlink()
        self._git(["rm", "-q", "--ignore-unmatch", "--", file.name], f"rm {file.name}")
        self._commit(f"lmnradius: remove {name}", file.name)

    def _commit(self, message: str, filename: str) -> None:
        # Nothing staged for this path (a put() that changed nothing, or a delete of a
        # file git never saw) is not an error; an empty commit would be.
        if not self._in_git_repo() or self._git_ok(["diff", "--cached", "--quiet", "--", filename]):
            return
        self._git(["commit", "-q", "-m", message, "--", filename], f"commit {filename}")

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

    def _git_ok(self, args: Sequence[str]) -> bool:
        """Run a git query; True iff it exits 0 (no output, no raise)."""
        try:
            result = subprocess.run(
                ["git", *args], cwd=self.path, capture_output=True, text=True, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _git(self, args: Sequence[str], what: str) -> None:
        """Run a git command inside the repo; fail loudly.

        Outside a git repository this is a no-op (logged once per call at WARNING so
        an operator who lost the repo notices), inside one a failing command raises
        :class:`StoreError` with git's message.
        """
        if not self._in_git_repo():
            logger.warning(
                "%s is not a git repository; instance changes are NOT versioned "
                "(git init as the service user to restore the change log)",
                self.path,
            )
            return
        try:
            result = subprocess.run(
                ["git", *_GIT_IDENT, *args],
                cwd=self.path,
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("git %s failed: %s", what, exc)
            raise StoreError(f"instance change log: git {what} failed: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            logger.error("git %s exited %d: %s", what, result.returncode, detail)
            raise StoreError(
                f"instance change log: git {what} exited {result.returncode}: {detail}"
            )
