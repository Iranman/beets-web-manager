"""SEC-002 / ARCH-003 Wave 32: library_normalize_artists()'s DB-side
albumartist rename migrated from raw UPDATE albums/UPDATE items SQL onto
album_metadata_repair_v1, and its write+move step migrated from local
subprocess.run([BEET_BIN, "write"/"move", ...]) onto
beets_client.update_album_metadata(force_write_tags=True) +
beets_client.relocate_album(). This closes both the SQL-sink problem and
the "no local Beets execution fallback" architecture violation for this
route -- the same migration already applied to the sibling auto-triggered
function _run_normalize_artists_if_needed() in Wave 30, confirmed to be
literally duplicate logic before migrating.

ARCH-007 (Wave 34): both routes' remaining read steps -- the initial
distinct-albumartist scan, and the per-name "which albums currently hold
this value" lookup -- were still raw `_db()` SELECTs, which real Docker
acceptance testing proved unconditionally raise in the actual two-service
deployment. Migrated onto beets_client.list_distinct_albumartists() (GET
/library/albumartists) and beets_client.find_all_albums_by_albumartist()
(GET /albums?albumartist=..., exact match) respectively -- both real,
structured, engine-side queries. The fixtures below mock those client
methods directly instead of backing a local sqlite file through a patched
`_db()`.
"""

import unittest
from unittest import mock

import app as app_module
from backend.beets_client import BeetsError, BeetsUnavailableError


class LibraryNormalizeArtistsTests(unittest.TestCase):
    def setUp(self):
        self._invalidate_patch = mock.patch.object(app_module, "_invalidate_lib_cache")
        self._invalidate_patch.start()

    def tearDown(self):
        self._invalidate_patch.stop()

    def _run(self, albumartist_values, albums_by_artist=None):
        captured = {}

        def fake_find(old_aa):
            rows = (albums_by_artist or {}).get(old_aa, [])
            return rows

        def fake_start_python(fn, label=None, metadata=None):
            log = []
            with mock.patch.object(
                app_module.beets_client, "list_distinct_albumartists",
                return_value=albumartist_values,
            ), mock.patch.object(
                app_module.beets_client, "find_all_albums_by_albumartist",
                side_effect=fake_find,
            ):
                fn(log, cancel_event=None)
            captured["log"] = log
            return mock.Mock(job_id="job-test")

        with app_module.app.test_request_context(
            "/api/library/normalize-artists", method="POST",
        ), mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
            app_module.library_normalize_artists()
        return captured.get("log", [])

    def test_no_op_when_nothing_needs_normalizing(self):
        with mock.patch.object(app_module.beets_client, "update_album_metadata") as mock_update, \
             mock.patch.object(app_module, "subprocess") as mock_subprocess:
            log = self._run(["Clean Artist"])
        mock_update.assert_not_called()
        mock_subprocess.run.assert_not_called()
        self.assertTrue(any("No artist names needed normalization" in line for line in log))

    def test_normalizes_via_engine_and_relocates_without_any_local_subprocess(self):
        dirty = "Wu‐Tang Clan"
        clean = app_module._normalize_albumartist(dirty)
        self.assertNotEqual(dirty, clean)

        with mock.patch.object(
            app_module.beets_client, "update_album_metadata",
            return_value={"ok": True, "album_fields_changed": 1, "items_changed": 3},
        ) as mock_update, mock.patch.object(
            app_module.beets_client, "relocate_album",
            return_value={"ok": True, "dest_dir": "/data/media/music/Wu-Tang Clan"},
        ) as mock_relocate, mock.patch.object(
            app_module, "subprocess",
        ) as mock_subprocess:
            log = self._run([dirty], {dirty: [{"id": 1, "albumartist": dirty}]})

        mock_update.assert_called_once_with(1, {"albumartist": clean}, force_write_tags=True)
        mock_relocate.assert_called_once_with(1, mode="rename")
        # No local `beet write`/`beet move` subprocess execution at all --
        # the architecture-violation half of this migration, not just the
        # SQL-sink half.
        mock_subprocess.run.assert_not_called()
        self.assertTrue(any("Renamed:" in line for line in log))
        self.assertTrue(any("Relocated album 1" in line for line in log))

    def test_engine_rejection_is_logged_not_raised_and_album_not_relocated(self):
        dirty = "Wu‐Tang Clan"
        with mock.patch.object(
            app_module.beets_client, "update_album_metadata",
            return_value={"ok": False, "error": "boom"},
        ), mock.patch.object(app_module.beets_client, "relocate_album") as mock_relocate:
            log = self._run([dirty], {dirty: [{"id": 1, "albumartist": dirty}]})
        mock_relocate.assert_not_called()
        self.assertTrue(any("Engine rejected normalize" in line for line in log))

    def test_engine_unavailable_is_logged_not_raised(self):
        dirty = "Wu‐Tang Clan"
        with mock.patch.object(
            app_module.beets_client, "update_album_metadata",
            side_effect=BeetsUnavailableError("offline"),
        ), mock.patch.object(app_module.beets_client, "relocate_album") as mock_relocate:
            log = self._run([dirty], {dirty: [{"id": 1, "albumartist": dirty}]})
        mock_relocate.assert_not_called()
        self.assertTrue(any("Engine unavailable" in line for line in log))

    def test_engine_unavailable_on_the_scan_itself_is_logged_not_raised(self):
        """ARCH-007 (Wave 34): the NEW failure mode this migration
        introduces a real recovery path for -- the initial distinct-
        albumartist scan can now fail with a real engine-offline error
        instead of the old unconditional raw-SQL raise. Must be caught and
        logged, not propagated as an unhandled job exception."""
        captured = {}

        def fake_start_python(fn, label=None, metadata=None):
            log = []
            with mock.patch.object(
                app_module.beets_client, "list_distinct_albumartists",
                side_effect=BeetsUnavailableError("engine offline"),
            ):
                fn(log, cancel_event=None)
            captured["log"] = log
            return mock.Mock(job_id="job-test")

        with mock.patch.object(app_module.beets_client, "update_album_metadata") as mock_update, \
             app_module.app.test_request_context("/api/library/normalize-artists", method="POST"), \
             mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
            app_module.library_normalize_artists()
        mock_update.assert_not_called()
        log = captured.get("log", [])
        self.assertTrue(any("Engine unavailable listing artist names" in line for line in log))

    def test_engine_unavailable_on_per_name_lookup_does_not_block_other_names(self):
        """ARCH-007 (Wave 34): a per-name find_all_albums_by_albumartist()
        failure for one dirty name must not abort normalization of the
        others."""
        dirty1, dirty2 = "Wu‐Tang Clan", "Sigur Rós‐ish"
        clean2 = app_module._normalize_albumartist(dirty2)
        self.assertNotEqual(dirty2, clean2)

        def fake_find(old_aa):
            if old_aa == dirty1:
                raise BeetsUnavailableError("engine offline")
            return [{"id": 2, "albumartist": dirty2}]

        captured = {}

        def fake_start_python(fn, label=None, metadata=None):
            log = []
            with mock.patch.object(
                app_module.beets_client, "list_distinct_albumartists",
                return_value=[dirty1, dirty2],
            ), mock.patch.object(
                app_module.beets_client, "find_all_albums_by_albumartist", side_effect=fake_find,
            ), mock.patch.object(
                app_module.beets_client, "update_album_metadata",
                return_value={"ok": True},
            ) as mock_update, mock.patch.object(
                app_module.beets_client, "relocate_album", return_value={"ok": True, "dest_dir": "x"},
            ) as mock_relocate:
                fn(log, cancel_event=None)
            captured["update"] = mock_update
            captured["relocate"] = mock_relocate
            captured["log"] = log
            return mock.Mock(job_id="job-test")

        with app_module.app.test_request_context("/api/library/normalize-artists", method="POST"), \
             mock.patch.object(app_module.jobs, "start_python", side_effect=fake_start_python):
            app_module.library_normalize_artists()

        log = captured.get("log", [])
        self.assertTrue(any("Engine unavailable looking up albums" in line for line in log))
        # Album 2 (under dirty2) still got normalized despite dirty1's lookup failing.
        captured["update"].assert_called_once_with(2, {"albumartist": clean2}, force_write_tags=True)
        captured["relocate"].assert_called_once_with(2, mode="rename")


if __name__ == "__main__":
    unittest.main()
