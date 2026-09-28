# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Repo-level contracts that have no other test: the Renovate regex manager must see
every pinned CI gate tool in ci.yml (a four-component version like
types-PyYAML==6.0.12.20260906 slipped through the old three-component pattern), the
resolver uv is pinned by hash in its own lockfile, not as a bare ci.yml `uv==` line (K1), and
no workflow step installs anything from a lockfile before the lock gate ran in that job (R2).
scripts/tests/lock_gates.sh replays those steps with a planted package; these tests keep the
order from being edited away. The scripts a caller starts clean their environment with one and
the same block before any other command (A3), and run.sh calls nothing from the caller's PATH
before its gate (A4); lock_gates.sh (R1) and make_deb_checks.sh show that this holds."""

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


def test_every_job_that_runs_the_lock_tests_passes_the_gate_first() -> None:
    # lock_gates.sh and make_deb_checks.sh copy the committed lockfiles into their cases; a job
    # runs them only after a step that ran the gate on those files (cold verification r3, F1).
    for wf in ("ci.yml", "release.yml"):
        for job, spec in _jobs(wf).items():
            gated = False
            for step in spec.get("steps", []):
                run = step.get("run", "")
                if (
                    "scripts/tests/lock_gates.sh" in run
                    or "scripts/tests/make_deb_checks.sh" in run
                ):
                    assert gated, f"{wf}/{job}: {step.get('name')!r} runs before the lock gate"
                if re.search(r"(^|\s)bash scripts/check-lockfiles\.sh\s*$", run, re.MULTILINE):
                    gated = True


def test_fast_tier_runs_the_gate_first_and_lockfile_jobs_are_only_the_gate() -> None:
    fast = _jobs("ci.yml")["fast"]["steps"]
    runs = [s["run"] for s in fast if "run" in s]
    assert runs[0].strip() == GATE_STEP, "the lock gate is the first run step of the fast tier"
    for wf in ("ci.yml", "release.yml"):
        steps = _jobs(wf)["lockfile"]["steps"]
        assert [s["run"].strip() for s in steps if "run" in s] == [GATE_STEP]
        assert not any("setup-python" in s.get("uses", "") for s in steps)
    ci_lockfile, release_lockfile = (
        dict(_jobs("ci.yml")["lockfile"]),
        dict(_jobs("release.yml")["lockfile"]),
    )
    assert release_lockfile.pop("name") == "release: lockfile"
    assert ci_lockfile == release_lockfile
    # the release waits for it
    assert "lockfile" in _jobs("release.yml")["release"]["needs"]


def test_release_jobs_never_carry_the_name_of_a_required_ci_check() -> None:
    # The main ruleset requires the ci.yml jobs by name; a release.yml run on the same commit
    # (workflow_dispatch on a PR branch) must not report a check that could stand in for one.
    ci_names = {spec.get("name", key) for key, spec in _jobs("ci.yml").items()}
    for key, spec in _jobs("release.yml").items():
        assert spec.get("name") == f"release: {key}", f"release.yml/{key}: display name"
        assert spec["name"] not in ci_names


def test_make_deb_never_waives_the_git_ownership_guard_wholesale() -> None:
    script = (ROOT / "packaging" / "make-deb.sh").read_text(encoding="utf-8")
    assert not re.search(r"safe\.directory\s*=\s*['\"]?\*", script)
    for wf in ("ci.yml", "release.yml"):
        text = (WORKFLOWS / wf).read_text(encoding="utf-8")
        for line in re.findall(r".*safe\.directory.*", text):
            if line.lstrip().startswith("#"):
                continue
            assert line.strip() == 'git config --global --add safe.directory "$GITHUB_WORKSPACE"'


# The scripts a caller or the build starts: each starts itself again in a clean environment,
# with one and the same block, before any other command (P1).
ENV_SCRIPTS = (
    "scripts/check-lockfiles.sh",
    "packaging/build-venv.sh",
    "packaging/make-deb.sh",
    "scripts/tests/lock_gates.sh",
    "scripts/tests/make_deb_checks.sh",
)
BLOCK_FIRST = 'if [[ "${1-}" != --lmnradius-clean-env ]]; then'
ALLOWLIST = {
    "HOME",
    "TMPDIR",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "PIP_CERT",
    "LMNRADIUS_ALLOW_REAL",
    "LMNRADIUS_ALLOW_SKIP",
    "LMNRADIUS_CALLER_PATH",
    "LOCK_GATES_DEB",
    "LOCK_GATES_VERBOSE",
}


def _code(path: str) -> list[str]:
    """The lines of a shell script without comment lines and blank lines."""
    text = (ROOT / path).read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _block(code: list[str], path: str) -> tuple[list[str], int]:
    start = code.index(BLOCK_FIRST)
    end = code.index("shift", start)
    return code[start : end + 1], end + 1


def test_entry_scripts_start_again_in_a_clean_environment_first() -> None:
    blocks = {}
    for path in ENV_SCRIPTS:
        code = _code(path)
        # before anything else, even `set`: a function of the caller may be named set
        assert code[0] == BLOCK_FIRST, f"{path}: the first command must be the clean-env block"
        blocks[path], after = _block(code, path)
        assert code[after].startswith("set -"), f"{path}: `set` comes right after the block"
    first = blocks[ENV_SCRIPTS[0]]
    for path, block in blocks.items():
        assert block == first, f"{path}: its clean-env block differs from {ENV_SCRIPTS[0]}'s"
    text = "\n".join(first)
    # up to the exec: an assignment that makes `exec` win over functions, one absolute command
    assert first[1].strip() == "POSIXLY_CORRECT=1"
    assert first[2].strip().startswith("exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin ")
    assert "LANG=C.UTF-8 LC_ALL=C.UTF-8" in first[2]
    assert '/bin/bash -p "$0" --lmnradius-clean-env "$@"' in text
    passed = set(re.findall(r'\$\{(\w+)\+"\1=\$\1"\}', text))
    assert passed == ALLOWLIST, sorted(passed ^ ALLOWLIST)
    # nothing else is passed on: every word of the exec is a fixed assignment or one of those
    words = re.findall(r"\S+", " ".join(first[2:-2]).replace("\\", " "))
    for w in words:
        assert (
            w in ("exec", "/usr/bin/env", "-i", "/bin/bash", "-p", '"$0"', '"$@"')
            or w == "--lmnradius-clean-env"
            or re.fullmatch(r'\$\{(\w+)\+"\1=\$\1"\}', w)
            or w in ("PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C.UTF-8", "LC_ALL=C.UTF-8")
        ), w


def test_run_sh_restarts_clean_and_calls_nothing_from_the_callers_path_before_its_gate() -> None:
    code = _code("scripts/tests/run.sh")
    assert code[0] == 'LMNRADIUS_CALLER_PATH="${LMNRADIUS_CALLER_PATH-$PATH}"'
    block, after = _block(code, "scripts/tests/run.sh")
    assert code[1] == BLOCK_FIRST
    assert block == _block(_code(ENV_SCRIPTS[0]), ENV_SCRIPTS[0])[0]
    assert code[after : after + 3] == [
        'CALLER_PATH="$LMNRADIUS_CALLER_PATH"',
        "unset LMNRADIUS_CALLER_PATH",
        "set -uo pipefail",
    ]
    before_gate = code[: next(i for i, ln in enumerate(code) if ln.startswith("summary()"))]
    assert any("/usr/bin/dirname" in ln for ln in before_gate)
    for ln in before_gate:
        assert not re.search(r"(?<![/\w-])dirname\b", ln), ln
    assert any(ln.strip().startswith("if /bin/bash -p scripts/check-lockfiles.sh") for ln in code)


def test_make_deb_starts_make_deb_sh_with_bash_p() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "\n\t/bin/bash -p packaging/make-deb.sh\n" in makefile


def test_rules_refuse_a_build_directory_with_whitespace_and_quote_the_paths() -> None:
    rules = (ROOT / "debian" / "rules").read_text(encoding="utf-8")
    assert "ifneq ($(words $(CURDIR)),1)" in rules
    assert '\tbash packaging/build-venv.sh "$(ROOT)$(DEST)"\n' in rules
    assert '\tdebian/venv-relocate "$(ROOT)" "$(DEST)"\n' in rules
    assert '\tdebian/venv-relocate --verify "$(ROOT)" "$(DEST)"\n' in rules


def test_build_venv_refuses_a_bad_venv_path_before_any_rm() -> None:
    code = _code("packaging/build-venv.sh")
    guard = next(i for i, ln in enumerate(code) if "refusing venv path with whitespace" in ln)
    first_rm = next(i for i, ln in enumerate(code) if "rm -rf" in ln)
    assert guard < first_rm
