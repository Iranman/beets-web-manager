#!/usr/bin/env python3
"""Validate Beets-specific Docker Compose security invariants.

This intentionally scopes the strictest hard-hardening failures (mounts,
security_opt, cap_drop, mem_limit, etc.) to docker-compose.arrs.yml, the
project owner's real deployment file. The image/digest-semantics, outbound
allowlist, and port-exposure checks below apply to both docker-compose.yml
(the generic reusable template) and docker-compose.arrs.yml, since both are
real deployable configurations for the same Beets services.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARRS_COMPOSE = ROOT / "docker-compose.arrs.yml"
STANDALONE_COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILE_BEETS = ROOT / "Dockerfile.beets"
ENV_EXAMPLE = ROOT / ".env.example"

# Dockerfile.beets selects its upstream base image via `ARG BEETS_BASE_IMAGE`
# so a build can intentionally pin an explicit version or digest. The digest
# itself is deliberately not attached to the locally-built beets-engine image
# tag (Docker rejects a digest on any image tag that also has a compose
# `build:` block: "build tag cannot contain a digest", independently
# reproduced against docker compose build) -- immutable upstream provenance
# belongs in the Dockerfile's ARG default or an explicit --build-arg.
BEETS_BASE_IMAGE_ARG_PREFIX = "ARG BEETS_BASE_IMAGE="

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


def _dockerfile_beets_base_is_approved() -> bool:
    """The Dockerfile's BEETS_BASE_IMAGE default must be a concrete, tested
    reference -- never the moving `latest` tag -- so a build that omits
    --build-arg BEETS_BASE_IMAGE still gets a safe, reproducible base. An
    explicit --build-arg override (e.g. CI's latest-compatibility job) is a
    deliberate, separate decision made at build time, not at this file."""
    if not DOCKERFILE_BEETS.exists():
        return False
    for line in _read(DOCKERFILE_BEETS).splitlines():
        stripped = line.strip()
        if stripped.startswith(BEETS_BASE_IMAGE_ARG_PREFIX):
            default_value = stripped[len(BEETS_BASE_IMAGE_ARG_PREFIX):].strip()
            if not default_value or default_value.endswith(":latest"):
                return False
            return ":" in default_value.rsplit("/", 1)[-1]
    return False


def _image_lacks_tag_or_digest(image: str) -> bool:
    ref = str(image or "").strip()
    if not ref or "@" in ref:
        return False
    last_component = ref.rsplit("/", 1)[-1]
    return ":" not in last_component

def _check_image_digest_semantics(label: str, image: str, has_build: bool, errors: list[str]) -> None:
    if not image:
        errors.append(f"{label} image is missing")
        return
    if ":latest" in image or image.endswith(":latest"):
        errors.append(f"{label} image uses latest: {image}")
    if has_build:
        # A locally built image's `image:` tag must stay a plain tag. Attaching
        # a digest here is not just misleading (a multi-layer custom build can
        # never legitimately carry the upstream base image's own manifest
        # digest) -- it is a hard Docker error: `docker compose build` fails
        # with "build tag cannot contain a digest", independently reproduced.
        # Immutable upstream provenance belongs in the Dockerfile's FROM line.
        if "@sha256:" in image:
            errors.append(
                f"{label} image must not attach a digest to a locally built image tag "
                f"(docker compose build fails with 'build tag cannot contain a digest'): {image}"
            )
        elif _image_lacks_tag_or_digest(image):
            errors.append(f"{label} image has no tag: {image}")
        if not _dockerfile_beets_base_is_approved():
            errors.append(
                f"{label} is locally built but Dockerfile.beets's ARG BEETS_BASE_IMAGE default is missing, "
                "unpinned, or set to a moving `latest` tag"
            )
    else:
        if "@sha256:" not in image:
            errors.append(f"{label} image is not digest-pinned: {image}")
        if _image_lacks_tag_or_digest(image):
            errors.append(f"{label} image has no tag or digest: {image}")


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


FULL_COMPOSE = ROOT / "docker-compose.full.yml"


def _check_production_hardening(text: str, label: str, errors: list[str]) -> None:
    """docker-compose.yml is the source-independent production deployment
    file. Unlike docker-compose.arrs.yml's owner-specific hardening block
    (checked separately below), these invariants must hold for the generic
    template shipped to every user, so they are enforced unconditionally
    rather than folded into the same opt-in required_snippets table."""
    if "beets-engine" in text:
        errors.append(f"{label}: production Compose must not reference beets-engine")

    web = _service_block(text, "beets-web-manager")
    active = "\n".join(_active_lines(web))

    if "read_only: true" not in active:
        errors.append(f"{label}: beets-web-manager must set read_only: true")
    if "cap_drop:" not in active or "- ALL" not in active:
        errors.append(f"{label}: beets-web-manager must drop all capabilities (cap_drop: [ALL])")
    if "no-new-privileges:true" not in active:
        errors.append(f"{label}: beets-web-manager must set no-new-privileges:true")
    if "/web-manager-data" not in active:
        errors.append(f"{label}: beets-web-manager must persist state to /web-manager-data")

    token_file_match = re.search(r"BEETS_WEB_AUTH_TOKEN_FILE:\s*(\S+)", active)
    if not token_file_match or not token_file_match.group(1).strip("\"'").startswith("/web-manager-data/"):
        errors.append(f"{label}: BEETS_WEB_AUTH_TOKEN_FILE must persist under /web-manager-data")

    for volume in _volume_lines(web):
        container_path = volume.split(":")[1] if volume.count(":") >= 1 else volume
        if volume.endswith(".db") or "library.db" in volume or container_path.startswith("/config"):
            errors.append(f"{label}: beets-web-manager must not directly mount the Beets SQLite database: {volume}")


def _check_compose_variant(path: Path, errors: list[str], warnings: list[str], require_beets: bool = False) -> None:
    if not path.exists():
        errors.append(f"{path.name} not found")
        return
    text = _read(path)
    label = path.name

    beets = _service_block(text, "beets")
    if require_beets and not beets:
        errors.append(f"{label}: beets service not found")
        return

    if beets:
        beets_image = _image_line(beets)
        _check_image_digest_semantics(f"{label}: beets", beets_image, _has_build_block(beets), errors)

        beets_active = "\n".join(_active_lines(beets))
        if re.search(r"^\s*privileged:\s*true\b", beets_active, re.M):
            errors.append(f"{label}: beets must not run privileged")
        if "/var/run/docker.sock" in beets_active:
            errors.append(f"{label}: beets must not mount the Docker socket")
        if re.search(r"^\s*network_mode:\s*host\b", beets_active, re.M):
            errors.append(f"{label}: beets must not use host networking")
        if re.search(r"^\s*user:\s*[\"']?0(?::0)?[\"']?\s*$", beets_active, re.M):
            errors.append(f"{label}: beets must not run as UID/GID 0")

    beets_web = _service_block(text, "beets-web-manager")
    if not beets_web:
        errors.append(f"{label}: beets-web-manager service not found")
        return

    loopback_default_re = re.compile(r"^(?:127\.0\.0\.1|\$\{[A-Za-z_][A-Za-z0-9_]*:-127\.0\.0\.1\}):")

    beets_ports = _port_lines(beets) if beets else []
    web_ports = _port_lines(beets_web)
    for p in beets_ports:
        if "8338" in p and not loopback_default_re.match(p):
            errors.append(f"{label}: beets control agent port must remain internal-only or bind to loopback")

    web_manager_binds_8337_loopback = any(
        loopback_default_re.match(p) and p.rstrip('"').endswith(":8337")
        for p in web_ports
    )
    if not web_manager_binds_8337_loopback:
        errors.append(f"{label}: beets-web-manager port 8337 must bind to loopback by default")

    _check_no_hardcoded_lan_allowlist(text, label, errors)


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    # Primary production compose must have NO build directives and NO local beets service
    if STANDALONE_COMPOSE.exists():
        standalone_text = _read(STANDALONE_COMPOSE)
        if "build:" in standalone_text:
            errors.append("docker-compose.yml: production Compose file must contain no build: directives")
        if _service_block(standalone_text, "beets"):
            errors.append("docker-compose.yml: production Compose file must not include local beets engine service")
        _check_production_hardening(standalone_text, "docker-compose.yml", errors)

    _check_compose_variant(STANDALONE_COMPOSE, errors, warnings, require_beets=False)
    _check_compose_variant(ARRS_COMPOSE, errors, warnings, require_beets=True)
    if FULL_COMPOSE.exists():
        _check_compose_variant(FULL_COMPOSE, errors, warnings, require_beets=True)

    if ENV_EXAMPLE.exists():
        _check_no_hardcoded_lan_allowlist(_read(ENV_EXAMPLE), ENV_EXAMPLE.name, errors)

    if not ARRS_COMPOSE.exists():
        print(json.dumps({"ok": False, "errors": errors, "warnings": warnings}, indent=2, sort_keys=True))
        return 1

    text = _read(ARRS_COMPOSE)
    beets = _service_block(text, "beets")
    bgutil = _service_block(text, "bgutil-provider")

    if not bgutil:
        errors.append("bgutil-provider service not found")
    bgutil_image = _image_line(bgutil)
    _check_image_digest_semantics("bgutil-provider", bgutil_image, _has_build_block(bgutil), errors)

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
    }
    for snippet, message in required_snippets.items():
        if snippet not in beets_active:
            errors.append(message)

    if "${BEETS_UID:-0}" in beets_active or "${BEETS_GID:-0}" in beets_active:
        errors.append("beets UID/GID defaults must not be root")

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
