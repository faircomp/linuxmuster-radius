# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Repo-level contracts that have no other test: the Renovate regex manager must see
every pinned CI gate tool in ci.yml (a four-component version like
types-PyYAML==6.0.12.20260906 slipped through the old three-component pattern), the
resolver uv is pinned by hash in its own lockfile, not as a bare ci.yml `uv==` line (K1), and
no workflow step installs anything from a lockfile before the lock gate ran in that job (R2).
scripts/tests/lock_gates.sh replays those steps with a planted package; these tests keep the
order from being edited away."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
GATE_STEP = "bash scripts/check-lockfiles.sh"
# A command that installs from a lockfile or the control plane (whose build needs the locks).
LOCK_INSTALL = re.compile(r"pip\S* install[^\n]*(-r\s+\S*\.lock|controlplane)")


def _lockfile_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "lockfile_gate", ROOT / "scripts" / "lockfile_gate.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve the module's annotations through it
    spec.loader.exec_module(mod)
    return mod


def _jobs(name: str) -> dict[str, Any]:
    doc = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    jobs: dict[str, Any] = doc["jobs"]
    return jobs


def _renovate_ci_pattern() -> re.Pattern[str]:
    cfg = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    manager = next(
        m for m in cfg["customManagers"] if any("ci" in p for p in m["managerFilePatterns"])
    )
    # Renovate uses JS-style named groups; Python wants (?P<name>...).
    return re.compile(manager["matchStrings"][0].replace("(?<", "(?P<"))


def test_renovate_matches_every_pinned_ci_tool() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    pinned = dict(re.findall(r"^\s+([A-Za-z0-9_.-]+)==([0-9][0-9.]*)", ci, re.MULTILINE))
    # uv is no longer a bare ci.yml pin: it is installed hash-pinned from its own lockfile
    # (K1), so it must NOT appear here (it would be an unhashed `pip install uv==...`).
    assert {"ruff", "mypy", "reuse", "pytest", "types-PyYAML"} <= set(pinned)
    assert "uv" not in pinned
    found = {m["depName"]: m["currentValue"] for m in _renovate_ci_pattern().finditer(ci)}
    assert found == pinned
    assert found["types-PyYAML"].count(".") == 3  # the four-part version is captured whole


def test_uv_is_pinned_by_hash_in_its_own_lockfile() -> None:
    # Parsed the way the gate parses it (every line, not only column 1): exactly uv, with
    # hashes, at the version uv-requirements.in pins.
    gate = _lockfile_gate()
    pins, errors = gate.parse(str(ROOT / "controlplane" / "uv-requirements.lock"))
    assert errors == []
    assert [p.name for p in pins] == ["uv"], "the uv lock must hold exactly uv"
    assert pins[0].hashes, "uv pin needs hashes"
    wanted = (ROOT / "controlplane" / "uv-requirements.in").read_text(encoding="ascii")
    assert f"uv=={pins[0].version}" in wanted.splitlines()


def test_no_workflow_installs_from_a_lockfile_before_the_lock_gate() -> None:
    for wf in ("ci.yml", "release.yml"):
        for job, spec in _jobs(wf).items():
            gated = False
            for step in spec.get("steps", []):
                run = step.get("run", "")
                if run.strip() == GATE_STEP:
                    gated = True
                assert not (LOCK_INSTALL.search(run) and not gated), (
                    f"{wf}/{job}: step {step.get('name')!r} installs from a lockfile "
                    f"before a `{GATE_STEP}` step"
                )
                # the gate brings its own uv; nothing installs the uv lock around it
                assert "uv-requirements.lock" not in run, f"{wf}/{job}: installs the uv lock"


def test_fast_tier_runs_the_gate_first_and_lockfile_jobs_are_only_the_gate() -> None:
    fast = _jobs("ci.yml")["fast"]["steps"]
    runs = [s["run"] for s in fast if "run" in s]
    assert runs[0].strip() == GATE_STEP, "the lock gate is the first run step of the fast tier"
    for wf in ("ci.yml", "release.yml"):
        steps = _jobs(wf)["lockfile"]["steps"]
        assert [s["run"].strip() for s in steps if "run" in s] == [GATE_STEP]
        assert not any("setup-python" in s.get("uses", "") for s in steps)
    assert _jobs("ci.yml")["lockfile"] == _jobs("release.yml")["lockfile"]
    # the release waits for it
    assert "lockfile" in _jobs("release.yml")["release"]["needs"]


def test_make_deb_never_waives_the_git_ownership_guard_wholesale() -> None:
    script = (ROOT / "packaging" / "make-deb.sh").read_text(encoding="utf-8")
    assert not re.search(r"safe\.directory\s*=\s*['\"]?\*", script)
    for wf in ("ci.yml", "release.yml"):
        text = (WORKFLOWS / wf).read_text(encoding="utf-8")
        for line in re.findall(r".*safe\.directory.*", text):
            if line.lstrip().startswith("#"):
                continue
            assert line.strip() == 'git config --global --add safe.directory "$GITHUB_WORKSPACE"'
