"""Authorization for unattended (scheduled) duplicate deletion.

This is the only switch that lets the maintenance runner delete duplicate
files without a person reviewing them. It is deliberately independent of
configuration: MUSIC_ROOT (or any other setting) only says where library files
live, so fixing or changing it can make the duplicate scan *see* files but can
never authorize deleting them. The switch is off unless an operator turned it
on explicitly; a missing, unreadable or malformed state file means off.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

STATE_FILE_NAME = "duplicate_cleanup_authorization.json"
ENABLE_CONFIRMATION = "ENABLE UNATTENDED DUPLICATE DELETION"


def _state_path(data_dir: Path) -> Path:
    return Path(data_dir) / STATE_FILE_NAME


def load_authorization(data_dir: Path) -> Dict[str, Any]:
    state: Dict[str, Any] = {"unattended_delete_enabled": False, "changed_at": None, "changed_by": "", "reason": ""}
    try:
        raw = json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if not isinstance(raw, dict):
        return state
    state["unattended_delete_enabled"] = raw.get("unattended_delete_enabled") is True
    for key in ("changed_at", "changed_by", "reason"):
        if key in raw:
            state[key] = raw[key]
    return state


def unattended_delete_enabled(data_dir: Path) -> bool:
    return load_authorization(data_dir)["unattended_delete_enabled"] is True


def set_unattended_delete(data_dir: Path, enabled: bool, *, actor: str, reason: str) -> Dict[str, Any]:
    state = {
        "unattended_delete_enabled": enabled is True,
        "changed_at": time.time(),
        "changed_by": str(actor or "")[:120],
        "reason": str(reason or "")[:500],
    }
    target = _state_path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dedup-auth-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return state
