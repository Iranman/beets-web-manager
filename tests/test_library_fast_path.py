"""Library load performance fix: the page-load path (GET /api/library without
a limit) must never walk the NFS music tree synchronously.

Root cause: get_library_payload() built the full disk-walked payload inline on
a cold/stale cache, and the only thing that kept the cache warm was the opt-in
legacy auto-scan -- so on a deployment with that scan disabled every page load
paid for a full os.scandir walk of the music tree over NFS (measured ~120s on
the live server). The fix serves the cached full payload if warm, else an
engine-only fast tree built with no disk access, and runs the disk
reconciliation (missing-on-disk red flags + disk-only/not-imported files) as a
background job_engine job that warms the cache for later loads.
"""
import unittest
from unittest import mock

import backend.library_service as ls
from backend.beets_adapter import RemoteAlbum, RemoteItem, BeetsUnavailableError
from job_engine import DuplicateJobError


def _fake_lib():
    items = [
        {"id": 1, "album_id": 10, "path": "Artist A/Album One/01.flac", "title": "One",
         "track": 1, "disc": 1, "tracktotal": 2, "albumartist": "Artist A", "artist": "Artist A",
         "album": "Album One", "year": 2001, "mb_trackid": "t1"},
        {"id": 2, "album_id": 10, "path": "Artist A/Album One/02.flac", "title": "Two",
         "track": 2, "disc": 1, "tracktotal": 2, "albumartist": "Artist A", "artist": "Artist A",
         "album": "Album One", "year": 2001, "mb_trackid": "t2"},
        # A singleton (no album row).
        {"id": 3, "album_id": 0, "path": "Artist B/loose.flac", "title": "Loose",
         "track": 0, "disc": 1, "tracktotal": 0, "albumartist": "Artist B", "artist": "Artist B",
         "album": "", "year": 0, "mb_trackid": ""},
    ]
    albums = [
        {"id": 10, "albumartist": "Artist A", "album": "Album One", "year": 2001,
         "mb_albumid": "a10", "mb_releasegroupid": "rg10", "albumtype": "album"},
    ]
    fake = mock.Mock()
    fake.items.side_effect = lambda q=None, fields=None: [RemoteItem(dict(r)) for r in items]
    fake.albums.side_effect = lambda q=None, fields=None: [RemoteAlbum(dict(r), adapter=object()) for r in albums]
    return fake


class FastBuilderTests(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(ls, "lib", _fake_lib()),
                  # Not the subject here: it reads a small local image cache dir,
                  # never the NFS music tree. Isolate so the no-disk assertion is
                  # specifically about the music-tree walk.
                  mock.patch.object(ls, "_attach_artist_image_cache_urls", lambda artists: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_fast_builder_never_walks_the_music_tree(self):
        import os
        with mock.patch("os.scandir", side_effect=AssertionError("scandir called")), \
             mock.patch("os.walk", side_effect=AssertionError("os.walk called")):
            payload = ls._build_library_payload_fast()
        self.assertTrue(payload["ok"])
        self.assertIs(payload["reconciled"], False)
        self.assertIs(payload["reconciling"], True)

    def test_fast_builder_groups_engine_items_into_the_tree(self):
        payload = ls._build_library_payload_fast()
        artists = {a["name"]: a for a in payload["artists"]}
        self.assertIn("Artist A", artists)
        self.assertIn("Artist B", artists)
        album = artists["Artist A"]["albums"][0]
        self.assertEqual(album["album"], "Album One")
        self.assertEqual(album["album_id"], 10)
        self.assertEqual(album["mb_releasegroupid"], "rg10")
        self.assertEqual(album["track_count"], 2)
        # Optimistic until reconciliation runs: nothing reported missing/disk-only.
        self.assertEqual(album["missing"], 0)
        self.assertEqual(album["not_imported"], 0)
        self.assertTrue(all(t["ok"] and t["imported"] and not t["missing"] for t in album["tracks"]))
        # Accurate, user-facing counts come from _library_payload_for_response
        # (singletons are tracks, not albums), same as the full builder.
        stats = ls._library_payload_for_response(payload, False, True)["stats"]
        self.assertEqual(stats["artists"], 2)
        self.assertEqual(stats["albums"], 1)
        self.assertEqual(stats["tracks"], 3)
        self.assertEqual(stats["singleton_tracks"], 1)

    def test_fast_builder_absolute_track_paths(self):
        payload = ls._build_library_payload_fast()
        album = {a["name"]: a for a in payload["artists"]}["Artist A"]["albums"][0]
        self.assertTrue(album["tracks"][0]["path"].endswith("Artist A/Album One/01.flac"))
        self.assertTrue(album["tracks"][0]["path"].startswith(str(ls.MUSIC_ROOT)))


class PageLoadNeverBlocksTests(unittest.TestCase):
    def setUp(self):
        ls.library_cache.invalidate()
        self.addCleanup(ls.library_cache.invalidate)
        for p in (mock.patch.object(ls, "lib", _fake_lib()),
                  mock.patch.object(ls, "_attach_artist_image_cache_urls", lambda artists: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_cold_cache_serves_fast_tree_and_schedules_reconcile_without_disk_walk(self):
        with mock.patch.object(ls, "_build_library_payload",
                               side_effect=AssertionError("full disk walk ran on the page-load path")) as full, \
             mock.patch.object(ls, "_schedule_library_reconcile") as sched:
            payload = ls.get_library_payload(block=False)
        full.assert_not_called()
        sched.assert_called_once()
        self.assertIs(payload["reconciled"], False)
        self.assertEqual(payload["stats"]["artists"], 2)

    def test_block_mode_builds_full_payload_synchronously(self):
        sentinel = {"ok": True, "artists": [], "stats": {"artists": 0, "albums": 0, "tracks": 0}}
        with mock.patch.object(ls, "_build_library_payload", return_value=sentinel) as full, \
             mock.patch.object(ls, "_schedule_library_reconcile") as sched:
            ls.get_library_payload(block=True)
        full.assert_called_once()
        sched.assert_not_called()

    def test_warm_cache_served_without_scheduling(self):
        ls.library_cache.store({"ok": True, "artists": [],
                                "stats": {"artists": 0, "albums": 0, "tracks": 0}})
        with mock.patch.object(ls, "_schedule_library_reconcile") as sched, \
             mock.patch.object(ls, "_build_library_payload",
                               side_effect=AssertionError("rebuilt a warm cache")):
            payload = ls.get_library_payload(block=False)
        sched.assert_not_called()
        self.assertIs(payload["reconciled"], True)
        self.assertIs(payload["reconciling"], False)

    def test_force_refresh_schedules_background_rebuild_and_serves_cache(self):
        ls.library_cache.store({"ok": True, "artists": [],
                                "stats": {"artists": 0, "albums": 0, "tracks": 0}})
        with mock.patch.object(ls, "_schedule_library_reconcile") as sched, \
             mock.patch.object(ls, "_build_library_payload",
                               side_effect=AssertionError("force rebuilt synchronously on the request")):
            payload = ls.get_library_payload(force=True, block=False)
        sched.assert_called_once()
        self.assertIs(payload["reconciling"], True)


class ScheduleReconcileTests(unittest.TestCase):
    def test_reconcile_job_is_read_only_and_deduped(self):
        captured = {}

        def fake_start(fn, label="", metadata=None):
            captured["label"] = label
            captured["metadata"] = metadata
            return mock.Mock()

        with mock.patch.object(ls.jobs, "start_python", side_effect=fake_start):
            ls._schedule_library_reconcile()
        self.assertEqual(captured["metadata"]["dedupe_key"], "library-reconcile")
        self.assertIs(captured["metadata"]["mutating"], False)

    def test_duplicate_running_reconcile_is_swallowed(self):
        with mock.patch.object(ls.jobs, "start_python",
                               side_effect=DuplicateJobError(mock.Mock(label="x", job_id="x", status="running"))):
            ls._schedule_library_reconcile()  # must not raise

    def test_reconcile_job_body_refreshes_and_stores(self):
        with mock.patch.object(ls, "_refresh_library_cache",
                               return_value={"stats": {"artists": 1, "albums": 2, "tracks": 3}}) as refresh:
            out = ls._library_reconcile_job(log=[])
        refresh.assert_called_once()
        self.assertEqual(out["stats"]["albums"], 2)


class EngineOfflineTests(unittest.TestCase):
    def setUp(self):
        ls.library_cache.invalidate()
        self.addCleanup(ls.library_cache.invalidate)

    def test_cold_cache_offline_engine_propagates_for_503(self):
        offline = mock.Mock()
        offline.items.side_effect = BeetsUnavailableError("engine down")
        offline.albums.side_effect = BeetsUnavailableError("engine down")
        with mock.patch.object(ls, "lib", offline), \
             mock.patch.object(ls, "_schedule_library_reconcile"):
            with self.assertRaises(BeetsUnavailableError):
                ls.get_library_payload(block=False)


if __name__ == "__main__":
    unittest.main()
