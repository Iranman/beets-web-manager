"""#565: a Review row authorises actions only on its own literal path.
"Album " (trailing space) and "Album" are different folders, so a row for
one must never authorise Delete Folder, Review cleanup or import on the
other. Whitespace-only paths stay refused."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
import backend.import_service as isvc
import backend.pending_review_store as prs
import routes_import as ri

_CSRF = {"Origin": "http://localhost", "X-Beets-CSRF": "1"}


class _Base(unittest.TestCase):
    rows: list = []

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.store = self.root / "ai_pending_review.json"
        for target, name, kw in (
            (prs, "_AI_PENDING_FILE", {"new": self.store}),
            (prs._serializers, "_import_review_cleanup_roots", {"return_value": [self.root]}),
            (ri.composite_workflows, "_import_review_allowed_roots", {"return_value": [str(self.root)]}),
        ):
            p = mock.patch.object(target, name, **kw)
            p.start()
            self.addCleanup(p.stop)

    def write_rows(self, *paths):
        self.store.write_text(json.dumps([{"path": p, "status": "review_required", "evidence": {}} for p in paths]))


class MatchingIsLiteral(_Base):
    """Platform independent: crafted strings, nothing created on disk."""

    def test_trailing_space_row_does_not_match_sibling(self):
        spaced, plain = str(self.root / "Album "), str(self.root / "Album")
        self.write_rows(spaced)
        self.assertFalse(prs._pending_review_matches(plain))
        self.assertFalse(prs._pending_review_matches(plain, "pending:" + plain))
        self.assertFalse(prs._pending_review_matches(plain, "pending:" + spaced))
        self.assertTrue(prs._pending_review_matches(spaced))
        self.assertTrue(prs._pending_review_matches(spaced, "pending:" + spaced))

    def test_plain_row_does_not_match_spaced_request(self):
        spaced, plain = str(self.root / "Album "), str(self.root / "Album")
        self.write_rows(plain)
        self.assertFalse(prs._pending_review_matches(spaced))
        self.assertTrue(prs._pending_review_matches(plain, "pending:" + plain))

    def test_trusted_stored_path_keeps_whitespace(self):
        spaced = str(self.root / "Album ")
        self.assertEqual(prs._trusted_stored_review_path(spaced), spaced)
        for blank in ("", " ", "\t\n"):
            self.assertIsNone(prs._trusted_stored_review_path(blank))
            self.assertFalse(prs._pending_review_matches(blank))

    def test_review_id_with_whitespace_is_not_a_sibling(self):
        plain = str(self.root / "Album")
        self.assertFalse(prs._review_id_matches_path("pending:" + plain + " ", plain))
        self.assertTrue(prs._review_id_matches_path("pending:" + plain, plain))
        self.assertTrue(prs._review_id_matches_path("", plain))


@unittest.skipIf(os.name == "nt", "Windows cannot create a folder whose name ends in a space")
class SiblingFoldersOnDisk(_Base):
    def setUp(self):
        super().setUp()
        self.plain = self.root / "Album"
        self.spaced = self.root / "Album "
        for folder in (self.plain, self.spaced):
            folder.mkdir()
            (folder / "01.flac").write_bytes(b"audio")
        self.delete = mock.Mock(return_value={"operation_id": "op", "deleted": [], "quarantined": [],
                                              "files_removed": 1, "status": "Completed"})
        for target, name, kw in (
            (ri, "_library_no_mb_album_matches_folder", {"return_value": False}),
            (ri, "_delete_review_source_folder", {"new": self.delete}),
            (ri, "_torrent_rows_protect", {"return_value": False}),
            (prs, "_record_ai_review_decision", {}),
        ):
            p = mock.patch.object(target, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.client = app_module.app.test_client()

    def post_delete(self, folder):
        return self.client.post("/api/import/review-folder/delete", headers=_CSRF, json={"path": str(folder)})

    def test_delete_folder_refused_on_sibling_without_row(self):
        for row, other in ((self.spaced, self.plain), (self.plain, self.spaced)):
            with self.subTest(row=row.name):
                self.write_rows(str(row))
                self.delete.reset_mock()
                resp = self.post_delete(other)
                self.assertEqual(resp.status_code, 400, resp.get_json())
                self.delete.assert_not_called()
                self.assertTrue((other / "01.flac").is_file())

    def test_delete_folder_allowed_on_row_own_literal_path(self):
        for row in (self.spaced, self.plain):
            with self.subTest(row=row.name):
                self.write_rows(str(row))
                self.delete.reset_mock()
                resp = self.post_delete(row)
                self.assertEqual(resp.status_code, 200, resp.get_json())
                self.assertEqual(self.delete.call_args.args[0], str(row))

    def test_review_cleanup_refused_on_sibling_without_row(self):
        self.write_rows(str(self.spaced))
        with mock.patch.object(ri.composite_workflows, "plan_import_review_cleanup") as plan:
            resp = self.client.post("/api/import/review-files/cleanup", headers=_CSRF, json={
                "path": str(self.plain), "review_item_id": "pending:" + str(self.plain),
                "action": "quarantine_rejected", "files": [str(self.plain / "01.flac")]})
        self.assertEqual(resp.status_code, 400, resp.get_json())
        plan.assert_not_called()

    def test_import_eligibility_refuses_sibling(self):
        self.write_rows(str(self.spaced))
        res = isvc.evaluate_import_eligibility({"path": str(self.plain),
                                                "review_item_id": "pending:" + str(self.spaced)})
        self.assertIn("review item does not match source folder", res.get("blockers", []))


if __name__ == "__main__":
    unittest.main()
