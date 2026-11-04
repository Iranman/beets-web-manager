"""QA re-verification of #544: an incomplete audio inspection must stay
retryable across every boundary.

- B1: the second audio check runs inside the child reimport job; its result
  must reach the acquisition loops as a structured reason (not log text), so
  they keep the staging folder instead of discarding the candidate.
- N1: ffprobe failing because of the host (I/O, permissions, memory) is not a
  verdict on the audio; a real decode error still is.
- N2: a batch with both rejected and uninspected files is retryable, not a
  terminal policy rejection that claims the files were deleted.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests._module_time import module_time

acq = ai = isvc = jobsvc = ap = batch_state = None
_MSG = ("import job failed: ERROR: Audio inspection did not complete for 1 file(s) (ffprobe could not "
        "finish, often because storage or the host was busy). They were kept in place; retry the import later.")


def setUpModule():
    global acq, ai, isvc, jobsvc, ap, batch_state
    import app  # noqa: F401
    import backend.acquisition_service as acq
    import backend.ai_batch_state_service as batch_state
    import backend.ai_service as ai
    import backend.audio_preferences as ap
    import backend.import_service as isvc
    import backend.job_service as jobsvc


def _code():
    return getattr(ap, "AUDIO_INSPECTION_INCOMPLETE", "audio_inspection_incomplete")


class ChildJobBoundary(unittest.TestCase):
    """B1, adopted from QA's probe test_qa544_child_job_boundary."""

    def _slskd_run(self, child_error):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        stage = root / "stage"
        stage.mkdir()
        (stage / "01.flac").write_bytes(b"x")
        m = dict(
            jobs=mock.Mock(),
            validated_downloads_root=mock.Mock(return_value=root),
            slskd_configured=mock.Mock(return_value=True),
            _slskd_fallback_methods=mock.Mock(return_value=["soundcloud"]),
            _direct_source_unavailable_reason=mock.Mock(return_value=""),
            _ytdlp_album_download=mock.Mock(),
            _resolve_album_release_for_import=mock.Mock(return_value="11111111-2222-3333-4444-555555555555"),
            _fetch_mb_release_tracklist=mock.Mock(return_value={"ok": False}),
            _slskd_search_and_queue=mock.Mock(
                return_value=("peer1", ["Music\\p\\01.flac"], str(root / "p"), "Music\\p")),
            _slskd_wait_downloads=mock.Mock(return_value=("", [])),
            _find_slskd_downloaded_files=mock.Mock(return_value=(str(root), [root / "01.flac"])),
            _stage_selected_audio_files=mock.Mock(return_value=str(stage)),
            _delete_staged_import_folder=mock.Mock(),
            _slskd_cancel_queued_downloads=mock.Mock(),
            _slskd_cleanup_failed_candidate_files=mock.Mock(),
            _validate_import_source_audio=mock.Mock(return_value={}),  # first check completes
            _start_reimport_disk_job_internal=mock.Mock(return_value="child-1"),
            _wait_for_child_job=mock.Mock(side_effect=child_error),  # the child's check did not
        )
        with mock.patch.multiple(acq, **m), module_time(acq, sleep=mock.Mock()):
            body, _ = acq.start_album_download({"artist": "A", "album": "B", "method": "slskd",
                                                "auto_import": True, "try_source_fallback": True})
            self.assertTrue(body["ok"], body)
            try:
                m["jobs"].start_python.call_args[0][0]([])
            except Exception as ex:  # noqa: BLE001 -- inspected below
                return m, ex
        self.fail("the job must not succeed")

    def test_child_job_incomplete_inspection_does_not_discard(self):
        failed_cls = getattr(jobsvc, "ChildJobFailed", None)
        child_error = failed_cls(_MSG, _code()) if failed_cls else RuntimeError(_MSG)
        m, ex = self._slskd_run(child_error)
        m["_delete_staged_import_folder"].assert_not_called()
        m["_slskd_cleanup_failed_candidate_files"].assert_not_called()
        m["_ytdlp_album_download"].assert_not_called()
        self.assertEqual(m["_slskd_search_and_queue"].call_count, 1)
        self.assertIsInstance(ex, ap.AudioInspectionIncomplete)

    def test_message_text_alone_is_not_trusted(self):
        # A plain child failure that merely says the words is still discarded:
        # only the structured reason code keeps the folder.
        m, _ex = self._slskd_run(RuntimeError(_MSG))
        self.assertGreaterEqual(m["_delete_staged_import_folder"].call_count, 1)

    def test_wait_for_child_job_carries_the_childs_reason_code(self):
        child = SimpleNamespace(status="failed", log=["ERROR: Audio inspection did not complete"],
                                state={}, metadata={},
                                result={"ok": False, "reason_code": _code()})
        with mock.patch.object(jobsvc, "jobs", SimpleNamespace(get=lambda _jid: child)):
            with self.assertRaises(RuntimeError) as caught:
                jobsvc._wait_for_child_job("child-1", [], prefix="import")
        self.assertEqual(getattr(caught.exception, "reason_code", None), _code())
        self.assertIn("Audio inspection did not complete", str(caught.exception))

    def test_reimport_child_returns_a_structured_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = mock.Mock()
            jobs.start_python.return_value = SimpleNamespace(job_id="child-1")
            with mock.patch.multiple(
                    isvc,
                    jobs=jobs,
                    _reimport_inspect_source=mock.Mock(return_value=({"ok": True, "path": tmp,
                                                                      "audio_file_count": 1}, None)),
                    _library_album_ids_for_folder=mock.Mock(return_value=[]),
                    _resolve_album_release_for_import=mock.Mock(return_value="rel-1"),
                    _reimport_log_template_tokens=mock.Mock(),
                    _reimport_prepare_existing_rows=mock.Mock(),
                    _validate_import_source_audio=mock.Mock(
                        side_effect=ap.AudioInspectionIncomplete("Audio inspection did not complete; retry"))):
                body, status = isvc.start_reimport_disk({"aldir": tmp, "mb_albumid": "rel-1",
                                                         "skip_import_lock": True})
                self.assertEqual(status, 200, body)
                job_fn = jobs.start_python.call_args[0][0]
                result = job_fn([], None)
        self.assertIsInstance(result, dict)
        self.assertIs(result.get("ok"), False)  # job_engine records it as a failed job
        self.assertEqual(result.get("reason_code"), _code())


class _SettlingChild:
    """A mutating child still inspecting audio when the parent gives up; once
    asked to stop, its check ends with the structured failure."""

    def __init__(self):
        self.status = "running"
        self.log = ["working"]
        self.state = {}
        self.metadata = {}
        self.result = None

    def kill(self):
        self.status = "failed"
        self.log.append("ERROR: Audio inspection did not complete for 1 file(s)")
        self.result = {"ok": False, "reason_code": _code()}


class GiveUpCarriesTheReason(unittest.TestCase):
    """C1, adopted from QA's probe test_qa544_giveup: the parent's wall timeout
    (or a cancel) settles the child; its reason code must survive."""

    def _wait(self, **kw):
        child = _SettlingChild()
        with mock.patch.object(jobsvc, "jobs", mock.Mock(get=lambda _j: child)), \
                mock.patch.object(jobsvc, "is_read_only_job", return_value=False):
            with self.assertRaises(RuntimeError) as caught:
                jobsvc._wait_for_child_job("c1", [], prefix="import", **kw)
        return caught.exception

    def test_timeout_settles_child_with_reason_code(self):
        ex = self._wait(timeout=1)
        self.assertEqual(getattr(ex, "reason_code", None), _code())
        self.assertIn("timed out", str(ex))

    def test_cancel_settles_child_with_reason_code(self):
        cancel = mock.Mock(is_set=mock.Mock(return_value=True))
        ex = self._wait(cancel_event=cancel)
        self.assertEqual(getattr(ex, "reason_code", None), _code())
        self.assertEqual(str(ex), "cancelled")

    def test_real_job_engine_timeout_end_to_end(self):
        import job_engine

        def child_fn(log, cancel_event):
            log.append("inspecting")
            cancel_event.wait(10)  # still inspecting when the parent gives up
            return {"ok": False, "error": "Audio inspection did not complete", "reason_code": _code()}

        store = job_engine.JobStore()
        child = store.start_python(child_fn, label="child", metadata={})
        with mock.patch.object(jobsvc, "jobs", store), \
                mock.patch.object(jobsvc, "is_read_only_job", return_value=False):
            with self.assertRaises(jobsvc.ChildJobFailed) as caught:
                jobsvc._wait_for_child_job(child.job_id, [], prefix="import", timeout=1)
        self.assertEqual(caught.exception.reason_code, _code())

def _proc(rc, out="", err=""):
    return subprocess.CompletedProcess(["ffprobe"], rc, out, err)


class EnvironmentFailureIsIncomplete(unittest.TestCase):
    """N1."""

    def _validate(self, stderr):
        with mock.patch("backend.audio_preferences.subprocess.run", return_value=_proc(1, err=stderr)):
            return ap.validate_audio_file("/downloads/a.flac", ap.DEFAULT_MUSIC_FORMAT_PREFERENCES)

    def test_host_errors_are_incomplete(self):
        for stderr in ("file:/downloads/a.flac: Input/output error",
                       "file:/downloads/a.flac: Permission denied",
                       "file:/downloads/a.flac: Cannot allocate memory\n",
                       "file:/downloads/a.flac: Resource temporarily unavailable"):
            with self.subTest(stderr=stderr):
                result = self._validate(stderr)
                self.assertFalse(result["ok"])
                self.assertTrue(result["inspection_incomplete"])

    def test_more_host_errors_are_incomplete(self):  # N5
        for stderr in ("file:/mnt/nas/a.flac: Connection timed out",
                       "file:/mnt/nas/a.flac: No such device",
                       "file:/mnt/nas/a.flac: No such device or address",
                       "file:/mnt/nas/a.flac: Host is down"):
            with self.subTest(stderr=stderr):
                self.assertTrue(self._validate(stderr)["inspection_incomplete"])

    def test_broken_ffprobe_binary_is_incomplete(self):  # N5
        cases = ((127, "ffprobe: not found"), (126, "ffprobe: Permission denied"),
                 (1, "ffprobe: error while loading shared libraries: libavformat.so.61: cannot open shared "
                     "object file: No such file or directory"))
        for rc, stderr in cases:
            with self.subTest(rc=rc), \
                    mock.patch("backend.audio_preferences.subprocess.run", return_value=_proc(rc, err=stderr)):
                result = ap.validate_audio_file("/downloads/a.flac", ap.DEFAULT_MUSIC_FORMAT_PREFERENCES)
                self.assertTrue(result["inspection_incomplete"])

    def test_media_errors_are_still_rejected(self):
        for stderr in ("file:/downloads/a.flac: Invalid data found when processing input",
                       # a file name carrying the words does not turn a decode error into a deferral
                       "file:/downloads/Permission denied.flac: Invalid data found when processing input",
                       # N6: nor does a file name with a newline and the words
                       "file:/downloads/x\n: Permission denied\n.flac: Invalid data found when processing input"):
            with self.subTest(stderr=stderr):
                result = self._validate(stderr)
                self.assertFalse(result["ok"])
                self.assertFalse(result["inspection_incomplete"])
                self.assertTrue(result["message"].startswith("Rejected download:"))


class MixedBatchIsRetryable(unittest.TestCase):
    """N2: rejected + uninspected files in one batch."""

    @staticmethod
    def _fake(cmd, **kw):
        if str(cmd[-1]).endswith("hung.flac"):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return _proc(1, err="Invalid data found when processing input")

    def test_folder_import_mixed_batch_is_retryable_not_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "bad.flac").write_bytes(b"x")
            (Path(tmp) / "hung.flac").write_bytes(b"x")
            with mock.patch("backend.audio_preferences.subprocess.run", side_effect=self._fake), \
                    mock.patch.object(ai, "_music_format_preferences",
                                      return_value=ap.DEFAULT_MUSIC_FORMAT_PREFERENCES), \
                    mock.patch.object(ai, "_handle_rejected_audio_download",
                                      return_value={"handling": "quarantine", "removed": True}) as handle:
                with self.assertRaises(RuntimeError) as caught:
                    ai._validate_import_source_audio(tmp, [])
            self.assertTrue((Path(tmp) / "hung.flac").exists())
        ex = caught.exception
        self.assertEqual([Path(c.args[0]).name for c in handle.call_args_list], ["bad.flac"])
        self.assertIsInstance(ex, ap.AudioInspectionIncomplete)
        self.assertFalse(batch_state._is_music_format_policy_handled_error(str(ex)))
        self.assertIn("kept in place", str(ex))
        outcome = ai._ai_batch_import_failure_outcome(ex)
        self.assertEqual(outcome["status"], "import_failed")  # retryable, not terminal
        self.assertIn("import_failed", batch_state._AI_BATCH_RETRYABLE_FOLDER_STATUSES)
        self.assertEqual(batch_state._ai_batch_effective_folder_status(
            {"status": "failed", "failure_reason": str(ex)}), "failed")

    def test_selected_files_mixed_batch_is_retryable(self):
        files = [Path("/downloads/x/bad.flac"), Path("/downloads/x/hung.flac")]
        with mock.patch("backend.audio_preferences.subprocess.run", side_effect=self._fake), \
                mock.patch.object(isvc, "_music_format_preferences",
                                  return_value=ap.DEFAULT_MUSIC_FORMAT_PREFERENCES), \
                mock.patch.object(isvc, "_handle_rejected_audio_download",
                                  return_value={"handling": "quarantine", "removed": True}):
            with self.assertRaises(RuntimeError) as caught:
                isvc._filter_import_review_selected_audio_files(files, [])
        self.assertIsInstance(caught.exception, ap.AudioInspectionIncomplete)
        self.assertFalse(batch_state._is_music_format_policy_handled_error(str(caught.exception)))


if __name__ == "__main__":
    unittest.main()
