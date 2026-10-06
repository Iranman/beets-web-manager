#!/usr/bin/env python3
"""Release metadata checks and release notes, used by CI.

    python scripts/release_metadata.py check                 # every build
    python scripts/release_metadata.py check --tag v0.1.50   # tag builds
    python scripts/release_metadata.py notes --tag v0.1.50 [--output notes.md]

`check` enforces that the `VERSION` file and the newest released
`## vX.Y.Z - YYYY-MM-DD` heading in CHANGELOG.md agree, and with --tag that
the tag is exactly `v` + VERSION. `notes` prints that version's CHANGELOG
section (without its heading) and fails if it is missing or empty.
Standard library only.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?$")
RELEASE_HEADING_RE = re.compile(r"^## v(\S+) - (\d{4}-\d{2}-\d{2})\s*$")
ANY_H2_RE = re.compile(r"^## ")


class ReleaseMetadataError(Exception):
    pass


def read_version(root: Path) -> str:
    path = root / "VERSION"
    if not path.is_file():
        raise ReleaseMetadataError("VERSION file is missing")
    version = path.read_text(encoding="utf-8").strip()
    if not SEMVER_RE.match(version):
        raise ReleaseMetadataError(f"VERSION '{version}' is not a semantic version (X.Y.Z or X.Y.Z-pre)")
    return version


def release_headings(changelog: str) -> List[Tuple[str, int]]:
    """(version, line index) for every `## vX.Y.Z - YYYY-MM-DD` heading, in file order."""
    found = []
    for idx, line in enumerate(changelog.splitlines()):
        m = RELEASE_HEADING_RE.match(line)
        if m:
            found.append((m.group(1), idx))
    return found


def section(changelog: str, version: str) -> Optional[str]:
    lines = changelog.splitlines()
    for found_version, idx in release_headings(changelog):
        if found_version == version:
            body = []
            for line in lines[idx + 1:]:
                if ANY_H2_RE.match(line):
                    break
                body.append(line)
            return "\n".join(body).strip()
    return None


def check(root: Path, tag: Optional[str]) -> List[str]:
    version = read_version(root)
    changelog_path = root / "CHANGELOG.md"
    if not changelog_path.is_file():
        raise ReleaseMetadataError("CHANGELOG.md is missing")
    changelog = changelog_path.read_text(encoding="utf-8")
    headings = release_headings(changelog)
    if not headings:
        raise ReleaseMetadataError("CHANGELOG.md has no '## vX.Y.Z - YYYY-MM-DD' release heading")
    newest = headings[0][0]
    if newest != version:
        raise ReleaseMetadataError(
            f"VERSION is {version} but the newest CHANGELOG release heading is v{newest}. "
            "A release PR retitles '## Unreleased' to '## v<VERSION> - <date>' and bumps VERSION together."
        )
    if not section(changelog, version):
        raise ReleaseMetadataError(f"the CHANGELOG section for v{version} is empty")
    messages = [f"VERSION {version} matches the newest CHANGELOG heading v{newest}"]
    if tag is not None:
        if tag != f"v{version}":
            raise ReleaseMetadataError(f"tag {tag} does not match VERSION {version} (expected v{version})")
        messages.append(f"tag {tag} matches VERSION")
    return messages


def notes(root: Path, tag: str) -> str:
    if not tag.startswith("v"):
        raise ReleaseMetadataError(f"tag '{tag}' must start with 'v'")
    version = tag[1:]
    body = section((root / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    if not body:
        raise ReleaseMetadataError(f"CHANGELOG.md has no non-empty '## {tag} - YYYY-MM-DD' section")
    return body + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="repository root")
    sub = parser.add_subparsers(dest="command", required=True)
    p_check = sub.add_parser("check")
    p_check.add_argument("--tag", default=None)
    p_notes = sub.add_parser("notes")
    p_notes.add_argument("--tag", required=True)
    p_notes.add_argument("--output", default=None, help="write the notes to this file (UTF-8) instead of stdout")
    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        if args.command == "check":
            for message in check(root, args.tag):
                print(message)
        else:
            body = notes(root, args.tag)
            if args.output:
                Path(args.output).write_text(body, encoding="utf-8")
            else:
                sys.stdout.buffer.write(body.encode("utf-8"))
    except ReleaseMetadataError as exc:
        print(f"release metadata error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
