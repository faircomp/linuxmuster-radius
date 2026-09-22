# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Repo-level contracts that have no other test: the Renovate regex manager must see
every pinned CI gate tool in ci.yml (a four-component version like
types-PyYAML==6.0.12.20260906 slipped through the old three-component pattern)."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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
    assert {"ruff", "mypy", "reuse", "pytest", "types-PyYAML"} <= set(pinned)
    found = {m["depName"]: m["currentValue"] for m in _renovate_ci_pattern().finditer(ci)}
    assert found == pinned
    assert found["types-PyYAML"].count(".") == 3  # the four-part version is captured whole
