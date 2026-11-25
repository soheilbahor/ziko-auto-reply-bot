#!/usr/bin/env python3
"""Validate the single canonical VERSION file and its release tag."""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="release tag, e.g. vX.Y.Z")
    parser.add_argument("--print", action="store_true", help="print the canonical version only")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    raw = (root / "VERSION").read_text(encoding="utf-8").strip()
    canonical = raw.removeprefix("v")
    if not SEMVER.fullmatch(canonical):
        raise SystemExit(f"Invalid VERSION value: {raw!r}")

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dynamic = project.get("project", {}).get("dynamic", [])
    version_file = project.get("tool", {}).get("setuptools", {}).get("dynamic", {}).get("version", {}).get("file", [])
    if "version" not in dynamic or version_file != ["VERSION"]:
        raise SystemExit("pyproject.toml must derive project version from the root VERSION file")

    if args.tag:
        tag_version = args.tag.removeprefix("v")
        if not SEMVER.fullmatch(tag_version):
            raise SystemExit(f"Invalid release tag: {args.tag!r}")
        if tag_version != canonical:
            raise SystemExit(f"Release tag {args.tag} does not match VERSION {canonical}")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(rf"^## \[{re.escape(canonical)}\]", changelog, re.MULTILINE):
        raise SystemExit(f"CHANGELOG.md has no '## [{canonical}]' section for VERSION {canonical}")

    if args.print:
        print(canonical)
        return 0
    print(f"Version OK: {canonical}" + (f" (tag {args.tag})" if args.tag else ""))
    return 0

if __name__ == "__main__":
    sys.exit(main())
