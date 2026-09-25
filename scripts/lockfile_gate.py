#!/usr/bin/env python3
# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""The gates between controlplane/*.lock and the venv that packaging/build-venv.sh builds.

    lockfile_gate.py lint LOCK...
        Every line is one pip reads exactly the way uv wrote it (see below).
    lockfile_gate.py pypi LOCK...
        Every hash is one PyPI publishes for that exact version, including the hashes of
        files pip never downloads (an sdist, another platform's wheel).
    lockfile_gate.py freeze [--own NAME] [--drop NAME] FREEZE LOCK...
        FREEZE (the output of `pip freeze --all`) holds exactly the pins of the LOCKs, minus
        --drop, plus --own; every line is a plain name==version pin.
    lockfile_gate.py closure --root NAME... [--path DIR]
        Run with the venv's python: every installed distribution is required, directly or
        through others (markers and extras evaluated here), by one of the roots. An extra
        pin that carries PyPI's genuine hashes passes lint, pypi and freeze, but not this.

Why lint is a whitelist: pip splits a requirements file with str.splitlines(), which also
breaks lines at \\r, \\v, \\f, \\x1c-\\x1e, \\x85, \\u2028 and \\u2029, ignores leading
whitespace, joins backslash continuations, and honours option lines (--index-url,
--extra-index-url, -f/--find-links, -e, -r, -c, ...) as well as URL requirements
(`name @ url#sha256=...`, whose fragment satisfies --require-hashes). A check that reads only
some lines misses lines pip does read: an indented `name @ file:///...whl#sha256=...` passed
the old check and was installed into the venv (linuxmusterDEV
work/verification/cold-stage-a.md, F2). So every byte must be printable ASCII or \\n, and every
line must be empty, a comment (`#` after optional spaces), a pin `name==version \\` as uv
writes it, or a `    --hash=sha256:<64 hex>` line continuing the pin above it. Anything else
fails.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

PIN = re.compile(r"([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)==([0-9][0-9A-Za-z.+!]*) \\")
HASH = re.compile(r"    --hash=sha256:([0-9a-f]{64})( \\)?")
COMMENT = re.compile(r" *#.*")
FREEZE_PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][0-9A-Za-z.+!]*)")


@dataclass
class Pin:
    name: str
    version: str
    line: int
    hashes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.name}=={self.version}"


def canonical(name: str) -> str:
    """PEP 503 normalised project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def ascii_lines(path: str, errors: list[str]) -> list[tuple[int, str | None]]:
    """The file split at \\n, as pip would see it only if every byte is printable ASCII.

    A line with any other byte comes back as None after an error was recorded.
    """
    data = Path(path).read_bytes()
    lines = data.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    out: list[tuple[int, str | None]] = []
    for n, raw in enumerate(lines, 1):
        bad = sorted({b for b in raw if not 0x20 <= b <= 0x7E})
        if bad:
            codes = ", ".join(f"0x{b:02x}" for b in bad)
            errors.append(
                f"{path}:{n}: byte(s) {codes} outside printable ASCII; pip may split or "
                f"decode this line differently than it looks: {raw!r}"
            )
            out.append((n, None))
        else:
            out.append((n, raw.decode("ascii")))
    return out


def parse(path: str) -> tuple[list[Pin], list[str]]:
    """The pins of a lockfile, and every line that is not one uv writes."""
    errors: list[str] = []
    pins: list[Pin] = []
    seen: dict[str, int] = {}
    cont: Pin | None = None  # the pin whose next line must be a hash line
    for n, line in ascii_lines(path, errors):
        if line is None:
            cont = None
            continue
        if cont is not None:
            m = HASH.fullmatch(line)
            if m is None:
                errors.append(
                    f"{path}:{n}: expected a '    --hash=sha256:<64 hex>' line continuing "
                    f"{cont.name}=={cont.version} (line {cont.line}): {line!r}"
                )
                cont = None
                continue
            cont.hashes.append(m[1])
            if m[2] is None:
                cont = None
            continue
        if line == "" or COMMENT.fullmatch(line):
            continue
        m = PIN.fullmatch(line)
        if m is None:
            errors.append(
                f"{path}:{n}: not an empty line, a comment, a 'name==version \\' pin or its "
                f"hash line: {line!r}"
            )
            continue
        if m[1] in seen:
            errors.append(
                f"{path}:{n}: {m[1]} is pinned twice (first on line {seen[m[1]]})"
            )
        seen[m[1]] = n
        cont = Pin(m[1], m[2], n)
        pins.append(cont)
    if cont is not None:
        errors.append(f"{path}: ends inside the pin {cont.name}=={cont.version}")
    return pins, errors


def report(errors: list[str], ok: str) -> int:
    for e in errors:
        print(f"FAIL {e}", file=sys.stderr)
    if errors:
        return 1
    print(ok)
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    errors: list[str] = []
    counts = []
    for path in args.locks:
        pins, errs = parse(path)
        errors += errs
        counts.append(
            f"{path}: {len(pins)} pins, {sum(len(p.hashes) for p in pins)} hashes"
        )
    return report(errors, "lint ok: " + "; ".join(counts))


def fetch_json(url: str) -> dict:
    """GET a JSON document; transient errors are retried, a 404 is final."""
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404 or attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
        time.sleep(attempt * 5)
    raise AssertionError("unreachable")


def published(pin: Pin) -> set[str]:
    """sha256 of every file PyPI lists for exactly this release (yanked ones included)."""
    data = fetch_json(f"https://pypi.org/pypi/{pin.name}/{pin.version}/json")
    if data["info"]["version"] != pin.version:
        raise ValueError(f"PyPI answered for version {data['info']['version']}")
    return {u["digests"]["sha256"] for u in data["urls"]}


def cmd_pypi(args: argparse.Namespace) -> int:
    errors: list[str] = []
    checked = 0
    for path in args.locks:
        pins, errs = parse(path)
        if errs:
            errors += errs
            continue
        for pin in pins:
            # Any answer but the release's file list fails closed.
            try:
                known = published(pin)
            except Exception as e:
                errors.append(f"{path}: {pin}: cannot read PyPI: {e}")
                continue
            for h in pin.hashes:
                if h in known:
                    checked += 1
                else:
                    errors.append(
                        f"{path}:{pin.line}: {pin.name}=={pin.version}: sha256:{h} is not a "
                        "file PyPI publishes for this version"
                    )
    return report(errors, f"pypi ok: {checked} hashes are files PyPI publishes")


def cmd_freeze(args: argparse.Namespace) -> int:
    errors: list[str] = []
    want: dict[str, str] = {}
    for path in args.locks:
        pins, errs = parse(path)
        errors += errs
        for pin in pins:
            if pin.name in want:
                errors.append(f"{path}: {pin.name} is pinned in more than one lockfile")
            want[pin.name] = pin.version
    for name in map(canonical, args.drop):
        if want.pop(name, None) is None:
            errors.append(f"--drop {name}: not pinned in any lockfile")
    have: dict[str, str] = {}
    for n, line in ascii_lines(args.freeze, errors):
        if line is None:
            continue
        m = FREEZE_PIN.fullmatch(line)
        if m is None:
            errors.append(
                f"{args.freeze}:{n}: not a plain name==version pin (installed from a URL, a "
                f"path or editable?): {line!r}"
            )
            continue
        name = canonical(m[1])
        if name in have:
            errors.append(f"{args.freeze}:{n}: {name} appears twice")
        have[name] = m[2]
    for name in map(canonical, args.own):
        if have.pop(name, None) is None:
            errors.append(f"{name} is not installed in the venv")
    for name in sorted(set(have) - set(want)):
        errors.append(f"in the venv, not in the lockfiles: {name}=={have[name]}")
    for name in sorted(set(want) - set(have)):
        errors.append(f"in the lockfiles, not in the venv: {name}=={want[name]}")
    for name in sorted(n for n in set(want) & set(have) if want[n] != have[n]):
        errors.append(
            f"{name}: the venv has {have[name]}, the lockfiles pin {want[name]}"
        )
    return report(
        errors, f"freeze ok: the venv holds exactly the {len(want)} locked pins"
    )


def cmd_closure(args: argparse.Namespace) -> int:
    from importlib import metadata

    # pip is in every venv this runs in (it is locked in build-requirements.lock).
    from pip._vendor.packaging.requirements import Requirement

    errors: list[str] = []
    dists: dict[str, metadata.Distribution] = {}
    for d in metadata.distributions(path=args.path or sys.path):
        name = canonical(d.metadata["Name"])
        if name in dists:
            errors.append(f"{name} is installed twice")
        dists[name] = d
    seen: set[tuple[str, str]] = set()
    todo = [(canonical(r), "") for r in args.root]
    while todo:
        name, extra = todo.pop()
        if (name, extra) in seen:
            continue
        seen.add((name, extra))
        if name not in dists:
            errors.append(f"{name} is required but not installed")
            continue
        for spec in dists[name].requires or []:
            req = Requirement(spec)
            if req.marker is None or req.marker.evaluate({"extra": extra}):
                dep = canonical(req.name)
                todo += [(dep, "")] + [(dep, canonical(e)) for e in req.extras]
    reached = {name for name, _ in seen}
    for name in sorted(set(dists) - reached):
        errors.append(
            f"{name}=={dists[name].version} is installed but required by nothing "
            f"(roots: {', '.join(args.root)}); an extra pin in a lockfile?"
        )
    return report(errors, f"closure ok: all {len(dists)} distributions are required")


def cmd_only(args: argparse.Namespace) -> int:
    """Fail unless LOCK holds exactly one pin, named NAME (the tool lock, e.g. uv)."""
    errors: list[str] = []
    pins, errs = parse(args.lock)
    errors += errs
    names = [p.name for p in pins]
    if names != [canonical(args.name)]:
        errors.append(
            f"{args.lock}: expected exactly the one pin {args.name}, found {names or 'none'}"
        )
    return report(
        errors,
        f"only ok: {args.lock} holds exactly {args.name}=={pins[0].version if pins else '?'}",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("lint")
    p.add_argument("locks", nargs="+")
    p.set_defaults(func=cmd_lint)
    p = sub.add_parser("only")
    p.add_argument("name")
    p.add_argument("lock")
    p.set_defaults(func=cmd_only)
    p = sub.add_parser("pypi")
    p.add_argument("locks", nargs="+")
    p.set_defaults(func=cmd_pypi)
    p = sub.add_parser("freeze")
    p.add_argument("--own", action="append", default=[])
    p.add_argument("--drop", action="append", default=[])
    p.add_argument("freeze")
    p.add_argument("locks", nargs="+")
    p.set_defaults(func=cmd_freeze)
    p = sub.add_parser("closure")
    p.add_argument("--root", action="append", required=True)
    p.add_argument("--path", action="append", help="search here instead of sys.path")
    p.set_defaults(func=cmd_closure)
    args = ap.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
