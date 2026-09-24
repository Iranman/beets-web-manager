"""SEC-002 / ARCH-003 Wave 30: _run_normalize_artists_if_needed()'s DB-side
albumartist rename migrated from raw UPDATE albums/items SQL onto
album_metadata_repair_v1 via BeetsClient.update_album_metadata() -- the
family's own album->item field propagation (an album-level identity field
in `updates` merges into every item's diff that doesn't already set it)
means a single per-album call replaces both the album and item UPDATE
statements the old code issued separately.

ARCH-007 (Wave 34): this function's own read step -- the initial distinct-
albumartist scan -- was itself still a raw `_db()` SELECT, unconditionally
broken in the real two-service topology (`_db()` always routes through
raw_sqlite_query()'s hard `raise`), meaning this whole background function
had been silently a no-op on every run (swallowed by its own outer
`except Exception: pass`) since the two-service migration, not just a
theoretical risk -- found tracing this function as the explicit "sibling"
of library_normalize_artists() while fixing that route's identical
defect. Migrated onto beets_client.list_distinct_albumartists() (GET
/library/albumartists) and beets_client.find_all_albums_by_albumartist()
(GET /albums?albumartist=..., exact match), the same two structured reads
library_normalize_artists() now uses.
"""

import unittest
from unittest import mock

import app as app_module
from backend.beets_adapter import BeetsError, BeetsUnavailableError


class NormalizeArtistsIfNeededTests(unittest.TestCase):
    def setUp(self):
        self._invalidate_patch = mock.patch.object(app_module, "_invalidate_lib_cache")
        self._invalidate_patch.start()

    def tearDown(self):
        self._invalidate_patch.stop()

    def _run(self, albumartist_values, albums_by_artist=None):
        captured = {}

        def fake_find(old_aa):
            return (albums_by_artist or {}).get(old_aa, [])

        def fake_start_python(fn, label=None, metadata=None):
            log = []
            fn(log, cancel_event=None)
            captured["log"] = log
            return mock.Mock(job_id="job-test")

        with mock.patch.object(
            app_module.composite_workflows, "list_distinct_albumartists",
            return_value=albumartist_values,
        ), mock.patch.object(
            app_module.composite_workflows, "find_all_albums_by_albumartist", side_effect=fake_find,
        ), mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
            app_module._run_normalize_artists_if_needed()
        return captured.get("log", [])

    def test_no_op_when_nothing_needs_normalizing(self):
        with mock.patch.object(app_module.composite_workflows, "update_album_metadata") as mock_update:
            log = self._run(["Clean Artist"])
        mock_update.assert_not_called()
        self.assertEqual(log, [])

    def test_normalizes_via_album_metadata_repair_and_relocates(self):
        # A fancy Unicode hyphen that _normalize_albumartist collapses to ASCII.
        dirty = "Wu‐Tang Clan"
        clean = app_module._normalize_albumartist(dirty)
        self.assertNotEqual(dirty, clean)

        with mock.patch.object(
            app_module.composite_workflows, "update_album_metadata",
            return_value={"ok": True, "album_fields_changed": 1, "items_changed": 3},
        ) as mock_update, mock.patch.object(
            app_module.composite_workflows, "relocate_album",
            return_value={"ok": True, "dest_dir": "/data/media/music/Wu-Tang Clan"},
        ) as mock_relocate:
            log = self._run([dirty], {dirty: [{"id": 1, "albumartist": dirty}]})

        mock_update.assert_called_once_with(1, {"albumartist": clean}, force_write_tags=True)
        mock_relocate.assert_called_once_with(1, mode="rename")
        self.assertTrue(any("Renamed:" in line for line in log))
        self.assertTrue(any("Relocated album 1" in line for line in log))

    def test_engine_rejection_is_logged_not_raised_and_album_not_relocated(self):
        dirty = "Wu‐Tang Clan"
        with mock.patch.object(
            app_module.composite_workflows, "update_album_metadata",
            return_value={"ok": False, "error": "boom"},
        ), mock.patch.object(app_module.composite_workflows, "relocate_album") as mock_relocate:
            log = self._run([dirty], {dirty: [{"id": 1, "albumartist": dirty}]})
        mock_relocate.assert_not_called()
        self.assertTrue(any("Engine rejected normalize" in line for line in log))

    def test_engine_unavailable_is_logged_not_raised(self):
        dirty = "Wu‐Tang Clan"
        with mock.patch.object(
            app_module.composite_workflows, "update_album_metadata",
            side_effect=BeetsUnavailableError("offline"),
        ), mock.patch.object(app_module.composite_workflows, "relocate_album") as mock_relocate:
            log = self._run([dirty], {dirty: [{"id": 1, "albumartist": dirty}]})
        mock_relocate.assert_not_called()
        self.assertTrue(any("Engine unavailable" in line for line in log))

    def test_scan_engine_unavailable_is_swallowed_silently_by_the_outer_guard(self):
        """ARCH-007 (Wave 34): this is a silent, best-effort background
        function by design (the outer `except Exception: pass`) -- a
        list_distinct_albumartists() failure must not raise out of
        _run_normalize_artists_if_needed() at all (there is no `log` at
        that scope to report to; the next periodic call retries)."""
        with mock.patch.object(
            app_module.composite_workflows, "list_distinct_albumartists",
            side_effect=BeetsUnavailableError("engine offline"),
        ), mock.patch.object(app_module.jobs, "start_python") as mock_start:
            app_module._run_normalize_artists_if_needed()  # must not raise
        mock_start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
