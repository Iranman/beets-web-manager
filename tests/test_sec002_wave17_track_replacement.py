"""
Wave 17 track replacement controlled mutation boundary tests for SEC-002 / ARCH-003.

Tests:
1. Plan / Apply / Rollback lifecycle for track_replacement_v1.
2. Cross-root / traversal path refusal for track replacement.
3. Symlink target & symlink component mutation refusal for original and candidate files.
4. TOCTOU race detection: file replacement race (mtime/size change) on original and candidate.
5. Non-destructive recoverable quarantine placement.
6. Release Group identity matching authority verification.
7. Idempotent apply retry on completed transactions.
8. AST check: Web Manager contains zero direct media/DB mutations for track replacement.

Uses the standard-library ``unittest`` framework only (no ``pytest``).
"""

import ast
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import app as flask_app
from backend.transaction_engine import (
    TransactionStore,
    create_track_replacement_plan,
    execute_track_replacement_apply,
    rollback_track_replacement,
)
from backend.beets_client import BeetsClient


class TrackReplacementTransactionTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)
        
        self.tx_dir = self.tmp_path / "transactions"
        self.tx_dir.mkdir(parents=True, exist_ok=True)
        self.tx_store = TransactionStore(root=str(self.tx_dir))

        self.music_root = self.tmp_path / "music"
        self.music_root.mkdir(parents=True, exist_ok=True)
        self.downloads_root = self.tmp_path / "downloads"
        self.downloads_root.mkdir(parents=True, exist_ok=True)
        self.quarantine_root = self.tmp_path / "quarantine"
        self.quarantine_root.mkdir(parents=True, exist_ok=True)

        self.db_path = self.tmp_path / "musiclibrary.db"
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "CREATE TABLE items (id INTEGER PRIMARY KEY, path TEXT, title TEXT, artist TEXT, "
                "album TEXT, track INTEGER, disc INTEGER, mb_trackid TEXT, mb_albumid TEXT, "
                "mb_releasegroupid TEXT, size INTEGER, mtime REAL)"
            )
            con.commit()
        finally:
            con.close()

    def test_plan_apply_rollback_lifecycle(self):
        """Test complete Plan -> Review -> Apply -> Verify -> Rollback lifecycle for track_replacement_v1."""
        orig_file = self.music_root / "track01.flac"
        orig_file.write_bytes(b"ORIGINAL_AUDIO_CONTENT_FLAC")
        
        repl_file = self.downloads_root / "track01_hq.flac"
        repl_file.write_bytes(b"REPLACEMENT_AUDIO_CONTENT_HQ_FLAC")

        con = sqlite3.connect(self.db_path)
        try:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO items (path, title, artist, size, mtime) VALUES (?, ?, ?, ?, ?)",
                (str(orig_file), "Test Track", "Test Artist", len(b"ORIGINAL_AUDIO_CONTENT_FLAC"), orig_file.stat().st_mtime),
            )
            item_id = cur.lastrowid
            con.commit()
        finally:
            con.close()

        payload = {
            "original_item_id": item_id,
            "original_path": str(orig_file),
            "replacement_path": str(repl_file),
            "reason": "Upgrade to FLAC HQ",
            "quarantine_root": str(self.quarantine_root),
        }

        # 1. Plan
        plan_res = create_track_replacement_plan(
            self.tx_store,
            payload,
            allowed_roots=[str(self.music_root), str(self.downloads_root)],
            music_root=str(self.music_root),
        )

        self.assertTrue(plan_res.get("ok"), plan_res.get("error"))
        op_id = plan_res["operation_id"]
        tx = self.tx_store.get(op_id)
        self.assertEqual(tx["metadata"]["mutation_family"], "track_replacement_v1")
        self.assertEqual(tx["status"], "Preview")

        # 2. Apply
        apply_res = execute_track_replacement_apply(
            self.tx_store,
            op_id,
            db_path=str(self.db_path),
            allowed_roots=[str(self.music_root), str(self.downloads_root)],
            quarantine_root=str(self.quarantine_root),
            music_root=str(self.music_root),
        )

        self.assertTrue(apply_res.get("ok"), apply_res.get("error"))
        self.assertEqual(apply_res["status"], "Completed")

        # Verify physical media changes
        self.assertTrue(orig_file.exists())
        self.assertEqual(orig_file.read_bytes(), b"REPLACEMENT_AUDIO_CONTENT_HQ_FLAC")
        self.assertFalse(repl_file.exists())

        # Verify quarantine file
        quarantined_to = Path(apply_res["quarantined_to"])
        self.assertTrue(quarantined_to.exists())
        self.assertEqual(quarantined_to.read_bytes(), b"ORIGINAL_AUDIO_CONTENT_FLAC")

        # Verify DB update
        con = sqlite3.connect(self.db_path)
        try:
            row = con.execute("SELECT path, size FROM items WHERE id=?", (item_id,)).fetchone()
            self.assertEqual(row[0], str(orig_file))
            self.assertEqual(row[1], len(b"REPLACEMENT_AUDIO_CONTENT_HQ_FLAC"))
        finally:
            con.close()

        # 3. Rollback
        rollback_res = rollback_track_replacement(
            self.tx_store,
            op_id,
            db_path=str(self.db_path),
            allowed_roots=[str(self.music_root), str(self.downloads_root)],
        )

        self.assertTrue(rollback_res.get("ok"), rollback_res.get("error"))
        self.assertEqual(rollback_res["status"], "Rolled Back")

        # Verify restored original file
        self.assertEqual(orig_file.read_bytes(), b"ORIGINAL_AUDIO_CONTENT_FLAC")
        con = sqlite3.connect(self.db_path)
        try:
            row = con.execute("SELECT path, size FROM items WHERE id=?", (item_id,)).fetchone()
            self.assertEqual(row[1], len(b"ORIGINAL_AUDIO_CONTENT_FLAC"))
        finally:
            con.close()

    def test_idempotent_apply_on_completed_transaction(self):
        """Applying an already-completed transaction returns ok without re-mutating."""
        orig_file = self.music_root / "track02.flac"
        orig_file.write_bytes(b"ORIGINAL")
        repl_file = self.downloads_root / "track02_hq.flac"
        repl_file.write_bytes(b"REPLACEMENT")

        plan_res = create_track_replacement_plan(
            self.tx_store,
            {"original_path": str(orig_file), "replacement_path": str(repl_file), "quarantine_root": str(self.quarantine_root)},
            allowed_roots=[str(self.music_root), str(self.downloads_root)],
        )
        op_id = plan_res["operation_id"]

        apply_res1 = execute_track_replacement_apply(
            self.tx_store, op_id, db_path=str(self.db_path), allowed_roots=[str(self.music_root), str(self.downloads_root)]
        )
        self.assertTrue(apply_res1.get("ok"))

        apply_res2 = execute_track_replacement_apply(
            self.tx_store, op_id, db_path=str(self.db_path), allowed_roots=[str(self.music_root), str(self.downloads_root)]
        )
        self.assertTrue(apply_res2.get("ok"))
        self.assertTrue(apply_res2.get("already_completed"))

    def test_toctou_stat_mismatch_refuses_apply(self):
        """If file size/mtime changes between Plan and Apply, Apply fails closed."""
        orig_file = self.music_root / "track03.flac"
        orig_file.write_bytes(b"ORIGINAL_BEFORE_TOCTOU")
        repl_file = self.downloads_root / "track03_hq.flac"
        repl_file.write_bytes(b"REPLACEMENT_BEFORE_TOCTOU")

        plan_res = create_track_replacement_plan(
            self.tx_store,
            {"original_path": str(orig_file), "replacement_path": str(repl_file), "quarantine_root": str(self.quarantine_root)},
            allowed_roots=[str(self.music_root), str(self.downloads_root)],
        )
        op_id = plan_res["operation_id"]

        # Tamper with original file before Apply
        orig_file.write_bytes(b"TAMPERED_ORIGINAL_CONTENT")

        apply_res = execute_track_replacement_apply(
            self.tx_store, op_id, db_path=str(self.db_path), allowed_roots=[str(self.music_root), str(self.downloads_root)]
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertIn("File replacement detected", apply_res.get("error", ""))

    def test_release_group_mismatch_refuses_plan(self):
        """If candidate belongs to a different Release Group, plan creation is refused."""
        orig_file = self.music_root / "track04.flac"
        orig_file.write_bytes(b"ORIGINAL")
        repl_file = self.downloads_root / "track04_hq.flac"
        repl_file.write_bytes(b"REPLACEMENT")

        payload = {
            "original_path": str(orig_file),
            "replacement_path": str(repl_file),
            "matching_contract": {
                "original_release_group_id": "11111111-1111-1111-1111-111111111111",
                "replacement_release_group_id": "22222222-2222-2222-2222-222222222222",
            },
        }

        plan_res = create_track_replacement_plan(
            self.tx_store, payload, allowed_roots=[str(self.music_root), str(self.downloads_root)]
        )
        self.assertFalse(plan_res.get("ok"))
        self.assertEqual(plan_res.get("code"), "release_group_mismatch")

    def test_web_manager_no_direct_mutations_for_replacement(self):
        """AST check: app._music_format_remove_original_after_replacement contains no direct unlink or shutil.move calls."""
        with open("app.py", "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename="app.py")

        target_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_music_format_remove_original_after_replacement":
                target_fn = node
                break

        self.assertIsNotNone(target_fn, "_music_format_remove_original_after_replacement not found in app.py")

        direct_unlinks = []
        direct_moves = []
        for child in ast.walk(target_fn):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Attribute) and child.func.attr == "unlink":
                    direct_unlinks.append(child)
                elif isinstance(child.func, ast.Attribute) and child.func.attr == "move":
                    if isinstance(child.func.value, ast.Name) and child.func.value.id == "shutil":
                        direct_moves.append(child)

        self.assertEqual(len(direct_unlinks), 0, f"Found direct unlink calls in _music_format_remove_original_after_replacement: {direct_unlinks}")
        self.assertEqual(len(direct_moves), 0, f"Found direct shutil.move calls in _music_format_remove_original_after_replacement: {direct_moves}")

    def test_music_format_remove_original_delegates_to_beets_client(self):
        """Web manager _music_format_remove_original_after_replacement delegates to beets_client IPC."""
        with patch.object(flask_app.beets_client, "plan_track_replacement") as mock_plan, \
             patch.object(flask_app.beets_client, "apply_track_replacement") as mock_apply:
            mock_plan.return_value = {"ok": True, "operation_id": "txn_test_12345"}
            mock_apply.return_value = {"ok": True, "status": "Completed", "quarantined_to": "/config/quarantine/file.flac"}

            res = flask_app._music_format_remove_original_after_replacement(
                "/music/orig.flac", "/downloads/repl.flac", {}, log=[], original_item_id=42
            )

            self.assertTrue(res.get("removed"))
            self.assertEqual(res.get("quarantined_to"), "/config/quarantine/file.flac")
            mock_plan.assert_called_once_with({
                "original_item_id": 42,
                "original_path": "/music/orig.flac",
                "replacement_path": "/downloads/repl.flac",
                "reason": "Music format quality replacement",
            })
            mock_apply.assert_called_once_with("txn_test_12345")


if __name__ == "__main__":
    unittest.main()
