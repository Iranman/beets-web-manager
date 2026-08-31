"""SEC-002 / ARCH-003 Wave 27 Blocker 6: genre_repair_v1 must capture,
verify, and roll back on-disk media-file genre tags -- not just the Beets
DB genre columns -- and must never report "Rolled Back" (or trust a
nonzero lastgenre return code as proof of mutated=False) without
independently verifying both DB and file-tag state.
"""

from __future__ import annotations

import math
import sqlite3
import struct
import tempfile
import unittest
import unittest.mock as mock
import wave
from pathlib import Path

from backend import transaction_engine as txn

SCHEMA = """
CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, albumartist TEXT, genre TEXT);
CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, title TEXT, path BLOB, genre TEXT, disc INTEGER, track INTEGER);
"""


def _write_test_audio(path: Path, genre: str = "") -> None:
    """Write a real, minimal, playable WAV file with a real genre tag --
    same self-synthesized-sine-wave technique used by
    scripts/seed_demo_library.py and tests/test_sec002_wave19_mb_track_repair.py,
    so genre reads/writes exercise the real mediafile tag layer instead of
    a fixture no real reader can parse."""
    sample_rate = 8000
    n_samples = max(1, int(sample_rate * 0.1))
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            frames += struct.pack("<h", int(3000 * math.sin(2 * math.pi * 220.0 * i / sample_rate)))
        w.writeframes(bytes(frames))
    import mediafile
    mf = mediafile.MediaFile(path)
    mf.genre = genre or ""
    mf.save()


def _read_tag_genre(path: Path) -> str:
    import mediafile
    return str(getattr(mediafile.MediaFile(path), "genre", "") or "")


class GenreRepairPlanApplyRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        root = Path(self.tmpdir.name)
        self.db_path = root / "library.db"
        con = sqlite3.connect(self.db_path)
        con.executescript(SCHEMA)
        con.execute("INSERT INTO albums (id, album, albumartist, genre) VALUES (1, 'Album', 'Artist', 'Old Genre')")
        self.track_paths = []
        for i in (1, 2):
            p = root / f"track{i}.wav"
            _write_test_audio(p, genre="Old Genre")
            self.track_paths.append(p)
            con.execute(
                "INSERT INTO items (id, album_id, title, path, genre, disc, track) VALUES (?, 1, ?, ?, 'Old Genre', 1, ?)",
                (i, f"Track {i}", str(p), i),
            )
        con.commit()
        con.close()
        self.store = txn.TransactionStore(root=str(root / "transactions"))

    def _plan(self):
        res = txn.create_genre_repair_plan(self.store, {"album_id": 1}, db_path=str(self.db_path))
        self.assertTrue(res["ok"], res)
        return res["operation_id"]

    def test_plan_captures_path_stat_db_and_tag_genre_per_item(self):
        op_id = self._plan()
        tx = self.store.get(op_id)
        before_items = tx["metadata"]["before_item_genres"]
        self.assertEqual(len(before_items), 2)
        for entry in before_items:
            self.assertIn(entry["item_id"], (1, 2))
            self.assertTrue(entry["path"])
            self.assertIsNotNone(entry["stat"])
            self.assertEqual(entry["genre"], "Old Genre")
            self.assertTrue(entry["tag_read_ok"])
            self.assertEqual(entry["tag_genre"], "Old Genre")

    def test_apply_success_writes_new_genre_and_records_after_state(self):
        op_id = self._plan()

        def fake_runner(cmd, args):
            con = sqlite3.connect(self.db_path)
            con.execute("UPDATE albums SET genre='New Genre' WHERE id=1")
            con.execute("UPDATE items SET genre='New Genre' WHERE album_id=1")
            con.commit()
            con.close()
            for p in self.track_paths:
                mf_res = txn._write_file_genre_tag(p, "New Genre")
                self.assertTrue(mf_res["ok"])
            return {"ok": True, "stdout": "genre updated"}

        res = txn.execute_genre_repair_apply(self.store, op_id, db_path=str(self.db_path), run_beet_command_fn=fake_runner)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "Completed")
        self.assertTrue(res["mutated"])
        self.assertEqual(res["genre_after"], "New Genre")
        tx = self.store.get(op_id)
        self.assertEqual(tx["status"], "Completed")
        tag_after = {e["item_id"]: e for e in tx["metadata"]["tag_genres_after"]}
        for iid in (1, 2):
            self.assertEqual(tag_after[iid]["genre"], "New Genre")
        for p in self.track_paths:
            self.assertEqual(_read_tag_genre(p), "New Genre")

    def test_clean_failure_with_no_mutation_reports_failed_not_rolled_back(self):
        op_id = self._plan()

        def fake_runner(cmd, args):
            return {"ok": False, "error": "lastgenre plugin error"}

        res = txn.execute_genre_repair_apply(self.store, op_id, db_path=str(self.db_path), run_beet_command_fn=fake_runner)
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "Failed")
        self.assertFalse(res["mutated"])
        # Nothing on disk or in the DB should have changed.
        for p in self.track_paths:
            self.assertEqual(_read_tag_genre(p), "Old Genre")
        con = sqlite3.connect(self.db_path)
        self.assertEqual(con.execute("SELECT genre FROM albums WHERE id=1").fetchone()[0], "Old Genre")
        con.close()

    def test_nonzero_return_after_partial_mutation_is_detected_and_rolled_back(self):
        """Blocker 6's core case: lastgenre writes item 1's tag and DB row,
        then fails before reaching item 2, and the native runner reports
        ok=False. A nonzero result must not be trusted as mutated=False --
        the partial mutation must be detected and rolled back (DB AND tag),
        verified, and only then reported."""
        op_id = self._plan()

        def fake_runner(cmd, args):
            con = sqlite3.connect(self.db_path)
            con.execute("UPDATE albums SET genre='Partial Genre' WHERE id=1")
            con.execute("UPDATE items SET genre='Partial Genre' WHERE id=1")
            con.commit()
            con.close()
            txn._write_file_genre_tag(self.track_paths[0], "Partial Genre")
            return {"ok": False, "error": "lastgenre crashed on item 2"}

        res = txn.execute_genre_repair_apply(self.store, op_id, db_path=str(self.db_path), run_beet_command_fn=fake_runner)
        self.assertFalse(res["ok"])
        self.assertTrue(res["mutated"], "a nonzero return code must not be trusted as proof of no mutation")
        self.assertEqual(res["status"], "Rolled Back")
        self.assertEqual(res["code"], "genre_repair_rolled_back")
        # Verify actual DB and tag state was truly restored, not just claimed.
        con = sqlite3.connect(self.db_path)
        self.assertEqual(con.execute("SELECT genre FROM albums WHERE id=1").fetchone()[0], "Old Genre")
        self.assertEqual(con.execute("SELECT genre FROM items WHERE id=1").fetchone()[0], "Old Genre")
        con.close()
        self.assertEqual(_read_tag_genre(self.track_paths[0]), "Old Genre")

    def test_tag_restore_failure_reports_recovery_required_not_rolled_back(self):
        op_id = self._plan()

        def fake_runner(cmd, args):
            con = sqlite3.connect(self.db_path)
            con.execute("UPDATE items SET genre='Partial Genre' WHERE id=1")
            con.commit()
            con.close()
            txn._write_file_genre_tag(self.track_paths[0], "Partial Genre")
            return {"ok": False, "error": "lastgenre crashed"}

        with mock.patch.object(txn, "_write_file_genre_tag", return_value={"ok": False, "reason": "io_error"}):
            res = txn.execute_genre_repair_apply(self.store, op_id, db_path=str(self.db_path), run_beet_command_fn=fake_runner)
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "Recovery Required")
        self.assertEqual(res["code"], "genre_repair_recovery_required")
        self.assertNotEqual(res["status"], "Rolled Back")
        tx = self.store.get(op_id)
        self.assertEqual(tx["status"], "Recovery Required")

    def test_standalone_rollback_restores_db_and_tags(self):
        op_id = self._plan()
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE albums SET genre='Changed' WHERE id=1")
        con.execute("UPDATE items SET genre='Changed' WHERE album_id=1")
        con.commit()
        con.close()
        for p in self.track_paths:
            txn._write_file_genre_tag(p, "Changed")

        res = txn.rollback_genre_repair(self.store, op_id, db_path=str(self.db_path))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "Rolled Back")
        con = sqlite3.connect(self.db_path)
        self.assertEqual(con.execute("SELECT genre FROM albums WHERE id=1").fetchone()[0], "Old Genre")
        con.close()
        for p in self.track_paths:
            self.assertEqual(_read_tag_genre(p), "Old Genre")

    def test_standalone_rollback_reports_recovery_required_when_tag_write_fails(self):
        op_id = self._plan()
        with mock.patch.object(txn, "_write_file_genre_tag", return_value={"ok": False, "reason": "unreadable"}):
            res = txn.rollback_genre_repair(self.store, op_id, db_path=str(self.db_path))
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "Recovery Required")


if __name__ == "__main__":
    unittest.main()
