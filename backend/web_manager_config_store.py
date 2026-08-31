"""Web Manager durable configuration store.

This module is for Web-Manager-owned state only: setup/settings files,
bootstrap credentials, and local app secrets. It deliberately does not own
Beets engine config or media-library state.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Optional

try:  # pragma: no cover - platform-specific import
    import fcntl  # type: ignore
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore

try:  # pragma: no cover - platform-specific import
    import msvcrt  # type: ignore
except Exception:  # pragma: no cover
    msvcrt = None  # type: ignore


class WebManagerConfigStoreError(RuntimeError):
    """Base exception for Web Manager config-store errors."""


class WebManagerConfigStoreConflictError(WebManagerConfigStoreError):
    """Raised when a CAS write uses a stale or missing revision."""


def _revision_for_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _reject_existing_symlink_components(path: Path) -> None:
    pieces = []
    current = path
    while True:
        pieces.append(current)
        if current.parent == current:
            break
        current = current.parent
    for component in reversed(pieces):
        if component.exists() and (component.is_symlink() or os.path.islink(str(component))):
            raise WebManagerConfigStoreError("Configuration root contains a symlink component")


class WebManagerConfigStore:
    def __init__(self, data_root: Optional[Path | str] = None) -> None:
        if data_root is None:
            data_root = os.environ.get("WEB_MANAGER_DATA_DIR") or "/web-manager-data"
        raw_root = Path(data_root).expanduser()
        if not raw_root.is_absolute():
            raw_root = Path.cwd() / raw_root
        self.data_root = raw_root
        self._ensure_root()
        self.data_root = self.data_root.resolve(strict=True)
        _reject_existing_symlink_components(self.data_root)
        self._lock_path = self.data_root / ".web_manager_config_store.lock"

    def _ensure_root(self) -> None:
        _reject_existing_symlink_components(self.data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        if self.data_root.is_symlink() or os.path.islink(str(self.data_root)):
            raise WebManagerConfigStoreError("Configuration root cannot be a symlink")
        if os.name == "posix":
            try:
                os.chmod(self.data_root, 0o700)
            except OSError:
                pass

    @contextmanager
    def _locked(self):
        self._ensure_root()
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(self._lock_path), flags, 0o600)
        fh = os.fdopen(fd, "r+b")
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                fh.close()

    def _reject_symlink_components(self, path: Path) -> None:
        current = self.data_root
        for part in path.relative_to(self.data_root).parts:
            current = current / part
            if current.exists() and (current.is_symlink() or os.path.islink(str(current))):
                raise WebManagerConfigStoreError("Configuration path contains a symlink component")

    def resolve_path(self, relative_name: str) -> Path:
        raw = str(relative_name or "")
        if not raw.strip():
            raise WebManagerConfigStoreError("Configuration filename cannot be empty")
        if "\x00" in raw or "\\" in raw or ":" in raw:
            raise WebManagerConfigStoreError("Configuration path contains an invalid character")
        requested = Path(raw)
        if requested.is_absolute() or any(part in ("", ".", "..") for part in requested.parts):
            raise WebManagerConfigStoreError("Configuration path traversal rejected")
        target = (self.data_root / requested).resolve(strict=False)
        try:
            target.relative_to(self.data_root)
        except ValueError as exc:
            raise WebManagerConfigStoreError("Configuration path escaped the data root") from exc
        self._reject_symlink_components(target)
        return target

    def _read_bytes_if_exists(self, target: Path) -> Optional[bytes]:
        if not target.exists():
            return None
        if target.is_symlink() or os.path.islink(str(target)):
            raise WebManagerConfigStoreError("Configuration target is a symlink")
        return target.read_bytes()

    def read_text_record(self, relative_name: str) -> dict[str, Any]:
        target = self.resolve_path(relative_name)
        data = self._read_bytes_if_exists(target)
        if data is None:
            return {"exists": False, "path": str(target), "content": None, "revision": None}
        return {
            "exists": True,
            "path": str(target),
            "content": data.decode("utf-8"),
            "revision": _revision_for_bytes(data),
        }

    def load_text(self, relative_name: str, default: Optional[str] = None) -> Optional[str]:
        try:
            record = self.read_text_record(relative_name)
            return record["content"] if record["exists"] else default
        except WebManagerConfigStoreError:
            return default

    def load_json(self, relative_name: str, default: Optional[Any] = None) -> Optional[Any]:
        raw = self.load_text(relative_name)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    def save_text(self, relative_name: str, content: str, *, is_secret: bool = False,
                  expected_revision: Optional[str] = None) -> dict[str, Any]:
        if not isinstance(content, str):
            raise WebManagerConfigStoreError("Configuration content must be text")
        target = self.resolve_path(relative_name)
        data = content.encode("utf-8")
        with self._locked():
            target = self.resolve_path(relative_name)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._reject_symlink_components(target)
            if os.name == "posix":
                try:
                    os.chmod(target.parent, 0o700)
                except OSError:
                    pass
            current = self._read_bytes_if_exists(target)
            current_revision = _revision_for_bytes(current) if current is not None else None
            if current is not None and expected_revision is None:
                raise WebManagerConfigStoreConflictError("expected_revision is required for existing config file")
            if expected_revision is not None and expected_revision != current_revision:
                raise WebManagerConfigStoreConflictError("config revision conflict")

            mode = 0o600 if is_secret else 0o644
            fd = None
            tmp_path = None
            try:
                fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
                tmp_path = Path(tmp_name)
                if hasattr(os, "O_NOFOLLOW") and os.path.islink(str(tmp_path)):
                    raise WebManagerConfigStoreError("Temporary config file is a symlink")
                os.write(fd, data)
                os.fsync(fd)
                os.close(fd)
                fd = None
                os.chmod(tmp_path, mode)
                if target.exists() and (target.is_symlink() or os.path.islink(str(target))):
                    raise WebManagerConfigStoreError("Configuration target became a symlink")
                os.replace(str(tmp_path), str(target))
                tmp_path = None
                if os.name == "posix":
                    os.chmod(target, mode)
                    _fsync_directory(target.parent)
            finally:
                if fd is not None:
                    os.close(fd)
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
        new_revision = _revision_for_bytes(data)
        return {
            "ok": True,
            "path": str(target),
            "revision": new_revision,
            "previous_revision": current_revision,
        }

    def save_json(self, relative_name: str, payload: Any, *, is_secret: bool = False,
                  expected_revision: Optional[str] = None) -> dict[str, Any]:
        return self.save_text(
            relative_name,
            json.dumps(payload, indent=2, sort_keys=True),
            is_secret=is_secret,
            expected_revision=expected_revision,
        )

    def remove(self, relative_name: str, *, expected_revision: Optional[str] = None) -> bool:
        target = self.resolve_path(relative_name)
        with self._locked():
            target = self.resolve_path(relative_name)
            current = self._read_bytes_if_exists(target)
            if current is None:
                return False
            current_revision = _revision_for_bytes(current)
            if expected_revision is None or expected_revision != current_revision:
                raise WebManagerConfigStoreConflictError("config revision conflict")
            target.unlink()
            if os.name == "posix":
                _fsync_directory(target.parent)
            return True


_default_config_store: Optional[WebManagerConfigStore] = None


def get_config_store() -> WebManagerConfigStore:
    global _default_config_store
    active_root = os.environ.get("WEB_MANAGER_DATA_DIR") or "/web-manager-data"
    resolved = Path(active_root).expanduser().resolve(strict=False)
    if _default_config_store is None or _default_config_store.data_root != resolved:
        _default_config_store = WebManagerConfigStore(resolved)
    return _default_config_store
