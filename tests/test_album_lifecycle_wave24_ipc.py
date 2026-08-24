import os
import sys
import tempfile
import unittest
import sqlite3
import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as _flask_app
from backend.transaction_engine import TransactionStore
from backend import transaction_engine


class TestWave24AlbumLifecycleIPC(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.music_dir = self.root / "music"
        self.staging_dir = self.root / "staging"
        self.quarantine_dir = self.root / "quarantine"
        self.db_path = self.root / "musiclibrary.blb"
        self.tx_dir = self.root / "transactions"
        self.tx_dir.mkdir(parents=True, exist_ok=True)
        self.store = TransactionStore(root=str(self.tx_dir))

        # Setup SQLite Beets DB
        con = sqlite3.connect(self.db_path)
        con.execute("""
            CREATE TABLE albums (
                id INTEGER PRIMARY KEY,
                album TEXT,
                artist TEXT,
                albumartist TEXT,
                year INTEGER,
                genre TEXT,
                artpath TEXT,
                mb_albumid TEXT,
                mb_releasegroupid TEXT,
                mb_artistid TEXT
            )
        """)
        con.execute("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY,
                album_id INTEGER,
                title TEXT,
                artist TEXT,
                albumartist TEXT,
                album TEXT,
                track INTEGER,
                disc INTEGER,
                year INTEGER,
                genre TEXT,
                format TEXT,
                path TEXT,
                mb_trackid TEXT,
                mb_albumid TEXT
            )
        """)

        # Insert sample album 1
        con.execute("""
            INSERT INTO albums (id, album, artist, albumartist, year, genre, artpath, mb_albumid, mb_releasegroupid)
            VALUES (1, 'Test Album', 'Test Artist', 'Test Artist', 2024, 'Rock', '', '11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222')
        """)
        self.item1_path = self.music_dir / "Test Artist" / "Test Album" / "01 - Track 1.mp3"
        self.item1_path.parent.mkdir(parents=True, exist_ok=True)
        self.item1_path.write_bytes(b"ID3TAG_TEST_AUDIO_BYTES_01")

        con.execute("""
            INSERT INTO items (id, album_id, title, artist, albumartist, album, track, disc, year, genre, format, path)
            VALUES (1, 1, 'Track 1', 'Test Artist', 'Test Artist', 'Test Album', 1, 1, 2024, 'Rock', 'MP3', ?)
        """, (str(self.item1_path),))

        con.commit()
        con.close()

    def tearDown(self):
        self.tmp_dir.cleanup()

    # ── 1. Album Artwork Tests ──────────────────────────────────────────────────

    def test_album_artwork_replace_base64_upload_and_db_update(self):
        # Valid 1x1 JPEG magic bytes
        jpeg_b64 = base64.b64encode(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x48\x00\x48\x00\x00\xff\xd9").decode("utf-8")
        plan_res = transaction_engine.create_album_artwork_plan(
            self.store,
            {
                "album_id": 1,
                "mode": "replace",
                "image_data_b64": jpeg_b64,
            },
            music_allowed_roots=[str(self.music_dir)],
            staging_allowed_roots=[str(self.staging_dir), str(self.quarantine_dir / "staging"), str(self.quarantine_dir)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertTrue(plan_res.get("ok"), f"Plan failed: {plan_res}")
        op_id = plan_res.get("operation_id")
        self.assertIsNotNone(op_id)

        apply_res = transaction_engine.execute_album_artwork_apply(
            self.store,
            op_id,
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertTrue(apply_res.get("ok"), f"Apply failed: {apply_res}")
        self.assertTrue(apply_res.get("mutated"))

        # Verify DB updated
        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT artpath FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertIsNotNone(row)
        self.assertTrue(row[0].endswith("albumart.jpg") or row[0].endswith("cover.jpg"))
        self.assertTrue(Path(row[0]).exists())

    def test_album_artwork_rejects_invalid_image_format(self):
        bad_b64 = base64.b64encode(b"NOT_AN_IMAGE_FILE").decode("utf-8")
        plan_res = transaction_engine.create_album_artwork_plan(
            self.store,
            {"album_id": 1, "mode": "replace", "image_data_b64": bad_b64},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertFalse(plan_res.get("ok"))
        self.assertEqual(plan_res.get("code"), "album_artwork_invalid_image_format")

    # ── 2. Album Relocation Tests ───────────────────────────────────────────────

    def test_album_relocation_rename_and_rollback(self):
        plan_res = transaction_engine.create_album_relocation_plan(
            self.store,
            {
                "album_id": 1,
                "mode": "rename",
                "new_artist": "Renamed Artist",
                "new_title": "Renamed Album",
            },
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan_res.get("ok"), f"Relocation plan failed: {plan_res}")
        op_id = plan_res.get("operation_id")

        apply_res = transaction_engine.execute_album_relocation_apply(
            self.store,
            op_id,
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(apply_res.get("ok"), f"Relocation apply failed: {apply_res}")

        # Check DB path updated
        con = sqlite3.connect(self.db_path)
        irow = con.execute("SELECT path FROM items WHERE id=1").fetchone()
        con.close()
        raw_p = irow[0].decode("utf-8") if isinstance(irow[0], bytes) else str(irow[0])
        new_path = Path(raw_p)
        self.assertTrue(new_path.exists())
        self.assertIn("Renamed Artist", str(new_path))
        self.assertIn("Renamed Album", str(new_path))

        # Test Rollback
        rb_res = transaction_engine.rollback_album_relocation(
            self.store,
            op_id,
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(rb_res.get("ok"), f"Rollback failed: {rb_res}")
        self.assertTrue(self.item1_path.exists())

    # ── 3. Album Metadata Tests ─────────────────────────────────────────────────

    def test_album_metadata_update_and_identity_conflict_protection(self):
        # Identity conflict check
        conflict_res = transaction_engine.create_album_metadata_plan(
            self.store,
            {
                "album_id": 1,
                "updates": {"mb_releasegroupid": "99999999-9999-9999-9999-999999999999"},
            },
            db_path=str(self.db_path),
        )
        self.assertFalse(conflict_res.get("ok"))
        self.assertEqual(conflict_res.get("code"), "album_metadata_identity_conflict")

        # Valid update
        plan_res = transaction_engine.create_album_metadata_plan(
            self.store,
            {
                "album_id": 1,
                "updates": {"album": "Updated Album Title", "year": 2025},
            },
            db_path=str(self.db_path),
        )
        self.assertTrue(plan_res.get("ok"), f"Metadata plan failed: {plan_res}")
        op_id = plan_res.get("operation_id")

        apply_res = transaction_engine.execute_album_metadata_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
        )
        self.assertTrue(apply_res.get("ok"), f"Metadata apply failed: {apply_res}")

        # Verify DB updated
        con = sqlite3.connect(self.db_path)
        arow = con.execute("SELECT album, year FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(arow[0], "Updated Album Title")
        self.assertEqual(arow[1], 2025)


if __name__ == "__main__":
    unittest.main()
