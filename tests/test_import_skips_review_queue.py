"""D2: a folder Beets skips during POST /api/import reaches the Review Queue.

/webmanager/import runs Beets' importer without an import log, so beet.log
never names the folder. Web Manager records the plugin's skipped folders and
the Review Queue's Skipped source lists them: one row per folder, never
duplicated by a re-import, gone once a later import of the source takes it.
"""
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import app as APP
import backend.import_service as isvc
import routes_import


class ImportSkipsReachReviewQueueTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.src = self.root / "downloads"
        self.album = self.src / "Artist - Album"
        self.album.mkdir(parents=True)
        (self.album / "01.flac").write_bytes(b"x")
        # beet.log does not exist: /webmanager/import writes no Beets import log.
        for name, value in (("_IMPORT_SKIPPED_FILE", self.root / "import_skipped.json"),
                            ("LOG_FILE", str(self.root / "beet.log"))):
            p = mock.patch.object(isvc, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.client = APP.app.test_client()

    def _import(self, skipped):
        def run_now(fn, label=""):
            self.job_result = fn([])
            return types.SimpleNamespace(job_id="j1")

        def fake_reimport(path, beets_options=None, timeout=0):
            return {"ok": True, "albums_imported": 0, "not_matched": [r["path"] for r in skipped],
                    "skipped": skipped, "not_matched_known": True}
        with mock.patch.object(routes_import, "_resolve_import_source_path", return_value=(self.src, None)), \
             mock.patch.object(routes_import, "_preserve_torrent_source_path", return_value=False), \
             mock.patch.object(routes_import, "_validate_import_source_audio"), \
             mock.patch.object(routes_import, "_delete_if_already_in_library"), \
             mock.patch.object(routes_import, "_invalidate_lib_cache", create=True), \
             mock.patch.object(routes_import.jobs, "start_python", side_effect=run_now), \
             mock.patch.object(APP.composite_workflows, "reimport_source", side_effect=fake_reimport):
            res = self.client.post("/api/import", json={"path": str(self.src)})
        self.assertEqual(res.status_code, 200, res.get_json())

    def _skipped_rows(self):
        with mock.patch.object(APP.composite_workflows, "get_unmatched_review_items",
                               return_value={"albums": [], "singletons": []}), \
             mock.patch.object(routes_import, "_load_pending_reviews", return_value=[]), \
             mock.patch("backend.pending_review_store._library_album_ids_for_folder", return_value=[]):
            res = self.client.get("/api/import/review-queue?status=skipped")
        self.assertEqual(res.status_code, 200, res.get_json())
        return [r for r in res.get_json()["items"] if r["type"] == "skipped"]

    def test_beets_skip_yields_exactly_one_review_item_across_reimports(self):
        skip = [{"path": str(self.album), "reason": "no_strong_match"}]
        self._import(skip)
        self._import(skip)  # re-import: still one row
        rows = self._skipped_rows()
        self.assertEqual([(r["path"], r["reason"]) for r in rows], [(str(self.album), "no_strong_match")])
        self.assertEqual(self.job_result["not_matched"][0]["reason"], "no_strong_match")

    def test_later_import_that_takes_the_folder_clears_it(self):
        self._import([{"path": str(self.album), "reason": "duplicate"}])
        self._import([])
        self.assertEqual(self._skipped_rows(), [])

    def test_recorded_and_log_skips_are_merged_without_duplicates(self):
        Path(isvc.LOG_FILE).write_text(f"skip {self.album}\n")
        isvc._record_import_skips(str(self.src), [{"path": str(self.album), "reason": "duplicate"}])
        self.assertEqual([(r["path"], r["reason"]) for r in isvc._import_skipped_items()],
                         [(str(self.album), "duplicate")])


if __name__ == "__main__":
    unittest.main()
