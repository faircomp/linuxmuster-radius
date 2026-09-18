# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Feed the package version from debian/changelog, the single version source.

pyproject.toml declares ``dynamic = ["version"]`` and setuptools takes the value
from here. The top entry of ``../debian/changelog`` is read the way
``dpkg-parsechangelog -S Version`` reads it (first line: ``<source> (<version>) ...``),
so ``pip install`` needs no dpkg-dev. Debian's ``~`` pre-release separator becomes
its PEP 440 form (``7.3.1~rc1`` -> ``7.3.1rc1``); everything else is identical to
the .deb version.
"""

from __future__ import annotations

import re
from pathlib import Path

from setuptools import setup

CHANGELOG = Path(__file__).resolve().parent.parent / "debian" / "changelog"
HEAD = re.compile(r"^\S+ \((?P<version>[^)]+)\) ")


def changelog_version() -> str:
    with CHANGELOG.open(encoding="utf-8") as fh:
        first = fh.readline()
    match = HEAD.match(first)
    if match is None:
        raise SystemExit(f"{CHANGELOG}: cannot parse a version from {first!r}")
    return match["version"].replace("~", "")


setup(version=changelog_version())
