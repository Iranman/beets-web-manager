#!/usr/bin/env python3
"""Validate Beets-specific Docker Compose security invariants.

This intentionally scopes hard failures to the Beets app and its direct bgutil
helper. Other Arr services may still need separate hardening, but this script
keeps this repository check focused on the app covered by SECURITY_AUDIT.md.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.arrs.yml"


def _read() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def _service_block(text: str, name: str) -> str:
    match = re.search(rf"^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:|\Z)", text, re.M | re.S)
    return match.group("body") if match else ""


def _active_lines(block: str) -> list[str]:
    return [line for line in block.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _image_line(block: str) -> str:
    for line in _active_lines(block):
        if line.strip().startswith("image:"):
            return line.strip().split("image:", 1)[1].strip()
    return ""


def _volume_lines(block: str) -> list[str]:
    in_volumes = False
    out: list[str] = []
    for line in _active_lines(block):
        stripped = line.strip()
        if stripped == "volumes:":
            in_volumes = True
            continue
        if in_volumes and re.match(r"^[a-zA-Z_][a-zA-Z0-9_-]*:", stripped):
            break
        if in_volumes and stripped.startswith("-"):
            out.append(stripped[1:].strip().strip('"'))
    return out


def _port_lines(block: str) -> list[str]:
    in_ports = False
    out: list[str] = []
    for line in _active_lines(block):
        stripped = line.strip()
        if stripped == "ports:":
            in_ports = True
            continue
        if in_ports and re.match(r"^[a-zA-Z_][a-zA-Z0-9_-]*:", stripped):
            break
        if in_ports and stripped.startswith("-"):
            out.append(stripped[1:].strip().strip('"'))
    return out


def main() -> int:
    text = _read()
    beets = _service_block(text, "beets")
    bgutil = _service_block(text, "bgutil-provider")
    errors: list[str] = []
    warnings: list[str] = []

    if not beets:
        errors.append("beets service not found")
    if not bgutil:
        errors.append("bgutil-provider service not found")

    beets_image = _image_line(beets)
    bgutil_image = _image_line(bgutil)
    for label, image in (("beets", beets_image), ("bgutil-provider", bgutil_image)):
        if not image:
            errors.append(f"{label} image is missing")
            continue
        if ":latest" in image or image.endswith(":latest"):
            errors.append(f"{label} image uses latest: {image}")
        if "@sha256:" not in image:
            errors.append(f"{label} image is not digest-pinned: {image}")
        if re.match(r"^[^:@/]+(?:/[^:@]+)*$", image):
            errors.append(f"{label} image has no tag or digest: {image}")

    beets_active = "\n".join(_active_lines(beets))
    required_snippets = {
        "platform: linux/amd64": "beets platform must be explicit for the architecture-specific digest",
        "security_opt:": "beets must set security_opt",
        "no-new-privileges:true": "beets must disable privilege escalation",
        "cap_drop:": "beets must drop capabilities",
        "- ALL": "beets must drop all Linux capabilities",
        "read_only: true": "beets must use a read-only root filesystem",
        "tmpfs:": "beets must provide tmpfs for writable runtime temp paths",
        "pids_limit:": "beets must set a PID limit",
        "mem_limit:": "beets must set a memory limit",
        "BEETS_OUTBOUND_ALLOWLIST:": "beets must declare an explicit outbound allowlist",
        "SPOTIFLAC_AUTO_INSTALL: \"0\"": "SpotiFLAC runtime installation must stay disabled",
    }
    for snippet, message in required_snippets.items():
        if snippet not in beets_active:
            errors.append(message)

    if re.search(r"^\s*privileged:\s*true\b", beets_active, re.M):
        errors.append("beets must not run privileged")
    if "/var/run/docker.sock" in beets_active:
        errors.append("beets must not mount the Docker socket")
    if re.search(r"^\s*network_mode:\s*host\b", beets_active, re.M):
        errors.append("beets must not use host networking")
    if re.search(r"^\s*user:\s*[\"']?0(?::0)?[\"']?\s*$", beets_active, re.M):
        errors.append("beets must not run as UID/GID 0")
    if "${BEETS_UID:-0}" in beets_active or "${BEETS_GID:-0}" in beets_active:
        errors.append("beets UID/GID defaults must not be root")

    ports = _port_lines(beets)
    if "127.0.0.1:8337:8337" not in ports:
        errors.append("beets port must bind to loopback by default")

    volumes = _volume_lines(beets)
    forbidden_mounts = {"/mnt/PLEX/data:/data", "/:/host", "/:/data", "/mnt/PLEX:/data"}
    for volume in volumes:
        if volume in forbidden_mounts or volume.startswith("/mnt/PLEX/data:/data"):
            errors.append(f"beets broad writable mount is forbidden: {volume}")
        if "/var/run/docker.sock" in volume:
            errors.append("beets Docker socket mount is forbidden")
    expected_mounts = {
        "/mnt/PLEX/Apps/Arrs/beets:/config:rw",
        "/mnt/PLEX/data/media/music:/data/media/music:rw",
        "/mnt/PLEX/data/torrents/music:/data/torrents/music:rw",
    }
    missing_mounts = sorted(expected_mounts.difference(volumes))
    for volume in missing_mounts:
        errors.append(f"beets required narrowed mount missing: {volume}")

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("image:") and ":latest" in stripped and "beets" not in stripped:
            warnings.append(f"non-Beets mutable image remains for separate review: {stripped}")

    result = {"ok": not errors, "errors": errors, "warnings": warnings}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())