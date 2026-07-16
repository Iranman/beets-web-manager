import math
import time
import unittest
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")


def _plex_key_fn_source() -> str:
    return (
        APP_SOURCE[
            APP_SOURCE.index("def _plex_translate_beets_path"):
            APP_SOURCE.index("def _plex_track_keys_for_items")
        ]
        + APP_SOURCE[
            APP_SOURCE.index("def _plex_track_keys_for_items"):
            APP_SOURCE.index("def _playlist_manifest_path")
        ]
    )


class PlexKeyDuplicateTests(unittest.TestCase):
    def test_playlist_sync_preserves_duplicate_rating_keys(self):
        namespace = {
            "Any": Any,
            "Dict": Dict,
            "Iterable": Iterable,
            "List": List,
            "Optional": Optional,
            "Tuple": Tuple,
            "Counter": Counter,
            "Path": Path,
            "math": math,
            "time": time,
            "PLEX_SYNC_MAX_FALLBACK_SEARCHES": 0,
            "_s": lambda value: "" if value is None else str(value),
            "_norm": lambda value: " ".join(str(value or "").lower().split()),
            "_plex_is_final_library_path": lambda value: bool(value),
            "_plex_norm_path": lambda value: str(value or "").replace("\\", "/").rstrip("/"),
            "_plex_path_case_key": lambda value: str(value or "").replace("\\", "/").rstrip("/").casefold(),
            "_plex_path_is_under": lambda path, root: str(path or "").startswith(str(root or "")),
            "_plex_section_track_index": lambda section_key, log=None, force=False: {
                "lookup": {},
                "path_exact": {"/plex/a.flac": "101"},
                "path_case": {"/plex/a.flac": "101"},
                "suffix_paths": {},
                "filename_duration": {},
                "text_duration": {},
                "status": "ready",
                "fetched": 1,
                "total": 1,
                "duration": 0,
                "plex_music_roots": ["/plex"],
                "section_locations": ["/plex"],
                "path_map": {"beets_root": "/music", "plex_root": "/plex"},
            },
            "_plex_mapped_beets_paths": lambda path, plex_roots=None, section_locations=None: [str(path or "")],
            "_plex_suffix_keys_for_path": lambda path, roots=None: [],
            "_plex_path_keys_for_beets_item": lambda item: [],
            "_plex_item_duration": lambda item: 0,
            "_plex_duration_values": lambda duration: [],
            "_plex_unique_index_value": lambda mapping, key: "",
            "_plex_item_lookup_keys": lambda item: [],
            "_plex_find_track": lambda section_key, artist, title: None,
            "_plex_timeout_error": lambda exc: False,
            "_plex_beets_music_root": lambda: "/music",
            "_plex_music_roots": lambda: ["/plex"],
        }
        exec(_plex_key_fn_source(), namespace)
        namespace["_plex_section_track_index"] = lambda section_key, log=None, force=False: {
            "lookup": {},
            "path_exact": {"/plex/a.flac": "101"},
            "path_case": {"/plex/a.flac": "101"},
            "suffix_paths": {},
            "filename_duration": {},
            "text_duration": {},
            "status": "ready",
            "fetched": 1,
            "total": 1,
            "duration": 0,
            "plex_music_roots": ["/plex"],
            "section_locations": ["/plex"],
            "path_map": {"beets_root": "/music", "plex_root": "/plex"},
        }
        namespace["_plex_is_final_library_path"] = lambda value: bool(value)
        namespace["_plex_translate_beets_path"] = lambda path, **_kwargs: {
            "local_exists": True,
            "relative_path": str(path or "").lstrip("/"),
            "translated_path": str(path or ""),
            "candidates": [str(path or "")],
        }
        namespace["_plex_suffix_keys_for_path"] = lambda path, roots=None: []
        namespace["_plex_mapped_beets_paths"] = lambda path, plex_roots=None, section_locations=None: [str(path or "")]
        namespace["_plex_path_keys_for_beets_item"] = lambda item: []
        namespace["_plex_item_lookup_keys"] = lambda item: []
        namespace["_plex_item_duration"] = lambda item: 0
        namespace["_playlist_status_id"] = lambda item: str(item.get("path") or item.get("title") or "")
        namespace["_plex_timeout_error"] = lambda exc: False

        keys, details = namespace["_plex_track_keys_for_items"](
            "3",
            [
                {"artist": "A", "title": "Song", "path": "/plex/a.flac"},
                {"artist": "A", "title": "Song", "path": "/plex/a.flac"},
                {"artist": "B", "title": "Missing", "path": "/plex/missing.flac"},
            ],
            log=[],
            wait_seconds=0,
            return_details=True,
        )

        self.assertEqual(["101", "101"], keys)
        self.assertEqual(1, details["duplicate_keys"])
        self.assertEqual(2, details["matched_by_path"])
        self.assertEqual(1, len(details["missing_examples"]))


if __name__ == "__main__":
    unittest.main()
