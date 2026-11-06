"""#445: a child import that outlives the parent's wait and then settles with a
terminal refusal (existing_album_merge_failed) must still stop acquisition:
the wait error keeps the child's error line, so the refusal code survives."""

import unittest
from unittest import mock

import backend.acquisition_service as acq
import backend.job_service as jobsvc
from backend.matching_service import (EXISTING_ALBUM_MERGE_FAILED, EXISTING_ALBUM_MERGE_FAILED_MESSAGE,
                                      import_refusal_reason_code)
import tests.test_slskd_noncandidate_refusal as peer_loop


class _MergeFailsWhenStopped:
    """Still running at the wall timeout; settles as failed with the merge
    error once asked to stop, as job_engine records a raised import error."""

    def __init__(self):
        self.status = "running"
        self.log = ["importing"]
        self.state = {}
        self.metadata = {}
        self.result = None

    def kill(self):
        self.status = "failed"
        self.log.append(f"ERROR: {EXISTING_ALBUM_MERGE_FAILED_MESSAGE}: existing album_id 5, boom.")


def _timed_out_wait_error(child=None):
    child = child or _MergeFailsWhenStopped()
    with mock.patch.object(jobsvc, "jobs", mock.Mock(get=lambda _j: child)), \
            mock.patch.object(jobsvc, "is_read_only_job", return_value=False):
        try:
            jobsvc._wait_for_child_job("c1", [], prefix="import", timeout=1)
        except RuntimeError as ex:
            return ex
    raise AssertionError("_wait_for_child_job did not raise")


class TimeoutKeepsTheSettledRefusal(unittest.TestCase):
    def test_wait_error_carries_the_merge_refusal_code(self):
        ex = _timed_out_wait_error()
        self.assertEqual(import_refusal_reason_code(ex), EXISTING_ALBUM_MERGE_FAILED, str(ex))
        self.assertIn("timed out", str(ex))

    def test_settled_failure_without_error_line_keeps_the_plain_reason(self):
        child = _MergeFailsWhenStopped()
        child.kill = lambda: setattr(child, "status", "failed")
        ex = _timed_out_wait_error(child)
        self.assertEqual(str(ex), "import job timed out")

    def test_timed_out_merge_failure_stops_peers_and_fallback(self):
        ex = _timed_out_wait_error()
        m, _log = peer_loop.NonCandidateRefusalStopsPeerLoop._run(
            self, "", wait=mock.Mock(side_effect=ex), raises="SLSKD import stopped after one candidate")
        self.assertEqual(m["_slskd_search_and_queue"].call_count, 1)
        self.assertEqual(m["_start_reimport_disk_job_internal"].call_count, 1)
        m["_ytdlp_album_download"].assert_not_called()

    def test_idle_stall_keeps_the_merge_refusal_code(self):
        clock = _Clock()
        child = _MergeFailsWhenStopped()
        with mock.patch.object(jobsvc, "jobs", mock.Mock(get=lambda _j: child)), \
                mock.patch.object(jobsvc, "is_read_only_job", return_value=False), \
                mock.patch.object(jobsvc, "time", clock):
            with self.assertRaises(jobsvc.ChildJobFailed) as caught:
                jobsvc._wait_for_child_job("c1", [], prefix="import", timeout=0, idle_timeout=10)
        self.assertEqual(import_refusal_reason_code(caught.exception), EXISTING_ALBUM_MERGE_FAILED)
        self.assertIn("stalled with no progress", str(caught.exception))

    def test_child_that_ends_just_before_the_settle_keeps_its_error(self):
        """QA race probe: the child fails after the loop's last log read, so
        its ERROR line is first seen when the wait gives up."""
        child = _FinishesUnseen()
        clock = mock.Mock(time=mock.Mock(side_effect=[0] + [5] * 20), sleep=mock.Mock())
        with mock.patch.object(jobsvc, "jobs", mock.Mock(get=lambda _j: child)), \
                mock.patch.object(jobsvc, "is_read_only_job", return_value=False), \
                mock.patch.object(jobsvc, "time", clock):
            with self.assertRaises(RuntimeError) as caught:
                jobsvc._wait_for_child_job("c1", [], prefix="import", timeout=1)
        self.assertEqual(import_refusal_reason_code(caught.exception), EXISTING_ALBUM_MERGE_FAILED,
                         str(caught.exception))

    def test_timed_out_merge_failure_stops_the_direct_source_loop(self):
        waits = mock.Mock(side_effect=[
            RuntimeError("import job failed: ERROR: Could not resolve a MusicBrainz release "
                         "ID for import [reason_code=acoustid_unavailable]"),
            _timed_out_wait_error()])
        with mock.patch.object(acq, "_slskd_fallback_methods", return_value=["soundcloud", "youtube"]), \
                mock.patch.object(acq, "_ytdlp_album_download", return_value=1) as ytdlp:
            m, _log = peer_loop.NonCandidateRefusalStopsPeerLoop._run(
                self, "", raises="Direct-source import stopped", wait=waits,
                keep=("_slskd_fallback_methods", "_ytdlp_album_download"))
        self.assertEqual(ytdlp.call_count, 1)
        self.assertEqual(m["_start_reimport_disk_job_internal"].call_count, 2)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class _FinishesUnseen:
    """Reads as running through the loop's checks, then as failed with the
    merge error, appended only once _settle looks at it."""

    def __init__(self):
        self.log, self.state, self.metadata, self.result = ["importing"], {}, {}, None
        self.reads = 0

    @property
    def status(self):
        self.reads += 1
        if self.reads <= 2:
            return "running"
        if len(self.log) == 1:
            self.log.append(f"ERROR: {EXISTING_ALBUM_MERGE_FAILED_MESSAGE}: boom")
        return "failed"

    def kill(self):
        pass


if __name__ == "__main__":
    unittest.main()
