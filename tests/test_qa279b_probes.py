"""QA #279 round 2: evidence through every acquisition cleanup path, mtime
granularity, clock skew, same-size foreign files, peer-name collisions."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import backend.acquisition_service as acq
import backend.app_runtime as rt
import backend.slskd_service as slskd
from backend.slskd import QueuedRemote, cleanup_failed_candidate_files

EXTS = [".flac"]
DATA = b"abcd"


def _touch(p, data=DATA, mtime=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


class _CancelOnWait(threading.Event):
    def wait(self, timeout=None):
        self.set()
        return True


class EvidenceThroughAcquisitionPaths(unittest.TestCase):
    """Own flat-layout files (slskd default) are removed on every path, and the
    cleanup receives QueuedRemote evidence."""

    def _run(self, dl, *, path):
        queued_at = time.time() - 60
        remotes = [QueuedRemote(f"Music\\Album\\0{i}.flac", len(DATA), queued_at) for i in (1, 2)]
        searches = [("peer", remotes, str(dl / "peer" / "Music" / "Album"), "Music\\Album")]

        def search(*_a, **_k):
            if searches:
                return searches.pop(0)
            raise RuntimeError("no more candidates")

        event = _CancelOnWait() if path == "cancel" else threading.Event()
        patches = [
            mock.patch.object(rt, "DOWNLOADS_ALLOWED_ROOTS", (dl,)),
            mock.patch.object(rt, "TORRENT_SOURCE_ROOTS", ()),
            mock.patch.object(slskd, "DOWNLOADS_ROOT", dl),
            mock.patch.object(acq, "jobs"),
            mock.patch.object(acq, "_slskd_search_and_queue", side_effect=search),
            mock.patch.object(slskd, "_slskd_req", return_value={}),
            mock.patch.object(slskd.time, "sleep"),
            mock.patch.object(acq.time, "sleep"),
            mock.patch.object(acq, "_slskd_fallback_methods", return_value=["ytdlp"]),
            mock.patch.object(acq, "_ytdlp_album_download", side_effect=RuntimeError("fell back")),
        ]
        if path == "stall":
            patches.append(mock.patch.object(acq, "_slskd_wait_downloads", side_effect=RuntimeError("stalled")))
        if path == "import":
            own = [dl / "Album" / "01.flac", dl / "Album" / "02.flac"]
            patches += [
                mock.patch.object(acq, "_slskd_wait_downloads", return_value=(str(dl / "Album"), [])),
                mock.patch.object(acq, "_find_slskd_downloaded_files",
                                  return_value=(str(dl / "Album"), [str(p) for p in own])),
                mock.patch.object(acq, "_stage_selected_audio_files", return_value=str(dl / "stage")),
                mock.patch.object(acq, "_delete_staged_import_folder"),
                mock.patch.object(acq, "_resolve_album_release_for_import",
                                  return_value="11111111-1111-1111-1111-111111111111"),
                mock.patch.object(acq, "_fetch_mb_release_tracklist", return_value={"ok": False}),
                mock.patch.object(acq, "_validate_import_source_audio",
                                  side_effect=RuntimeError("wrong album")),
            ]
        started = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        payload = {"artist": "A", "album": "Album", "method": "slskd",
                   "auto_import": path == "import", "try_source_fallback": True}
        if path == "import":
            payload["mb_albumid"] = "11111111-1111-1111-1111-111111111111"
        with mock.patch.object(acq, "_slskd_cleanup_failed_candidate_files",
                               wraps=acq._slskd_cleanup_failed_candidate_files) as spy:
            body, _ = acq.start_album_download(payload)
            self.assertTrue(body["ok"], body)
            log = []
            try:
                started[3].start_python.call_args[0][0](log, event)
            except Exception:
                pass
        self.assertTrue(spy.called, log)
        for call in spy.call_args_list:
            for r in call.args[1]:
                self.assertIsInstance(r, QueuedRemote)
                self.assertEqual(r.size, len(DATA))
        return log

    def _check(self, path):
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp).resolve()
            own = [_touch(dl / "Album" / n) for n in ("01.flac", "02.flac")]
            log = self._run(dl, path=path)
            for p in own:
                self.assertFalse(p.exists(), (p, [l for l in log if "slskd" in str(l)]))

    def test_stall_path_577(self):
        self._check("stall")

    def test_import_validation_path_648(self):
        self._check("import")

    def test_cancel_path(self):
        self._check("cancel")


class EvidenceEdgeCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dl = Path(self.tmp.name)
        self.q_at = time.time() - 60

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, remotes, user="peer"):
        log = []
        return cleanup_failed_candidate_files(self.dl, user, remotes, EXTS, log, [self.dl]), log

    def _q(self, name="Music\\Album\\01.flac"):
        return QueuedRemote(name, len(DATA), self.q_at)

    # --- 2 s mtime granularity (FAT/exFAT truncate down to even seconds) ---
    def test_fat_truncation_own_file_written_within_2s(self):
        # Download finished 1.5 s after queueing; FAT stores floor-to-even.
        q_at = 1_700_000_001.2  # odd second
        written = q_at + 1.5
        stored = int(written) - (int(written) % 2)  # 1_700_000_002 >= q_at -> ok
        own = _touch(self.dl / "Album" / "01.flac", mtime=stored)
        removed, _ = self._run([QueuedRemote("Music\\Album\\01.flac", len(DATA), q_at)])
        self.assertEqual(removed, 1, "own file within FAT granularity")

    def test_fat_truncation_own_file_written_within_same_2s_window(self):
        q_at = 1_700_000_000.9  # even second
        written = q_at + 0.8   # 1_700_000_001.7 -> stored 1_700_000_000
        stored = int(written) - (int(written) % 2)
        own = _touch(self.dl / "Album" / "01.flac", mtime=stored)
        removed, log = self._run([QueuedRemote("Music\\Album\\01.flac", len(DATA), q_at)])
        # Documents behaviour: fail-safe (left + logged), not a wrong delete.
        self.assertEqual(removed, 0)
        self.assertTrue(own.exists())
        self.assertIn("Left", "\n".join(log))

    # --- clock skew between app clock (queued_at) and file-server clock ---
    def test_server_clock_behind_leaves_own_file_logged(self):
        own = _touch(self.dl / "Album" / "01.flac", mtime=self.q_at - 120)  # own, written later, clock 3 min behind
        removed, log = self._run([self._q()])
        self.assertEqual(removed, 0)
        self.assertTrue(own.exists())
        self.assertIn("Left", "\n".join(log))

    def test_server_clock_ahead_passes_foreign_same_size_file(self):
        # Foreign file written 10 s BEFORE queue, server 30 s ahead -> mtime > queued_at.
        foreign = _touch(self.dl / "Album" / "01.flac", mtime=self.q_at + 20)
        removed, _ = self._run([self._q()])
        self.assertEqual(removed, 1)  # residual risk, documented
        self.assertFalse(foreign.exists())

    # --- same-name, same-size file of another download ---
    def test_same_size_older_redownload_survives(self):
        old = _touch(self.dl / "Album" / "01.flac", mtime=self.q_at - 86400)
        removed, log = self._run([self._q()])
        self.assertEqual(removed, 0)
        self.assertTrue(old.exists())
        self.assertIn("Left", "\n".join(log))

    def test_same_size_concurrent_foreign_download_is_deleted(self):
        foreign = _touch(self.dl / "Album" / "01.flac")  # written now (after queue)
        removed, _ = self._run([self._q()])
        self.assertEqual(removed, 1)  # residual risk, documented
        self.assertFalse(foreign.exists())

    # --- ungated peer-folder path collides with flat layout when the remote
    #     file has no folder and the peer's name equals another download's
    #     folder name ---
    def test_peer_named_like_folder_with_rootless_remote_deletes_foreign_file(self):
        foreign = _touch(self.dl / "CD1" / "01.flac", mtime=self.q_at - 86400)
        removed, _ = self._run([QueuedRemote("..\\01.flac", 999, self.q_at)], user="CD1")
        self.assertEqual(removed, 0, "foreign flat file deleted with no evidence")
        self.assertTrue(foreign.exists())

    def test_rootless_remote_plain(self):
        foreign = _touch(self.dl / "CD1" / "01.flac", mtime=self.q_at - 86400)
        removed, _ = self._run(["01.flac"], user="CD1")
        self.assertEqual(removed, 0, "foreign flat file deleted with no evidence")
        self.assertTrue(foreign.exists())


if __name__ == "__main__":
    unittest.main()
