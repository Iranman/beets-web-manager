#!/usr/bin/env python3
"""Small CI-safe secret scanner for this repository.

The scanner prints path, line, and rule only. It never prints the matched value.
It is intentionally conservative enough for CI while catching high-confidence
private keys, provider tokens, and committed config credentials.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".cfg", ".conf", ".env", ".html", ".ini", ".js", ".json", ".jsx",
    ".mjs", ".md", ".py", ".sh", ".toml", ".ts", ".tsx", ".txt",
    ".yaml", ".yml",
}
CONFIG_SUFFIXES = {".cfg", ".conf", ".env", ".ini", ".json", ".toml", ".txt", ".yaml", ".yml"}
SKIP_DIRS = {
    ".git", ".github_cache", ".mypy_cache", ".next", ".playwright-cli",
    ".pytest_cache", "__pycache__", "dist", "node_modules", "out",
}
LOCAL_ARTIFACT_DIRS = {"_codex_backups", ".codex-live-backups", ".local-archive", "output"}
ALLOWLIST_VALUES = {
    "", "false", "true", "none", "null", "changeme", "change-me", "example",
    "placeholder", "configured", "redacted", "***redacted***", "<redacted>",
}
HIGH_CONFIDENCE_RULES = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b")),
    ("github-token", re.compile(r"\bgh[psuor]_[A-Za-z0-9_]{20,}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
]
CONFIG_ASSIGNMENT = re.compile(
    r"(?i)^\s*(?:[A-Z0-9_]+_)?(api[_-]?key|auth[_-]?token|client[_-]?secret|password|secret|token|user[_-]?token)\s*[:=]\s*(.+?)\s*(?:#.*)?$"
)


def iter_files(include_local_artifacts: bool) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(ROOT):
        current = Path(dirpath)
        rel_parts = set(current.relative_to(ROOT).parts) if current != ROOT else set()
        skip = set(SKIP_DIRS)
        if not include_local_artifacts:
            skip |= LOCAL_ARTIFACT_DIRS
        dirnames[:] = [d for d in dirnames if d not in skip]
        if rel_parts & skip:
            continue
        for name in filenames:
            path = current / name
            if path.suffix.lower() in TEXT_SUFFIXES or name.lower().startswith(".env"):
                yield path


def clean_value(value: str) -> str:
    value = value.strip().strip('"').strip("'").strip()
    return value.rstrip(",")


def is_placeholder(value: str) -> bool:
    lowered = clean_value(value).lower()
    if lowered in ALLOWLIST_VALUES:
        return True
    if lowered.startswith("${") or lowered.startswith("$env:"):
        return True
    if "os.environ" in value or "getenv" in value:
        return True
    if "set in .env" in lowered or "not configured" in lowered:
        return True
    return False


def scan_file(path: Path) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    for line_no, line in enumerate(text.splitlines(), start=1):
        for rule_name, pattern in HIGH_CONFIDENCE_RULES:
            if pattern.search(line):
                findings.append((line_no, rule_name))
        if path.suffix.lower() in CONFIG_SUFFIXES or path.name.lower().startswith(".env"):
            match = CONFIG_ASSIGNMENT.match(line)
            if match and not is_placeholder(match.group(2)):
                findings.append((line_no, "config-secret-assignment"))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-local-artifacts", action="store_true", help="also scan local backup/output folders")
    args = parser.parse_args()
    all_findings: list[tuple[Path, int, str]] = []
    for path in iter_files(args.include_local_artifacts):
        for line_no, rule_name in scan_file(path):
            all_findings.append((path, line_no, rule_name))
    if all_findings:
        for path, line_no, rule_name in all_findings:
            rel = path.relative_to(ROOT).as_posix()
            print(f"{rel}:{line_no}: {rule_name}", file=sys.stderr)
        print(f"secret scan failed with {len(all_findings)} finding(s)", file=sys.stderr)
        return 1
    print("secret scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())