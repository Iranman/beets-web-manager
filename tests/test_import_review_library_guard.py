"""Security F2 (PR #235 review): Import Review cleanup must refuse a target
that CONTAINS the music library, and an irreversible delete of a file inside
the library without the library-delete gate -- at plan time and again at
apply time. Real temp folders; no sink is mocked."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as cw
from backend.transaction_engine import (
    TransactionStore, execute_import_review_cleanup_apply, execute_import_review_cleanup_plan)


class ImportReviewLibraryGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name).resolve() / "data"
        self.music = self.data / "media" / "music"
        self.track = self.music / "Artist" / "Album" / "01.flac"
        self.track.parent.mkdir(parents=True)
        self.track.write_bytes(b"audio")
        self.store = TransactionStore(root=str(Path(tmp.name) / "tx"))
        env = mock.patch.dict(os.environ, {
            "DOWNLOADS_ROOT": str(self.data), "MUSIC_ROOT": str(self.music),
            "WEB_MANAGER_DATA_DIR": str(Path(tmp.name) / "wm"),
            "IMPORT_REVIEW_QUARANTINE_DIR": str(Path(tmp.name) / "q")})
        env.start()
        self.addCleanup(env.stop)

    def test_probe_target_containing_library_is_refused(self):
        res = cw.plan_import_review_cleanup({"path": str(self.data / "media"), "action": "delete"},
                                            store=self.store)
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(res.get("code"), "import_review_target_contains_library")
        self.assertEqual(self.store.list()[1], 0)
        self.assertTrue(self.track.exists())

    def test_library_ancestor_refused_even_with_gate(self):
        res = execute_import_review_cleanup_plan(
            self.store, {"path": str(self.data / "media"), "action": "delete", "allow_library_delete": True},
            [str(self.data)], music_root=str(self.music))
        self.assertEqual(res.get("code"), "import_review_target_contains_library")

    def test_delete_inside_library_needs_gate_at_plan(self):
        # Library is listed as an allowed root, but the delete gate is absent.
        res = execute_import_review_cleanup_plan(
            self.store, {"path": str(self.track.parent), "action": "delete"},
            [str(self.data)], music_root=str(self.music))
        self.assertFalse(res.get("ok"), res)
        self.assertTrue(self.track.exists())

    def test_apply_rechecks_against_current_music_root(self):
        """Plan made while MUSIC_ROOT pointed elsewhere; at apply the album
        folder is inside the configured library: refused, nothing deleted."""
        res = execute_import_review_cleanup_plan(
            self.store, {"path": str(self.track.parent), "action": "delete"},
            [str(self.data)], music_root=str(self.data / "elsewhere"))
        self.assertTrue(res.get("ok"), res)
        op = res["operation_id"]
        self.store.update(op, status="Approved", metadata={"music_root": str(self.data / "elsewhere")})
        out = execute_import_review_cleanup_apply(self.store, op, quarantine_root=str(self.data / "q"))
        self.assertEqual(out.get("code"), "import_review_library_delete_refused", out)
        self.assertIs(out.get("mutated"), False)
        self.assertTrue(self.track.exists())

    def test_gated_library_delete_still_plans(self):
        res = execute_import_review_cleanup_plan(
            self.store, {"path": str(self.track.parent), "action": "delete", "allow_library_delete": True},
            [str(self.data)], music_root=str(self.music))
        self.assertTrue(res.get("ok"), res)


if __name__ == "__main__":
    unittest.main()
