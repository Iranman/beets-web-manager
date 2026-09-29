"""Read-only library integrity reports: duplicate album rows and untracked
files. Both only read (the Beets engine's web API, the read-only music
mount, the AcoustID file cache) and persist their evidence under the Web
Manager data directory for later, separately reviewed cleanup work."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import backend.album_duplicate_analysis as album_duplicate_analysis
import backend.untracked_inventory as untracked_inventory
from backend.acoustid_service import _acoustid_cached_fingerprint_ids, _album_item_abs_path
from backend.app_runtime import MUSIC_ROOT, WEB_MANAGER_DATA_DIR, jobs
from backend.beets_adapter import beets_adapter

ALBUM_ANALYSIS_FILE = "album_duplicate_analysis.json"
INVENTORY_DIR = "untracked_inventory"
INVENTORY_JOB_TYPE = "untracked-inventory"


def _data_dir() -> Path:
    return Path(WEB_MANAGER_DATA_DIR)


def run_album_duplicate_analysis() -> Dict[str, Any]:
    albums = beets_adapter.get_albums() or []
    items = beets_adapter.get_items() or []
    report = album_duplicate_analysis.analyze(
        albums, items, cached_ids=_acoustid_cached_fingerprint_ids, abs_path=_album_item_abs_path)
    target = _data_dir() / ALBUM_ANALYSIS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=1), encoding="utf-8")
    tmp.replace(target)
    return report


def load_album_duplicate_analysis() -> Optional[Dict[str, Any]]:
    try:
        return json.loads((_data_dir() / ALBUM_ANALYSIS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def start_untracked_inventory_job() -> Any:
    def _run(log, cancel_event=None, update_state=None):
        log.append(f"[inventory] Read-only walk of {MUSIC_ROOT}; AcoustID cache only, no API calls.")
        items = beets_adapter.get_items() or []
        summary = untracked_inventory.build_inventory(
            Path(MUSIC_ROOT), items, abs_path=_album_item_abs_path,
            cached_ids=_acoustid_cached_fingerprint_ids, out_dir=_data_dir() / INVENTORY_DIR,
            progress=update_state, cancel=(lambda: bool(cancel_event and cancel_event.is_set())))
        log.append(f"[inventory] {summary['untracked_audio_files']} untracked of "
                   f"{summary['audio_files_on_disk']} audio files: {summary['counts']}")
        return {"ok": True, "summary": summary}

    return jobs.start_python(_run, label="Untracked file inventory (read-only)",
                             metadata={"type": INVENTORY_JOB_TYPE, "path": str(MUSIC_ROOT)})


def load_untracked_inventory_summary() -> Optional[Dict[str, Any]]:
    try:
        return json.loads((_data_dir() / INVENTORY_DIR / "untracked_inventory_summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
