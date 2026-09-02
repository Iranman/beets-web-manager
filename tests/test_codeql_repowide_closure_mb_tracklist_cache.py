"""Regression coverage for a real vulnerability found during the
repository-wide CodeQL closure pass (alerts 249/250/251/252, py/path-injection,
app.py:31728-31749 at baseline commit 7b05844d58d657ce6f1ae6c5b1724f5ba70ee257).

_mb_release_tracklist_cache_path() previously lowercased/stripped mb_albumid
with NO format check before joining it onto _MB_RELEASE_TRACKLIST_CACHE_DIR --
unlike the sibling release-art cache, which anchors on _RELEASE_ART_MBID_RE
first. Because the resulting Path was built with an f-string containing the
raw value (`f"{release_id}.json"` under a directory joined with
`release_id[:2]`), an mb_albumid containing "/" or ".." segments could escape
_MB_RELEASE_TRACKLIST_CACHE_DIR entirely on both the read path
(json.loads(cache_path.read_text(...))) and, more seriously, the write path
(cache_path.parent.mkdir(parents=True, exist_ok=True) followed by
tmp_path.write_text(...) and tmp_path.replace(cache_path)) -- an arbitrary
file write primitive if any of _fetch_mb_release_tracklist()'s ~28 call sites
ever passes attacker-controlled text.

Fixed by validating mb_albumid against the existing _is_valid_mb_uuid() /
_MB_UUID_RE (already used throughout this file) at the single shared
cache-path builder both the read and write sides funnel through.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module


class MbReleaseTracklistCachePathContainmentTests(unittest.TestCase):
    def test_absolute_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "mb-release-tracklists"
            with mock.patch.object(app_module, "_MB_RELEASE_TRACKLIST_CACHE_DIR", cache_dir):
                outside = str(Path(tmp) / "elsewhere" / "evil")
                self.assertIsNone(app_module._mb_release_tracklist_cache_path(outside))

    def test_relative_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "mb-release-tracklists"
            with mock.patch.object(app_module, "_MB_RELEASE_TRACKLIST_CACHE_DIR", cache_dir):
                self.assertIsNone(
                    app_module._mb_release_tracklist_cache_path("../../../etc/cron.d/evil")
                )

    def test_embedded_separator_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "mb-release-tracklists"
            with mock.patch.object(app_module, "_MB_RELEASE_TRACKLIST_CACHE_DIR", cache_dir):
                self.assertIsNone(
                    app_module._mb_release_tracklist_cache_path("aa/bb-cccc-dddd-eeee-ffffffffffff")
                )

    def test_valid_mbid_still_resolves_under_cache_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "mb-release-tracklists"
            with mock.patch.object(app_module, "_MB_RELEASE_TRACKLIST_CACHE_DIR", cache_dir):
                mbid = "11111111-2222-3333-4444-555555555555"
                path = app_module._mb_release_tracklist_cache_path(mbid)
        self.assertIsNotNone(path)
        self.assertTrue(str(path.resolve()).startswith(str(cache_dir.resolve())))
        self.assertEqual(path.name, f"{mbid}.json")

    def test_write_disk_with_malicious_mbid_creates_no_file_outside_cache_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "mb-release-tracklists"
            outside = Path(tmp) / "elsewhere"
            with mock.patch.object(app_module, "_MB_RELEASE_TRACKLIST_CACHE_DIR", cache_dir), \
                 mock.patch.object(app_module, "_MB_RELEASE_TRACKLIST_DISK_CACHE_TTL", 604800):
                app_module._mb_release_tracklist_write_disk(
                    str(outside / "evil"), {"tracks": []}
                )
            self.assertFalse(outside.exists())
            self.assertFalse(cache_dir.exists())

    def test_write_disk_with_valid_mbid_still_writes_the_cache_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "mb-release-tracklists"
            mbid = "11111111-2222-3333-4444-555555555555"
            with mock.patch.object(app_module, "_MB_RELEASE_TRACKLIST_CACHE_DIR", cache_dir), \
                 mock.patch.object(app_module, "_MB_RELEASE_TRACKLIST_DISK_CACHE_TTL", 604800):
                app_module._mb_release_tracklist_write_disk(mbid, {"tracks": ["a"]})
                cached = app_module._mb_release_tracklist_read_disk(mbid, __import__("time").time())
        self.assertEqual(cached, {"tracks": ["a"]})


if __name__ == "__main__":
    unittest.main()
