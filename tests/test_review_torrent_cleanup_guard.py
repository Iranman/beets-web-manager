"""Import Review "Remove rejected files" (review-files/cleanup) and Delete
Folder never move or delete a seeding torrent's files.

A qBittorrent-origin Review row is always torrent data: its files are never
quarantined or deleted, even with ALLOW_TORRENT_SOURCE_MOVE=1, and a pending
parent folder cannot carry them out (the rule judges each overlapping row's
own path, not the folder being deleted). Both routes answer 409
torrent_source_protected and plan nothing; the planner refuses too, so every
caller of import_review_cleanup_v1 is covered. A staged copy the app made
(under ``_beets_missing_import``) and a normal local row are unchanged.

Real temp folders, real planner/engine apply; only roots and the queue file
are pointed at the temp tree."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
import backend.composite_workflows as cw
import backend.library_service as ls
import backend.pending_review_store as prs
import routes_import as ri
from backend.transaction_engine import TransactionStore

CODE = "torrent_source_protected"
_CSRF = {"Origin": "http://localhost", "X-Beets-CSRF": "1"}


def _tree(folder: Path) -> dict:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.rglob("*")) if p.is_file()}


class ReviewTorrentCleanupGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(os.path.realpath(tmp.name))
        self.downloads = base / "downloads"
        self.music = base / "music"
        self.quarantine = base / "quarantine"
        for d in (self.downloads, self.music, self.quarantine):
            d.mkdir()
        self.qbit = self._folder("qbit/Artist - Album [FLAC]")
        self.parent = self.downloads / "C"
        self.nested = self._folder("C/torrents/Artist - Nested [FLAC]")
        (self.parent / "stray.flac").write_bytes(b"stray")
        self.stage = self._folder("_beets_missing_import/0123456789abcdef-Album")
        self.local = self._folder("Local - Album")
        self.store_file = base / "ai_pending_review.json"
        self.store_file.write_text(json.dumps([
            {"path": str(self.qbit), "status": "review_required", "evidence": {"source_mode": "qbittorrent"}},
            {"path": str(self.parent), "status": "review_required", "evidence": {}},
            {"path": str(self.nested), "status": "review_required", "evidence": {"source_mode": "qbittorrent"}},
            {"path": str(self.stage), "status": "review_required", "evidence": {"source_mode": "qbittorrent"}},
            {"path": str(self.local), "status": "review_required", "evidence": {}},
        ]))
        self.tx = TransactionStore(root=str(base / "transactions"))
        roots = [str(self.downloads), str(self.music)]
        for target, name, kw in (
            (prs, "_AI_PENDING_FILE", {"new": self.store_file}),
            (prs._serializers, "_import_review_cleanup_roots", {"return_value": roots}),
            (prs, "_record_ai_review_decision", {}),
            (cw, "_import_review_allowed_roots", {"return_value": [str(self.downloads)]}),
            (cw, "_music_root", {"return_value": self.music}),
            (cw, "_import_review_quarantine_root", {"return_value": str(self.quarantine)}),
            (cw, "_get_store", {"side_effect": lambda store=None: store or self.tx}),
            (ri, "_library_no_mb_album_matches_folder", {"return_value": False}),
            (ri, "MUSIC_ROOT", {"new": self.music}),
            (ls, "MUSIC_ROOT", {"new": self.music}),
            (ls, "DOWNLOADS_ALLOWED_ROOTS", {"new": [self.downloads]}),
            (ls, "PLAYLIST_DOWNLOAD_ALLOWED_ROOTS", {"new": []}),
            (ls, "TORRENT_SOURCE_ROOTS", {"new": []}),
            (ls, "TORRENT_SOURCE_MOVE_ALLOWED", {"new": False}),
        ):
            p = mock.patch.object(target, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.client = app_module.app.test_client()

    def _folder(self, rel):
        folder = self.downloads / rel
        folder.mkdir(parents=True)
        (folder / "01.flac").write_bytes(b"audio-" + rel.encode())
        (folder / "02.flac").write_bytes(b"rejected-" + rel.encode())
        return folder

    def cleanup(self, folder):
        return self.client.post("/api/import/review-files/cleanup", headers=_CSRF, json={
            "path": str(folder), "files": [str(folder / "02.flac")], "action": "quarantine_rejected"})

    def delete(self, folder):
        return self.client.post("/api/import/review-folder/delete", headers=_CSRF, json={"path": str(folder)})

    def assert_refused(self, resp, folder, before):
        body = resp.get_json()
        self.assertEqual(resp.status_code, 409, body)
        self.assertEqual((body["error_code"], body["code"], body["ok"]), (CODE, CODE, False))
        self.assertEqual(_tree(folder), before)
        self.assertEqual(self.tx.list()[1], 0, "a plan was created")
        self.assertEqual(list(self.quarantine.rglob("*")), [])

    # B-1 -----------------------------------------------------------------
    def test_cleanup_on_qbittorrent_row_refused_without_plan(self):
        before = _tree(self.qbit)
        self.assert_refused(self.cleanup(self.qbit), self.qbit, before)

    def test_cleanup_on_qbittorrent_row_refused_even_with_torrent_moves_allowed(self):
        before = _tree(self.qbit)
        with mock.patch.object(ls, "TORRENT_SOURCE_MOVE_ALLOWED", True):
            self.assert_refused(self.cleanup(self.qbit), self.qbit, before)

    def test_planner_never_plans_a_torrent_file(self):
        before = _tree(self.qbit)
        res = cw.plan_import_review_cleanup(
            {"path": str(self.qbit), "files": [str(self.qbit / "02.flac")], "action": "quarantine_rejected"},
            store=self.tx)
        if res.get("ok"):  # pre-fix: show the damage the plan would do
            cw.apply_import_review_cleanup(res["operation_id"], store=self.tx, approved_by="test")
        self.assertEqual(_tree(self.qbit), before)
        self.assertEqual((res.get("ok"), res.get("code")), (False, CODE))

    def test_planner_fails_closed_on_unreadable_queue(self):
        self.store_file.write_text("{not json")
        res = cw.plan_import_review_cleanup(
            {"path": str(self.local), "files": [str(self.local / "02.flac")], "action": "quarantine_rejected"},
            store=self.tx)
        self.assertEqual((res.get("ok"), res.get("code")), (False, CODE))
        self.assertTrue((self.local / "02.flac").is_file())

    # B-2 -----------------------------------------------------------------
    def test_delete_pending_parent_of_qbittorrent_row_refused_with_narrow_roots(self):
        before = _tree(self.parent)
        with mock.patch.object(ls, "TORRENT_SOURCE_ROOTS", [self.parent / "torrents"]):
            self.assert_refused(self.delete(self.parent), self.parent, before)

    def test_delete_qbittorrent_row_refused_even_with_torrent_moves_allowed(self):
        before = _tree(self.qbit)
        with mock.patch.object(ls, "TORRENT_SOURCE_MOVE_ALLOWED", True):
            self.assert_refused(self.delete(self.qbit), self.qbit, before)

    # Unchanged ---------------------------------------------------------------
    def test_staged_copy_cleanup_and_delete_still_allowed(self):
        # The app marks the stage it makes (#566); markers need POSIX, so stand in for one.
        marked = mock.patch.object(ls, "app_download_marker_valid",
                                   side_effect=lambda folder: Path(folder) == self.stage)
        marked.start()
        self.addCleanup(marked.stop)
        resp = self.cleanup(self.stage)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertFalse((self.stage / "02.flac").exists())
        self.assertTrue((self.stage / "01.flac").is_file())
        resp = self.delete(self.stage)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertFalse((self.stage / "01.flac").exists())

    def test_unmarked_staged_copy_of_a_torrent_row_is_protected(self):
        """#566: the staging location alone is no proof the app made the folder."""
        resp = self.cleanup(self.stage)
        self.assertNotEqual(resp.status_code, 200, resp.get_json())
        self.assertTrue((self.stage / "02.flac").is_file())

    def test_local_row_cleanup_and_delete_unchanged(self):
        resp = self.cleanup(self.local)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertFalse((self.local / "02.flac").exists())
        resp = self.delete(self.local)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertFalse((self.local / "01.flac").exists())


if __name__ == "__main__":
    unittest.main()
