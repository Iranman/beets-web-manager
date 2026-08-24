"""Real HTTP IPC Acceptance Suite for Wave 24 Album Lifecycle.

Exercises BeetsClient -> actual HTTP socket -> ThreadingHTTPServer running
the real ControlAgentHandler -> transaction_engine -> a disposable Beets
SQLite DB & filesystem under a temp directory. This is genuine process-
boundary HTTP IPC: a real socket, real request parsing, real bearer-token
auth, and the real outbound-URL SSRF policy (backend/security.py) sitting in
front of every request -- nothing here calls transaction_engine or
ControlAgentHandler methods directly in-process.

Wave 24 final review round 3, section 2: the SSRF policy correctly rejects
an unallowlisted loopback destination by default. Rather than weakening that
policy for tests, each test's own ephemeral port is added to
BEETS_OUTBOUND_ALLOWLIST for the duration of the test only (setUp/tearDown),
using the project's real, already-supported allowlist mechanism
(parse_outbound_allowlist() / BEETS_OUTBOUND_ALLOWLIST) -- exact host:port
entries only, restored to the previous environment afterward. See
test_ssrf_allowlist_is_exact_not_broad below for the negative-port proof
that this does not broaden the policy beyond the exact test port.

Coverage across the 6 album-lifecycle-adjacent domains:
1. Artwork (replace, delete/quarantine, rollback, RGID conflict, invalid
   image fail-closed, Plan non-mutation, DB artpath trust-root regression)
2. Relocation (rename, move-to-library, rollback, native-Beets custom path
   template proof)
3. Metadata (repair update, identity conflict, tag readback, rollback
   restores DB + tags) and MB track repair (plan/apply/rollback,
   LIB_PATH/db_path regression)
4. Retirement (remove delete_files=False vs True, rollback)
5. Deduplication (album_maintenance_v1 "deduplicate" mode: real removal,
   identity-mismatch rejection, hardlink safety -- see the module docstring
   note on divided responsibility with tests/test_dedup_acoustid_fingerprint.py
   for the AcoustID/Recording-ID safety properties, which live in app.py's
   own route logic, not in this engine-level mode)
6. Engine Unavailable (unreachable server fail-closed, zero local mutation,
   across every domain above)

Division of responsibility note (dedup): album_maintenance_v1's
"deduplicate" mode is a safe-deletion primitive -- given a caller-supplied
list of item ids to remove, it independently re-verifies each id actually
belongs to the stated album and that every path is inside the configured
roots with no symlink components, then deletes/quarantines exactly those
items and nothing else. It does NOT itself decide which tracks are
duplicates: AcoustID fingerprint cross-checking and Recording-ID-based
"is this really the same recording" evidence is production logic living in
app.py's own /api/albums/<id>/deduplicate route (see
tests/test_dedup_acoustid_fingerprint.py for that coverage), not something
this engine boundary can meaningfully re-prove on its own.
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
from backend import security as backend_security
from backend import transaction_engine


def _get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _real_jpeg_bytes(size=(10, 10), color="blue"):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


def _real_wav_bytes(freq=0, duration=1.0, rate=44100):
    import io
    import wave
    import struct
    import math
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        n = int(rate * duration)
        frames = bytearray()
        for i in range(n):
            val = int(3000 * math.sin(2 * math.pi * freq * i / rate)) if freq else 0
            frames += struct.pack("<h", val)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


class TestWave24RealHTTPIPC(unittest.TestCase):
    """Real HTTP IPC Acceptance Tests for Wave 24 Album Lifecycle."""

    @classmethod
    def setUpClass(cls):
        cls.token = "ci-test-real-ipc-token-32chars-long-secret!"

    def setUp(self):
        self.port = _get_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"

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
                mb_albumid TEXT,
                length REAL
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
        self.item1_path.write_bytes(_real_wav_bytes(freq=220))
        # Give the fixture file real tags matching the DB row -- a file
        # with no tags at all (DB says "IPC Artist", file says nothing) is
        # an internally inconsistent fixture that makes tag-rollback tests
        # meaningless (there is no real "original tag value" to roll back
        # to). Same fix as the engine-unit test suite's identical bug.
        import mediafile as _mediafile_fixture
        _mf_fixture = _mediafile_fixture.MediaFile(str(self.item1_path))
        _mf_fixture.title = "Track 1"
        _mf_fixture.artist = "IPC Artist"
        _mf_fixture.save()

        con.execute("""
            INSERT INTO items (id, album_id, title, artist, albumartist, album, track, disc, year, genre, format, path, mb_trackid, mb_albumid, length)
            VALUES (1, 1, 'Track 1', 'IPC Artist', 'IPC Artist', 'IPC Album', 1, 1, 2024, 'Rock', 'WAV', ?, '33333333-3333-3333-3333-333333333333', '11111111-1111-1111-1111-111111111111', 1.0)
        """, (str(self.item1_path),))

        con.commit()
        con.close()

        # SEC-002 / ARCH-003 Wave 24 final review round 3, section 2: allow
        # exactly this test's own ephemeral loopback port, and nothing else
        # -- the real, already-supported allowlist mechanism, not a policy
        # weakening. Active before the first BeetsClient request; restored
        # in tearDown.
        self.env_patcher = mock.patch.dict(os.environ, {
            "BEETS_API_TOKEN": self.token,
            "BEETSDIR": str(self.beets_dir),
            "LIB_PATH": str(self.db_path),
            "MUSIC_LIBRARY_PATH": str(self.music_dir),
            "MUSIC_ROOT": str(self.music_dir),
            "DOWNLOAD_PATH": str(self.staging_dir),
            "DOWNLOADS_ROOT": str(self.staging_dir),
            "STAGING_ROOT": str(self.staging_dir),
            "RECONCILE_QUARANTINE_DIR": str(self.quarantine_dir),
            "BEETS_ALBUM_ART_TRASH_DIR": str(self.quarantine_dir),
            "BEETS_OUTBOUND_ALLOWLIST": f"127.0.0.1:{self.port}",
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

    # ── helpers ─────────────────────────────────────────────────────────────

    def _db(self):
        return sqlite3.connect(self.db_path)

    def _insert_item(self, item_id, path, *, track=2, disc=1, title="Track 2",
                      mb_trackid="", mb_albumid="11111111-1111-1111-1111-111111111111"):
        con = self._db()
        con.execute("""
            INSERT INTO items (id, album_id, title, artist, albumartist, album, track, disc, year, genre, format, path, mb_trackid, mb_albumid)
            VALUES (?, 1, ?, 'IPC Artist', 'IPC Artist', 'IPC Album', ?, ?, 2024, 'Rock', 'WAV', ?, ?, ?)
        """, (item_id, title, track, disc, str(path), mb_trackid, mb_albumid))
        con.commit()
        con.close()

    # ── SSRF / outbound allowlist ─────────────────────────────────────────────

    def test_ssrf_allowlist_is_exact_not_broad(self):
        """The allowlist grants exactly this test's port, not the whole of
        127.0.0.0/8 or 127.0.0.1 on any port."""
        # The allowlisted port accepts.
        res = self.client.get_album(1)
        self.assertIsNotNone(res)

        # A DIFFERENT loopback port (not allowlisted) must still be
        # rejected, proving the fix did not broaden the policy.
        other_port = _get_free_port()
        self.assertNotEqual(other_port, self.port)
        other_client = BeetsClient(base_url=f"http://127.0.0.1:{other_port}", token=self.token)
        with self.assertRaises(BeetsUnavailableError) as ctx:
            other_client.get_album(1)
        # The underlying cause must be the SSRF policy, not just "nothing
        # is listening there" (that would still raise, but for the wrong
        # reason -- confirm no server is bound there AND that the outbound
        # policy itself would reject it directly).
        with self.assertRaises(backend_security.OutboundPolicyError):
            backend_security.validate_outbound_url(f"http://127.0.0.1:{other_port}/albums/1")

    # ── 1. Real Artwork HTTP IPC ───────────────────────────────────────────────

    def test_real_ipc_artwork_replace_and_delete_with_rollback(self):
        """Exercise POST replace artwork & DELETE artwork over real HTTP socket."""
        jpeg_bytes = _real_jpeg_bytes(color="blue")

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
        self.assertTrue(str(art_path).startswith(str(self.music_dir)), "artpath must be inside configured music root")

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

    def test_real_ipc_artwork_plan_is_non_mutating(self):
        """Plan must not touch the filesystem or DB, even over real HTTP --
        only Apply (implicit inside replace_album_art) may."""
        before_files = {p.relative_to(self.quarantine_dir) for p in self.quarantine_dir.rglob("*")} if self.quarantine_dir.exists() else set()
        con = sqlite3.connect(self.db_path)
        before_artpath = con.execute("SELECT artpath FROM albums WHERE id=1").fetchone()[0]
        con.close()

        jpeg_b64 = base64.b64encode(_real_jpeg_bytes(color="green")).decode("utf-8")
        plan_res = self.client.plan_album_artwork({"album_id": 1, "mode": "replace", "image_data_b64": jpeg_b64})
        self.assertTrue(plan_res.get("ok"), f"Plan failed: {plan_res}")

        after_files = {p.relative_to(self.quarantine_dir) for p in self.quarantine_dir.rglob("*")} if self.quarantine_dir.exists() else set()
        self.assertEqual(before_files, after_files, "Plan must not create/modify quarantine directory contents")
        con = sqlite3.connect(self.db_path)
        after_artpath = con.execute("SELECT artpath FROM albums WHERE id=1").fetchone()[0]
        con.close()
        self.assertEqual(before_artpath, after_artpath, "Plan must not mutate the DB")

    def test_real_ipc_artwork_rgid_conflict_blocks_replace(self):
        """A mismatched expected_mb_releasegroupid must block the replace
        over real HTTP, not just in-process."""
        jpeg_b64 = base64.b64encode(_real_jpeg_bytes(color="red")).decode("utf-8")
        with self.assertRaises(BeetsError) as ctx:
            self.client.plan_album_artwork({
                "album_id": 1, "mode": "replace", "image_data_b64": jpeg_b64,
                "expected_mb_releasegroupid": "99999999-9999-9999-9999-999999999999",
            })
        self.assertIn("release group", str(ctx.exception).lower())

    def test_real_ipc_artwork_replace_rollback_restores_file_and_artpath(self):
        """Rollback after a real HTTP replace restores the original artwork
        file bytes and the original (empty) artpath."""
        first_jpeg = _real_jpeg_bytes(color="blue")
        first_op = self.client.plan_album_artwork({
            "album_id": 1, "mode": "replace",
            "image_data_b64": base64.b64encode(first_jpeg).decode("utf-8"),
        })
        self.assertTrue(first_op.get("ok"))
        apply1 = self.client.apply_album_artwork(first_op["operation_id"])
        self.assertTrue(apply1.get("ok"), apply1)

        rb_res = self.client.rollback_album_artwork(first_op["operation_id"])
        self.assertTrue(rb_res.get("ok"), f"Rollback HTTP failed: {rb_res}")

        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT artpath FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(transaction_engine._s(row[0]), "")

    def test_real_ipc_artwork_delete_db_artpath_outside_root_cannot_expand_authority(self):
        """Wave 24 final review round 3, section 11: a corrupt/malicious
        albums.artpath pointing outside the configured music root must
        never be used to expand what DELETE .../art is allowed to touch,
        over the real HTTP boundary. The file it points at must be left
        byte-for-byte untouched."""
        outside_dir = self.root / "outside"
        outside_dir.mkdir(parents=True, exist_ok=True)
        outside_file = outside_dir / "important.jpg"
        original_bytes = _real_jpeg_bytes(color="yellow")
        outside_file.write_bytes(original_bytes)

        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE albums SET artpath=? WHERE id=1", (str(outside_file),))
        con.commit()
        con.close()

        del_res = self.client.delete_album_art(1)
        # Either fails closed, or reports ok with nothing removed -- either
        # way the untrusted DB pointer must never have been treated as a
        # newly-allowed root.
        if del_res.get("ok"):
            removed = del_res.get("removed") or []
            self.assertNotIn(str(outside_file), removed)

        self.assertTrue(outside_file.exists(), "file outside the configured root must not be removed")
        self.assertEqual(outside_file.read_bytes(), original_bytes, "file outside the configured root must be byte-for-byte unchanged")

    def test_real_ipc_artwork_delete_db_artpath_symlink_outside_root_cannot_expand_authority(self):
        """Symlink variant of the trust-root regression above: artpath
        pointing at a symlink whose target resolves outside the music
        root must not be followed/removed either."""
        outside_dir = self.root / "outside_symlink_target"
        outside_dir.mkdir(parents=True, exist_ok=True)
        outside_file = outside_dir / "real.jpg"
        original_bytes = _real_jpeg_bytes(color="purple")
        outside_file.write_bytes(original_bytes)

        link_path = self.music_dir / "art_symlink.jpg"
        try:
            os.symlink(str(outside_file), str(link_path))
        except (OSError, NotImplementedError) as ex:
            self.skipTest(f"symlinks not supported in this environment: {ex}")

        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE albums SET artpath=? WHERE id=1", (str(link_path),))
        con.commit()
        con.close()

        try:
            self.client.delete_album_art(1)
        except BeetsError:
            pass  # failing closed (symlink/access-denied rejection) is an acceptable outcome here

        self.assertTrue(outside_file.exists())
        self.assertEqual(outside_file.read_bytes(), original_bytes)

    def test_real_ipc_artpath_delete_clears_pointer_only_no_file_mutation(self):
        """Wave 24 final review round 3, section 12: DELETE .../artpath's
        contract is "clear the DB pointer only" -- it must never delete or
        move the actual artwork file, unlike DELETE .../art."""
        jpeg_bytes = _real_jpeg_bytes(color="orange")
        replace_res = self.client.replace_album_art(1, jpeg_bytes)
        self.assertTrue(replace_res.get("ok"))
        art_path = Path(replace_res["artpath"])
        self.assertTrue(art_path.exists())

        clear_res = self.client.clear_album_artpath(1)
        self.assertTrue(clear_res.get("ok"), f"Clear artpath HTTP failed: {clear_res}")

        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT artpath FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(transaction_engine._s(row[0]), "")
        # The file itself must be completely untouched -- clearing the
        # pointer is not the same operation as deleting the artwork.
        self.assertTrue(art_path.exists(), "clearing the DB pointer must not delete the file")
        self.assertEqual(art_path.read_bytes(), jpeg_bytes)

    # ── 2. Real Relocation HTTP IPC ───────────────────────────────────────────

    def test_real_ipc_relocation_rename_and_rollback(self):
        """Exercise album relocation over real HTTP socket with rollback."""
        old_path = self.item1_path
        self.assertTrue(old_path.exists(), "precondition: source exists before Apply")

        plan_res = self.client.plan_album_relocation({
            "album_id": 1,
            "mode": "rename",
            "new_artist": "Renamed IPC Artist",
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
        self.assertTrue(new_path.exists(), "destination must exist after Apply")
        self.assertTrue(str(new_path).startswith(str(self.music_dir)), "destination must be inside configured music root")
        self.assertIn("Renamed IPC Artist", str(new_path), "destination must reflect the operator's configured Beets path template")
        self.assertFalse(old_path.exists(), "source must be removed/moved as expected")

        rb_res = self.client.rollback_album_relocation(op_id)
        self.assertTrue(rb_res.get("ok"), f"Rollback HTTP failed: {rb_res}")
        self.assertTrue(old_path.exists(), "rollback must restore the original path")
        con = sqlite3.connect(self.db_path)
        irow2 = con.execute("SELECT path FROM items WHERE id=1").fetchone()
        con.close()
        self.assertEqual(transaction_engine._s(irow2[0]), str(old_path), "rollback must restore the original DB path")

    def test_real_ipc_relocation_move_to_library(self):
        """move_to_library relocates an item from a configured staging root
        into the authoritative music root, over real HTTP."""
        staging_item_dir = self.staging_dir / "incoming"
        staging_item_dir.mkdir(parents=True, exist_ok=True)
        staging_path = staging_item_dir / "Track 2.wav"
        staging_path.write_bytes(_real_wav_bytes(freq=330))
        self._insert_item(2, staging_path, track=2, title="Track 2")

        plan_res = self.client.plan_album_relocation({
            "album_id": 1,
            "mode": "move_to_library",
        })
        self.assertTrue(plan_res.get("ok"), f"Plan move_to_library failed: {plan_res}")
        apply_res = self.client.apply_album_relocation(plan_res["operation_id"])
        self.assertTrue(apply_res.get("ok"), f"Apply move_to_library failed: {apply_res}")

        con = sqlite3.connect(self.db_path)
        irow = con.execute("SELECT path FROM items WHERE id=2").fetchone()
        con.close()
        new_path = Path(transaction_engine._s(irow[0]))
        self.assertTrue(str(new_path).startswith(str(self.music_dir)))
        self.assertTrue(new_path.exists())

    def test_real_ipc_relocation_uses_custom_native_beets_path_template(self):
        """Prove the destination comes from asking the REAL installed Beets
        for its actual configured path template over the real HTTP
        boundary -- not a hand-built formatter -- by pointing Beets at a
        template deliberately different from the default shape."""
        try:
            import beets as beets_pkg
        except ImportError:
            self.skipTest("beets package not installed in this environment")
        original_paths = dict(beets_pkg.config["paths"].get())
        beets_pkg.config["paths"] = {
            "default": "$year - $albumartist - $album/$disc-$track $title",
        }
        try:
            plan_res = self.client.plan_album_relocation({"album_id": 1, "mode": "rename"})
            self.assertTrue(plan_res.get("ok"), f"Plan failed: {plan_res}")
            self.assertIn("2024 - IPC Artist - IPC Album", plan_res.get("dest_dir", ""))
        finally:
            beets_pkg.config["paths"] = original_paths

    # ── 3. Real Metadata + MB Track Repair HTTP IPC ───────────────────────────

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

    def test_real_ipc_metadata_tag_readback_and_rollback_restores_db_and_tags(self):
        """Metadata acceptance must not stop at "SQLite changed" -- verify
        the actual on-disk tag, and that rollback restores both DB and
        tags, over real HTTP."""
        import mediafile
        original_artist = mediafile.MediaFile(str(self.item1_path)).artist

        plan_res = self.client.plan_album_metadata({
            "album_id": 1, "updates": {}, "item_updates": {"1": {"artist": "HTTP Changed Artist"}},
        })
        self.assertTrue(plan_res.get("ok"), f"Plan failed: {plan_res}")
        op_id = plan_res["operation_id"]
        apply_res = self.client.apply_album_metadata(op_id)
        self.assertTrue(apply_res.get("ok"), f"Apply failed: {apply_res}")

        con = sqlite3.connect(self.db_path)
        irow = con.execute("SELECT artist FROM items WHERE id=1").fetchone()
        con.close()
        self.assertEqual(irow[0], "HTTP Changed Artist")
        mf = mediafile.MediaFile(str(self.item1_path))
        self.assertEqual(mf.artist, "HTTP Changed Artist", "actual on-disk tag must reflect the update, not just SQLite")

        rb_res = self.client.rollback_album_metadata(op_id)
        self.assertTrue(rb_res.get("ok"), f"Rollback HTTP failed: {rb_res}")

        con = sqlite3.connect(self.db_path)
        irow2 = con.execute("SELECT artist FROM items WHERE id=1").fetchone()
        con.close()
        self.assertEqual(irow2[0], original_artist or "")
        mf_after = mediafile.MediaFile(str(self.item1_path))
        self.assertEqual(mf_after.artist, original_artist, "rollback must restore the actual on-disk tag, not just SQLite")

    def test_real_ipc_mb_track_repair_plan_apply_rollback(self):
        """Wave 24 final review round 3, section 7: album_mb_track_repair_v1
        is part of the album-metadata domain -- exercise its real HTTP
        Plan/Apply/Rollback and confirm the db_path=LIB_PATH fix (it used
        to pass db_path=MUSIC_LIBRARY_PATH, a media directory, which
        sqlite3.connect() cannot open at all)."""
        # An item with an existing, nonblank Recording ID is treated as
        # already-established identity evidence (a real, intentional
        # safety policy: fuzzy title/position matching alone must not
        # overwrite it -- see backend/transaction_engine.py's
        # create_album_mb_track_repair_plan "requires_review" branch).
        # Clear it here so this test exercises the normal repair-an-
        # unmatched-track path the fixture's blank-mb_trackid items would
        # naturally hit.
        con = self._db()
        con.execute("UPDATE items SET mb_trackid='' WHERE id=1")
        con.commit()
        con.close()

        fake_tracklist = {
            "ok": True,
            "release_id": "11111111-1111-1111-1111-111111111111",
            "release_group": "22222222-2222-2222-2222-222222222222",
            "release_title": "IPC Album",
            "tracks": [
                {"disc": 1, "track": 1, "title": "Track 1", "mb_trackid": "44444444-4444-4444-4444-444444444444", "duration_ms": 1000},
            ],
        }
        with mock.patch("helpers_mb.fetch_mb_release_tracklist", return_value=fake_tracklist):
            plan_res = self.client.plan_album_mb_track_repair({"album_id": 1})
            self.assertTrue(plan_res.get("ok"), f"MB track repair plan failed (db_path regression?): {plan_res}")
            op_id = plan_res.get("operation_id")
            self.assertIsNotNone(op_id)

            apply_res = self.client.apply_album_mb_track_repair(op_id, write_tags=True)
            self.assertTrue(apply_res.get("ok"), f"MB track repair apply failed: {apply_res}")

            con = sqlite3.connect(self.db_path)
            irow = con.execute("SELECT mb_trackid FROM items WHERE id=1").fetchone()
            con.close()
            self.assertEqual(transaction_engine._s(irow[0]).lower(), "44444444-4444-4444-4444-444444444444")

            rb_res = self.client.rollback_album_mb_track_repair(op_id)
            self.assertTrue(rb_res.get("ok"), f"MB track repair rollback failed: {rb_res}")
            con = sqlite3.connect(self.db_path)
            irow2 = con.execute("SELECT mb_trackid FROM items WHERE id=1").fetchone()
            con.close()
            self.assertEqual(transaction_engine._s(irow2[0]), "", "rollback must restore the pre-repair (blank) Recording ID")

    # ── 4. Real Retirement HTTP IPC ────────────────────────────────────────────

    def test_real_ipc_retirement_delete_files_false_retains_media(self):
        """delete_files=False must remove intended DB rows but retain the
        actual media file, over real HTTP."""
        res_no_del = self.client.delete_album(1, delete_files=False)
        self.assertTrue(res_no_del.get("ok"), res_no_del)
        self.assertTrue(self.item1_path.exists(), "media file must remain on delete_files=False")

        con = sqlite3.connect(self.db_path)
        count = con.execute("SELECT count(*) FROM items WHERE album_id=1").fetchone()[0]
        con.close()
        self.assertEqual(count, 0, "DB item rows should be removed")

    def test_real_ipc_retirement_delete_files_true_removes_media_and_rolls_back(self):
        """delete_files=True must perform a controlled quarantine/removal,
        update the DB, and support rollback with truthful recovery."""
        res_del = self.client.delete_album(1, delete_files=True)
        self.assertTrue(res_del.get("ok"), res_del)
        self.assertFalse(self.item1_path.exists(), "media file must be removed on delete_files=True")

        con = sqlite3.connect(self.db_path)
        count = con.execute("SELECT count(*) FROM items WHERE album_id=1").fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

        # Find the operation this created so we can prove rollback recovers
        # both the file and the DB rows -- BeetsClient.delete_album()
        # doesn't itself surface the operation_id, so recover it from the
        # transaction store the same way an operator/audit trail would.
        txs, _total = self.tx_store.list(limit=20)
        candidates = [t for t in txs if (t.get("metadata") or {}).get("mode") == "remove_tracks"]
        self.assertTrue(candidates, "expected a remove_tracks transaction to have been recorded")
        op_id = sorted(candidates, key=lambda t: t.get("metadata", {}).get("created_at", 0))[-1]["id"]

        rb_res = self.client.rollback_album_maintenance(op_id)
        self.assertTrue(rb_res.get("ok"), f"Retirement rollback HTTP failed: {rb_res}")
        self.assertTrue(self.item1_path.exists(), "rollback must restore the quarantined media file")
        con = sqlite3.connect(self.db_path)
        count2 = con.execute("SELECT count(*) FROM items WHERE id=1").fetchone()[0]
        con.close()
        self.assertEqual(count2, 1, "rollback must restore the DB item row")

    # ── 5. Real Deduplication HTTP IPC ────────────────────────────────────────

    def test_real_ipc_dedup_removes_confirmed_duplicate_keeps_unique_track(self):
        """Real HTTP exercise of album_maintenance_v1's deduplicate mode:
        a genuine duplicate (identical-content collision file for track 1)
        is removed; an unrelated, genuinely unique track (track 2) is
        completely untouched because it was never included in the
        caller's to_delete list."""
        dup_path = self.item1_dir / "01 - Track 1.1.wav"
        dup_path.write_bytes(self.item1_path.read_bytes())  # identical content: a genuine collision duplicate
        self._insert_item(2, dup_path, track=1, title="Track 1", mb_trackid="55555555-5555-5555-5555-555555555555")

        unique_path = self.item1_dir / "02 - Track 2.wav"
        unique_path.write_bytes(_real_wav_bytes(freq=440))
        self._insert_item(3, unique_path, track=2, title="Track 2", mb_trackid="66666666-6666-6666-6666-666666666666")

        plan_res = self.client.plan_album_maintenance({
            "mode": "deduplicate",
            "album_id": 1,
            "to_delete": [{"id": 2, "path": str(dup_path)}],
        })
        self.assertTrue(plan_res.get("ok"), f"Dedup plan failed: {plan_res}")
        apply_res = self.client.apply_album_maintenance(plan_res["operation_id"])
        self.assertTrue(apply_res.get("ok"), f"Dedup apply failed: {apply_res}")

        con = sqlite3.connect(self.db_path)
        remaining_ids = {r[0] for r in con.execute("SELECT id FROM items WHERE album_id=1").fetchall()}
        con.close()
        self.assertNotIn(2, remaining_ids, "confirmed duplicate must be removed")
        self.assertIn(1, remaining_ids, "primary track 1 must be retained")
        self.assertIn(3, remaining_ids, "unrelated unique track must never be auto-deleted")
        self.assertFalse(dup_path.exists())
        self.assertTrue(self.item1_path.exists())
        self.assertTrue(unique_path.exists(), "unique track's file must be completely untouched")
        self.assertEqual(unique_path.read_bytes()[:4], b"RIFF")  # still a real, undamaged WAV file

    def test_real_ipc_dedup_rejects_item_not_belonging_to_album(self):
        """A to_delete entry whose id doesn't actually belong to the
        stated album (an identity/same-path-style mismatch the caller
        cannot paper over with a plausible-looking path) must be rejected,
        never silently skipped or trusted."""
        other_con = self._db()
        other_con.execute("INSERT INTO albums (id, album, artist, albumartist) VALUES (2, 'Other Album', 'Other Artist', 'Other Artist')")
        other_dir = self.music_dir / "Other Artist" / "Other Album"
        other_dir.mkdir(parents=True, exist_ok=True)
        other_path = other_dir / "01 - Other Track.wav"
        other_path.write_bytes(_real_wav_bytes(freq=550))
        other_con.execute(
            "INSERT INTO items (id, album_id, title, path) VALUES (99, 2, 'Other Track', ?)",
            (str(other_path),),
        )
        other_con.commit()
        other_con.close()

        with self.assertRaises(BeetsError) as ctx:
            self.client.plan_album_maintenance({
                "mode": "deduplicate",
                "album_id": 1,
                "to_delete": [{"id": 99, "path": str(other_path)}],
            })
        self.assertIn("99", str(ctx.exception))
        self.assertTrue(other_path.exists(), "the wrongly-targeted file must be untouched")

    def test_real_ipc_dedup_hardlink_safety(self):
        """Two DB item rows whose files are hardlinked (same inode):
        removing one must not corrupt or remove the other's independent
        directory entry."""
        primary_target = self.item1_dir / "hardlink_primary.wav"
        primary_target.write_bytes(_real_wav_bytes(freq=660))
        linked_path = self.item1_dir / "hardlink_dup.wav"
        try:
            os.link(str(primary_target), str(linked_path))
        except (OSError, NotImplementedError) as ex:
            self.skipTest(f"hardlinks not supported in this environment: {ex}")

        self._insert_item(4, primary_target, track=3, title="Hardlink Primary")
        self._insert_item(5, linked_path, track=3, title="Hardlink Dup", mb_trackid="77777777-7777-7777-7777-777777777777")

        plan_res = self.client.plan_album_maintenance({
            "mode": "deduplicate",
            "album_id": 1,
            "to_delete": [{"id": 5, "path": str(linked_path)}],
        })
        self.assertTrue(plan_res.get("ok"), f"Dedup plan failed: {plan_res}")
        apply_res = self.client.apply_album_maintenance(plan_res["operation_id"])
        self.assertTrue(apply_res.get("ok"), f"Dedup apply failed: {apply_res}")

        self.assertFalse(linked_path.exists(), "the removed hardlink's own directory entry must be gone")
        self.assertTrue(primary_target.exists(), "the surviving hardlink must be unaffected")
        self.assertEqual(primary_target.read_bytes()[:4], b"RIFF", "surviving hardlink's content must be intact, not truncated/corrupted")

    # ── 6. Real Engine Unavailable Fail-Closed IPC ─────────────────────────────

    def test_real_ipc_engine_unavailable_fails_closed(self):
        """Verify unreachable Control Agent fails closed cleanly, across
        every lifecycle domain, without local state corruption."""
        # Snapshot state before stopping the server.
        db_bytes_before = self.db_path.read_bytes()
        item_bytes_before = self.item1_path.read_bytes()

        self.server.shutdown()
        self.server.server_close()

        dead_client = BeetsClient(base_url=f"http://127.0.0.1:{self.port}", token=self.token)

        with self.assertRaises(BeetsUnavailableError):
            dead_client.replace_album_art(1, _real_jpeg_bytes())

        with self.assertRaises(BeetsUnavailableError):
            dead_client.relocate_album(1, mode="rename")

        with self.assertRaises(BeetsUnavailableError):
            dead_client.update_album_metadata(1, {"album": "Unreachable Title"})

        with self.assertRaises(BeetsUnavailableError):
            dead_client.plan_album_mb_track_repair({"album_id": 1})

        with self.assertRaises(BeetsUnavailableError):
            dead_client.delete_album(1, delete_files=False)

        with self.assertRaises(BeetsUnavailableError):
            dead_client.plan_album_maintenance({"mode": "deduplicate", "album_id": 1, "to_delete": [{"id": 1, "path": str(self.item1_path)}]})

        # No local mutation of any kind occurred while the engine was down.
        self.assertEqual(self.db_path.read_bytes(), db_bytes_before, "DB must be byte-for-byte unchanged when the engine is unreachable")
        self.assertEqual(self.item1_path.read_bytes(), item_bytes_before, "media file must be byte-for-byte unchanged when the engine is unreachable")


class TestWave24RelativeDbPathNormalization(unittest.TestCase):
    """Wave 24 final review round 3, section 11: a real Beets library
    stores items.path / albums.artpath RELATIVE to the configured music
    directory (beets/dbcore/pathutils.py normalize_path_for_db(), gated on
    beets.context's music-dir ContextVar being set -- true for every real
    `beet` CLI invocation) whenever the absolute path is inside that
    directory. transaction_engine.py reads these columns via raw sqlite3
    (to avoid a hard dependency on the `beets` package), which bypasses
    that expansion. This fixture seeds the DB with genuinely RELATIVE
    path/artpath strings -- exactly what a real Beets-managed library
    contains -- and proves every affected family (relocation, artwork,
    metadata, mb-track-repair) still resolves and operates on the real
    file over the actual HTTP IPC path, rather than either crashing or
    (worse) silently no-op'ing against a bogus relative-looking path.
    """

    @classmethod
    def setUpClass(cls):
        cls.token = "ci-test-relpath-token-32chars-long-secret!!"

    def setUp(self):
        self.port = _get_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"

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

        self.item_dir = self.music_dir / "Rel Artist" / "Rel Album"
        self.item_dir.mkdir(parents=True, exist_ok=True)
        self.item_path = self.item_dir / "01 - Rel Track.wav"
        self.item_path.write_bytes(_real_wav_bytes(freq=440))
        import mediafile as _mf_mod
        mf = _mf_mod.MediaFile(str(self.item_path))
        mf.title = "Rel Track"
        mf.artist = "Rel Artist"
        mf.save()

        self.art_path = self.item_dir / "cover.jpg"
        self.art_path.write_bytes(_real_jpeg_bytes(color="red"))

        # The relative forms a real Beets Library would have stored for
        # paths inside its configured `directory` (music_dir) -- POSIX-
        # style, relative to music_dir, exactly per
        # beets/dbcore/pathutils.py's normalize_path_for_db().
        rel_item_path = self.item_path.relative_to(self.music_dir).as_posix()
        rel_art_path = self.art_path.relative_to(self.music_dir).as_posix()

        con = sqlite3.connect(self.db_path)
        con.execute("""
            CREATE TABLE albums (
                id INTEGER PRIMARY KEY, album TEXT, artist TEXT, albumartist TEXT,
                year INTEGER, genre TEXT, artpath TEXT, mb_albumid TEXT,
                mb_releasegroupid TEXT, mb_artistid TEXT
            )
        """)
        con.execute("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY, album_id INTEGER, title TEXT, artist TEXT,
                albumartist TEXT, album TEXT, track INTEGER, disc INTEGER, year INTEGER,
                genre TEXT, format TEXT, path TEXT, mb_trackid TEXT, mb_albumid TEXT, length REAL
            )
        """)
        con.execute("""
            INSERT INTO albums (id, album, artist, albumartist, year, genre, artpath, mb_albumid, mb_releasegroupid)
            VALUES (1, 'Rel Album', 'Rel Artist', 'Rel Artist', 2024, 'Rock', ?, '44444444-4444-4444-4444-444444444444', '55555555-5555-5555-5555-555555555555')
        """, (rel_art_path,))
        con.execute("""
            INSERT INTO items (id, album_id, title, artist, albumartist, album, track, disc, year, genre, format, path, mb_trackid, mb_albumid, length)
            VALUES (1, 1, 'Rel Track', 'Rel Artist', 'Rel Artist', 'Rel Album', 1, 1, 2024, 'Rock', 'WAV', ?, '', '44444444-4444-4444-4444-444444444444', 1.0)
        """, (rel_item_path,))
        con.commit()
        con.close()

        # Sanity: the seeded DB values really are relative, not absolute --
        # otherwise this fixture would not actually exercise the fix.
        self.assertFalse(Path(rel_item_path).is_absolute())
        self.assertFalse(Path(rel_art_path).is_absolute())

        self.env_patcher = mock.patch.dict(os.environ, {
            "BEETS_API_TOKEN": self.token,
            "BEETSDIR": str(self.beets_dir),
            "LIB_PATH": str(self.db_path),
            "MUSIC_LIBRARY_PATH": str(self.music_dir),
            "MUSIC_ROOT": str(self.music_dir),
            "DOWNLOADS_ROOT": str(self.staging_dir),
            "STAGING_ROOT": str(self.staging_dir),
            "RECONCILE_QUARANTINE_DIR": str(self.quarantine_dir),
            "BEETS_ALBUM_ART_TRASH_DIR": str(self.quarantine_dir),
            "BEETS_OUTBOUND_ALLOWLIST": f"127.0.0.1:{self.port}",
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

    def test_relocation_rename_resolves_relative_item_and_art_path(self):
        """A relative items.path/albums.artpath must still be found,
        moved, and correctly re-stored -- not rejected as 'outside
        allowed roots' (the pre-fix failure mode) nor silently ignored."""
        res = self.client.relocate_album(1, mode="rename", new_artist="Rel Artist Renamed")
        self.assertTrue(res.get("ok"), f"Relocation over relative DB paths failed: {res}")

        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT path FROM items WHERE id=1").fetchone()
        art_row = con.execute("SELECT artpath FROM albums WHERE id=1").fetchone()
        con.close()
        item_path_str = row[0].decode("utf-8", "replace") if isinstance(row[0], bytes) else str(row[0])
        new_item_path = Path(item_path_str)
        resolved_item = new_item_path if new_item_path.is_absolute() else (self.music_dir / item_path_str)
        self.assertTrue(resolved_item.exists(), "renamed item file must exist on disk")
        self.assertTrue(str(resolved_item.resolve()).startswith(str(self.music_dir.resolve())))
        if art_row and art_row[0]:
            art_path_str = art_row[0].decode("utf-8", "replace") if isinstance(art_row[0], bytes) else str(art_row[0])
            resolved_art = Path(art_path_str) if Path(art_path_str).is_absolute() else (self.music_dir / art_path_str)
            self.assertTrue(resolved_art.exists(), "relocated artwork must exist on disk")

    def test_metadata_update_resolves_relative_item_path_for_tag_write(self):
        """Metadata repair must write tags to the real file even when
        items.path is stored relative to the music root."""
        res = self.client.update_album_metadata(1, {"album": "Rel Album Fixed"}, {"1": {"title": "Rel Track Fixed"}})
        self.assertTrue(res.get("ok"), f"Metadata update over relative DB path failed: {res}")

        import mediafile as _mf_mod
        mf = _mf_mod.MediaFile(str(self.item_path))
        self.assertEqual(mf.title, "Rel Track Fixed", "tag must be written to the real (relative-in-DB) file, not a bogus path")

    def test_mb_track_repair_plan_resolves_relative_item_path(self):
        """mb-track-repair's own containment check must not reject a
        correctly-relative items.path as 'outside allowed music roots'."""
        fake_mb = {
            "ok": True,
            "release_group": "55555555-5555-5555-5555-555555555555",
            "tracks": [{"position": 1, "recording_id": "66666666-6666-6666-6666-666666666666", "title": "Rel Track"}],
        }
        with mock.patch("helpers_mb.fetch_mb_release_tracklist", return_value=fake_mb):
            res = self.client.plan_album_mb_track_repair({"album_id": 1})
        self.assertTrue(res.get("ok"), f"mb-track-repair plan over relative DB path incorrectly rejected: {res}")

    def test_artwork_replace_trust_root_check_resolves_relative_artpath(self):
        """The DB-artpath trust-root check (section 3) must resolve a
        relative artpath before deciding whether it lands inside the
        configured roots -- a relative value must not be misjudged as
        'untrusted' purely because it looked non-absolute."""
        res = self.client.replace_album_art(1, _real_jpeg_bytes(color="green"))
        self.assertTrue(res.get("ok"), f"Artwork replace over relative DB artpath failed: {res}")


if __name__ == "__main__":
    unittest.main()
