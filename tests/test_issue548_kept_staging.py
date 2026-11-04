"""#548 follow-ups from #544.

1. A staging folder acquisition keeps for a retry survives the orphan sweep
   (retention marker), and expires after KEPT_STAGING_RETENTION_S.
2. A recovery retry refuses to restage while the earlier attempt's import job
   (recorded on the carrier) still runs.
3. A child job record that vanishes is an unknown outcome, not a plain error.
4. Only the dynamic loader's own line defers an ffprobe failure.
"""
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests._module_time import module_time

acq = ap = jobsvc = rc = recovery_service = job_engine = None
RGID = "37cb4a72-3333-4444-5555-666666666666"
PREFIX = "rec-0123456789abcdef"


def setUpModule():
    global acq, ap, jobsvc, rc, recovery_service, job_engine
    import app  # noqa: F401
    import backend.acquisition_service as acq
    import backend.audio_preferences as ap
    import backend.job_service as jobsvc
    import backend.recovery_carrier as rc
    import backend.recovery_service as recovery_service
    import job_engine


_needs_fd = unittest.skipUnless(
    hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd and os.rename in os.supports_dir_fd,
    "the marker write needs fd-relative ops (Linux CI runs these)")


class _Env(unittest.TestCase):
    """A temp downloads root with its staging dir and carrier store."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = Path(self.tmp) / "_beets_missing_import"
        self.root.mkdir()
        patcher = mock.patch.object(recovery_service, "_staging_root",
                                    return_value=os.path.realpath(str(self.root)))
        patcher.start()
        self.addCleanup(patcher.stop)
        prev = os.environ.get("RECOVERY_CARRIER_STATE_DIR")
        os.environ["RECOVERY_CARRIER_STATE_DIR"] = os.path.join(self.tmp, "carriers")
        rc._store_registry.clear()

        def _restore():
            rc._store_registry.clear()
            if prev is None:
                os.environ.pop("RECOVERY_CARRIER_STATE_DIR", None)
            else:
                os.environ["RECOVERY_CARRIER_STATE_DIR"] = prev
        self.addCleanup(_restore)

    def _old_folder(self, name):
        d = self.root / name
        d.mkdir()
        (d / "01.flac").write_bytes(b"x")
        return d


class KeptFolderSurvivesSweep(_Env):
    def _sweep(self):
        removed = []

        def _delete(path):
            removed.append(path)
            shutil.rmtree(path)
            return {"deleted": True}
        with mock.patch.object(recovery_service, "_live_carrier_prefixes", return_value={}), \
                mock.patch("backend.composite_workflows.delete_file", side_effect=_delete):
            recovery_service.reconcile_orphan_staging_folders(log=[])
        return removed

    def _age(self, path, seconds):
        t = time.time() - seconds
        os.utime(str(path), (t, t))

    @_needs_fd
    def test_marked_folder_is_kept_until_the_marker_expires(self):
        d = self._old_folder("abc123-A - B")
        self.assertTrue(recovery_service.mark_staging_folder_kept(str(d)))
        self._age(d, recovery_service.ORPHAN_MIN_AGE_S + 3600)
        self.assertEqual(self._sweep(), [])
        self.assertTrue(d.exists())
        self._age(d / recovery_service.KEPT_STAGING_MARKER, recovery_service.KEPT_STAGING_RETENTION_S + 1)
        self.assertEqual(len(self._sweep()), 1)
        self.assertFalse(d.exists())

    def test_marker_only_written_inside_the_staging_root(self):
        outside = Path(self.tmp) / "elsewhere"
        outside.mkdir()
        self.assertFalse(recovery_service.mark_staging_folder_kept(str(outside)))
        self.assertFalse((outside / recovery_service.KEPT_STAGING_MARKER).exists())

    @_needs_fd
    def test_folder_kept_by_acquisition_survives_the_sweep(self):
        stage = self._old_folder("abc123-A - B")
        log, _ex = self._acquire(stage)
        self._age(stage, recovery_service.ORPHAN_MIN_AGE_S + 3600)
        self.assertEqual(self._sweep(), [])
        self.assertTrue((stage / "01.flac").is_file())
        self.assertTrue(any("removed after 7 days" in line for line in log), log)

    def test_unmarked_folder_says_copies_may_go(self):  # F4
        stage = self._old_folder("abc123-A - B")
        with mock.patch.object(recovery_service, "mark_staging_folder_kept", return_value=False):
            log, ex = self._acquire(stage)
        self.assertIsInstance(ex, acq.ImportStillRunning)
        self.assertIn("could not be marked for retention", str(ex))
        self.assertIn("original Soulseek downloads stay in place", str(ex))
        self.assertTrue(any("WARN could not mark" in line for line in log), log)

    def _acquire(self, stage):
        dl_root = Path(self.tmp)
        err = jobsvc.ChildJobFailed("import job timed out; job child-1 is still running",
                                    jobsvc.CHILD_OUTCOME_UNKNOWN)
        m = dict(
            jobs=mock.Mock(),
            validated_downloads_root=mock.Mock(return_value=dl_root),
            slskd_configured=mock.Mock(return_value=True),
            _slskd_fallback_methods=mock.Mock(return_value=[]),
            _resolve_album_release_for_import=mock.Mock(return_value="11111111-2222-3333-4444-555555555555"),
            _fetch_mb_release_tracklist=mock.Mock(return_value={"ok": False}),
            _slskd_search_and_queue=mock.Mock(
                return_value=("peer1", ["Music\\p\\01.flac"], str(dl_root / "p"), "Music\\p")),
            _slskd_wait_downloads=mock.Mock(return_value=("", [])),
            _find_slskd_downloaded_files=mock.Mock(return_value=(str(dl_root), [dl_root / "01.flac"])),
            _stage_selected_audio_files=mock.Mock(return_value=str(stage)),
            _delete_staged_import_folder=mock.Mock(),
            _validate_import_source_audio=mock.Mock(return_value={}),
            _start_reimport_disk_job_internal=mock.Mock(return_value="child-1"),
            _wait_for_child_job=mock.Mock(side_effect=err),
        )
        with mock.patch.multiple(acq, **m), module_time(acq, sleep=mock.Mock()):
            body, _ = acq.start_album_download({"artist": "A", "album": "B", "method": "slskd",
                                                "auto_import": True, "try_source_fallback": False})
            self.assertTrue(body["ok"], body)
            log = []
            with self.assertRaises(acq.ImportStillRunning) as caught:
                m["jobs"].start_python.call_args[0][0](log)
        return log, caught.exception


@_needs_fd
class MarkerWriteIsFdSafe(_Env):
    """F1: the marker write never opens a planted entry's target."""

    def setUp(self):
        super().setUp()
        self.folder = self._old_folder("abc123-A - B")
        self.marker = self.folder / recovery_service.KEPT_STAGING_MARKER
        self.outside = Path(self.tmp) / "library-file.flac"
        self.outside.write_bytes(b"precious audio bytes")

    def _outside_intact(self):
        self.assertEqual(self.outside.read_bytes(), b"precious audio bytes")

    def test_hardlinked_marker_target_is_untouched(self):
        os.link(self.outside, self.marker)
        self.assertTrue(recovery_service.mark_staging_folder_kept(str(self.folder)))
        self._outside_intact()
        self.assertEqual(os.stat(self.outside).st_nlink, 1)  # our link was replaced, not written through
        self.assertEqual(os.stat(self.marker, follow_symlinks=False).st_nlink, 1)

    def test_symlinked_marker_target_is_untouched(self):
        os.symlink(self.outside, self.marker)
        recovery_service.mark_staging_folder_kept(str(self.folder))
        self._outside_intact()
        self.assertFalse(self.marker.is_symlink())

    def test_symlinked_folder_is_refused(self):
        target = Path(self.tmp) / "elsewhere"
        target.mkdir()
        link = self.root / "rec-0123456789abcdef-X"
        os.symlink(target, link)
        self.assertFalse(recovery_service.mark_staging_folder_kept(str(link)))
        self.assertEqual(os.listdir(target), [])

    def test_planted_hardlink_marker_does_not_keep_the_folder(self):
        os.link(self.outside, self.marker)
        self.assertFalse(recovery_service._kept_for_retry(str(self.folder), time.time()))
        self._outside_intact()

    def test_no_temp_file_left_behind(self):
        self.assertTrue(recovery_service.mark_staging_folder_kept(str(self.folder)))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()),
                         sorted(["01.flac", recovery_service.KEPT_STAGING_MARKER]))


class _ReachedStaging(Exception):
    pass


class RetryWaitsForRunningImport(_Env):
    def setUp(self):
        super().setUp()
        self.store = job_engine.JobStore()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.cid = rc.create_carrier(existing_album_id=42, mb_releasegroupid=RGID,
                                     wanted_tracks=[{"track": 1}], state="downloaded")["carrier_id"]

    def _run(self):
        req = SimpleNamespace(recovery_carrier_id=self.cid, forced_albumartist="", existing_album_id=42,
                              replace_existing_item_ids=[], mb_albumid="rel-1", track_count=1,
                              wanted_tracks=[{"track": 1}], recovery_stage_prefix=PREFIX)
        return acq._AlbumDownloadRun(req, [], None)

    def test_running_import_on_the_prefix_blocks_without_a_carrier_note(self):  # F2/F3
        child = self.store.start_python(lambda log, cancel: self.release.wait(30) and {"ok": True},
                                        label="reimport", metadata={"aldir": f"/dl/x/{PREFIX}-A - B"})
        retry = self._run()  # prior_carrier is None: the carrier read failed / no note
        with mock.patch.object(acq, "jobs", self.store):
            with self.assertRaises(RuntimeError) as caught:
                retry._recovery_refuse_while_import_runs()
        self.assertIn(child.job_id, str(caught.exception))

    def test_job_listing_failure_fails_closed(self):  # F2
        broken = mock.Mock()
        broken.all.side_effect = OSError("boom")
        with mock.patch.object(acq, "jobs", broken):
            with self.assertRaises(RuntimeError) as caught:
                self._run()._recovery_refuse_while_import_runs()
        self.assertIn("retry the download later", str(caught.exception))

    def test_unrelated_running_job_does_not_block(self):
        self.store.start_python(lambda log, cancel: self.release.wait(30) and {"ok": True},
                                label="reimport", metadata={"aldir": "/dl/x/rec-ffffffffffffffff-C - D"})
        with mock.patch.object(acq, "jobs", self.store):
            self._run()._recovery_refuse_while_import_runs()

    def test_resume_read_is_kept_for_the_check(self):
        run = self._run()
        run.req.is_recovery, run.req.job_id, run.req.resume_carrier_id = True, "job-2", self.cid
        run.req.requested_rgid = RGID
        self.assertIsNone(run._recovery_resume_short_circuit())
        self.assertEqual(run.prior_carrier["carrier_id"], self.cid)

    def test_retry_refuses_while_the_first_attempts_import_runs_then_proceeds(self):
        # First attempt: the import job outlives the wait (ImportStillRunning).
        child = self.store.start_python(lambda log, cancel: self.release.wait(30) and {"ok": True},
                                        label="reimport", metadata={})
        unknown = jobsvc.ChildJobFailed("import job timed out", jobsvc.CHILD_OUTCOME_UNKNOWN)
        with mock.patch.object(acq, "_start_reimport_disk_job_internal", return_value=child.job_id), \
                mock.patch.object(acq, "_wait_for_child_job", side_effect=unknown):
            with self.assertRaises(acq.ImportStillRunning):
                self._run()._run_import_job("/stage/rec-0123456789abcdef-A - B", [], [])
        self.assertEqual(rc.get_carrier(self.cid)["state"], "downloaded")  # state untouched
        # Retry while that job still runs: it must not reach the download/restage.
        retry = self._run()

        def _short_circuit():  # the real one reads the carrier the same way, then runs on
            retry.prior_carrier = rc.get_carrier(self.cid)
        with mock.patch.object(acq, "jobs", self.store), \
                mock.patch.object(retry, "_recovery_resume_short_circuit", side_effect=_short_circuit), \
                mock.patch.object(retry, "_resolve_release", side_effect=_ReachedStaging):
            with self.assertRaises(RuntimeError) as caught:
                retry.execute()
            self.assertNotIsInstance(caught.exception, _ReachedStaging)
            self.assertIn(child.job_id, str(caught.exception))
            # Once the job has ended, the retry goes ahead.
            self.release.set()
            for _ in range(100):
                if child.status != "running":
                    break
                time.sleep(0.05)
            with self.assertRaises(_ReachedStaging):
                retry.execute()


class VanishedChildIsUnknown(unittest.TestCase):
    def _wait(self, **kw):
        store = job_engine.JobStore()
        with mock.patch.object(jobsvc, "jobs", store):
            with self.assertRaises(RuntimeError) as caught:
                jobsvc._wait_for_child_job("no-such-job", [], prefix="import", **kw)
        return caught.exception

    def test_wait_path(self):
        ex = self._wait()
        self.assertIsInstance(ex, jobsvc.ChildJobFailed)
        self.assertEqual(ex.reason_code, jobsvc.CHILD_OUTCOME_UNKNOWN)

    def test_cancel_path(self):
        cancel = threading.Event()
        cancel.set()
        ex = self._wait(cancel_event=cancel)
        self.assertIsInstance(ex, jobsvc.ChildJobFailed)
        self.assertEqual(ex.reason_code, jobsvc.CHILD_OUTCOME_UNKNOWN)
        self.assertEqual(str(ex), "cancelled")


class LoaderLineOnly(unittest.TestCase):
    def _validate(self, stderr):
        proc = subprocess.CompletedProcess(["ffprobe"], 1, "", stderr)
        with mock.patch("backend.audio_preferences.subprocess.run", return_value=proc):
            return ap.validate_audio_file("/downloads/a.flac", ap.DEFAULT_MUSIC_FORMAT_PREFERENCES)

    def test_file_name_with_the_phrase_is_rejected(self):
        for stderr in ("file:/downloads/error while loading shared libraries.flac: "
                       "Invalid data found when processing input",
                       "file:/downloads/x: error while loading shared libraries: y.flac: "
                       "Invalid data found when processing input",
                       # F5: a spoofed loader line after a newline in a file name
                       "file:/downloads/a\n/usr/bin/ffprobe: error while loading shared libraries: "
                       "libx.so: cannot open shared object file"):
            with self.subTest(stderr=stderr):
                result = self._validate(stderr)
                self.assertFalse(result["inspection_incomplete"])
                self.assertTrue(result["message"].startswith("Rejected download:"))

    def test_loader_line_is_incomplete(self):
        result = self._validate("/usr/bin/ffprobe: error while loading shared libraries: libavformat.so.61: "
                                "cannot open shared object file: No such file or directory")
        self.assertTrue(result["inspection_incomplete"])


if __name__ == "__main__":
    unittest.main()
