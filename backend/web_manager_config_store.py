"""Web Manager Configuration Persistence Primitive (ARCH-003 Wave 27).

Provides safe, durable, atomic, and root-bound persistence for Web-Manager-owned
configuration files, application settings, and bootstrap credentials.

Enforces:
- Fixed root directory (Web-Manager-owned data directory)
- Path containment & traversal rejection (no '..', no symlinks)
- Atomic write via temporary file + fsync + os.replace
- Restrictive permissions (0o600 for secrets, 0o700 for directories)
- Safe cleanup on error
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, Optional


class WebManagerConfigStoreError(RuntimeError):
    """Base exception for Web Manager Config Store errors."""
    pass


class WebManagerConfigStore:
    def __init__(self, data_root: Optional[Path | str] = None) -> None:
        if data_root:
            self.data_root = Path(data_root).resolve(strict=False)
        else:
            env_dir = os.environ.get("WEB_MANAGER_DATA_DIR") or os.environ.get("METADATA_CACHE_DIR")
            if env_dir:
                self.data_root = Path(env_dir).resolve(strict=False)
            else:
                self.data_root = Path("/data/web-manager-data").resolve(strict=False)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._set_restrictive_dir_permissions(self.data_root)

    def _set_restrictive_dir_permissions(self, path: Path) -> None:
        if os.name == "posix":
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass

    def _set_restrictive_file_permissions(self, path: Path) -> None:
        if os.name == "posix":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    def resolve_path(self, relative_name: str) -> Path:
        """Resolve a relative filename within the authoritative data_root.

        Raises WebManagerConfigStoreError on path traversal or symlink detection.
        """
        raw = str(relative_name or "").strip()
        if not raw:
            raise WebManagerConfigStoreError("Configuration filename cannot be empty")
        
        if ".." in raw or raw.startswith("/") or raw.startswith("\\") or ":" in raw:
            raise WebManagerConfigStoreError(f"Path traversal rejected for config file: {relative_name!r}")
        
        unresolved_target = self.data_root / raw
        if unresolved_target.is_symlink() or os.path.islink(str(unresolved_target)):
            raise WebManagerConfigStoreError(f"Symlink target rejected for config file: {relative_name!r}")

        target = unresolved_target.resolve(strict=False)
        
        try:
            target.relative_to(self.data_root)
        except ValueError:
            raise WebManagerConfigStoreError(f"Path escape rejected for config file: {relative_name!r}")
        
        if target.is_symlink() or os.path.islink(str(target)):
            raise WebManagerConfigStoreError(f"Symlink target rejected for config file: {relative_name!r}")
        
        return target

    def save_text(self, relative_name: str, content: str, *, is_secret: bool = False) -> Path:
        """Atomically write text content to a config file inside data_root."""
        target = self.resolve_path(relative_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._set_restrictive_dir_permissions(target.parent)

        tmp_name = f".tmp_{target.name}_{uuid.uuid4().hex}"
        tmp_path = target.parent / tmp_name

        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY

            mode = 0o600 if is_secret else 0o644
            fd = os.open(str(tmp_path), flags, mode)
            try:
                data = content.encode("utf-8")
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)

            if is_secret:
                self._set_restrictive_file_permissions(tmp_path)

            os.replace(str(tmp_path), str(target))
            if is_secret:
                self._set_restrictive_file_permissions(target)
            return target
        except Exception as exc:
            if tmp_path.exists():
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise WebManagerConfigStoreError(f"Failed to persist config file {relative_name!r}: {exc}") from exc

    def save_json(self, relative_name: str, payload: Any, *, is_secret: bool = False) -> Path:
        """Atomically write JSON payload to a config file inside data_root."""
        content = json.dumps(payload, indent=2, sort_keys=True)
        return self.save_text(relative_name, content, is_secret=is_secret)

    def load_text(self, relative_name: str, default: Optional[str] = None) -> Optional[str]:
        """Load text content from a config file inside data_root."""
        try:
            target = self.resolve_path(relative_name)
            if not target.exists() or os.path.islink(target):
                return default
            return target.read_text(encoding="utf-8")
        except Exception:
            return default

    def load_json(self, relative_name: str, default: Optional[Any] = None) -> Optional[Any]:
        """Load JSON payload from a config file inside data_root."""
        raw = self.load_text(relative_name)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    def remove(self, relative_name: str) -> bool:
        """Safely remove a config file inside data_root."""
        try:
            target = self.resolve_path(relative_name)
            if target.exists() and not os.path.islink(target):
                target.unlink(missing_ok=True)
                return True
        except Exception:
            pass
        return False


# Global default store instance dynamically tracking WEB_MANAGER_DATA_DIR / METADATA_CACHE_DIR
_default_config_store: Optional[WebManagerConfigStore] = None


def get_config_store() -> WebManagerConfigStore:
    global _default_config_store
    active_root = os.environ.get("WEB_MANAGER_DATA_DIR") or os.environ.get("METADATA_CACHE_DIR") or "/data/web-manager-data"
    if _default_config_store is None or str(_default_config_store.data_root) != str(Path(active_root).resolve(strict=False)):
        _default_config_store = WebManagerConfigStore(active_root)
    return _default_config_store
