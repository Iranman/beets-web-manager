"""#280 (slskd playlist transfers left queued; search polling ignores
cancel), #281/#263 (cleanup flattened-path collision, check-then-unlink
race, bounded walk) and #339 (Plex index wall timeout without a thread)."""
import contextlib
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import app as app_module  # noqa: F401  -- loads the module family in order
import backend.playlist_service as ps
import backend.plex_service as plex_service
import backend.slskd as slskd_mod
import backend.slskd_service as slskd
from backend.slskd import QueuedRemote, cleanup_failed_candidate_files


class PlaylistSlskdCleanupTests(unittest.TestCase):  # #280
    def _download(self, *, wait=None, find=None, search=None):
        cancel, cleanup = mock.Mock(), mock.Mock()
        patches = dict(
            SLSKD_API_KEY="k",
            _slskd_search_and_queue=search or mock.Mock(return_value=("peer", ["A\\01.flac"], "/dl/A", "A")),
            _slskd_wait_downloads=wait or mock.Mock(return_value=("/dl/A", [])),
            _find_slskd_downloaded_files=find or mock.Mock(return_value=("/dl/A", [])),
            _slskd_cancel_queued_downloads=cancel,
            _slskd_cleanup_failed_candidate_files=cleanup,
        )
        with mock.patch.multiple(ps, **patches):
            with self.assertRaises(RuntimeError) as ctx:
                ps._playlist_slskd_download_track("A", "T", Path("/stage"), lambda _l: None,
                                                  cancel_event=threading.Event())
        return ctx.exception, cancel, cleanup

    def test_cancelled_wait_cancels_and_cleans_the_queued_transfer(self):
        exc, cancel, cleanup = self._download(wait=mock.Mock(side_effect=RuntimeError("cancelled")))
        self.assertEqual(str(exc), "cancelled")
        cancel.assert_called_once_with("peer", ["A\\01.flac"], mock.ANY)
        cleanup.assert_called_once_with("peer", ["A\\01.flac"], mock.ANY)

    def test_no_file_found_cancels_and_cleans(self):
        exc, cancel, cleanup = self._download()
        self.assertIn("no queued playlist file", str(exc))
        cancel.assert_called_once()
        cleanup.assert_called_once()

    def test_failed_search_has_nothing_to_clean(self):
        exc, cancel, cleanup = self._download(search=mock.Mock(side_effect=RuntimeError("no results")))
        cancel.assert_not_called()
        cleanup.assert_not_called()

    def test_cleanup_failure_does_not_hide_the_original_error(self):
        cancel = mock.Mock(side_effect=OSError("boom"))
        with mock.patch.multiple(ps, SLSKD_API_KEY="k",
                                 _slskd_search_and_queue=mock.Mock(return_value=("peer", ["A\\01.flac"], "", "")),
                                 _slskd_wait_downloads=mock.Mock(side_effect=RuntimeError("cancelled")),
                                 _slskd_cancel_queued_downloads=cancel,
                                 _slskd_cleanup_failed_candidate_files=mock.Mock()):
            with self.assertRaisesRegex(RuntimeError, "^cancelled$"):
                ps._playlist_slskd_download_track("A", "T", Path("/stage"), lambda _l: None)


class SearchPollCancelTests(unittest.TestCase):  # #280
    def test_cancel_during_poll_stops_without_sleeping(self):
        event = mock.Mock()
        event.wait.return_value = True  # set while waiting
        with mock.patch.object(slskd, "_slskd_req", return_value={}), \
                mock.patch.object(slskd.time, "sleep", side_effect=AssertionError("slept despite cancel")):
            with self.assertRaisesRegex(RuntimeError, "^cancelled$"):
                slskd._slskd_search_and_queue("A", "B", "", [], cancel_event=event)
        event.wait.assert_called_once_with(1)


class BoundedWalkTests(unittest.TestCase):  # #263
    def _tree(self, tmp):
        root = Path(tmp)
        for i in range(5):
            (root / f"d{i}").mkdir()
            (root / f"d{i}" / "01.flac").write_bytes(b"x")
        return root

    def test_walk_stops_at_its_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            ticks = iter([0.0, 0.0, 0.0, 10.0, 10.0, 10.0, 10.0, 10.0])
            with mock.patch.object(slskd.time, "monotonic", side_effect=lambda: next(ticks)):
                found = list(slskd._slskd_bounded_walk(root, 5.0))
            self.assertLess(len(found), 5)

    def test_walk_honours_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            event = threading.Event()
            event.set()
            with self.assertRaisesRegex(RuntimeError, "^cancelled$"):
                list(slskd._slskd_bounded_walk(root, 60.0, event))

    def test_walk_does_not_follow_symlinked_folders(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            (Path(outside) / "secret.flac").write_bytes(b"x")
            try:
                os.symlink(outside, root / "link", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not permitted on this host")
            self.assertEqual(list(slskd._slskd_bounded_walk(root, 60.0)), [])


def _dirfd_supported():
    return hasattr(os, "O_NOFOLLOW") and os.unlink in os.supports_dir_fd


class CleanupHardeningTests(unittest.TestCase):  # #281
    def _q(self, remote, size=1):
        return QueuedRemote(remote, size, time.time() - 60)

    @unittest.skipUnless(_dirfd_supported(), "needs dir_fd unlink (Linux)")
    def test_flattened_path_is_skipped_when_the_full_path_holds_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp)
            full = dl / "peer" / "Fake" / "Album B" / "01.flac"
            flat = dl / "peer" / "Album B" / "01.flac"  # another album of this peer
            for p in (full, flat):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"x")
            removed = cleanup_failed_candidate_files(dl, "peer", [self._q("Fake\\Album B\\01.flac")],
                                                     [".flac"], [], [dl])
            self.assertEqual(removed, 1)
            self.assertFalse(full.exists())
            self.assertTrue(flat.exists())

    @unittest.skipUnless(_dirfd_supported(), "needs dir_fd unlink (Linux)")
    def test_folder_swapped_for_symlink_after_the_check_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as lib:
            dl = Path(tmp)
            own = dl / "peer" / "Album" / "01.flac"
            own.parent.mkdir(parents=True)
            own.write_bytes(b"x")
            victim = Path(lib) / "01.flac"
            victim.write_bytes(b"x")
            real_unlink = slskd_mod._unlink_proven_under

            def swap_then_unlink(root, path, remote):
                # The race: after within_roots passed, the folder becomes a
                # symlink to the library.
                (dl / "peer" / "Album").rename(dl / "peer" / "Album.real")
                os.symlink(lib, dl / "peer" / "Album", target_is_directory=True)
                return real_unlink(root, path, remote)

            with mock.patch.object(slskd_mod, "_unlink_proven_under", side_effect=swap_then_unlink):
                removed = cleanup_failed_candidate_files(dl, "peer", [self._q("Album\\01.flac")],
                                                         [".flac"], [], [dl])
            self.assertEqual(removed, 0)
            self.assertTrue(victim.exists())

    def test_unlink_fails_closed_without_dir_fd_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "a" / "01.flac"
            f.parent.mkdir()
            f.write_bytes(b"x")
            with mock.patch.object(slskd_mod.os, "supports_dir_fd", set()):
                self.assertFalse(slskd_mod._unlink_proven_under(root, f, self._q("a\\01.flac")))
            self.assertTrue(f.exists())


class _SlowBody:
    def __init__(self, chunks, clock, step):
        self.chunks, self.clock, self.step = list(chunks), clock, step
        self.status = 200

    def read(self, n=-1):
        self.clock[0] += self.step
        return self.chunks.pop(0) if self.chunks else b""


class PlexDeadlineTests(unittest.TestCase):  # #339
    SETTINGS = {"url": "http://plex.test:32400", "token": "t"}

    def _request(self, body, clock, deadline, timeouts):
        @contextlib.contextmanager
        def opened(provider, req, *, timeout=None, max_attempts=None):
            timeouts.append(timeout)
            yield body

        with mock.patch.object(plex_service, "_plex_settings", return_value=dict(self.SETTINGS)), \
                mock.patch.object(plex_service.provider_boundary, "opened", side_effect=opened), \
                mock.patch.object(plex_service.time, "time", side_effect=lambda: clock[0]):
            return plex_service._plex_request("/x", timeout=30, attempts=1, deadline=deadline)

    def test_slow_body_past_the_deadline_times_out_without_a_thread(self):
        clock, timeouts = [1000.0], []
        before = {t.name for t in threading.enumerate()}
        body = _SlowBody([b"{", b"}"] * 50, clock, step=3.0)
        with self.assertRaises(TimeoutError):
            self._request(body, clock, 1010.0, timeouts)
        self.assertEqual(timeouts, [10.0])  # socket wait capped at the time left
        self.assertEqual({t.name for t in threading.enumerate()} - before, set())

    def test_fast_body_is_read_whole(self):
        clock, timeouts = [1000.0], []
        body = _SlowBody([b'{"MediaContainer"', b': {"size": 1}}'], clock, step=0.1)
        self.assertEqual(self._request(body, clock, 1100.0, timeouts), {"MediaContainer": {"size": 1}})
        self.assertEqual(timeouts, [30])

    def test_past_deadline_sends_nothing(self):
        clock, timeouts = [1000.0], []
        with self.assertRaises(TimeoutError):
            self._request(_SlowBody([], clock, 0), clock, 999.0, timeouts)
        self.assertEqual(timeouts, [])

    def test_index_pages_share_one_total_deadline(self):
        pages = [{"MediaContainer": {"totalSize": 4, "Metadata": [{"ratingKey": str(i), "title": "t"}]}}
                 for i in range(4)]
        req = mock.Mock(side_effect=pages)
        with mock.patch.multiple(plex_service,
                                 _plex_settings=mock.Mock(return_value={"url": "u", "plex_index_timeout": 40}),
                                 _plex_section_locations=mock.Mock(return_value=[]),
                                 _plex_effective_music_roots=mock.Mock(return_value=[]),
                                 _plex_selected_path_map=mock.Mock(return_value={}),
                                 _plex_request=req), \
                mock.patch.dict(plex_service._PLEX_TRACK_INDEX_CACHE, {}, clear=True):
            index = plex_service._plex_section_track_index("3", force=True)
        self.assertEqual(index["fetched"], 4)
        deadlines = {c.kwargs["deadline"] for c in req.call_args_list}
        self.assertEqual(len(deadlines), 1)
        self.assertTrue(all(c.kwargs["attempts"] == 1 for c in req.call_args_list))
        self.assertFalse(hasattr(plex_service, "_plex_request_with_wall_timeout"))


if __name__ == "__main__":
    unittest.main()
