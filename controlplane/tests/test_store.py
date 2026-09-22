# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Store tests: the git change log must really commit, and fail loudly when it cannot.

Up to 7.3.0 the postinst left the repository without an identity and every commit
failed silently ("empty ident name"); the instance records were only ever staged.
These tests run real git in an isolated tmp repo (no global/system config, no
identity) and assert the commits exist.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from lmnradius.models import Instance
from lmnradius.store import Store, StoreError


@pytest.fixture
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No identity from the developer's ~/.gitconfig, no system config, nothing in env.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _git_config(repo: Path, key: str) -> str:
    # `git config --get` exits 1 when the key is unset -> treat as "".
    res = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", key],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "instances"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    return repo


def test_put_and_delete_commit_without_configured_identity(
    isolated_git: None, tmp_path: Path, instance: Instance
) -> None:
    repo = _repo(tmp_path)
    assert _git_config(repo, "user.name") == ""  # no identity anywhere, on purpose
    store = Store(str(repo))

    store.put(instance)
    assert _git(repo, "log", "--format=%s") == f"lmnradius: update {instance.name}"
    assert _git(repo, "status", "--porcelain") == ""  # nothing left staged

    store.put(instance.model_copy(update={"wifi_group": "all-wifi"}))
    store.delete(instance.name)
    log = _git(repo, "log", "--format=%s %an <%ae>").splitlines()
    assert log == [
        f"lmnradius: remove {instance.name} linuxmuster-radius <lmnradius@localhost>",
        f"lmnradius: update {instance.name} linuxmuster-radius <lmnradius@localhost>",
        f"lmnradius: update {instance.name} linuxmuster-radius <lmnradius@localhost>",
    ]
    assert not (repo / f"{instance.name}.yaml").exists()


def test_unchanged_put_is_not_an_empty_commit(
    isolated_git: None, tmp_path: Path, instance: Instance
) -> None:
    repo = _repo(tmp_path)
    store = Store(str(repo))
    store.put(instance)
    store.put(instance)  # identical content -> nothing to commit, no error
    assert len(_git(repo, "log", "--format=%h").splitlines()) == 1


def test_outside_a_repo_is_a_warning_not_an_error(
    isolated_git: None, tmp_path: Path, instance: Instance, caplog: Any
) -> None:
    store = Store(str(tmp_path / "plain"))
    store.put(instance)
    assert store.get(instance.name) is not None
    assert "not a git repository" in caplog.text


def test_git_failure_raises_store_error(
    isolated_git: None, tmp_path: Path, instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    # NEGATIVE: a repo whose commit fails (here: git itself reports an error) must
    # not be swallowed -- the record is written, the error surfaces to the API/CLI.
    repo = _repo(tmp_path)
    store = Store(str(repo))
    real_run = subprocess.run

    def failing(cmd: list[str], **kwargs: Any) -> Any:
        if cmd[0] == "git" and "commit" in cmd:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: simulated")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", failing)
    with pytest.raises(StoreError, match="git commit .* exited 128: fatal: simulated"):
        store.put(instance)
    assert (repo / f"{instance.name}.yaml").is_file()
