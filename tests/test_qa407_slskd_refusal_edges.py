"""QA #407 edge tests: every refusal code's peer-loop behaviour, and slskd
transfer cancellation against slskd's real GET downloads/{username} shape
(one UserResponse grouped by directory; DELETE takes the transfer Guid)."""

import unittest
from unittest import mock

import backend.matching_service as ms
import backend.slskd_service as slskd
import tests.test_slskd_noncandidate_refusal as base  # module import: its tests are not re-collected here


class EveryRefusalCodeTests(unittest.TestCase):
    _run = base.NonCandidateRefusalStopsPeerLoop._run

    def test_every_false_code_stops_and_every_true_code_retries(self):
        for code, candidate_specific in ms.IMPORT_REFUSAL_REASONS.items():
            with self.subTest(code=code):
                m, log = self._run(f" [reason_code={code}]")
                stopped = [line for line in log if "Not trying other Soulseek peers" in line]
                if candidate_specific:
                    self.assertEqual(m["_slskd_search_and_queue"].call_count, 12)
                    self.assertFalse(stopped)
                else:
                    self.assertEqual(m["_slskd_search_and_queue"].call_count, 1)
                    self.assertEqual(len(stopped), 1)
                # Cleanup runs for every failed candidate; the fallback always runs.
                n = m["_slskd_search_and_queue"].call_count
                self.assertEqual(m["_slskd_cancel_queued_downloads"].call_count, n)
                self.assertEqual(m["_slskd_cleanup_failed_candidate_files"].call_count, n)
                self.assertEqual(m["_delete_staged_import_folder"].call_count, n)
                m["_ytdlp_album_download"].assert_called_once()

    def test_unknown_code_retries(self):
        m, _log = self._run(" [reason_code=made_up_code]")
        self.assertEqual(m["_slskd_search_and_queue"].call_count, 12)


class CancelAgainstRealShapeTests(unittest.TestCase):
    NAME = "Music\\Björk – Homogenic\\01 Hunter.flac"
    OTHER = "Music\\Björk – Homogenic\\02 Jóga.flac"

    def _cancel(self, listing, files, delete_error=None):
        calls = []

        def req(method, path, *_a):
            calls.append((method, path))
            if method == "GET":
                return listing
            if delete_error:
                raise delete_error
            return {}

        log = []
        with mock.patch.object(slskd, "_slskd_req", side_effect=req):
            slskd._slskd_cancel_queued_downloads("peer one", files, log)
        return calls, log

    def user_response(self, *files):
        return {"username": "peer one", "directories": [{
            "directory": "Music\\Björk – Homogenic", "fileCount": len(files),
            "files": [{"id": i, "username": "peer one", "filename": f, "state": s} for i, f, s in files],
        }]}

    def test_unicode_backslash_name_and_quoted_user(self):
        calls, log = self._cancel(
            self.user_response(("1b4e28ba-2fa1-11d2-883f-0016d3cca427", self.NAME, "Queued, Remotely")),
            [slskd._SlskdQueuedRemote(self.NAME, 100, 1.0)])
        self.assertEqual(calls, [
            ("GET", "transfers/downloads/peer%20one"),
            ("DELETE", "transfers/downloads/peer%20one/1b4e28ba-2fa1-11d2-883f-0016d3cca427?remove=true"),
        ])
        self.assertIn("Cancelled 1", log[0])

    def test_every_record_of_a_filename_is_cancelled(self):
        # A stale record from an earlier attempt and the live one share a name.
        calls, _log = self._cancel(self.user_response(
            ("old-id", self.NAME, "Completed, Cancelled"),
            ("new-id", self.NAME, "Queued, Remotely"),
            ("other-id", self.OTHER, "Completed, Succeeded"),
        ), [self.NAME])
        deleted = sorted(p.rsplit("/", 1)[1] for m, p in calls if m == "DELETE")
        self.assertEqual(deleted, ["new-id?remove=true", "old-id?remove=true"])

    def test_list_response_ignores_other_users(self):
        listing = [{"username": "someone else", "directories": [{"files": [{"id": "x", "filename": self.NAME}]}]},
                   self.user_response(("mine", self.NAME, "Queued, Remotely"))]
        calls, _log = self._cancel(listing, [self.NAME])
        self.assertEqual([p for m, p in calls if m == "DELETE"],
                         ["transfers/downloads/peer%20one/mine?remove=true"])

    def test_delete_failure_is_counted_and_warned(self):
        _calls, log = self._cancel(self.user_response(("a", self.NAME, "Queued, Remotely")),
                                   [self.NAME], delete_error=RuntimeError("HTTP 500"))
        self.assertEqual(log, ["  [slskd] WARN: could not cancel 1 queued transfer(s); continuing cleanup."])

    def test_no_transfers_404_is_a_quiet_no_op(self):
        log = []
        err = RuntimeError("slskd GET /transfers/downloads/peer%20one → HTTP 404")
        with mock.patch.object(slskd, "_slskd_req", side_effect=err) as req:
            slskd._slskd_cancel_queued_downloads("peer one", [self.NAME], log)
        self.assertEqual(req.call_count, 1)
        self.assertEqual(log, [])

    def test_malformed_listing_cancels_nothing_quietly(self):
        for listing in (None, "", {"directories": None}, {"directories": [{"files": [None, "x"]}]}):
            with self.subTest(listing=listing):
                calls, log = self._cancel(listing, [self.NAME])
                self.assertEqual([c for c in calls if c[0] == "DELETE"], [])
                self.assertEqual(log, [])


if __name__ == "__main__":
    unittest.main()
