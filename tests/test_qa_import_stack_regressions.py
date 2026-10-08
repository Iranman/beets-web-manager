"""QA regressions for the integrated import stack (#295 + #299 + #301).

Found by end-to-end runs against stock Beets 2.14.1 + webmanager plugin 1.8.1:

- A verification failure after Beets imported (release_group_mismatch) kept
  the rows and failed the job, but queued no review item, although the
  Import contract says "the job fails and the folder goes to review".
- import-with-id replaced the operator's confirmed single/EP Release with an
  album release, so Beets was asked to import a
  different Release (and Release Group) than the one confirmed, and the
  import of a confirmed single failed as not_imported.
- ai-batch's import step planned use_move=True for a preserved torrent
  source, so Beets would move the qBittorrent source.
"""
import unittest
from unittest import mock

try:
    import test_import_paths_beets_native as base
except ImportError:  # pragma: no cover
    from tests import test_import_paths_beets_native as base

REL, RG = base.REL, base.RG
OTHER_REL = "44444444-4444-4444-4444-444444444444"


class VerificationFailureReviewTests(base.ConfirmedImportJobTests):
    def test_reimport_disk_rg_mismatch_queues_review(self):
        res = self._reimport_disk(self.RG_OTHER)
        self.assertEqual(res["status"], "failed")
        self.assertTrue(self.isvc._queue_folder_for_manual_review.called,
                        "verification failure must queue the folder for review")

    def test_import_with_id_rg_mismatch_queues_review(self):
        res = self._import_with_id(self.RG_OTHER)
        self.assertEqual(res["status"], "failed")
        self.assertTrue(self.isvc._queue_folder_for_manual_review.called,
                        "verification failure must queue the folder for review")

    def test_import_with_id_imports_the_confirmed_release(self):
        isvc = self.isvc
        self.ad = base._adapter({"id": 9, "mb_albumid": REL, "mb_releasegroupid": RG})
        src = "/downloads/Artist - Single"
        with mock.patch.object(isvc, "_resolve_import_review_source_path", return_value=(src, None)), \
                mock.patch.object(isvc, "_preserve_torrent_source_path", return_value=True), \
                mock.patch.object(isvc, "_beet_import_timeout", return_value=60):
            body, code = isvc.start_folder_import_with_id({"path": src, "mb_albumid": REL})
        self.assertEqual(code, 200, body)
        self.assertTrue(self.ad.run_import.called, self.result)
        self.assertEqual(self.ad.run_import.call_args.kwargs["search_ids"], [REL],
                         "Beets must import the Release the operator confirmed")


class AiBatchTorrentSourceTests(unittest.TestCase):
    def test_preserved_torrent_source_is_not_moved(self):
        import backend.ai_service as ai
        planned = {}

        def plan(payload, *a, **k):
            planned.update(payload)
            return {"ok": True, "operation_id": "op-1"}

        with mock.patch.object(ai, "_preserve_torrent_source_path", return_value=True), \
                mock.patch.object(ai, "_validate_import_source_audio"), \
                mock.patch.object(ai, "_fetch_mb_release_tracklist",
                                  return_value={"ok": True, "release_group": RG, "tracks": []}), \
                mock.patch.object(ai.composite_workflows, "plan_confirmed_import", side_effect=plan), \
                mock.patch.object(ai.composite_workflows, "apply_confirmed_import",
                                  return_value={"ok": False, "error": "stop here"}):
            with self.assertRaises(RuntimeError):
                ai._ai_import_folder("/downloads/Artist - Album", REL, {}, [])
        self.assertIs(planned.get("use_move"), False,
                      "a preserved torrent source must be copied, never moved")


if __name__ == "__main__":
    unittest.main()
