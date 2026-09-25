#!/usr/bin/python3
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Print the `run:` script of one step of a GitHub workflow job (stdlib only, no PyYAML).

    ci_step.py WORKFLOW JOB STEP-NAME

For scripts/tests/lock_gates.sh, which replays the lock-related steps of ci.yml and
release.yml word for word with a planted lockfile package (R2): the test runs what CI runs,
not a copy that could drift. Reads the layout these workflows use -- `jobs:` at column 0, a
job at 2, `- name:` of a step, `run: |` block or one-line `run:` -- and fails loudly on
anything else, so a reformatted workflow breaks the test instead of silently testing nothing.
"""

from __future__ import annotations

import re
import sys


def indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def main() -> int:
    path, job, name = sys.argv[1:4]
    lines = open(path, encoding="utf-8").read().split("\n")
    try:
        start = lines.index(f"  {job}:", lines.index("jobs:"))
    except ValueError:
        sys.exit(f"{path}: no job {job!r}")
    end = next(
        (n for n in range(start + 1, len(lines)) if re.match(r"^  \S", lines[n])),
        len(lines),
    )
    for n in range(start + 1, end):
        m = re.match(r"^( *)- name: (.*)$", lines[n])
        if not m or m[2].strip().strip("'\"") != name:
            continue
        dash, keys = len(m[1]), len(m[1]) + 2
        k = n + 1
        while k < end and (not lines[k].strip() or indent(lines[k]) > dash):
            line = lines[k]
            if indent(line) == keys and re.fullmatch(r" *run: \|", line):
                block = []
                k += 1
                while k < end and (not lines[k].strip() or indent(lines[k]) > keys):
                    block.append(lines[k])
                    k += 1
                while block and not block[-1].strip():
                    block.pop()
                if not block:
                    sys.exit(f"{path}: {job}/{name}: empty run block")
                cut = indent(block[0])
                print("\n".join(b[cut:] if b.strip() else "" for b in block))
                return 0
            one = re.fullmatch(r" *run: (\S.*)", line)
            if indent(line) == keys and one:
                print(one[1])
                return 0
            k += 1
        sys.exit(f"{path}: {job}/{name}: no run")
    sys.exit(f"{path}: no step {name!r} in job {job!r}")


if __name__ == "__main__":
    sys.exit(main())
