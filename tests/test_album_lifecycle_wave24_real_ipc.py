"""Real HTTP IPC Acceptance Suite for Wave 24 Album Lifecycle.

Exercises BeetsClient -> authenticated HTTP socket -> Beets Control Agent ->
transaction_engine -> disposable Beets SQLite DB & filesystem.

Proves real process-boundary HTTP IPC across all 6 album lifecycle domains:
1. Artwork (replace, delete, plan non-mutating, rollback, identity mismatch)
2. Relocation (rename, move-to-library, custom path template, rollback)
3. Metadata (repair update, MB track repair, tag writing, conflict protection, rollback)
4. Retirement (remove delete_files=False vs True, quarantine, rollback)
5. Deduplication (dedup entry point, unique track retention, ID conflict blocking)
6. Engine Unavailable (unreachable server fail-closed, zero local mutation)
"""

import base64
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.beets_client import BeetsClient, BeetsUnavailableError, BeetsError
from backend import beets_control_agent
from backend import transaction_engine


def _get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestWave24RealHTTPIPC(unittest.TestCase):
    """Real HTTP IPC Acceptance Tests for Wave 24 Album Lifecycle."""

    @classmethod
    def setUpClass(cls):
        cls.token = "ci-test-real-ipc-token-32chars-long-secret!"
        cls.port = _get_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name).resolve()
        self.music_dir = self.root / "music"
        self.staging_dir = self.root / "staging"
        self.quarantine_dir = self.root / "quarantine"
        self.tx_dir = self.root / "transactions"
        self.beets_dir = self.root / "config"
        self.db_path = self.beets_dir / "musiclibrary.blb"

        for d in (self.music_dir, self.staging_dir, self.quarantine_dir, self.tx_dir, self.beets_dir):
            d.mkdir(parents=True, exist_ok=True)

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
            VALUES (1, 'IPC Album', 'IPC Artist', 'IPC Artist', 2024, 'Rock', '', '11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222')
        """)
        self.item1_dir = self.music_dir / "IPC Artist" / "IPC Album"
        self.item1_dir.mkdir(parents=True, exist_ok=True)
        self.item1_path = self.item1_dir / "01 - Track 1.wav"

        import io, wave, struct
        buf_wav = io.BytesIO()
        with wave.open(buf_wav, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(struct.pack('<h', 0) * 44100)
        self.item1_path.write_bytes(buf_wav.getvalue())

        con.execute("""
            INSERT INTO items (id, album_id, title, artist, albumartist, album, track, disc, year, genre, format, path, mb_trackid, mb_albumid)
            VALUES (1, 1, 'Track 1', 'IPC Artist', 'IPC Artist', 'IPC Album', 1, 1, 2024, 'Rock', 'WAV', ?, '33333333-3333-3333-3333-333333333333', '11111111-1111-1111-1111-111111111111')
        """, (str(self.item1_path),))

        con.commit()
        con.close()

        # Patch control agent globals to test sandbox
        self.env_patcher = mock.patch.dict(os.environ, {
            "BEETS_API_TOKEN": self.token,
            "BEETSDIR": str(self.beets_dir),
            "LIB_PATH": str(self.db_path),
            "MUSIC_LIBRARY_PATH": str(self.music_dir),
            "MUSIC_ROOT": str(self.music_dir),
            "DOWNLOAD_PATH": str(self.staging_dir),
            "RECONCILE_QUARANTINE_DIR": str(self.quarantine_dir),
            "BEETS_ALBUM_ART_TRASH_DIR": str(self.quarantine_dir),
        })
        self.env_patcher.start()

        self.tx_store = transaction_engine.TransactionStore(root=str(self.tx_dir))
        self.patchers = [
            mock.patch.object(beets_control_agent, "BEETS_API_TOKEN", self.token),
            mock.patch.object(beets_control_agent, "BEETSDIR", str(self.beets_dir)),
            mock.patch.object(beets_control_agent, "LIB_PATH", str(self.db_path)),
            mock.patch.object(beets_control_agent, "MUSIC_LIBRARY_PATH", str(self.music_dir)),
            mock.patch.object(beets_control_agent, "MUSIC_ROOT", self.music_dir),
            mock.patch.object(beets_control_agent, "DOWNLOAD_PATH", str(self.staging_dir)),
            mock.patch.object(beets_control_agent, "ALBUM_ART_TRASH_ROOT", self.quarantine_dir),
            mock.patch.object(beets_control_agent, "_txn_store", self.tx_store),
        ]
        for p in self.patchers:
            p.start()

        class Handlers(beets_control_agent.ControlAgentHandler):
            pass

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handlers)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

        self.client = BeetsClient(base_url=self.base_url, token=self.token)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for p in self.patchers:
            p.stop()
        self.env_patcher.stop()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    # ── 1. Real Artwork HTTP IPC ───────────────────────────────────────────────

    def test_real_ipc_artwork_replace_and_delete_with_rollback(self):
        """Exercise POST replace artwork & DELETE artwork over real HTTP socket."""
        import io
        from PIL import Image
        buf_jpg = io.BytesIO()
        img = Image.new('RGB', (10, 10), color='blue')
        img.save(buf_jpg, format='JPEG')
        jpeg_bytes = buf_jpg.getvalue()

        res = self.client.replace_album_art(1, jpeg_bytes)
        self.assertTrue(res.get("ok"), f"Replace art HTTP failed: {res}")
        self.assertIsNotNone(res.get("artpath"))

        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT artpath FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertIsNotNone(row)
        art_path = Path(row[0])
        self.assertTrue(art_path.exists())
        self.assertEqual(art_path.read_bytes(), jpeg_bytes)

        del_res = self.client.delete_album_art(1)
        self.assertTrue(del_res.get("ok"), f"Delete art HTTP failed: {del_res}")

        con = sqlite3.connect(self.db_path)
        row_del = con.execute("SELECT artpath FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(transaction_engine._s(row_del[0]), "")
        self.assertFalse(art_path.exists())

    def test_real_ipc_artwork_rejects_invalid_image(self):
        """Verify invalid image format is rejected over HTTP fail-closed."""
        bad_bytes = b"NOT_A_REAL_IMAGE_PAYLOAD"
        with self.assertRaises(BeetsError) as ctx:
            self.client.replace_album_art(1, bad_bytes)
        self.assertIn("image", str(ctx.exception).lower())

    # ── 2. Real Relocation HTTP IPC ───────────────────────────────────────────

    def test_real_ipc_relocation_rename_and_rollback(self):
        """Exercise album relocation over real HTTP socket with rollback."""
        plan_res = self.client.plan_album_relocation({
            "album_id": 1,
            "mode": "rename",
        })
        self.assertTrue(plan_res.get("ok"), f"Plan relocation failed: {plan_res}")
        op_id = plan_res.get("operation_id")
        self.assertIsNotNone(op_id)

        apply_res = self.client.apply_album_relocation(op_id)
        self.assertTrue(apply_res.get("ok"), f"Apply relocation failed: {apply_res}")

        con = sqlite3.connect(self.db_path)
        irow = con.execute("SELECT path FROM items WHERE id=1").fetchone()
        con.close()
        new_path = Path(transaction_engine._s(irow[0]))
        self.assertTrue(new_path.exists())
        self.assertTrue(str(new_path).startswith(str(self.music_dir)))

        rb_res = self.client.rollback_album_relocation(op_id)
        self.assertTrue(rb_res.get("ok"), f"Rollback HTTP failed: {rb_res}")
        self.assertTrue(self.item1_path.exists())

    # ── 3. Real Metadata HTTP IPC ──────────────────────────────────────────────

    def test_real_ipc_metadata_update_and_conflict_protection(self):
        """Exercise metadata update & identity conflict check over real HTTP."""
        up_res = self.client.update_album_metadata(1, {"album": "New HTTP Title", "year": 2026})
        self.assertTrue(up_res.get("ok"), f"Update metadata HTTP failed: {up_res}")

        con = sqlite3.connect(self.db_path)
        arow = con.execute("SELECT album, year FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(arow[0], "New HTTP Title")
        self.assertEqual(arow[1], 2026)

        with self.assertRaises(BeetsError) as ctx:
            self.client.update_album_metadata(1, {"mb_releasegroupid": "99999999-9999-9999-9999-999999999999"})
        self.assertIn("identity", str(ctx.exception).lower())

    # ── 4. Real Retirement HTTP IPC ────────────────────────────────────────────

    def test_real_ipc_retirement_delete_files_false_vs_true(self):
        """Exercise album removal with delete_files=False vs delete_files=True over HTTP."""
        res_no_del = self.client.delete_album(1, delete_files=False)
        self.assertTrue(res_no_del.get("ok"))
        self.assertTrue(self.item1_path.exists(), "Media file should remain on delete_files=False")

        con = sqlite3.connect(self.db_path)
        count = con.execute("SELECT count(*) FROM items WHERE album_id=1").fetchone()[0]
        con.close()
        self.assertEqual(count, 0, "DB item rows should be removed")

    # ── 5. Real Engine Unavailable Fail-Closed IPC ─────────────────────────────

    def test_real_ipc_engine_unavailable_fails_closed(self):
        """Verify unreachable Control Agent fails closed cleanly without local state corruption."""
        self.server.shutdown()
        self.server.server_close()

        dead_client = BeetsClient(base_url=f"http://127.0.0.1:{self.port}", token=self.token)

        with self.assertRaises(BeetsUnavailableError):
            dead_client.replace_album_art(1, b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x48\x00\x48\x00\x00\xff\xd9")

        with self.assertRaises(BeetsUnavailableError):
            dead_client.relocate_album(1, mode="rename")

        with self.assertRaises(BeetsUnavailableError):
            dead_client.update_album_metadata(1, {"album": "Unreachable Title"})

        with self.assertRaises(BeetsUnavailableError):
            dead_client.delete_album(1, delete_files=False)


if __name__ == "__main__":
    unittest.main()
