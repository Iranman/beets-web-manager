#!/usr/bin/env python3
"""Validate the Docker Compose security and single-engine invariants.

This repository ships generic, reusable Compose configurations, each with
one Web Manager container that runs Beets inside it (ADR 0006):
- docker-compose.yml (production, published GHCR image, no build:)
- docker-compose.full.yml (published image plus the optional sidecar)
- docker-compose.dev.yml (built from source)

None may run a separate Beets container (two writers on one library), mount
/music read-only (Beets moves and tags files from Web Manager now), or drop
the healthcheck that reports Beets' self-test (`beets.ok` on /api/health).
None may encode the project owner's deployment topology, host paths, or
credentials; see the "Deployment Files Stay Generic" rule in
docs/DEVELOPMENT.md.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STANDALONE_COMPOSE = ROOT / "docker-compose.yml"
FULL_COMPOSE = ROOT / "docker-compose.full.yml"
DEV_COMPOSE = ROOT / "docker-compose.dev.yml"
ENV_EXAMPLE = ROOT / ".env.example"
WEB = "beets-web-manager"

_PRIVATE_IPV4_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|127\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3})\b"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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


def _has_build_block(block: str) -> bool:
    return any(line.strip() == "build:" or line.strip().startswith("build:") for line in _active_lines(block))


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


def _image_lacks_tag_or_digest(image: str) -> bool:
    ref = str(image or "").strip()
    if not ref or "@" in ref:
        return False
    last_component = ref.rsplit("/", 1)[-1]
    return ":" not in last_component


_PROJECT_IMAGE_REPOS = frozenset({"ghcr.io/iranman/beets-web-manager"})


def _image_repo(image: str) -> str:
    """Repository part of an image reference: no digest, no tag."""
    ref = str(image or "").strip().strip("\"'").split("@", 1)[0]
    head, _, last = ref.rpartition("/")
    last = last.split(":", 1)[0]
    return f"{head}/{last}".lower() if head else last.lower()


def _check_image_digest_semantics(label: str, image: str, errors: list[str]) -> None:
    if not image:
        errors.append(f"{label} image is missing")
        return
    if ":latest" in image or image.endswith(":latest"):
        errors.append(f"{label} image uses latest: {image}")
    # Project images may carry a tag variable (e.g. ${BEETS_WEB_MANAGER_VERSION:-stable}),
    # but only after their exact repository name.
    if _image_repo(image) in _PROJECT_IMAGE_REPOS:
        if _image_lacks_tag_or_digest(image):
            errors.append(f"{label} image has no tag: {image}")
    else:
        if "${" in image:
            # A variable could swap in any image at deploy time, and a
            # ${VAR:-img@sha256:...} default only pins the fallback.
            errors.append(f"{label} third-party image must be a literal reference, not a variable: {image}")
        elif "@sha256:" not in image:
            errors.append(f"{label} image is not digest-pinned: {image}")
        if _image_lacks_tag_or_digest(image):
            errors.append(f"{label} image has no tag or digest: {image}")


_PROJECT_SERVICES = (WEB,)


def _check_third_party_images(text: str, label: str, errors: list[str]) -> None:
    """Every service other than beets-web-manager (e.g. the
    bgutil-provider sidecar) runs a third-party image: it must be pinned by
    digest so a moved tag can never change what runs. Services built from
    source here (`build:`) are not third-party images and are skipped."""
    services = _services(text)
    if not services and re.search(r"(?m)^services:", text):
        errors.append(f"{label}: has a services: section but no service could be read from it")
    for name, block in services.items():
        if _has_build_block(block):
            continue
        # #253 S-12: the project names are exempt only while they actually
        # run a project image; `beets-web-manager: image: evil/x:latest` is third-party.
        # A `${VAR:-default}` reference is judged by its default (dev file).
        image = re.sub(r"^\$\{\w+:?-(.*)\}$", r"\1", _image_line(block).strip("\"'"))
        if name in _PROJECT_SERVICES and _image_repo(image) in _PROJECT_IMAGE_REPOS:
            continue
        _check_image_digest_semantics(f"{label}: {name}", _image_line(block), errors)


def _services(text: str) -> dict[str, str]:
    """{name: body} for the top-level `services:` mapping, at whatever
    indentation the file uses. Blank and comment lines (even at column 0)
    do not end the section; the next top-level key does."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(r"services:\s*(#.*)?$", line)), None)
    if start is None:
        return {}
    bodies: dict[str, list[str]] = {}
    name, indent = None, None
    for line in lines[start + 1:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        depth = len(line) - len(line.lstrip())
        if depth == 0:
            break
        if indent is None:
            indent = depth
        if depth <= indent:
            match = re.match(r"\s*[\"']?([A-Za-z0-9_.-]+)[\"']?\s*:", line)
            name = match.group(1) if match else None
            if name:
                bodies[name] = []
        elif name:
            bodies[name].append(line)
    return {n: "\n".join(body) for n, body in bodies.items()}


def _check_no_hardcoded_lan_allowlist(text: str, source_label: str, errors: list[str]) -> None:
    """BEETS_OUTBOUND_ALLOWLIST documents (and, only in the web-manager
    process, enforces) which LAN/internal hosts are reachable. It must never
    ship with an owner-specific private IP baked in as an active default in
    a reusable configuration file -- only an environment-variable passthrough
    (`${BEETS_OUTBOUND_ALLOWLIST:-...}`) or a safely empty/illustrative value.
    """
    for line_no, line in enumerate(text.splitlines(), start=1):
        if "BEETS_OUTBOUND_ALLOWLIST" not in line:
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        value = re.split(r"[:=]", stripped, maxsplit=1)[-1] if (":" in stripped or "=" in stripped) else ""
        if "${" in value:
            continue
        if _PRIVATE_IPV4_RE.search(value):
            errors.append(
                f"{source_label}:{line_no}: BEETS_OUTBOUND_ALLOWLIST must not hard-code a private/LAN "
                f"IP address as an active default: {stripped}"
            )


def _check_production_hardening(text: str, label: str, errors: list[str]) -> None:
    """docker-compose.yml is the source-independent production deployment
    file deploying published images."""
    web = _service_block(text, WEB)
    active = "\n".join(_active_lines(web))

    if "/web-manager-data" not in active and ":/data" not in active and "/data" not in active:
        errors.append(f"{label}: beets-web-manager must persist state to /data or /web-manager-data")

    token_file_match = re.search(r"BEETS_WEB_AUTH_TOKEN_FILE:\s*(\S+)", active)
    if token_file_match and not (token_file_match.group(1).strip("\"'").startswith("/web-manager-data/") or token_file_match.group(1).strip("\"'").startswith("/data/")):
        errors.append(f"{label}: BEETS_WEB_AUTH_TOKEN_FILE must persist under /data or /web-manager-data")

    for volume in _volume_lines(web):
        if volume.endswith(".db") or "library.db" in volume:
            errors.append(f"{label}: beets-web-manager must not directly mount the Beets SQLite database file: {volume}")


_SEPARATE_BEETS_IMAGE_RE = re.compile(r"(^|/)linuxserver/beets$")


def _check_compose_variant(path: Path, errors: list[str]) -> None:
    """One Web Manager service, no separate Beets, /music writable, port 8337
    exposed, and a healthcheck that reports Beets' self-test."""
    if not path.exists():
        errors.append(f"{path.name} not found")
        return
    text = _read(path)
    label = path.name

    for name, block in _services(text).items():
        image = re.sub(r"^\$\{\w+:?-(.*)\}$", r"\1", _image_line(block).strip("\"'"))
        if name == "beets" or _SEPARATE_BEETS_IMAGE_RE.search(_image_repo(image)):
            errors.append(
                f"{label}: {name}: no separate Beets container -- Beets runs inside Web Manager, "
                "and two programs must never write one library (docs/INSTALLATION.md)")

    web = _service_block(text, WEB)
    if not web:
        errors.append(f"{label}: {WEB} service not found")
        return

    music = [v for v in _volume_lines(web) if re.search(r":/music(:|$)", v)]
    if not music:
        errors.append(f"{label}: {WEB} must mount the music library at /music")
    elif any(re.search(r":/music:[^:]*\bro\b", v) for v in music):
        errors.append(f"{label}: /music must be read-write (Beets moves and tags files from Web Manager)")

    active = "\n".join(_active_lines(web))
    health = active.split("healthcheck:", 1)[1] if "healthcheck:" in active else ""
    if "/api/health" not in health or "'beets'" not in health or "'ok'" not in health:
        errors.append(f"{label}: {WEB} healthcheck must fail while beets.ok on /api/health is false")

    loopback_default_re = re.compile(r"^(?:127\.0\.0\.1|\$\{[A-Za-z_][A-Za-z0-9_]*:-127\.0\.0\.1\}):")
    if not any(loopback_default_re.match(p) or p.rstrip('"').endswith(":8337") or p.strip('"') == "8337:8337"
               for p in _port_lines(web)):
        errors.append(f"{label}: {WEB} port 8337 must be exposed")

    _check_no_hardcoded_lan_allowlist(text, label, errors)


# Structural classes of "this looks like somebody's real machine", not any
# one maintainer's specific former value -- a denylist of exact former
# literals would itself have to contain them. See
# tests/test_no_owner_specific_deployment_details.py for the synthetic-
# fixture self-test proving each pattern actually fires.
_HOST_SPECIFIC_PATH_PATTERNS = (
    re.compile(r"/mnt/[A-Za-z0-9_.-]+/"),                                    # absolute /mnt/<pool>/... mount
    re.compile(r"/home/[A-Za-z0-9_.-]+/"),                                   # absolute /home/<user>/... path
    re.compile(r"[A-Za-z]:\\\\?Users\\\\?[A-Za-z0-9_.-]+", re.IGNORECASE),   # C:\Users\<user>\... path
    re.compile(r"\\\\[A-Za-z0-9_.-]+\\"),                                    # UNC \\<host>\... path
    re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3})\b"
    ),                                                                        # hardcoded RFC1918 host address
)


def _check_no_owner_specific_paths(text: str, label: str, errors: list[str]) -> None:
    """Regression guard: no public Compose file may ship a maintainer's
    actual host paths or LAN addresses as an active default (see the
    "Deployment Files Stay Generic" rule in docs/DEVELOPMENT.md)."""
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("#"):
            continue
        for pattern in _HOST_SPECIFIC_PATH_PATTERNS:
            if pattern.search(line):
                errors.append(f"{label}:{line_no}: host-specific path/address must not appear in a public example: {line.strip()}")
                break


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    # The production compose file deploys the published image only.
    if STANDALONE_COMPOSE.exists():
        standalone_text = _read(STANDALONE_COMPOSE)
        if "build:" in standalone_text:
            errors.append("docker-compose.yml: production Compose file must contain no build: directives")
        _check_production_hardening(standalone_text, "docker-compose.yml", errors)

    for path in (STANDALONE_COMPOSE, FULL_COMPOSE, DEV_COMPOSE):
        if path is STANDALONE_COMPOSE or path.exists():
            _check_compose_variant(path, errors)

    if ENV_EXAMPLE.exists():
        _check_no_hardcoded_lan_allowlist(_read(ENV_EXAMPLE), ENV_EXAMPLE.name, errors)

    for path in (STANDALONE_COMPOSE, FULL_COMPOSE, DEV_COMPOSE, ENV_EXAMPLE):
        if path.exists():
            _check_no_owner_specific_paths(_read(path), path.name, errors)
            if path is not ENV_EXAMPLE:
                _check_third_party_images(_read(path), path.name, errors)
    examples_dir = ROOT / "examples"
    if examples_dir.is_dir():
        for path in sorted(examples_dir.glob("*.yml")):
            _check_no_owner_specific_paths(_read(path), f"examples/{path.name}", errors)
            _check_third_party_images(_read(path), f"examples/{path.name}", errors)

    result = {"ok": not errors, "errors": errors, "warnings": warnings}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
