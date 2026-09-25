#!/usr/bin/python3
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the attack wheel of the lock-gate regression tests (stdlib only).

    k1_wheel.py OUTDIR MARKER  ->  prints the path of OUTDIR/zzzk1-1.0-py3-none-any.whl

The wheel of the cold verifications (linuxmusterDEV work/tasks/nachbesserung-kalte-pruefung-
umbau.md, K1, R6): a `.pth` that appends a line to MARKER in every interpreter of the venv it
is installed into, and executable scripts under bin/ -- the tools the gates use (diff, comm,
sort, awk, grep, cut, cp, python3, uv, ...) plus python and pip, the entry points a venv's own
programs go through -- each appending "bin/<name> ran: <args>" to MARKER and exiting 0. Any
line in MARKER proves that code of a lockfile package ran.

The scripts are *.data/scripts/ entries with S_IFREG|0755 in the zip: pip marks an installed
script executable only for a regular-file mode with an x bit (a bare 0o755 << 16, as the first
version of this test had, installs them 0644 and the bin/ half of the test could never fire;
cold verification r2 of radius, F8).
"""

from __future__ import annotations

import base64
import hashlib
import stat
import sys
import zipfile

TOOLS = (
    "diff comm sort awk grep cut cp python3 python pip uv sed tar git mktemp find env xargs "
    "head tail wc tr cat rm mv chmod touch readlink bash"
).split()


def main() -> int:
    out, marker = sys.argv[1:3]
    pth = (
        "import os; open(%r, 'a').write('pth ran in pid %%d\\n' %% os.getpid())\n"
        % marker
    )
    files = {
        "zzzk1/__init__.py": "",
        "zzzk1.pth": pth,
        "zzzk1-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: zzzk1\nVersion: 1.0\n",
        "zzzk1-1.0.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    for tool in TOOLS:
        files[f"zzzk1-1.0.data/scripts/{tool}"] = (
            f'#!/bin/sh\necho "bin/{tool} ran: $*" >> {marker}\nexit 0\n'
        )
    record = []
    for name, text in files.items():
        data = text.encode()
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(data).digest())
            .rstrip(b"=")
            .decode()
        )
        record.append(f"{name},sha256={digest},{len(data)}")
    record.append("zzzk1-1.0.dist-info/RECORD,,")
    files["zzzk1-1.0.dist-info/RECORD"] = "\n".join(record) + "\n"
    path = f"{out}/zzzk1-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as whl:
        for name, text in files.items():
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            mode = 0o755 if "/scripts/" in name else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            whl.writestr(info, text)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
