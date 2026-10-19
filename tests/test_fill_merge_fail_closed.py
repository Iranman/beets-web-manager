"""A missing-track fill into an existing album fails closed (#429 QA N3).

When the reconcile/merge of the newly imported album into existing_album_id
does not complete, or the requested tracks are not read back on the existing
album, the job fails and the folder goes to Review with
[reason_code=existing_album_merge_failed]. The imported album row is never
removed here. Format replacement (N4) keeps allow_missing_tracks and the
AcoustID gate, and its Preview result is not mistaken for a failed fill.
"""
import contextlib
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as cw
import backend.import_service as isvc
from backend.matching_service import import_refusal_reason_code
from backend.transaction_engine import TransactionStore

REL = "11111111-1111-1111-1111-111111111111"
RG = "22222222-2222-2222-2222-222222222222"
EXISTING, IMPORTED = 5, 9
WANTED = [{"disc": 1, "track": 2, "title": "Two", "mb_trackid": "rec-2"}]
CODE = "[reason_code=existing_album_merge_failed]"


class _JobHarness(unittest.TestCase):
    def setUp(self):
        self.result = {}
        self.planned = {}
        self.reviews = []
        # Items Beets imported (album 9) and the existing album's items (album 5).
        self.items = {IMPORTED: [{"id": 92, "disc": 1, "track": 2, "mb_trackid": "rec-2"}],
                      EXISTING: [{"id": 51, "disc": 1, "track": 1, "mb_trackid": "rec-1"}]}

        def run_now(fn, label="", metadata=None):
            log = []
            try:
                self.result = {"status": "completed", "result": fn(log), "log": log}
            except Exception as ex:
                self.result = {"status": "failed", "error": str(ex), "log": log}
            return types.SimpleNamespace(job_id="job-1")

        def plan(payload, *a, **k):
            self.planned.update(payload)
            return {"ok": True, "operation_id": "op-1"}

        def queue(folder, suggestion, reason, log, **kw):
            self.reviews.append(reason)
            return True

        cw = isvc.composite_workflows
        album = {"id": EXISTING, "mb_albumid": REL, "mb_releasegroupid": RG}
        self.acoustid = mock.MagicMock(return_value={"ok": True})
        for target, name, kw in (
            (isvc.jobs, "start_python", {"side_effect": run_now}),
            (isvc.job_contract, "held", {"side_effect": lambda *a, **k: contextlib.nullcontext()}),
            (cw, "inspect_import_source", {"return_value": {
                "ok": True, "path": str(isvc.DOWNLOADS_ROOT) + "/Artist/Album", "audio_file_count": 1}}),
            (cw, "get_album", {"side_effect": lambda aid: dict(album, id=aid)}),
            (cw, "find_all_items_by_album_id", {"side_effect": lambda aid: list(self.items.get(int(aid), []))}),
            (cw, "plan_confirmed_import", {"side_effect": plan}),
            (cw, "apply_confirmed_import", {"return_value": {"ok": True, "album_id": IMPORTED}}),
            (cw, "fetch_and_embed_album_art", {"return_value": {"ok": True}}),
            (isvc, "_resolve_album_release_for_import", {"return_value": REL}),
            (isvc, "_folder_release_preflight", {"return_value": {"ok": True, "matches": 1, "expected": 3}}),
            (isvc, "_source_audio_missing_track_scan", {"return_value": {"ok": False}}),
            (isvc, "_prune_stale_wanted_rows_before_import", {}),
            (isvc, "_validate_import_source_audio", {}),
            (isvc, "_preserve_torrent_source_path", {"return_value": False}),
            (isvc, "_fetch_mb_release_tracklist", {"return_value": {"ok": True, "release_group": RG, "tracks": []}}),
            (isvc, "_validate_wanted_album_items_with_acoustid", {"new": self.acoustid}),
            (isvc, "_queue_folder_for_manual_review", {"side_effect": queue}),
            (isvc, "_record_recent_import_for_album", {}),
            (isvc, "_remove_pending_review_for_path", {}),
            (isvc, "_invalidate_lib_cache", {}),
            (isvc, "_trigger_plex_refresh", {}),
        ):
            patcher = mock.patch.object(target, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, merge=None, **payload):
        body = {"aldir": str(isvc.DOWNLOADS_ROOT) + "/Artist/Album", "mb_albumid": REL,
                "existing_album_id": EXISTING, "wanted_tracks": WANTED, "skip_import_lock": True, **payload}
        with (mock.patch.object(isvc, "_merge_imported_album_into_existing", **merge)
              if merge is not None else contextlib.nullcontext()):
            resp, code = isvc.start_reimport_disk(body)
        self.assertEqual(code, 200, resp)
        return self.result

    def _move_items(self, *a, **k):
        self.items[EXISTING] += self.items.pop(IMPORTED)
        return EXISTING

    def _assert_failed_to_review(self, res):
        self.assertEqual(res["status"], "failed", res)
        self.assertIn(CODE, res["error"])
        self.assertEqual(len(self.reviews), 1)
        self.assertIn(CODE, self.reviews[0])
        self.assertIn(f"Album_id {IMPORTED} was left in the library", self.reviews[0])
        self.assertTrue(any(f"imported album_id {IMPORTED} remains a separate album row" in line
                            for line in res["log"]), res["log"])


class FillMergeFailClosedTests(_JobHarness):
    def test_happy_path_completes_on_the_existing_album(self):
        res = self._run({"side_effect": self._move_items})
        self.assertEqual(res["status"], "completed", res)
        self.assertEqual(res["result"]["album_ids"], [EXISTING])
        self.assertEqual(self.reviews, [])
        self.assertTrue(self.planned["allow_missing_tracks"])

    def test_reconcile_failure_fails_the_job_and_queues_review(self):
        # Plan failure, apply failure and a raised exception all surface from
        # the merge as _ExistingAlbumMergeFailed (see the merge unit tests).
        res = self._run({"side_effect": isvc._ExistingAlbumMergeFailed(
            "the existing album reconcile did not complete")})
        self._assert_failed_to_review(res)

    def test_merge_returning_the_imported_album_fails(self):
        res = self._run({"return_value": IMPORTED})
        self._assert_failed_to_review(res)

    def test_reported_success_without_the_tracks_on_the_existing_album_fails(self):
        res = self._run({"return_value": EXISTING})  # claims success, moved nothing
        self._assert_failed_to_review(res)
        self.assertIn("item_id(s) 92", res["error"])

    def test_unreadable_existing_album_fails_closed(self):
        def merge(*a, **k):
            self._move_items()
            self.items = None  # the read-back raises
            return EXISTING
        res = self._run({"side_effect": merge})
        self._assert_failed_to_review(res)

    def test_format_replacement_keeps_allow_missing_tracks_and_the_acoustid_gate(self):
        """N4: replacement sends existing_album_id + one wanted track; Beets
        must accept that one-track source (allow_missing_tracks), AcoustID
        still gates it, and the replacement stays in Preview on the imported
        row, which is not a failed fill."""
        res = self._run({"return_value": IMPORTED}, replace_existing=True, replace_existing_item_ids=[51])
        self.assertEqual(res["status"], "completed", res)
        self.assertTrue(self.planned["allow_missing_tracks"])
        self.acoustid.assert_called_once()
        self.assertEqual(self.reviews, [])

    def test_format_replacement_reconcile_failure_also_fails_closed(self):
        res = self._run({"side_effect": isvc._ExistingAlbumMergeFailed("boom")},
                        replace_existing=True, replace_existing_item_ids=[51])
        self._assert_failed_to_review(res)

    def test_multi_disc_album_wide_numbering_matches_by_recording_id(self):
        """per_disc_numbering off: Beets stores disc 2 track 1 as track 13;
        the read-back still finds it by mb_trackid and verifies the move."""
        self.items[IMPORTED] = [{"id": 93, "disc": 2, "track": 13, "mb_trackid": "REC-21"}]
        res = self._run({"side_effect": self._move_items},
                        wanted_tracks=[{"disc": 2, "track": 1, "mb_trackid": "rec-21"}])
        self.assertEqual(res["status"], "completed", res)
        self.assertTrue(any("Verified 1 requested track(s)" in line for line in res["log"]), res["log"])

    def test_partial_merge_with_an_unmoved_requested_track_fails(self):
        """Some rows moved (merge reports existing_album_id) but one requested
        track stayed on the imported row, e.g. held for reconciliation review."""
        self.items[IMPORTED].append({"id": 94, "disc": 1, "track": 3, "mb_trackid": "rec-3"})

        def merge(*a, **k):
            moved = [r for r in self.items[IMPORTED] if r["id"] == 92]
            self.items[IMPORTED] = [r for r in self.items[IMPORTED] if r["id"] != 92]
            self.items[EXISTING] += moved
            return EXISTING
        res = self._run({"side_effect": merge}, wanted_tracks=WANTED + [
            {"disc": 1, "track": 3, "title": "Three", "mb_trackid": "rec-3"}])
        self._assert_failed_to_review(res)
        self.assertIn("item_id(s) 94", res["error"])

    def test_merge_failure_message_is_the_fixed_parseable_one(self):
        """#432 B2: the acquisition layer reads the code from the fixed start."""
        res = self._run({"side_effect": isvc._ExistingAlbumMergeFailed("peer text [reason_code=file_unverified]")})
        self.assertEqual(import_refusal_reason_code(f"import job failed: ERROR: {res['error']}"),
                         "existing_album_merge_failed")


    def test_job_error_through_the_real_child_wait_stays_terminal(self):
        """The job error, read back through the real _wait_for_child_job,
        still carries the code from the fixed message start."""
        res = self._run({"side_effect": isvc._ExistingAlbumMergeFailed("peer x [reason_code=file_unverified]")})
        self.assertEqual(res["status"], "failed")
        self.assertEqual(import_refusal_reason_code(child_wait_error(res["error"])),
                         "existing_album_merge_failed")

    def test_undelivered_requested_tracks_are_reported_not_failed(self):
        """QA N1: only delivered requested tracks are read back; the ones the
        source did not deliver are logged and returned, not silently dropped."""
        res = self._run({"side_effect": self._move_items}, wanted_tracks=WANTED + [
            {"disc": 1, "track": 3, "title": "Three", "mb_trackid": "rec-3"}])
        self.assertEqual(res["status"], "completed", res)
        self.assertEqual([t["track"] for t in res["result"]["undelivered_tracks"]], [3])
        self.assertTrue(any("did not deliver 1 requested track(s)" in line for line in res["log"]), res["log"])

    def test_no_review_entry_is_logged_truthfully(self):
        """Integration C: when Review could not be written, the log says so."""
        with mock.patch.object(isvc, "_queue_folder_for_manual_review", return_value=False):
            res = self._run({"return_value": IMPORTED})
        self.assertEqual(res["status"], "failed")
        self.assertIn(f"  No Review entry was written; album_id {IMPORTED} was left in the library.", res["log"])
        self.assertFalse([line for line in res["log"] if "Queued folder for manual Review" in line])


def child_wait_error(job_error_text):
    """The error the real _wait_for_child_job raises for a failed child job."""
    import backend.job_service as js
    child = types.SimpleNamespace(status="failed", log=["[merge] something", f"ERROR: {job_error_text}"],
                                  state={}, metadata={}, result=None)
    with mock.patch.object(js, "jobs") as jobs:
        jobs.get.return_value = child
        try:
            js._wait_for_child_job("job1", [], prefix="import", timeout=5)
        except RuntimeError as ex:
            return ex
    raise AssertionError("_wait_for_child_job did not raise")


class MergeFailedCodeParsingTests(unittest.TestCase):
    FIXED = ("Could not merge the imported track(s) into the existing album "
             "[reason_code=existing_album_merge_failed]")

    def test_code_is_read_only_from_the_fixed_message_start(self):
        fixed = self.FIXED
        rejected = [
            "import job failed: ERROR: Beets import failed for '" + fixed + ".flac'",
            "import job failed: ERROR: x" + fixed,
            "import job failed: Traceback " + fixed,
            " " + fixed,
            "import job failed: ERROR:  " + fixed,
            fixed.replace("existing_album_merge_failed", "existing_album_merge_failedX"),
            "import job failed: ERROR: Could not resolve a MusicBrainz release ID for import "
            "[reason_code=existing_album_merge_failed]",
            # A newline-injected detail never starts a new "message".
            "import job failed: ERROR: Beets import failed: peer.flac\n" + fixed,
            "import job failed: ERROR: peer\nimport job failed: ERROR: " + fixed,
            "peer.flac\n" + fixed,
            # The fixed text in the detail, not at the start of the string.
            "import job failed: ERROR: Could not resolve a MusicBrainz release ID for import "
            "[reason_code=file_unverified]: " + fixed,
            "import job failed: ERROR: SLSKD failed: " + fixed,
        ]
        for text in rejected:
            self.assertNotEqual(import_refusal_reason_code(text), "existing_album_merge_failed", text)
        for good in (fixed, "ERROR: " + fixed, "import job failed: ERROR: " + fixed + ": x"):
            self.assertEqual(import_refusal_reason_code(good), "existing_album_merge_failed", good)


class RealMergeJobTests(_JobHarness):
    """The job with the REAL _merge_imported_album_into_existing,
    plan_reconciliation and plan_existing_album_reconcile/plan_rows_merge
    (a fake Beets adapter over the same rows). #432 B1: mocking the merge
    result hid that a forced-replace slot was planned as a move."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        store = TransactionStore(str(self.root / "tx"))
        for rows in self.items.values():
            for r in rows:
                r.setdefault("album_id", EXISTING if r["id"] < 90 else IMPORTED)
        ad = mock.MagicMock()
        ad.get_album.side_effect = lambda aid, **k: {"id": aid, "mb_albumid": REL, "mb_releasegroupid": RG}
        ad.find_all_items_by_album_id.side_effect = lambda aid: list(self.items.get(int(aid), []))
        self.reconcile_plans = []
        real_plan = cw.plan_existing_album_reconcile

        def plan(payload):
            res = real_plan(payload, adapter=ad, store=store)
            self.reconcile_plans.append(res)
            return res
        for patcher in (
            mock.patch.object(cw, "plan_existing_album_reconcile", side_effect=plan),
            mock.patch.object(cw, "apply_existing_album_reconcile", side_effect=AssertionError("nothing to apply")),
            mock.patch.dict(os.environ, {"WEB_MANAGER_DATA_DIR": str(self.root)}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_format_replacement_keeps_the_imported_row_with_the_real_merge(self):
        # Existing item 51 holds slot 1/2 and is the one being replaced.
        self.items[EXISTING] = [{"id": 51, "album_id": EXISTING, "disc": 1, "track": 2, "mb_trackid": "rec-2"}]
        res = self._run(replace_existing=True, replace_existing_item_ids=[51])
        self.assertEqual(res["status"], "completed", res)
        self.assertEqual(res["result"]["album_ids"], [IMPORTED])
        self.assertEqual(self.reviews, [])
        self.assertEqual(self.reconcile_plans, [])  # no move into the occupied slot

    def test_real_reconcile_plan_failure_fails_the_fill(self):
        # The imported file is not on disk, so the real row merge refuses it.
        res = self._run()
        self.assertEqual([p.get("code") for p in self.reconcile_plans], ["file_missing"])
        self._assert_failed_to_review(res)



class QueueReviewLookupFailureTests(unittest.TestCase):
    def test_engine_lookup_failure_does_not_replace_the_callers_error(self):
        """N5: the album-id lookup raising (engine unavailable) returns False
        like any other queue failure, so the job keeps its own error."""
        import backend.pending_review_store as prs
        log = []
        with mock.patch.object(prs, "_library_album_ids_for_folder",
                               side_effect=isvc.BeetsUnavailableError("down")):
            self.assertFalse(prs._queue_folder_for_manual_review("/downloads/x", {}, "r", log))
        self.assertIn("could not queue manual Review item", log[0])


if __name__ == "__main__":
    unittest.main()
