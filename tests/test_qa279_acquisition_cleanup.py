"""QA #279: failed/cancelled SLSKD candidate cleanup through the real
start_album_download job (faked slskd HTTP, no network).

slskd layouts (slskd source, Options.cs / DownloadService.cs):
- completed files: <downloads>/${SOURCE_DIRECTORY}/<file>  (default pattern,
  every release 0.21 - 0.26 and master; no username folder)
- 0.26+ incomplete: <incomplete>/<username>/<full remote dir>/<file>
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import backend.acquisition_service as acq
import backend.app_runtime as rt
import backend.slskd_service as slskd
from backend.slskd import QueuedRemote

REMOTES = ["Music\\Album\\01.flac", "Music\\Album\\02.flac"]


def _touch(p: Path, data: bytes = b"x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


class _CancelOnWait(threading.Event):
    def wait(self, timeout=None):
        self.set()
        return True


class FailedCandidateCleanupE2E(unittest.TestCase):
    def _run(self, dl, wait_effect, event=None):
        # Like the real _slskd_search_and_queue: queued size and queue time per file (#277).
        queued = [QueuedRemote(r, 1, time.time() - 60) for r in REMOTES]
        searches = [("peer", queued,str(dl / "peer" / "Music" / "Album"), "Music\\Album")]

        def search(*_a, **_k):
            if searches:
                return searches.pop(0)
            raise RuntimeError("no more candidates")

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
        if wait_effect is not None:
            patches.append(mock.patch.object(acq, "_slskd_wait_downloads", side_effect=wait_effect))
        started = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        body, _ = acq.start_album_download({
            "artist": "A", "album": "Album", "method": "slskd",
            "auto_import": False, "try_source_fallback": True,
        })
        self.assertTrue(body["ok"], body)
        job_fn = started[3].start_python.call_args[0][0]
        log = []
        try:
            job_fn(log, event or threading.Event())
        except Exception:
            pass
        return log

    def test_peer_folder_layout_is_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp).resolve()
            own = [_touch(dl / "peer" / "Music" / "Album" / n) for n in ("01.flac", "02.flac")]
            other = _touch(dl / "Album" / "01.flac", b"another download")
            log = self._run(dl, RuntimeError("stalled"))
            for p in own:
                self.assertFalse(p.exists(), (p, log))
            self.assertTrue(other.exists(), log)

    def test_slskd_default_layout_own_files_are_cleaned(self):
        """slskd's default destination ${SOURCE_DIRECTORY}: <dl>/Album/<file>."""
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp).resolve()
            own = [_touch(dl / "Album" / n) for n in ("01.flac", "02.flac")]
            log = self._run(dl, RuntimeError("stalled"))
            for p in own:
                self.assertFalse(p.exists(), (p, [l for l in log if "slskd" in str(l)]))

    def test_cancel_still_cleans_peer_folder(self):
        """Cancel during the transfer wait (real _slskd_wait_downloads where it
        accepts cancel_event, i.e. with #271; else a wait that raises)."""
        import inspect
        with tempfile.TemporaryDirectory() as tmp:
            dl = Path(tmp).resolve()
            own = _touch(dl / "peer" / "Music" / "Album" / "01.flac")
            other = _touch(dl / "Album" / "02.flac", b"another download")
            has_cancel = "cancel_event" in inspect.signature(slskd._slskd_wait_downloads).parameters
            event = _CancelOnWait() if has_cancel else threading.Event()

            def wait(*_a, **_k):
                event.set()
                raise RuntimeError("cancelled")

            with mock.patch.object(acq, "_slskd_cleanup_failed_candidate_files",
                                   wraps=acq._slskd_cleanup_failed_candidate_files) as spy:
                log = self._run(dl, None if has_cancel else wait, event)
            spy.assert_called_once()
            self.assertFalse(own.exists(), log)
            self.assertTrue(other.exists(), log)


if __name__ == "__main__":
    unittest.main()
