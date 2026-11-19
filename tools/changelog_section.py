#!/usr/bin/env python3
"""Extract one version's section from CHANGELOG.md, for GitHub Release notes.

Hand-copying release notes drifts from the changelog, so the release workflow
extracts them here instead. Rules:

* a version heading looks like ``## [0.4.0] - 2026-11-19``;
* the section body runs to the next ``## `` heading;
* ``[Unreleased]`` is an empty placeholder and is never a valid release target;
* a missing or empty section exits non-zero, so the workflow fails rather than
  publishing a Release with no description.

Read-only. Usage::

    python3 tools/changelog_section.py 0.4.0           # -> stdout
    python3 tools/changelog_section.py v0.4.0 --check  # existence only
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHANGELOG = REPO / "CHANGELOG.md"

_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\S+))?\s*$")


def normalize(version: str) -> str:
    """Strip a leading ``v`` and whitespace from a tag."""
    return version.strip().lstrip("vV")


def section(text: str, version: str) -> str:
    """Return the body of ``version`` (heading excluded).

    Raises KeyError when the version is absent or its body is empty.
    """
    want = normalize(version)
    if want.lower() == "unreleased":
        raise KeyError("[Unreleased] is not a release target")
    lines = text.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if not m:
            continue
        if start is not None:
            body = "\n".join(lines[start:i]).strip()
            if body:
                return body
            raise KeyError(f"version {want} has an empty section")
        if normalize(m.group("version")) == want:
            start = i + 1
    if start is not None:
        body = "\n".join(lines[start:]).strip()
        if body:
            return body
        raise KeyError(f"version {want} has an empty section")
    raise KeyError(f"version {want} not found in CHANGELOG.md")


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract a CHANGELOG version section")
    parser.add_argument("version")
    parser.add_argument("--check", action="store_true", help="validate only")
    parser.add_argument("--changelog", default=str(CHANGELOG))
    args = parser.parse_args(argv)

    text = Path(args.changelog).read_text(encoding="utf-8")
    try:
        body = section(text, args.version)
    except KeyError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if args.check:
        print(f"OK: {normalize(args.version)} ({len(body.splitlines())} lines)")
        return 0
    print(body)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
