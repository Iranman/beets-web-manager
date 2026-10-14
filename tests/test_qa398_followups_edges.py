"""QA #398 (independent): edges the implementer's tests do not pin -- the
#381 link-probe budget boundary, the Find Music refresh-reuse limits and
concurrent refreshes, and cover-art detection in the ARCH-031 library build.
Synthetic data only."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import backend.import_identity as ii
import backend.library_service as ls
import tests.test_acquisition_queue_cache as base
from backend.beets_adapter import RemoteItem
from tests.test_acquisition_queue_cache import _CacheCase, _wanted
from tests.test_import_folder_identity import _FolderFixture


def setUpModule():
    base.setUpModule()


class LinkProbeBudget(_FolderFixture):
    def _linked_dir(self, name, files):
        outside = Path(self.tmp.name) / name
        outside.mkdir()
        for i in range(files):  # empty folders: any file would block (QA394-B1)
            (outside / f"scan{i}").mkdir()
        try:
            os.symlink(outside, self.folder / name, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported here")

    def _linked(self, budget):
        # N8: every linked folder blocks; the budget bounds only the diagnostic
        # probe, so report whether the probe gave up (or found files).
        with mock.patch.object(ii, "_LINKED_AUDIO_PROBE_MAX_ENTRIES", budget), \
                self.assertLogs(ii._app_logger, "DEBUG") as logs:
            self.assertTrue(ii._recursive_audio(str(self.folder))[1])
        return any("holds files" in line for line in logs.output)

    def test_budget_boundary_is_exact(self):
        self._linked_dir("Scans", 6)  # top: 1 + 6 entries, then 6 x 1 = 13
        self.assertFalse(self._linked(13))
        self.assertTrue(self._linked(12))

    def test_budget_is_shared_by_every_link_in_the_folder(self):
        self._linked_dir("A", 6)  # 13 each
        self._linked_dir("B", 6)
        self.assertFalse(self._linked(26))
        self.assertTrue(self._linked(25))

    def test_link_loop_terminates_without_audio(self):
        outside = Path(self.tmp.name) / "loop"
        outside.mkdir()
        try:
            os.symlink(outside, outside / "self", target_is_directory=True)
            os.symlink(outside, self.folder / "Loop", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported here")
        self.assertFalse(self._linked(ii._LINKED_AUDIO_PROBE_MAX_ENTRIES))  # probe terminates: empty


class RefreshReuseLimits(_CacheCase):
    def setUp(self):
        super().setUp()
        self.wanted = [_wanted(i) for i in range(1, 6)]
        self._build()
        self.acq = base.acq
        self.rows = self.acq.acquisition_queue_snapshot()[0]["items"]

    def _refresh(self, **body):
        res = self.client.post("/api/acquisition/queue/refresh", json=body)
        return res.status_code, res.get_json()

    def _join(self, body):
        if body.get("job_id"):
            self.store.get(body["job_id"])._thread.join(10)

    def test_window_expired_rebuilds(self):
        self.acq.acquisition_queue_mark_done(mbid=self.rows[0]["mbid"])
        self.acq._acq_queue["absorb_refresh_until"] = time.time() - 1
        status, body = self._refresh(after_download=True)
        self.assertEqual((status, body["reused"]), (202, False))
        self._join(body)

    def test_stale_snapshot_is_not_reused(self):
        self.acq.acquisition_queue_mark_done(mbid=self.rows[0]["mbid"])
        snap = self.acq._acq_queue["snapshot"]
        self.acq._acq_queue["snapshot"] = dict(snap, built_at=time.time() - 7 * 3600)
        status, body = self._refresh(after_download=True)
        self.assertEqual((status, body["reused"]), (202, False))
        self._join(body)

    def test_unmonitored_request_is_not_served_by_a_monitored_only_list(self):
        self.acq.acquisition_queue_mark_done(mbid=self.rows[0]["mbid"])
        status, body = self._refresh(include_unmonitored=True, after_download=True)
        self.assertEqual((status, body["reused"]), (202, False))
        self._join(body)

    def test_two_refreshes_at_once_share_one_build(self):
        release = self._hold_builds()
        before = len([j for j in self.store.all() if j.metadata.get("type") == "acquisition-queue-build"])
        results = []
        threads = [threading.Thread(target=lambda: results.append(self._refresh())) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        release.set()
        self.assertEqual(sorted(s for s, _ in results), [202, 202])
        self.assertEqual(len({b["job_id"] for _, b in results}), 1, results)
        self.assertEqual(sorted(b["already_running"] for _, b in results), [False, True])
        self._join(results[0][1])
        after = len([j for j in self.store.all() if j.metadata.get("type") == "acquisition-queue-build"])
        self.assertEqual(after - before, 1)

    def test_file_matches_memory_after_mark_done_races_a_build(self):
        release = self._hold_builds()
        job_id, _ = self.acq.start_acquisition_queue_build(force=True)
        self.acq.acquisition_queue_mark_done(mbid=self.rows[3]["mbid"])
        release.set()
        self.store.get(job_id)._thread.join(10)
        mem = [r["key"] for r in self.acq.acquisition_queue_snapshot()[0]["items"]]
        disk = [r["key"] for r in json.loads(self.cache_file.read_text(encoding="utf-8"))["items"]]
        self.assertEqual(mem, disk)
        self.assertNotIn(self.rows[3]["key"], mem)


@unittest.skipIf(os.name != "posix", "case-sensitive POSIX filesystem semantics")
class LibraryCoverArtNames(unittest.TestCase):
    """The scandir lookup must find exactly what Path.exists() found before."""

    def test_cover_art_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = {"Exact": "cover.jpg", "Upper": "Cover.JPG", "Dir": "folder.jpg", "Broken": "front.jpg",
                     "Later": "cover.png"}
            items = []
            for n, (album, art) in enumerate(cases.items(), 1):
                d = root / "Art" / f"{album} (2000)"
                d.mkdir(parents=True)
                (d / "01.flac").write_bytes(b"")
                if album == "Dir":
                    (d / art).mkdir()
                elif album == "Broken":
                    os.symlink("/nonexistent/front.jpg", d / art)
                else:
                    (d / art).write_bytes(b"")
                items.append({"id": n, "album_id": 0, "path": f"Art/{album} (2000)/01.flac", "title": "t",
                              "track": 1, "disc": 1, "albumartist": "Art", "artist": "Art", "album": album,
                              "year": 2000})
            fake = mock.Mock()
            fake.items.side_effect = lambda q=None: [RemoteItem(dict(r)) for r in items]
            fake.albums.side_effect = lambda q=None: []
            with mock.patch.object(ls, "MUSIC_ROOT", root), mock.patch.object(ls, "lib", fake), \
                    mock.patch.object(ls, "_load_pending_reviews", return_value=[]):
                payload = ls._build_library_payload()
            art = {al["album"]: Path(al["disk_art"]).name if al["disk_art"] else ""
                   for a in payload["artists"] for al in a["albums"]}
            self.assertEqual(art, {"Exact": "cover.jpg", "Upper": "", "Dir": "folder.jpg", "Broken": "",
                                   "Later": "cover.png"})


if __name__ == "__main__":
    unittest.main()
