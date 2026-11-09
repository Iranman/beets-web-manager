"""Review "Delete Folder" never deletes a folder a qBittorrent-batch or
quality-upgrade row points at while it may hold a seeding torrent's files:
409 torrent_source_protected, no cleanup plan, files and row kept. A staged
copy the app made (and any other Review row) is still deleted, and the row
can still be dismissed."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
import backend.library_service as ls
import backend.pending_review_store as prs
import routes_import as ri

CODE = "torrent_source_protected"
_CSRF = {"Origin": "http://localhost", "X-Beets-CSRF": "1"}


class ReviewFolderDeleteTorrentGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.torrent = self._folder("Artist - Album [FLAC]")
        self.upgrade = self._folder("Artist - Better [FLAC]")
        self.stage = self._folder("_beets_missing_import/0123456789abcdef-Album")
        self.local = self._folder("Other - Album")
        self.store = self.root / "ai_pending_review.json"
        self.store.write_text(json.dumps([
            {"path": str(self.torrent), "status": "review_required", "evidence": {"source_mode": "qbittorrent"}},
            {"path": str(self.upgrade), "status": "review_required",
             "evidence": {"origin_step": "quality_upgrade", "duplicate_rule": "quality_upgrade"}},
            {"path": str(self.stage), "status": "review_required", "evidence": {"source_mode": "qbittorrent"}},
            {"path": str(self.local), "status": "review_required", "evidence": {}},
        ]))
        self.delete = mock.Mock(return_value={"operation_id": "op", "deleted": [], "quarantined": [],
                                              "files_removed": 1, "status": "Completed"})
        for target, name, kw in (
            (prs, "_AI_PENDING_FILE", {"new": self.store}),
            (ri.composite_workflows, "_import_review_allowed_roots", {"return_value": [str(self.root)]}),
            (prs._serializers, "_import_review_cleanup_roots", {"return_value": [str(self.root)]}),
            (ri, "_library_no_mb_album_matches_folder", {"return_value": False}),
            (ri, "_delete_review_source_folder", {"new": self.delete}),
            # The preserve rule as configured with a downloads root: app-made
            # staging folders are not preserved, everything else is.
            (ls, "_preserve_torrent_source_path", {"side_effect": lambda p: "_beets_missing_import" not in str(p)}),
            (ls, "_app_managed_download_path", {"side_effect": lambda p: "_beets_missing_import" in str(p)}),
            (prs, "_record_ai_review_decision", {}),
        ):
            p = mock.patch.object(target, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.client = app_module.app.test_client()

    def _folder(self, rel):
        folder = self.root / rel
        folder.mkdir(parents=True)
        (folder / "01.flac").write_bytes(b"audio")
        return folder

    def post_delete(self, folder):
        return self.client.post("/api/import/review-folder/delete", headers=_CSRF, json={"path": str(folder)})

    def test_qbittorrent_and_upgrade_rows_refused_without_mutation(self):
        for folder in (self.torrent, self.upgrade):
            resp = self.post_delete(folder)
            self.assertEqual(resp.status_code, 409, (folder, resp.get_json()))
            self.assertEqual(resp.get_json()["error_code"], CODE)
            self.assertTrue((folder / "01.flac").is_file())
        self.delete.assert_not_called()
        self.assertEqual(len(json.loads(self.store.read_text())), 4)

    def test_app_made_stage_and_other_rows_still_deleted(self):
        for folder in (self.stage, self.local):
            resp = self.post_delete(folder)
            self.assertEqual(resp.status_code, 200, (folder, resp.get_json()))
        self.assertEqual([c.args[0] for c in self.delete.call_args_list], [str(self.stage), str(self.local)])

    def test_dismiss_still_removes_a_qbittorrent_row(self):
        resp = self.client.delete("/api/ai-pending-review", headers=_CSRF, json={"path": str(self.torrent)})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertNotIn(str(self.torrent), [r["path"] for r in json.loads(self.store.read_text())])
        self.assertTrue((self.torrent / "01.flac").is_file())


if __name__ == "__main__":
    unittest.main()
