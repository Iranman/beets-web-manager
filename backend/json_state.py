"""Canonical atomic save and tolerant load for whole-document state files.

Use it for a file that is rewritten whole, where the last writer wins or the
caller already holds the lock that orders its read-modify-write. The write
goes through a unique same-directory temp file that is flushed and fsynced
before ``os.replace``, so a crash leaves the old document or the new one, and
two concurrent writers can never share a temp file.

Deliberately NOT routed through here, because their semantics differ:

* ``job_engine.JobStore._write``: snapshots the job inside its write lock,
  retries the replace on Windows and skips fsync (progress writes are constant
  and restart recovery already tolerates a lost update).
* ``resource_locks.ResourceLocks``: each record is a lock owned under the
  registry's inter-process file lock, and heartbeats rewrite them constantly.
* ``web_manager_config_store``: compare-and-swap on a content revision, secret
  file modes, symlink refusal and a directory fsync.
* ``dedup_authorization``, ``import_reconciliation`` reviews: ``mkstemp`` gives
  them owner-only files; this helper would widen them to the umask default.
* ``config_manager``, ``config_layers``, ``beets_plugins``: the operator's YAML
  and env files, whose owner and mode are preserved, and which are not JSON.
* ``playlist_service._playlist_atomic_json_replace``: no-follow writes confined
  to a validated state root.
* The MusicBrainz tracklist disk cache: a rebuildable cache not worth an fsync.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a same-directory temp file that is
    removed on every failure path, including a refused replace."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def atomic_write_json(path: Path, payload: Any, **dumps_kwargs: Any) -> None:
    """Serialize first (a bad payload never touches the file), create the
    parent directory, and replace ``path``. ``dumps_kwargs`` go to json.dumps."""
    text = json.dumps(payload, **dumps_kwargs)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)


def load_json_object(path: Path) -> Dict[str, Any]:
    """The JSON object at ``path``; ``{}`` when it is missing, unreadable,
    malformed or not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
