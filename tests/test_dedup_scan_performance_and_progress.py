"""Tests for duplicate scan performance, library caching, progress reporting, and cancellation."""

import inspect
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.library_cache import library_cache
from backend.playlist_service import _playlist_library_index
import backend.playlist_service as playlist_service
import backend.dedup_service as dedup_service
from backend.app_runtime import jobs
from job_engine import JobStore, PythonJob


class MockBeetsItem:
    def __init__(
        self,
        item_id: int,
        path: str,
        artist: str,
        title: str,
        album: str,
        mb_trackid: str = "",
        length: float = 180.0,
        format: str = "FLAC",
        bitrate: int = 1000,
    ):
        self.id = item_id
        self.path = path.encode("utf-8") if isinstance(path, str) else path
        self.artist = artist
        self.title = title
        self.album = album
        self.albumartist = artist
        self.mb_trackid = mb_trackid
        self.length = length
        self.format = format
        self.bitrate = bitrate
        self.track = 1
        self.disc = 1
        self.year = 2024

    def get(self, key, default=None):
        return getattr(self, key, default)

    def __getitem__(self, key):
        return getattr(self, key)


class MockBeetsLibrary:
    def __init__(self, items):
        self._items = list(items)
        self.items_call_count = 0
        self.items_call_args = []

    def items(self, query=None):
        self.items_call_count += 1
        self.items_call_args.append(query)
        if not query or query == [] or query == ():
            return list(self._items)
        return list(self._items)


class LibraryCacheGenerationTests(unittest.TestCase):
    def setUp(self):
        library_cache.invalidate()

    def test_cache_generation_is_stable_when_timestamp_is_zero(self):
        """Uninitialized cache (ts=0.0) must retain a stable generation and not rebuild on every call."""
        self.assertEqual(library_cache.ts, 0.0)
        initial_gen = library_cache.generation

        items = [
            MockBeetsItem(1, "/music/Artist/Album/01.flac", "Artist", "Song 1", "Album"),
            MockBeetsItem(2, "/music/Artist/Album/02.flac", "Artist", "Song 2", "Album"),
        ]
        mock_lib = MockBeetsLibrary(items)

        with patch.object(playlist_service, "lib", mock_lib):
            # First call builds index
            idx1 = _playlist_library_index()
            self.assertEqual(mock_lib.items_call_count, 1)

            # Second call within same generation MUST reuse cached index without calling lib.items
            idx2 = _playlist_library_index()
            self.assertEqual(mock_lib.items_call_count, 1)
            self.assertIs(idx1, idx2)

            # Invalidation bumps generation and causes rebuild on next call
            library_cache.invalidate()
            self.assertGreater(library_cache.generation, initial_gen)

            idx3 = _playlist_library_index()
            self.assertEqual(mock_lib.items_call_count, 2)

    def test_store_increments_generation(self):
        """Storing new library cache data must increment generation."""
        gen_before = library_cache.generation
        library_cache.store({"items": [], "albums": []})
        self.assertGreater(library_cache.generation, gen_before)


class DedupScanPerformanceAndProgressTests(unittest.TestCase):
    def setUp(self):
        library_cache.invalidate()

    def test_3000_item_library_scan_loads_data_once_and_emits_live_progress(self):
        """A 3000-item library scan must call lib.items([]) exactly once at startup and 0 times during iteration."""
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp) / "music"
            music_root.mkdir(parents=True, exist_ok=True)
            downloads_root = Path(tmp) / "downloads"
            downloads_root.mkdir(parents=True, exist_ok=True)

            items = []
            for i in range(1, 3001):
                mbid = f"mb-track-{i % 500}" if i % 10 == 0 else ""
                item_path = str(music_root / f"Artist_{i % 100}" / f"Album_{i % 50}" / f"Track_{i:04d}.flac")
                items.append(
                    MockBeetsItem(
                        item_id=i,
                        path=item_path,
                        artist=f"Artist {i % 100}",
                        title=f"Track {i:04d}",
                        album=f"Album {i % 50}",
                        mb_trackid=mbid,
                    )
                )

            mock_lib = MockBeetsLibrary(items)

            mock_stat = MagicMock()
            mock_stat.st_size = 1024 * 1024 * 20

            with patch.object(dedup_service, "lib", mock_lib), \
                 patch.object(playlist_service, "lib", mock_lib), \
                 patch.object(dedup_service, "MUSIC_ROOT", music_root), \
                 patch.object(dedup_service, "_BROWSE_ALLOWED_ROOTS", (music_root, downloads_root)), \
                 patch("pathlib.Path.exists", return_value=True), \
                 patch("pathlib.Path.is_file", return_value=True), \
                 patch("pathlib.Path.stat", return_value=mock_stat), \
                 patch.object(dedup_service, "_read_file_media_tags", return_value={}), \
                 patch.object(dedup_service, "_maintenance_same_file_hash", return_value=True), \
                 patch.object(dedup_service, "_acoustid_fingerprint_match", return_value=(None, [], [])):

                start_time = time.time()
                res, status_code = dedup_service.start_dedup_scan({
                    "path": str(music_root),
                    "tracked_only": True,
                })
                self.assertEqual(status_code, 200, f"Scan start failed: {res}")
                self.assertTrue(res.get("ok"))
                job_id = res["job_id"]
                job = jobs.get(job_id)
                self.assertIsNotNone(job)

                # Wait for job completion
                t0 = time.time()
                while job.finished_at is None and time.time() - t0 < 10.0:
                    time.sleep(0.01)
                elapsed = time.time() - start_time

            # Assert: lib.items called ONLY ONCE at startup, NEVER per-file
            self.assertEqual(mock_lib.items_call_count, 1, "lib.items must only be called once at scan startup")
            self.assertEqual(mock_lib.items_call_args, [[]])

            # Assert: 3000 items scanned extremely fast (pure in-memory index)
            self.assertLess(elapsed, 10.0, f"3000-item scan took {elapsed:.2f}s, expected < 10s")
            self.assertEqual(job.status, "success")
            self.assertEqual(job.returncode, 0)

            # Assert: Structured progress fields
            state = job.state
            self.assertEqual(state.get("scanned_count"), 3000)
            self.assertEqual(state.get("total_count"), 3000)
            self.assertEqual(state.get("remaining_count"), 0)
            self.assertEqual(state.get("progress_percent"), 100)
            self.assertIn("current_result", state)

    def test_dedup_scan_cancellation_halts_cleanly(self):
        """Cancellation during scan must set job status to 'cancelled' without completing full scan."""
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp) / "music"
            music_root.mkdir(parents=True, exist_ok=True)
            downloads_root = Path(tmp) / "downloads"
            downloads_root.mkdir(parents=True, exist_ok=True)

            items = [
                MockBeetsItem(
                    item_id=i,
                    path=str(music_root / f"Artist_{i}" / f"Album_{i}" / f"Track_{i:04d}.flac"),
                    artist=f"Artist {i}",
                    title=f"Track {i:04d}",
                    album=f"Album {i}",
                )
                for i in range(1, 1001)
            ]
            mock_lib = MockBeetsLibrary(items)

            mock_stat = MagicMock()
            mock_stat.st_size = 1024 * 1024 * 20

            with patch.object(dedup_service, "lib", mock_lib), \
                 patch.object(dedup_service, "MUSIC_ROOT", music_root), \
                 patch.object(dedup_service, "_BROWSE_ALLOWED_ROOTS", (music_root, downloads_root)), \
                 patch("pathlib.Path.exists", return_value=True), \
                 patch("pathlib.Path.is_file", return_value=True), \
                 patch("pathlib.Path.stat", return_value=mock_stat), \
                 patch.object(dedup_service, "_read_file_media_tags", return_value={}):

                res, status_code = dedup_service.start_dedup_scan({
                    "path": str(music_root),
                    "tracked_only": True,
                })
                self.assertEqual(status_code, 200)
                job = jobs.get(res["job_id"])
                self.assertIsNotNone(job)

                # Request cancellation immediately
                job.kill()

                # Wait for job completion
                t0 = time.time()
                while job.finished_at is None and time.time() - t0 < 5.0:
                    time.sleep(0.01)

            # Job must be cancelled
            self.assertEqual(job.status, "cancelled")
            self.assertEqual(job.returncode, -1)
            self.assertNotIn("ERROR: cancelled", "\n".join(job.log))

    def test_fuzzy_match_score_display_never_exceeds_100_percent(self):
        """Even if internal score composition adds bonuses (> 1.0), display output must never exceed 100%."""
        src = inspect.getsource(dedup_service.start_dedup_scan)
        self.assertIn("min(1.0, score)", src, "Fuzzy match display score must be clamped with min(1.0, score)")

    def test_log_evidence_terminology(self):
        """Scan log must use clear evidence tags ([CANDIDATE], [FINGERPRINT VERIFIED], etc.) and not '✓ DUPLICATE'."""
        src = inspect.getsource(dedup_service.start_dedup_scan)
        self.assertIn('"CANDIDATE"', src)
        self.assertIn('"FINGERPRINT VERIFIED"', src)
        self.assertIn('"BYTE VERIFIED"', src)
        self.assertIn('"REVIEW REQUIRED"', src)
        self.assertIn("[REJECTED]", src)
        self.assertNotIn("✓ DUPLICATE", src, "Legacy misleading '✓ DUPLICATE' tag must not be used")


class JobEngineCancellationStatusTests(unittest.TestCase):
    def test_job_cancellation_results_in_cancelled_status(self):
        """Cancelled jobs must have status='cancelled' and returncode=-1, not failed."""
        store = JobStore()

        def mock_worker(log, cancel_event):
            cancel_event.set()
            dedup_service._dedup_raise_if_cancelled(cancel_event, {})

        job = store.start_python(
            mock_worker,
            label="test_cancellation",
            metadata={"job_type": "dedup-scan"},
        )
        t0 = time.time()
        while job.finished_at is None and time.time() - t0 < 5.0:
            time.sleep(0.01)

        self.assertIsNotNone(job.finished_at)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.returncode, -1)
        self.assertNotIn("ERROR: cancelled", "\n".join(job.log))



class FuzzyMatchKeepsRealLibraryItemTests(unittest.TestCase):
    def test_fuzzy_candidate_resolves_to_the_real_beets_item(self):
        """Release-slot evidence (album, disc, track, IDs) needs the real item,
        not a synthetic object built from the playlist match payload."""
        import inspect
        from backend import dedup_service
        src = inspect.getsource(dedup_service.start_dedup_scan)
        self.assertIn("items_by_id.get(int(cand_payload.get(\"id\") or 0))", src)


class CancelledImportStaysRetryableTests(unittest.TestCase):
    def test_cancelled_import_job_is_retryable(self):
        src = (Path(__file__).resolve().parents[1] / "routes_import.py").read_text(encoding="utf-8")
        self.assertIn('"retryable": job.status in ("failed", "cancelled"),', src)


if __name__ == "__main__":
    unittest.main()
