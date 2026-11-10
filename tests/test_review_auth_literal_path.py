"""#565 / #572: a Review row authorises actions only on its own literal path,
and the planner, apply and Review bookkeeping act on exactly that path.
"Album ", "Album<NBSP>" and "Album" are different folders, and
"root/link/../Album" is refused rather than resolved to a third folder.
Whitespace-only paths stay refused."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
import backend.import_service as isvc
import backend.pending_review_store as prs
import backend.transaction_engine as te
import routes_import as ri

_CSRF = {"Origin": "http://localhost", "X-Beets-CSRF": "1"}
NBSP = chr(0xA0)


class _Base(unittest.TestCase):
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

    def row_paths(self):
        return [r["path"] for r in json.loads(self.store.read_text())]


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

    def test_nbsp_row_does_not_match_sibling(self):
        nbsp, plain = str(self.root / ("Album" + NBSP)), str(self.root / "Album")
        self.write_rows(nbsp)
        self.assertFalse(prs._pending_review_matches(plain))
        self.assertTrue(prs._pending_review_matches(nbsp))

    def test_plain_row_does_not_match_spaced_request(self):
        spaced, plain = str(self.root / "Album "), str(self.root / "Album")
        self.write_rows(plain)
        self.assertFalse(prs._pending_review_matches(spaced))
        self.assertTrue(prs._pending_review_matches(plain, "pending:" + plain))

    def test_trusted_stored_path_keeps_whitespace(self):
        spaced = str(self.root / "Album ")
        self.assertEqual(prs._trusted_stored_review_path(spaced), spaced)
        for blank in ("", " ", "\t\n", NBSP):
            self.assertIsNone(prs._trusted_stored_review_path(blank))
            self.assertFalse(prs._pending_review_matches(blank))

    def test_review_id_with_whitespace_is_not_a_sibling(self):
        plain = str(self.root / "Album")
        self.assertFalse(prs._review_id_matches_path("pending:" + plain + " ", plain))
        self.assertTrue(prs._review_id_matches_path("pending:" + plain, plain))
        self.assertTrue(prs._review_id_matches_path("", plain))

    def test_remove_and_mark_keep_the_literal_row(self):
        spaced, plain = str(self.root / "Album "), str(self.root / "Album")
        self.write_rows(spaced, plain)
        with mock.patch.object(prs, "_record_ai_review_decision"):
            self.assertTrue(prs._mark_pending_review_status(spaced, "auto_enqueue_failed", "x"))
            rows = {r["path"]: r for r in json.loads(self.store.read_text())}
            self.assertEqual(rows[spaced].get("status"), "auto_enqueue_failed")
            self.assertNotEqual(rows[plain].get("status"), "auto_enqueue_failed")
            self.assertTrue(prs._remove_pending_review_for_path(spaced))
        self.assertEqual(self.row_paths(), [plain])

    def test_planner_keeps_literal_path(self):
        # QA regression: the engine plans on the authorised string, not a
        # trimmed sibling. The first refusal carries the planned path.
        for name in ("Album ", "Album" + NBSP):
            target = str(self.root / name)
            with mock.patch("os.path.islink", return_value=True):
                res = te.execute_import_review_cleanup_plan(
                    mock.Mock(), {"path": target, "action": "delete_folder"}, [str(self.root)])
            self.assertTrue(res["error"].endswith(target), res["error"])
        for blank in (" ", NBSP):
            res = te.execute_import_review_cleanup_plan(
                mock.Mock(), {"path": blank, "action": "delete_folder"}, [str(self.root)])
            self.assertEqual(res["error"], "Review path is required.")

    def test_eligibility_refuses_sibling(self):
        spaced, plain = str(self.root / "Album "), str(self.root / "Album")
        self.write_rows(spaced)
        res = isvc.evaluate_import_eligibility({"path": plain, "review_item_id": "pending:" + spaced})
        self.assertIn("review item does not match source folder", res.get("blocking_reasons", []))


@unittest.skipIf(os.name == "nt", "Windows cannot hold folder names ending in a space or NBSP")
class SiblingFoldersOnDisk(_Base):
    """Real torrent guard, planner and apply; only the library lookup and the
    decision log are stubbed."""

    def setUp(self):
        super().setUp()
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        data_dir = Path(os.path.realpath(data.name))
        music = data_dir / "music"
        music.mkdir()
        tx = te.TransactionStore(str(data_dir / "tx.db"))
        for target, name, kw in (
            (ri, "_library_no_mb_album_matches_folder", {"return_value": False}),
            (prs, "_record_ai_review_decision", {}),
            (ri.composite_workflows, "_get_store", {"side_effect": lambda store=None: store or tx}),
            (ri.composite_workflows, "_music_root", {"return_value": music}),
        ):
            p = mock.patch.object(target, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.client = app_module.app.test_client()

    def folder(self, rel):
        folder = self.root / rel
        folder.mkdir(parents=True)
        (folder / "01.flac").write_bytes(b"audio")
        return folder

    def post_delete(self, path):
        return self.client.post("/api/import/review-folder/delete", headers=_CSRF, json={"path": str(path)})

    def test_delete_folder_deletes_only_the_row_folder(self):
        for suffix in (" ", NBSP):
            with self.subTest(suffix=repr(suffix)):
                plain, odd = self.folder("Album"), self.folder("Album" + suffix)
                self.write_rows(str(odd))
                resp = self.post_delete(odd)
                self.assertEqual(resp.status_code, 200, resp.get_json())
                self.assertFalse((odd / "01.flac").exists())
                self.assertTrue((plain / "01.flac").is_file(), "sibling must survive")
                self.assertEqual(self.row_paths(), [])
                shutil.rmtree(plain)
                shutil.rmtree(odd, ignore_errors=True)

    def test_delete_folder_refused_on_sibling_without_row(self):
        plain, spaced = self.folder("Album"), self.folder("Album ")
        for row, other in ((spaced, plain), (plain, spaced)):
            with self.subTest(row=repr(row.name)):
                self.write_rows(str(row))
                resp = self.post_delete(other)
                self.assertEqual(resp.status_code, 400, resp.get_json())
                self.assertTrue((other / "01.flac").is_file())
                self.assertTrue((row / "01.flac").is_file())

    def test_dotdot_through_symlink_is_refused(self):
        row = self.folder("Album")
        victim = self.folder("other/Album")
        (self.root / "other" / "deep").mkdir()
        os.symlink(str(self.root / "other" / "deep"), str(self.root / "x"))
        self.write_rows(str(row))
        resp = self.post_delete(str(self.root / "x") + "/../Album")
        self.assertEqual(resp.status_code, 400, resp.get_json())
        self.assertTrue((victim / "01.flac").is_file(), "third folder must survive")
        self.assertTrue((row / "01.flac").is_file())

    def test_review_cleanup_never_touches_sibling_file(self):
        plain, spaced = self.folder("Album"), self.folder("Album ")
        self.write_rows(str(spaced))
        self.client.post("/api/import/review-files/cleanup", headers=_CSRF, json={
            "path": str(spaced), "review_item_id": "pending:" + str(spaced),
            "action": "quarantine_rejected", "files": [str(plain / "01.flac")]})
        self.assertTrue((plain / "01.flac").is_file(), "sibling file must survive")

    def test_review_cleanup_refused_on_sibling_without_row(self):
        plain, spaced = self.folder("Album"), self.folder("Album ")
        self.write_rows(str(spaced))
        resp = self.client.post("/api/import/review-files/cleanup", headers=_CSRF, json={
            "path": str(plain), "review_item_id": "pending:" + str(plain),
            "action": "quarantine_rejected", "files": [str(plain / "01.flac")]})
        self.assertEqual(resp.status_code, 400, resp.get_json())
        self.assertTrue((plain / "01.flac").is_file())

    def test_dismiss_removes_only_the_literal_row(self):
        # Folders with audio, so loading the queue does not prune the rows.
        plain, spaced = str(self.folder("Album")), str(self.folder("Album "))
        self.write_rows(plain, spaced)
        resp = self.client.delete("/api/ai-pending-review", headers=_CSRF, json={"path": spaced})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(self.row_paths(), [plain])


if __name__ == "__main__":
    unittest.main()
