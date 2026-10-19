"""A missing-track fill into an existing album fails closed (#429 QA N3).

When the reconcile/merge of the newly imported album into existing_album_id
does not complete, or the requested tracks are not read back on the existing
album, the job fails and the folder goes to Review with
[reason_code=existing_album_merge_failed]. The imported album row is never
removed here. Format replacement (N4) keeps allow_missing_tracks and the
AcoustID gate, and its Preview result is not mistaken for a failed fill.
"""
import contextlib
import types
import unittest
from unittest import mock

import backend.import_service as isvc

REL = "11111111-1111-1111-1111-111111111111"
RG = "22222222-2222-2222-2222-222222222222"
EXISTING, IMPORTED = 5, 9
WANTED = [{"disc": 1, "track": 2, "title": "Two", "mb_trackid": "rec-2"}]
CODE = "[reason_code=existing_album_merge_failed]"


class FillMergeFailClosedTests(unittest.TestCase):
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

    def _run(self, merge, **payload):
        body = {"aldir": str(isvc.DOWNLOADS_ROOT) + "/Artist/Album", "mb_albumid": REL,
                "existing_album_id": EXISTING, "wanted_tracks": WANTED, "skip_import_lock": True, **payload}
        with mock.patch.object(isvc, "_merge_imported_album_into_existing", **merge):
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


if __name__ == "__main__":
    unittest.main()
