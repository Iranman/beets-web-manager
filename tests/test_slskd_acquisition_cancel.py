"""#251: cancelling an SLSKD acquisition or playlist job stops it during the
transfer wait or the completed-file search, without sitting out the timeout,
retrying another peer, or falling back to direct sources."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import backend.acquisition_service as acq
import backend.app_runtime as rt
import backend.slskd_service as slskd


class _CancelOnWait(threading.Event):
    """The user presses Cancel while the job waits."""

    def __init__(self):
        super().__init__()
        self.waits = 0

    def wait(self, timeout=None):
        self.waits += 1
        self.set()
        return True


class AcquisitionCancelDuringSlskd(unittest.TestCase):
    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        if len(self.sleeps) > 5:
            raise AssertionError("slept despite cancel_event")

    def _run(self, stage):
        self.sleeps = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        downloads = Path(tmp.name).resolve()
        event = _CancelOnWait()
        patches = [
            mock.patch.object(rt, "DOWNLOADS_ALLOWED_ROOTS", (downloads,)),
            mock.patch.object(rt, "TORRENT_SOURCE_ROOTS", ()),
            mock.patch.object(slskd, "DOWNLOADS_ROOT", downloads),
            mock.patch.object(acq, "jobs"),
            mock.patch.object(acq, "_slskd_search_and_queue",
                              return_value=("peer", ["Music\\Album\\01.flac"], str(downloads / "peer"), "Music\\Album")),
            mock.patch.object(slskd, "_slskd_req", return_value={}),
            # One shared time module: record every sleep; a busy loop fails fast.
            mock.patch.object(slskd.time, "sleep", side_effect=self._sleep),
            mock.patch.object(acq, "_slskd_cancel_queued_downloads"),
            mock.patch.object(acq, "_slskd_cleanup_failed_candidate_files"),
            # Any direct-source fallback fails fast instead of reaching the network.
            mock.patch.object(acq, "_slskd_fallback_methods", return_value=["ytdlp"]),
            mock.patch.object(acq, "_ytdlp_album_download", side_effect=RuntimeError("fell back")),
        ]
        if stage == "search":
            patches.append(mock.patch.object(acq, "_slskd_wait_downloads", return_value=("", [])))
        started = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        jobs = started[3]
        body, _ = acq.start_album_download({
            "artist": "A", "album": "B", "method": "slskd",
            "auto_import": False, "try_source_fallback": True,
        })
        self.assertTrue(body["ok"], body)
        job_fn = jobs.start_python.call_args[0][0]
        log = []
        with self.assertRaisesRegex(RuntimeError, "^cancelled$"):
            job_fn(log, event)
        self.assertFalse([line for line in log if "[fallback]" in str(line)], log)
        return event, started

    def test_cancel_during_transfer_wait(self):
        event, started = self._run("wait")
        self.assertEqual(event.waits, 1)
        started[4].assert_called_once()  # no second peer was searched
        started[7].assert_called_once()  # the queued transfer was cancelled
        self.assertEqual(self.sleeps, [])

    def test_cancel_during_file_search(self):
        event, started = self._run("search")
        self.assertEqual(event.waits, 1)
        started[4].assert_called_once()
        started[7].assert_called_once()
        self.assertEqual(self.sleeps, [3])  # only the acquisition's settle delay



class PlaylistCancelDuringSlskd(unittest.TestCase):
    def test_cancel_stops_the_round_without_other_sources_or_tracks(self):
        import backend.playlist_service as ps
        event = threading.Event()
        calls = []

        def slskd_track(artist, title, *_a, cancel_event=None, **_k):
            calls.append(title)
            self.assertIs(cancel_event, event)
            event.set()  # the user cancels during the SLSKD wait
            raise RuntimeError("cancelled")

        state = {"done": 0, "failed": 0, "log": [], "playlist_name": "P"}
        tracks = [{"artist": "A", "title": "One"}, {"artist": "A", "title": "Two"}]
        with mock.patch.object(ps, "_playlist_ensure_staging_dirs"),                 mock.patch.object(ps, "_playlist_key", return_value="p"),                 mock.patch.object(ps.composite_workflows, "list_playlist_staged_files",
                                  return_value={"ok": True, "files": []}),                 mock.patch.object(ps, "_playlist_reusable_download_files", return_value=[]),                 mock.patch.object(ps, "_playlist_set_track_status"),                 mock.patch.object(ps, "_playlist_review_required_count_from_state", return_value=0),                 mock.patch.object(ps, "_playlist_slskd_download_track", side_effect=slskd_track),                 mock.patch.object(ps, "_ytdlp_missing_tracks_download",
                                  side_effect=AssertionError("fell back after cancel")):
            result = ps._playlist_download_missing_tracks(
                tracks, Path(tempfile.gettempdir()), state, lambda _line: None,
                ["slskd", "ytdlp"], cancel_event=event)
        self.assertEqual(calls, ["One"])
        self.assertEqual(result["failed"], 0)


if __name__ == "__main__":
    unittest.main()
