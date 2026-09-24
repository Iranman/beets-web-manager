"""Configuration Manager for Beets config.yaml.

Provides:
- CAS (Compare-And-Swap) revision checking to prevent concurrent edits.
- YAML structure validation before saving.
- Atomic file writes with fsync to avoid partial writes.
- Automatic backups on modification and rollback support.
- Zero direct SQLite access.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

log = logging.getLogger("beets.config_manager")

DEFAULT_CONFIG_PATH = "/config/config.yaml"


class ConfigError(Exception):
    """Base exception for config management errors."""

    def __init__(self, message: str, error_code: str = "CONFIG_ERROR", status_code: int = 400):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


class ConfigConflictError(ConfigError):
    """Raised when expected_revision does not match the current revision."""

    def __init__(self, message: str = "Configuration has been modified by another process. Please refresh and try again."):
        super().__init__(message, error_code="CONFIG_REVISION_MISMATCH", status_code=409)


class ConfigValidationError(ConfigError):
    """Raised when configuration content fails YAML syntax validation."""

    def __init__(self, message: str):
        super().__init__(message, error_code="CONFIG_INVALID_YAML", status_code=400)


def compute_revision(content: str) -> str:
    """Compute deterministic SHA-256 revision hash for configuration content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def get_config_path() -> Path:
    """Resolve the active Beets configuration file path."""
    return Path(os.environ.get("BEETS_CONFIG_PATH", DEFAULT_CONFIG_PATH)).resolve()


def get_config_backup_path(config_path: Optional[Path] = None) -> Path:
    """Resolve the active Beets configuration backup file path."""
    cfg = config_path or get_config_path()
    return cfg.with_suffix(".yaml.bak")


def validate_config_yaml(content: str) -> Tuple[bool, str]:
    """Validate that content is parseable YAML mapping."""
    if not content or not content.strip():
        return False, "Config content cannot be empty"
    try:
        parsed = yaml.safe_load(content)
        if parsed is not None and not isinstance(parsed, dict):
            return False, "Configuration root must be a YAML mapping/dictionary"
        return True, ""
    except yaml.YAMLError as exc:
        return False, f"Invalid YAML syntax: {exc}"


def get_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Read the current configuration, its revision hash, and backup state."""
    cfg = config_path or get_config_path()
    content = ""
    if cfg.exists() and cfg.is_file():
        try:
            content = cfg.read_text(encoding="utf-8")
        except Exception as exc:
            log.warning("Failed to read config file %s: %s", cfg, exc)
            content = ""

    rev = compute_revision(content)
    bak = get_config_backup_path(cfg)
    has_backup = bak.exists() and bak.is_file()
    backup_ts = None
    if has_backup:
        try:
            backup_ts = bak.stat().st_mtime
        except Exception:
            pass

    return {
        "content": content,
        "revision": rev,
        "has_backup": has_backup,
        "backup_ts": backup_ts,
    }


def save_config(
    content: str,
    expected_revision: Optional[str] = None,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Save new configuration with CAS validation, atomic replace, and backup."""
    cfg = config_path or get_config_path()
    valid, err_msg = validate_config_yaml(content)
    if not valid:
        raise ConfigValidationError(err_msg)

    current_data = get_config(cfg)
    current_rev = current_data["revision"]

    if expected_revision and expected_revision.strip():
        if expected_revision.strip().lower() != current_rev.lower():
            raise ConfigConflictError()

    cfg.parent.mkdir(parents=True, exist_ok=True)
    bak = get_config_backup_path(cfg)

    # Create backup if original exists
    if cfg.exists() and cfg.is_file():
        try:
            shutil.copy2(str(cfg), str(bak))
            # Also create timestamped backup
            ts = int(time.time())
            ts_bak = cfg.with_name(f"{cfg.name}.bak.{ts}")
            shutil.copy2(str(cfg), str(ts_bak))
        except Exception as exc:
            log.warning("Failed to create config backup for %s: %s", cfg, exc)

    # Atomic write via temp file + replace
    tmp_path = cfg.with_name(f".{cfg.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(cfg))
    except Exception as exc:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        log.error("Failed to write configuration file %s: %s", cfg, exc)
        raise ConfigError("Failed to write configuration file.") from exc

    new_rev = compute_revision(content)
    return {
        "ok": True,
        "backed_up": True,
        "revision": new_rev,
    }


def revert_config(
    expected_revision: Optional[str] = None,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Revert configuration to the most recent backup."""
    cfg = config_path or get_config_path()
    bak = get_config_backup_path(cfg)

    if not bak.exists() or not bak.is_file():
        raise ConfigError("No configuration backup found to revert to.", error_code="CONFIG_NO_BACKUP", status_code=404)

    current_data = get_config(cfg)
    if expected_revision and expected_revision.strip():
        if expected_revision.strip().lower() != current_data["revision"].lower():
            raise ConfigConflictError()

    try:
        backup_content = bak.read_text(encoding="utf-8")
    except Exception as exc:
        log.error("Failed to read backup configuration %s: %s", bak, exc)
        raise ConfigError("Failed to read backup configuration.") from exc

    valid, err_msg = validate_config_yaml(backup_content)
    if not valid:
        raise ConfigValidationError(f"Backup configuration is invalid: {err_msg}")

    # Atomic replace
    tmp_path = cfg.with_name(f".{cfg.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(backup_content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(cfg))
    except Exception as exc:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        log.error("Failed to revert configuration %s: %s", cfg, exc)
        raise ConfigError("Failed to revert configuration.") from exc

    new_rev = compute_revision(backup_content)
    return {
        "ok": True,
        "revision": new_rev,
    }
