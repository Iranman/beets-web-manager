#!/usr/bin/env python3
"""Report missing or placeholder security configuration without printing secrets."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_ENV = [
    "BEETS_WEB_AUTH_TOKEN",
    "PLEX_TOKEN",
    "LIDARR_API_KEY",
    "OPENAI_API_KEY",
    "DIGARR_INITIAL_PASSWORD",
    "POSTGRES_PASSWORD",
    "SLSKD_SLSK_USERNAME",
    "SLSKD_SLSK_PASSWORD",
]
OPTIONAL_ROTATION_ENV = [
    "BEETS_WEB_PASSWORD",
    "DISCOGS_TOKEN",
    "DISCOGS_USER_TOKEN",
    "LISTENBRAINZ_TOKEN",
    "OPENROUTER_API_KEY",
    "ACOUSTID_API_KEY",
    "ACOUSTID_KEY",
    "RADARR_API_KEY",
    "SONARR_API_KEY",
    "PROWLARR_API_KEY",
    "YTDLP_COOKIE_FILE",
]
PLACEHOLDER_MARKERS = (
    "changeme",
    "change-me",
    "placeholder",
    "example",
    "set in .env",
    "set a strong owner token",
    "password",
    "secret",
    "token",
)
TRACKED_FILES = [
    ROOT / "config.yaml",
    ROOT / "docker-compose.arrs.yml",
    ROOT / ".env",
    ROOT / ".env.example",
]
SECRET_NAME_RE = re.compile(r"(?i)(token|password|secret|api[_-]?key|cookie|authorization)")
KNOWN_LEAK_RE = re.compile(r"(?i)(sk-proj|sk-or-|xox[baprs]-|ghp_[a-z0-9]{20,}|BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY)")


def _is_placeholder(value: str, *, min_length: int = 16) -> bool:
    text = (value or "").strip()
    if not text:
        return True
    compact = re.sub(r"[^a-z0-9]+", "", text.lower())
    if len(text) < min_length:
        return True
    return any(marker.replace(" ", "") in compact for marker in PLACEHOLDER_MARKERS)


def _tracked_file_findings() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    findings: list[dict[str, object]] = []
    placeholders: list[dict[str, object]] = []
    for path in TRACKED_FILES:
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception as exc:
            findings.append({"file": str(path.relative_to(ROOT)), "error": type(exc).__name__})
            continue
        for idx, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if KNOWN_LEAK_RE.search(stripped):
                findings.append({"file": str(path.relative_to(ROOT)), "line": idx, "type": "known_secret_pattern"})
            if SECRET_NAME_RE.search(stripped):
                lower = stripped.lower()
                if any(marker in lower for marker in PLACEHOLDER_MARKERS):
                    placeholders.append({"file": str(path.relative_to(ROOT)), "line": idx, "type": "placeholder_marker"})
    return findings, placeholders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tracked-files-only", action="store_true", help="skip live environment checks")
    args = parser.parse_args(argv)

    tracked_findings, tracked_placeholders = _tracked_file_findings()
    result: dict[str, object] = {
        "ok": True,
        "tracked_file_findings": tracked_findings,
        "tracked_placeholder_lines": tracked_placeholders,
        "unset_required_env": [],
        "placeholder_or_weak_env": [],
        "optional_rotation_env_present": [],
    }

    if not args.tracked_files_only:
        unset: list[str] = []
        weak: list[str] = []
        for name in REQUIRED_ENV:
            value = os.environ.get(name, "")
            if not value:
                unset.append(name)
            elif _is_placeholder(value, min_length=32 if name == "BEETS_WEB_AUTH_TOKEN" else 16):
                weak.append(name)
        present_optional = [name for name in OPTIONAL_ROTATION_ENV if os.environ.get(name)]
        result["unset_required_env"] = unset
        result["placeholder_or_weak_env"] = weak
        result["optional_rotation_env_present"] = present_optional

    result["ok"] = not result["tracked_file_findings"] and not result["unset_required_env"] and not result["placeholder_or_weak_env"]
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())