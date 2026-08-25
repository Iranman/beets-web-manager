"""Direct in-process unit tests for the album_artwork_v1 /
album_relocation_v1 / album_metadata_repair_v1 Plan/Apply/Rollback
functions in backend/transaction_engine.py.

Wave 24 final review section 38: despite this module's former filename
(test_album_lifecycle_wave24_ipc.py), these tests import
backend.transaction_engine directly and call create_*/execute_*/
rollback_* in-process -- there is no HTTP hop, no Control Agent process,
and no BeetsClient involved. That is a legitimate, fast, valuable unit
test layer for the engine's own Plan/Apply/Verify/Rollback logic, but it
is not IPC and must not be described as exercising the real
Web Manager -> HTTP -> Control Agent -> engine boundary. Real IPC/two-
service acceptance coverage (actual BeetsClient over HTTP to an actual
Control Agent process) is tracked separately -- see
docs/TECHNICAL_DEBT.md -- and was not built out in this pass.
"""
import math
import os
import struct
import sys
import tempfile
import unittest
import unittest.mock
import sqlite3
import base64
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as _flask_app
from backend.transaction_engine import TransactionStore
from backend import transaction_engine


def _write_test_audio(path: Path, *, freq: float = 220.0, duration: float = 0.2, title: str = "", artist: str = "") -> None:
    """A real, minimal, independently-readable WAV file -- Wave 24 review
    section 24: the fake `b"..."` byte fixtures this test used to write
    only ever "worked" because tag-write failures were silently swallowed.
    Real audio bytes are required now that failures are structural.
    Same technique as tests/test_sec002_wave19_mb_track_repair.py and
    scripts/seed_demo_library.py's demo library (no copyrighted material).
    """
    sample_rate = 8000
    n_samples = max(1, int(sample_rate * duration))
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            frames += struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * i / sample_rate)))
        w.writeframes(bytes(frames))
    if title or artist:
        import mediafile
        mf = mediafile.MediaFile(str(path))
        if title:
            mf.title = title
        if artist:
            mf.artist = artist
        mf.save()


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
        _write_test_audio(self.item1_path, title="Track 1", artist="Test Artist")

        con.execute("""
            INSERT INTO items (id, album_id, title, artist, albumartist, album, track, disc, year, genre, format, path)
            VALUES (1, 1, 'Track 1', 'Test Artist', 'Test Artist', 'Test Album', 1, 1, 2024, 'Rock', 'MP3', ?)
        """, (str(self.item1_path),))

        con.commit()
        con.close()

    def tearDown(self):
        self.tmp_dir.cleanup()

    # ── 1. Album Artwork Tests ──────────────────────────────────────────────────

    @staticmethod
    def _real_jpeg_bytes(size=(4, 4)) -> bytes:
        """A genuine, Pillow-decodable JPEG -- Wave 24 review section 5:
        the artwork family now really parses images, so fixtures must be
        real images, not magic-byte stubs."""
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", size, color=(200, 50, 50)).save(buf, format="JPEG")
        return buf.getvalue()

    def test_album_artwork_replace_base64_upload_and_db_update(self):
        jpeg_b64 = base64.b64encode(self._real_jpeg_bytes()).decode("utf-8")
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
        self.assertEqual(plan_res.get("code"), "album_artwork_corrupt_image")

    def test_album_artwork_plan_does_not_mutate_filesystem(self):
        """Wave 24 review section 2/41: Plan must be non-mutating. Snapshot
        the staging/quarantine tree before and after Plan and prove zero
        filesystem change, including for the base64 image-upload path that
        used to write to disk during Plan."""
        jpeg_b64 = base64.b64encode(self._real_jpeg_bytes()).decode("utf-8")

        def _snapshot(base: Path):
            if not base.exists():
                return set()
            return {str(p.relative_to(base)) for p in base.rglob("*")}

        before = _snapshot(self.quarantine_dir)
        plan_res = transaction_engine.create_album_artwork_plan(
            self.store,
            {"album_id": 1, "mode": "replace", "image_data_b64": jpeg_b64},
            music_allowed_roots=[str(self.music_dir)],
            staging_allowed_roots=[str(self.staging_dir), str(self.quarantine_dir / "staging"), str(self.quarantine_dir)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertTrue(plan_res.get("ok"), f"Plan failed: {plan_res}")
        after = _snapshot(self.quarantine_dir)
        self.assertEqual(before, after, "Plan must not create/modify any filesystem entries")

        con = sqlite3.connect(self.db_path)
        artpath_before = con.execute("SELECT artpath FROM albums WHERE id=1").fetchone()[0]
        con.close()
        self.assertIn(artpath_before, ("", None))

        # Apply DOES mutate -- proves the plan captured real, applicable work.
        apply_res = transaction_engine.execute_album_artwork_apply(
            self.store, plan_res["operation_id"],
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertTrue(apply_res.get("ok"), f"Apply failed: {apply_res}")
        self.assertTrue(apply_res.get("mutated"))

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

    def test_album_relocation_uses_real_native_beets_path_template(self):
        """Wave 24 review section 8/42: prove the destination comes from
        asking the REAL installed Beets for its actual configured path
        template, not a hand-built 'Artist/Album/NN - Title' formatter --
        by pointing Beets at a path template deliberately different from
        that shape and asserting the plan matches it exactly.
        """
        import beets as beets_pkg
        original_paths = dict(beets_pkg.config["paths"].get())
        beets_pkg.config["paths"] = {
            "default": "$year - $albumartist - $album/$disc-$track $title",
        }
        try:
            plan_res = transaction_engine.create_album_relocation_plan(
                self.store,
                {"album_id": 1, "mode": "rename"},
                music_allowed_roots=[str(self.music_dir)],
                db_path=str(self.db_path),
            )
            self.assertTrue(plan_res.get("ok"), f"Relocation plan failed: {plan_res}")
            self.assertTrue(plan_res.get("native_beets"))
            new_path = plan_res["dest_dir"]
            # The custom template's shape ($year - $albumartist - $album,
            # then $disc-$track $title) must be exactly what was planned --
            # a hand-rolled "Artist/Album/NN - Title" formatter would never
            # produce this structure.
            self.assertIn("2024 - Test Artist - Test Album", new_path)
        finally:
            beets_pkg.config["paths"] = original_paths

    def test_album_relocation_move_to_library_rejects_unauthorized_source_root(self):
        """Wave 24 review section 10/47: move_to_library must not accept a
        DB path outside configured staging/import roots -- an arbitrary
        host path is not sufficient authority to be pulled into the
        authoritative library."""
        rogue_dir = self.root / "not_a_staging_root"
        rogue_dir.mkdir(parents=True, exist_ok=True)
        rogue_path = rogue_dir / "01 - Track 1.mp3"
        _write_test_audio(rogue_path, title="Track 1")

        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET path=? WHERE id=1", (str(rogue_path),))
        con.commit()
        con.close()

        plan_res = transaction_engine.create_album_relocation_plan(
            self.store,
            {"album_id": 1, "mode": "move_to_library"},
            music_allowed_roots=[str(self.music_dir)],
            staging_allowed_roots=[str(self.staging_dir)],
            db_path=str(self.db_path),
        )
        self.assertFalse(plan_res.get("ok"))
        self.assertEqual(plan_res.get("code"), "album_relocation_source_root_not_authorized")

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

    def test_album_metadata_rejects_unknown_field(self):
        """Wave 24 review section 19/46: an unknown/disallowed field must
        be rejected at Plan time -- before a transaction (and any dynamic
        SQL) is even created."""
        plan_res = transaction_engine.create_album_metadata_plan(
            self.store,
            {
                "album_id": 1,
                "updates": {"album": "X", "artpath); DROP TABLE albums;--": "evil"},
            },
            db_path=str(self.db_path),
        )
        self.assertFalse(plan_res.get("ok"))
        self.assertEqual(plan_res.get("code"), "album_metadata_field_not_allowed")

        # The DB must be completely untouched -- no transaction, no SQL.
        con = sqlite3.connect(self.db_path)
        arow = con.execute("SELECT album FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(arow[0], "Test Album")

    def test_album_metadata_force_identity_override_is_ignored(self):
        """Wave 24 review section 21/45: the client-supplied
        force_identity_override must never bypass an identity conflict."""
        plan_res = transaction_engine.create_album_metadata_plan(
            self.store,
            {
                "album_id": 1,
                "updates": {"mb_releasegroupid": "99999999-9999-9999-9999-999999999999"},
                "force_identity_override": True,
            },
            db_path=str(self.db_path),
        )
        self.assertFalse(plan_res.get("ok"), f"force_identity_override must not bypass the conflict: {plan_res}")
        self.assertEqual(plan_res.get("code"), "album_metadata_identity_conflict")

    def test_album_metadata_force_write_tags_really_writes(self):
        """Wave 24 review section 30: retag's 'write tags' stage must
        really write current DB values to the file, not silently no-op on
        an empty diff."""
        # Give item 1 a distinctive artist value directly in the DB (as if
        # a prior mbsync step had already updated it) with no further
        # field diff requested -- force_write_tags is the only thing that
        # should cause a write.
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET artist=? WHERE id=1", ("Resynced Artist",))
        con.commit()
        con.close()

        plan_res = transaction_engine.create_album_metadata_plan(
            self.store,
            {"album_id": 1, "updates": {}, "force_write_tags": True},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan_res.get("ok"), f"Plan failed: {plan_res}")
        self.assertEqual(plan_res.get("items_changed"), 1)
        op_id = plan_res["operation_id"]

        apply_res = transaction_engine.execute_album_metadata_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path),
        )
        self.assertTrue(apply_res.get("ok"), f"Apply failed: {apply_res}")
        self.assertEqual(apply_res.get("tags_written_count"), 1)

        import mediafile
        mf = mediafile.MediaFile(str(self.item1_path))
        self.assertEqual(mf.artist, "Resynced Artist")

    def test_album_metadata_apply_and_rollback_restores_tags(self):
        """Wave 24 review section 26: rollback must restore file tags, not
        just DB rows."""
        plan_res = transaction_engine.create_album_metadata_plan(
            self.store,
            {"album_id": 1, "updates": {}, "item_updates": {"1": {"artist": "Changed Artist"}}},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan_res.get("ok"), f"Plan failed: {plan_res}")
        op_id = plan_res["operation_id"]

        apply_res = transaction_engine.execute_album_metadata_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path),
        )
        self.assertTrue(apply_res.get("ok"), f"Apply failed: {apply_res}")

        import mediafile
        mf = mediafile.MediaFile(str(self.item1_path))
        self.assertEqual(mf.artist, "Changed Artist")

        rb_res = transaction_engine.rollback_album_metadata(self.store, op_id, db_path=str(self.db_path))
        self.assertTrue(rb_res.get("ok"), f"Rollback failed: {rb_res}")
        self.assertEqual(rb_res.get("tags_failed"), 0)

        mf_after = mediafile.MediaFile(str(self.item1_path))
        self.assertEqual(mf_after.artist, "Test Artist")

        con = sqlite3.connect(self.db_path)
        irow = con.execute("SELECT artist FROM items WHERE id=1").fetchone()
        con.close()
        self.assertEqual(irow[0], "Test Artist")

    def test_album_metadata_tag_write_failure_after_db_commit_not_completed(self):
        """Wave 24 review section 43: force the tag write to fail AFTER
        the DB has already been updated. Expected: never Completed, and
        either fully restored (Rolled Back) or truthfully flagged
        (Recovery Required) -- DB/tag state must never silently diverge
        behind a Completed status."""
        plan_res = transaction_engine.create_album_metadata_plan(
            self.store,
            {"album_id": 1, "updates": {}, "item_updates": {"1": {"artist": "Should Not Land"}}},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan_res.get("ok"), f"Plan failed: {plan_res}")
        op_id = plan_res["operation_id"]

        def _always_fail(*a, **k):
            return {"ok": False, "error": "simulated tag write failure", "code": "tag_write_test_forced_failure"}

        with unittest.mock.patch.object(transaction_engine, "native_beets_write_item_tags", side_effect=_always_fail), \
             unittest.mock.patch.object(transaction_engine, "_write_media_tag_fields", side_effect=_always_fail):
            apply_res = transaction_engine.execute_album_metadata_apply(
                self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path),
            )

        self.assertFalse(apply_res.get("ok"))
        tx = self.store.get(op_id)
        self.assertNotEqual(tx["status"], "Completed")
        self.assertIn(tx["status"], ("Rolled Back", "Recovery Required", "Failed"))

        if tx["status"] == "Rolled Back":
            con = sqlite3.connect(self.db_path)
            irow = con.execute("SELECT artist FROM items WHERE id=1").fetchone()
            con.close()
            self.assertEqual(irow[0], "Test Artist", "DB must not diverge from actual file tags after a failed tag write")

    def test_album_relocation_rejects_symlink_source(self):
        """Wave 24 review section 11/48: a symlinked source component must
        be rejected, not silently followed."""
        real_dir = self.music_dir / "Real Artist" / "Real Album"
        real_dir.mkdir(parents=True, exist_ok=True)
        real_path = real_dir / "01 - Track 1.mp3"
        _write_test_audio(real_path, title="Track 1")

        linked_dir = self.music_dir / "Test Artist" / "Test Album"
        # Replace the album directory itself with a symlink to a
        # different real directory -- item1_path (set up in setUp) is
        # still referenced by the DB as living under this now-symlinked
        # path.
        import shutil as _shutil
        _shutil.rmtree(linked_dir)
        os.symlink(str(real_dir), str(linked_dir), target_is_directory=True)

        plan_res = transaction_engine.create_album_relocation_plan(
            self.store,
            {"album_id": 1, "mode": "rename", "new_artist": "Renamed Artist", "new_title": "Renamed Album"},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertFalse(plan_res.get("ok"))
        self.assertEqual(plan_res.get("code"), "album_relocation_symlink_rejected")

    def test_album_relocation_stale_destination_rejected_at_apply(self):
        """Wave 24 review section 12/49: Plan while the destination is
        free; something else claims that exact path before Apply runs.
        Expected: Apply refuses (stale plan), never silently overwrites
        the new arrival."""
        plan_res = transaction_engine.create_album_relocation_plan(
            self.store,
            {"album_id": 1, "mode": "rename", "new_artist": "Renamed Artist", "new_title": "Renamed Album"},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan_res.get("ok"), f"Plan failed: {plan_res}")
        op_id = plan_res["operation_id"]
        tx = self.store.get(op_id)
        new_path = Path(tx["metadata"]["items_plan"][0]["new_path"])
        self.assertFalse(new_path.exists(), "destination must have been free at plan time for this test to be meaningful")

        # Something else claims the destination between Plan and Apply.
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_bytes(b"a legitimate, unrelated file that landed here after Plan")

        apply_res = transaction_engine.execute_album_relocation_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path),
        )
        self.assertFalse(apply_res.get("ok"))
        # The racing file must survive untouched.
        self.assertEqual(new_path.read_bytes(), b"a legitimate, unrelated file that landed here after Plan")
        # And the original source must still be exactly where it was.
        self.assertTrue(self.item1_path.exists())


if __name__ == "__main__":
    unittest.main()
