"""Comprehensive Unit & Behavioral Tests for Beets Transaction Engine Families (SEC-002 Wave 22).

Covers folder_cleanup_v1, the only engine family with a production caller
(the other families were removed with their direct Beets SQLite access, BA-7).
"""

import tempfile
import unittest
from pathlib import Path

from backend import transaction_engine


class TestBeetsTransactionEngineFamilies(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name).resolve()
        self.music_dir = self.root / "music"
        self.music_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir = self.root / "staging"
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir = self.root / "quarantine"
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)


        self.store_path = self.root / "transactions.db"
        self.store = transaction_engine.TransactionStore(str(self.store_path))

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass

    # ── 1. folder_cleanup_v1 ──────────────────────────────────────────────────

    def test_folder_cleanup_plan_nonmutation(self):
        src = self.music_dir / "old_album"
        dst = self.music_dir / "new_album"
        src.mkdir()
        (src / "track1.mp3").write_bytes(b"audio content")

        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "safe_rename", "source": str(src), "target": str(dst)},
            music_allowed_roots=[str(self.music_dir)]
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())

    def test_folder_cleanup_apply_success_and_idempotency(self):
        src = self.music_dir / "old_album"
        dst = self.music_dir / "new_album"
        src.mkdir()
        (src / "track1.mp3").write_bytes(b"audio content")

        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "safe_rename", "source": str(src), "target": str(dst)},
            music_allowed_roots=[str(self.music_dir)]
        )
        op_id = plan["operation_id"]

        apply1 = transaction_engine.execute_folder_cleanup_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)]
        )
        self.assertTrue(apply1.get("ok"))
        self.assertTrue(dst.exists())
        self.assertFalse(src.exists())

        # Replay / Idempotency check: second apply returns success without duplicate mutation
        apply2 = transaction_engine.execute_folder_cleanup_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)]
        )
        self.assertTrue(apply2.get("ok"))
        self.assertTrue(apply2.get("idempotent", True))

    def test_folder_cleanup_allowed_roots_enforcement(self):
        outside = self.root / "outside_dir"
        outside.mkdir()
        dst = self.music_dir / "new_album"

        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "safe_rename", "source": str(outside), "target": str(dst)},
            music_allowed_roots=[str(self.music_dir)]
        )
        self.assertFalse(plan.get("ok"))
        self.assertIn("allowed root", plan.get("error", "").lower())

    def test_folder_cleanup_rollback(self):
        src = self.root / "old_album"
        dst = self.root / "new_album"
        src.mkdir()
        (src / "track1.mp3").write_bytes(b"audio content")

        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "safe_rename", "source": str(src), "target": str(dst)},
            music_allowed_roots=[str(self.root)]
        )
        op_id = plan["operation_id"]
        transaction_engine.execute_folder_cleanup_apply(self.store, op_id, music_allowed_roots=[str(self.root)])

        rollback = transaction_engine.rollback_folder_cleanup(self.store, op_id, music_allowed_roots=[str(self.root)])
        self.assertTrue(rollback.get("ok"))
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())

    def test_folder_cleanup_refuses_allowed_root_itself(self):
        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "remove_empty", "source": str(self.music_dir)},
            music_allowed_roots=[str(self.music_dir)]
        )
        self.assertFalse(plan.get("ok"))
        self.assertEqual(plan.get("code"), "folder_cleanup_root_refused")

    def test_folder_cleanup_merge_refuses_missing_target_parent(self):
        src = self.music_dir / "old_album"
        dst = self.music_dir / "canonical_album"
        src.mkdir()
        dst.mkdir()
        (src / "Disc 2").mkdir()
        (src / "Disc 2" / "track2.mp3").write_bytes(b"audio content")

        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "merge_source_files", "source": str(src), "target": str(dst)},
            music_allowed_roots=[str(self.music_dir)]
        )
        self.assertFalse(plan.get("ok"))
        self.assertEqual(plan.get("code"), "folder_cleanup_target_parent_missing")
        self.assertFalse((dst / "Disc 2").exists())

    def test_folder_cleanup_apply_fails_when_planned_file_disappears(self):
        src = self.music_dir / "old_album"
        dst = self.music_dir / "canonical_album"
        src.mkdir()
        dst.mkdir()
        track = src / "track1.mp3"
        track.write_bytes(b"audio content")
        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "merge_source_files", "source": str(src), "target": str(dst)},
            music_allowed_roots=[str(self.music_dir)]
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        track.unlink()

        apply = transaction_engine.execute_folder_cleanup_apply(
            self.store,
            plan["operation_id"],
            music_allowed_roots=[str(self.music_dir)]
        )
        self.assertFalse(apply.get("ok"))
        self.assertEqual(apply.get("code"), "folder_cleanup_toctou_mismatch")
        self.assertFalse((dst / "track1.mp3").exists())


    def test_folder_cleanup_source_and_target_path_keys(self):
        src = self.music_dir / "old_src_path"
        dst = self.music_dir / "new_dst_path"
        src.mkdir()
        (src / "track1.mp3").write_bytes(b"audio content")

        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "safe_rename", "source_path": str(src), "target_path": str(dst)},
            music_allowed_roots=[str(self.music_dir)]
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]
        apply_res = transaction_engine.execute_folder_cleanup_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)]
        )
        self.assertTrue(apply_res.get("ok"), msg=apply_res.get("error"))
        self.assertTrue(dst.exists())
        self.assertFalse(src.exists())
        self.assertEqual(len(apply_res.get("moved_records", [])), 1)
        self.assertEqual(apply_res.get("changed_count"), 1)


if __name__ == "__main__":
    unittest.main()
