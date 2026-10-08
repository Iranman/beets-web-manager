"""D1/F2 (release-candidate QA): a provided Release or Release Group that cannot
be imported inside its own Release Group goes to review with the real reason,
and the release preflight reports why it really refused a release."""
import unittest
from unittest import mock

try:
    import test_import_paths_beets_native as base
except ImportError:  # pragma: no cover
    from tests import test_import_paths_beets_native as base

import backend.library_service as lsvc
import backend.matching_service as ms

REL, RG = base.REL, base.RG
RG_OTHER = "33333333-3333-3333-3333-333333333333"
SRC = "/downloads/batch/Plain LP"


class ImportWithIdReviewTests(unittest.TestCase):
    setUp = base.ConfirmedImportJobTests.setUp
    RETAG_AND_REMOVAL = base.ConfirmedImportJobTests.RETAG_AND_REMOVAL

    def _run(self, payload, new_album=None):
        isvc = self.isvc
        self.ad = base._adapter(new_album)
        with mock.patch.object(isvc, "_resolve_import_review_source_path", return_value=(SRC, None)), \
                mock.patch.object(isvc, "_preserve_torrent_source_path", return_value=False), \
                mock.patch.object(isvc, "_beet_import_timeout", return_value=60):
            body, code = isvc.start_folder_import_with_id({"path": SRC, **payload})
        self.assertEqual(code, 200, body)
        self.assertEqual(self.result["status"], "failed", self.result)
        return self.result

    def _queued(self):
        q = self.isvc._queue_folder_for_manual_review
        self.assertEqual(q.call_count, 1, "exactly one review item")
        folder, sug, reason = q.call_args.args[:3]
        self.assertEqual(folder, SRC)
        self.assertEqual(sug["reason"], reason)
        return sug, reason

    def test_not_imported_queues_review_with_the_provided_release(self):
        res = self._run({"mb_albumid": REL})
        sug, reason = self._queued()
        self.assertEqual(sug["provided_mb_id"], REL)
        self.assertEqual(sug["mb_albumid"], REL)
        self.assertEqual(sug["mb_releasegroupid"], RG)
        self.assertIn("could not confidently match", reason)
        self.assertIn("Nothing was imported", reason)
        self.assertIn(reason, res["error"])

    def test_refused_release_group_resolution_queues_review_with_the_real_reason(self):
        def refuse(*_a, **_k):
            _a[5].append("  Resolved release-group candidate rejected by identity not verified "
                         "(artist_conflict), 2/2 track(s) matched")
            _a[5].append(f"  REFUSED: no release in the requested release-group {RG} passed the "
                         "folder preflight; not searching other release groups. Manual Review is required.")
            return ""
        with mock.patch.object(self.isvc, "_resolve_album_release_for_import", side_effect=refuse):
            self._run({"mb_releasegroupid": RG})
        sug, reason = self._queued()
        self.assertEqual(sug["provided_mb_id"], RG)
        self.assertEqual(sug["mb_releasegroupid"], RG)
        self.assertEqual(sug["mb_albumid"], "")  # a Release Group ID is never offered as a Release
        self.assertIn("artist_conflict", reason)
        self.assertIn("REFUSED", reason)
        self.ad.run_import.assert_not_called()

    def test_release_outside_the_selected_release_group_queues_review(self):
        self._run({"mb_albumid": REL, "mb_releasegroupid": RG_OTHER})
        sug, reason = self._queued()
        self.assertIn(f"release group {RG}", reason)
        self.assertIn(RG_OTHER, reason)
        self.ad.run_import.assert_not_called()

    def test_queue_review_false_fails_without_a_review_item(self):
        self._run({"mb_albumid": REL, "queue_review": False})
        self.isvc._queue_folder_for_manual_review.assert_not_called()


def _pre(**kw):
    return {"ok": False, "matches": 2, "expected": 2, "error": "", **kw}


class PreflightRejectionReasonTests(unittest.TestCase):
    def test_tracklist_failure(self):
        self.assertEqual(ms._preflight_rejection_reason(_pre(matches=1, expected=5)),
                         "folder tracklist: 1/5 track(s) matched")

    def test_identity_conflict_is_not_reported_as_tracklist(self):
        reason = ms._preflight_rejection_reason(_pre(
            tracklist_ok=True, matching_decision={"conflicts": ["artist_conflict"], "warnings": []}))
        self.assertIn("artist_conflict", reason)
        self.assertNotIn("folder tracklist", reason)

    def _acoustid_reason(self, key):
        with mock.patch.object(ms, "acoustid_api_key", return_value=key):
            return ms._preflight_rejection_reason(_pre(
                tracklist_ok=True, matching_decision={
                    "conflicts": [], "warnings": ["acoustid_unavailable"], "reason_code": "review_required"}))

    def test_acoustid_unavailable_says_how_to_resolve(self):
        reason = self._acoustid_reason("")
        self.assertIn("acoustid_unavailable", reason)
        self.assertIn("set ACOUSTID_API_KEY", reason)

    def test_acoustid_unavailable_with_a_key_does_not_blame_the_key(self):
        reason = self._acoustid_reason("synthetic-test-key")
        self.assertIn("acoustid_unavailable", reason)
        self.assertNotIn("ACOUSTID_API_KEY", reason)

    def test_review_reason_does_not_claim_a_tracklist_mismatch(self):
        reason = ms._preflight_review_reason(_pre(
            tracklist_ok=True, matching_decision={"conflicts": ["artist_conflict"]}), "default")
        self.assertNotIn("only 2/2", reason)
        self.assertIn("artist_conflict", reason)

    def test_resolver_logs_the_real_rejection_reason(self):
        pre = _pre(tracklist_ok=True, matching_decision={
            "conflicts": [], "warnings": ["acoustid_unavailable"], "reason_code": "review_required"})
        log = []
        with mock.patch.object(lsvc, "_folder_release_preflight", return_value=pre), \
                mock.patch.object(lsvc, "_folder_import_track_count", return_value=2), \
                mock.patch.object(lsvc, "_resolve_mb_release_id", return_value=REL), \
                mock.patch.object(lsvc, "_mb_release_has_tracks", return_value=True), \
                mock.patch.object(lsvc, "_fetch_mb_release_tracklist", return_value={"release_group": RG}), \
                mock.patch.object(lsvc, "_mb_release_group_candidates", return_value=[]), \
                mock.patch.object(lsvc, "_mb_release_search", side_effect=AssertionError("free search")):
            got = lsvc._resolve_album_release_for_import(REL, "batch", "Plain LP", "", 2, log,
                                                         source_folder=SRC)
        self.assertEqual(got, "")  # fails closed to review; no cross-RG search
        text = "\n".join(log)
        self.assertIn("acoustid_unavailable", text)
        self.assertNotIn("rejected by folder tracklist", text)


if __name__ == "__main__":
    unittest.main()
