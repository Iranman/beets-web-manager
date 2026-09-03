"""Comprehensive Unit & Behavioral Tests for Beets Transaction Engine Families (SEC-002 Wave 22).

Covers all transaction families:
- folder_cleanup_v1
- playlist_media_cleanup_v1
- album_maintenance_v1 (deduplicate, remove_tracks)
- album_artwork_v1
- import_folder_v1
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

        self.db_path = self.root / "beets.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, album_id INT, title TEXT, artist TEXT, album TEXT, albumartist TEXT, mb_trackid TEXT, mb_albumid TEXT, mb_releasegroupid TEXT, disc INT, track INT, length REAL)")
            conn.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, albumartist TEXT, mb_albumid TEXT, mb_releasegroupid TEXT, artpath BLOB, year INT, country TEXT, label TEXT)")
            conn.execute("CREATE TABLE item_attributes (id INTEGER PRIMARY KEY, entity_id INT, key TEXT, value TEXT)")

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
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path)
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
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path)
        )
        op_id = plan["operation_id"]

        apply1 = transaction_engine.execute_folder_cleanup_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path)
        )
        self.assertTrue(apply1.get("ok"))
        self.assertTrue(dst.exists())
        self.assertFalse(src.exists())

        # Replay / Idempotency check: second apply returns success without duplicate mutation
        apply2 = transaction_engine.execute_folder_cleanup_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path)
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
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path)
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
            music_allowed_roots=[str(self.root)],
            db_path=str(self.db_path)
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
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertFalse(plan.get("ok"))
        self.assertEqual(plan.get("code"), "folder_cleanup_root_refused")

    def test_folder_cleanup_safe_rename_refuses_db_tracked_source(self):
        src = self.music_dir / "old_album"
        dst = self.music_dir / "new_album"
        src.mkdir()
        track = src / "track1.mp3"
        track.write_bytes(b"audio content")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO items (id, path, album_id, title) VALUES (900, ?, 1, 'Track')", (str(track).encode("utf-8"),))

        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "safe_rename", "source": str(src), "target": str(dst)},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertFalse(plan.get("ok"))
        self.assertEqual(plan.get("code"), "folder_cleanup_db_references")
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())

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
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
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
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        track.unlink()

        apply = transaction_engine.execute_folder_cleanup_apply(
            self.store,
            plan["operation_id"],
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertFalse(apply.get("ok"))
        self.assertEqual(apply.get("code"), "folder_cleanup_toctou_mismatch")
        self.assertFalse((dst / "track1.mp3").exists())

    def test_item_metadata_write_tags_false_updates_db_only(self):
        track = self.music_dir / "db_only.mp3"
        track.write_bytes(b"not a real media file")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO items (id, path, album_id, title, artist, album) VALUES (901, ?, 1, 'Old', 'Artist', 'Album')",
                (str(track).encode("utf-8"),),
            )

        plan = transaction_engine.create_item_metadata_plan(
            self.store,
            {"item_id": 901, "updates": {"title": "Mapped Title"}, "write_tags": False},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        with mock.patch.object(transaction_engine, "native_beets_write_item_tags", side_effect=AssertionError("tag writer called")),              mock.patch.object(transaction_engine, "_write_media_tag_fields", side_effect=AssertionError("fallback tag writer called")):
            apply = transaction_engine.execute_item_metadata_apply(
                self.store,
                plan["operation_id"],
                music_allowed_roots=[str(self.music_dir)],
                db_path=str(self.db_path),
            )
        self.assertTrue(apply.get("ok"), msg=apply.get("error"))
        self.assertEqual(apply.get("tags_written_count"), 0)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT title FROM items WHERE id=901").fetchone()
        self.assertEqual(row[0], "Mapped Title")

    # ── 2. playlist_media_cleanup_v1 ──────────────────────────────────────────

    def test_playlist_media_cleanup_plan_and_apply(self):
        file1 = self.music_dir / "song1.mp3"
        file1.write_bytes(b"song content")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO items (id, path) VALUES (10, ?)", (str(file1).encode("utf-8"),))

        plan = transaction_engine.create_playlist_media_cleanup_plan(
            self.store,
            {"item_ids": [10]},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path)
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        apply = transaction_engine.execute_playlist_media_cleanup_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertTrue(apply.get("ok"))
        self.assertFalse(file1.exists())

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT id FROM items WHERE id=10").fetchone()
            self.assertIsNone(row)

    def test_playlist_media_cleanup_rollback_restores_empty_album_row(self):
        """SEC-002 Wave 22 final review, finding #24: the album-row
        restore comprehension was malformed (`[arow[c] for arow in cols]`,
        shadowing `arow` and referencing an undefined `c`) and raised a
        NameError the first time rollback tried to restore an album row
        retired because its last item was removed. Regression: delete the
        album's only item, then roll back, and expect both the item and
        the album row to come back -- not a crash."""
        file1 = self.music_dir / "only_track.mp3"
        file1.write_bytes(b"only track content")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album, albumartist) VALUES (30, 'Solo Album', 'Solo Artist')")
            conn.execute("INSERT INTO items (id, path, album_id, title) VALUES (300, ?, 30, 'Only Track')", (str(file1).encode("utf-8"),))

        plan = transaction_engine.create_playlist_media_cleanup_plan(
            self.store, {"item_ids": [300]},
            music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path),
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        apply = transaction_engine.execute_playlist_media_cleanup_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertTrue(apply.get("ok"), msg=apply.get("error"))
        self.assertEqual(apply.get("deleted_albums"), 1)

        with sqlite3.connect(self.db_path) as conn:
            self.assertIsNone(conn.execute("SELECT id FROM items WHERE id=300").fetchone())
            self.assertIsNone(conn.execute("SELECT id FROM albums WHERE id=30").fetchone())

        rollback = transaction_engine.rollback_playlist_media_cleanup(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path),
        )
        self.assertTrue(rollback.get("ok"), msg=rollback)
        self.assertEqual(rollback.get("status"), "Rolled Back")
        self.assertTrue(file1.exists())

        with sqlite3.connect(self.db_path) as conn:
            self.assertIsNotNone(conn.execute("SELECT id FROM items WHERE id=300").fetchone())
            self.assertIsNotNone(conn.execute("SELECT id FROM albums WHERE id=30").fetchone())

    # ── 3. album_maintenance_v1 (deduplicate, remove_tracks) ──

    def test_album_maintenance_remove_tracks_mode(self):
        file1 = self.music_dir / "track1.mp3"
        file1.write_bytes(b"track 1 content")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album) VALUES (1, 'Test Album')")
            conn.execute("INSERT INTO items (id, path, album_id) VALUES (100, ?, 1)", (str(file1).encode("utf-8"),))

        plan = transaction_engine.create_album_maintenance_plan(
            self.store,
            {
                "mode": "remove_tracks",
                "album_id": 1,
                "item_ids": [100],
                "delete_files": True
            },
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path)
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        apply = transaction_engine.execute_album_maintenance_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertTrue(apply.get("ok"))
        self.assertFalse(file1.exists())

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT id FROM items WHERE id=100").fetchone()
            self.assertIsNone(row)

    def test_album_maintenance_deduplicate_mode(self):
        file1 = self.music_dir / "dup_track.mp3"
        file1.write_bytes(b"audio stream")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album) VALUES (5, 'Album')")
            conn.execute("INSERT INTO items (id, path, album_id) VALUES (200, ?, 5)", (str(file1).encode("utf-8"),))

        plan = transaction_engine.create_album_maintenance_plan(
            self.store,
            {
                "mode": "deduplicate",
                "album_id": 5,
                "to_delete": [{"id": 200, "path": str(file1)}]
            },
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path)
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        apply = transaction_engine.execute_album_maintenance_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertTrue(apply.get("ok"))
        self.assertFalse(file1.exists())

    def test_album_maintenance_filename_cleanup_actually_renames(self):
        """SEC-002 Wave 22 final review, finding #15: `filename_cleanup`
        had no Plan implementation at all, so Apply could report
        renamed=1/db_updates=1 with the file never having moved.
        Regression: after Apply, the file must actually be at the new
        path on disk, not just in the DB."""
        old_path = self.music_dir / "01 Track [LID12345].mp3"
        old_path.write_bytes(b"track content")
        new_path = self.music_dir / "01 Track.mp3"

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album) VALUES (40, 'Album')")
            conn.execute("INSERT INTO items (id, path, album_id) VALUES (400, ?, 40)", (str(old_path).encode("utf-8"),))

        plan = transaction_engine.create_album_maintenance_plan(
            self.store,
            {
                "mode": "filename_cleanup",
                "candidates": [{
                    "item_id": 400,
                    "source": str(old_path),
                    "destination": str(new_path),
                    "conflict": False,
                }],
            },
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        apply = transaction_engine.execute_album_maintenance_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertTrue(apply.get("ok"), msg=apply.get("error"))
        self.assertEqual(apply.get("moved_count"), 1)
        self.assertTrue(new_path.exists())
        self.assertFalse(old_path.exists())

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT path FROM items WHERE id=400").fetchone()
            self.assertEqual(row[0].decode("utf-8") if isinstance(row[0], bytes) else row[0], str(new_path))

    def test_album_maintenance_unsupported_mode_fails_closed(self):
        """SEC-002 Wave 22 final review, finding #14: cleanup_issue (and
        any other unimplemented mode) must never silently create an
        empty, still-'ok' transaction."""
        plan = transaction_engine.create_album_maintenance_plan(
            self.store, {"mode": "cleanup_issue", "album_id": 1},
            music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path),
        )
        self.assertFalse(plan.get("ok"))
        self.assertEqual(plan.get("code"), "album_maintenance_invalid_mode")

    # ── 4. album_artwork_v1 ───────────────────────────────────────────────────

    def test_album_artwork_plan(self):
        cover = self.staging_dir / "cover.jpg"
        cover.write_bytes(b"JPEG artwork binary data")
        target_dir = self.music_dir / "Artist" / "Album"
        target_dir.mkdir(parents=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album) VALUES (10, 'Album')")
            conn.execute("INSERT INTO items (id, path, album_id) VALUES (50, ?, 10)", (str(target_dir / "01.mp3").encode("utf-8"),))

        plan = transaction_engine.create_album_artwork_plan(
            self.store,
            {
                "mode": "move",
                "album_id": 10,
                "target_dir": str(target_dir),
                "candidates": [{"source": str(cover)}]
            },
            music_allowed_roots=[str(self.root)],
            staging_allowed_roots=[str(self.staging_dir)],
            db_path=str(self.db_path)
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        self.assertEqual(plan.get("move_count"), 1)

        op_id = plan["operation_id"]
        apply_res = transaction_engine.execute_album_artwork_apply(
            self.store, op_id, music_allowed_roots=[str(self.root)], db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertTrue(apply_res.get("ok"), msg=apply_res.get("error"))
        self.assertTrue((target_dir / "cover.jpg").exists())
        self.assertFalse(cover.exists())

    # ── 5. import_folder_v1 ───────────────────────────────────────────────────

    def test_import_folder_plan_and_apply(self):
        import_src = self.staging_dir / "Artist - Album (2024)"
        import_src.mkdir()
        (import_src / "01 - Track.mp3").write_bytes(b"mp3 content")

        plan = transaction_engine.create_import_folder_plan(
            self.store,
            {
                "source_folder": str(import_src),
                "mb_albumid": "11111111-1111-1111-1111-111111111111"
            },
            music_allowed_roots=[str(self.root)],
            staging_allowed_roots=[str(self.root)],
            db_path=str(self.db_path)
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        apply = transaction_engine.execute_import_folder_apply(
            self.store, op_id, music_allowed_roots=[str(self.root)], db_path=str(self.db_path),
            beets_import_runner=lambda payload: {"ok": True, "imported": True},
        )
        self.assertTrue(apply.get("ok"), msg=apply.get("error"))
        self.assertEqual(apply.get("import_result"), {"ok": True, "imported": True})

    def test_import_folder_apply_without_runner_fails_closed(self):
        """SEC-002 Wave 22 final review, finding #3 (CRITICAL): Apply
        previously marked filesystem_mutated/db_mutated=True and returned
        Completed even when no `beets_import_runner` was supplied at all
        -- exactly the production Control Agent's actual call shape --
        meaning a real import never happened but was reported as one."""
        import_src = self.staging_dir / "Artist - Album (2024)"
        import_src.mkdir()
        (import_src / "01 - Track.mp3").write_bytes(b"mp3 content")

        plan = transaction_engine.create_import_folder_plan(
            self.store,
            {"source_folder": str(import_src), "mb_albumid": "11111111-1111-1111-1111-111111111111"},
            music_allowed_roots=[str(self.root)], staging_allowed_roots=[str(self.root)], db_path=str(self.db_path),
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        apply = transaction_engine.execute_import_folder_apply(
            self.store, op_id, music_allowed_roots=[str(self.root)], db_path=str(self.db_path),
        )
        self.assertFalse(apply.get("ok"))
        self.assertEqual(apply.get("code"), "import_folder_no_runner_configured")
        self.assertFalse(apply.get("mutated"))

    def test_import_folder_rollback_of_real_import_is_not_claimed_reversed(self):
        """SEC-002 Wave 22 final review, finding #7/#33: rollback must
        never claim 'Rolled Back' for a real import it cannot actually
        undo."""
        import_src = self.staging_dir / "Artist - Album (2024)"
        import_src.mkdir()
        (import_src / "01 - Track.mp3").write_bytes(b"mp3 content")

        plan = transaction_engine.create_import_folder_plan(
            self.store,
            {"source_folder": str(import_src), "mb_albumid": "11111111-1111-1111-1111-111111111111"},
            music_allowed_roots=[str(self.root)], staging_allowed_roots=[str(self.root)], db_path=str(self.db_path),
        )
        op_id = plan["operation_id"]
        apply = transaction_engine.execute_import_folder_apply(
            self.store, op_id, music_allowed_roots=[str(self.root)], db_path=str(self.db_path),
            beets_import_runner=lambda payload: {"ok": True},
        )
        self.assertTrue(apply.get("ok"), msg=apply.get("error"))

        rollback = transaction_engine.rollback_import_folder(
            self.store, op_id, music_allowed_roots=[str(self.root)], db_path=str(self.db_path),
        )
        self.assertFalse(rollback.get("ok"))
        self.assertEqual(rollback.get("status"), "Recovery Required")

    # ── Wave 29 Engine & BeetsClient Expansion Tests (cherry-picked from
    # feat/sec002-arch003-wave29-direct-mutations, reconciled against this
    # branch's folder_cleanup_v1 hardening) ─────────────────────────────────

    def test_folder_cleanup_source_and_target_path_keys(self):
        src = self.music_dir / "old_src_path"
        dst = self.music_dir / "new_dst_path"
        src.mkdir()
        (src / "track1.mp3").write_bytes(b"audio content")

        plan = transaction_engine.create_folder_cleanup_plan(
            self.store,
            {"action": "safe_rename", "source_path": str(src), "target_path": str(dst)},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]
        apply_res = transaction_engine.execute_folder_cleanup_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path)
        )
        self.assertTrue(apply_res.get("ok"), msg=apply_res.get("error"))
        self.assertTrue(dst.exists())
        self.assertFalse(src.exists())
        self.assertEqual(len(apply_res.get("moved_records", [])), 1)
        self.assertEqual(apply_res.get("changed_count"), 1)

    def test_album_maintenance_remove_album_empty_album(self):
        # Insert an orphaned album row with no items
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album, albumartist) VALUES (999, 'Empty Album', 'Ghost Artist')")

        plan = transaction_engine.create_album_maintenance_plan(
            self.store,
            {"mode": "remove_album", "album_id": 999},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        apply_res = transaction_engine.execute_album_maintenance_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path)
        )
        self.assertTrue(apply_res.get("ok"), msg=apply_res.get("error"))

        # Verify album row deleted from SQLite
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT id FROM albums WHERE id=999").fetchone()
            self.assertIsNone(row)

    def test_existing_album_reconcile_releasegroup_mismatch_has_no_bypass(self):
        """The Release-Group-mismatch identity gate on existing_album_reconcile_v1
        is unconditional -- an earlier cherry-picked allow_different_releasegroup/
        force override was reviewed and removed (see docs/TECHNICAL_DEBT.md
        ARCH-003) because none of the remaining unmigrated callers need it and
        every one of this repo's real duplicate/RGID-merge routes already
        enforces the same RG match requirement itself with no override path.
        This proves the payload key cannot silently re-enable a bypass."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album, albumartist, mb_releasegroupid) VALUES (101, 'Target Album', 'Artist A', '11111111-1111-1111-1111-111111111111')")
            conn.execute("INSERT INTO albums (id, album, albumartist, mb_releasegroupid) VALUES (102, 'Source Album', 'Artist A', '22222222-2222-2222-2222-222222222222')")
            item_file = self.music_dir / "item1.mp3"
            item_file.write_bytes(b"audio track 1")
            conn.execute(
                "INSERT INTO items (id, path, album_id, title, artist, album, mb_trackid, mb_albumid, mb_releasegroupid, disc, track) "
                "VALUES (501, ?, 102, 'Track 1', 'Artist A', 'Source Album', '33333333-3333-3333-3333-333333333333', '44444444-4444-4444-4444-444444444444', '22222222-2222-2222-2222-222222222222', 1, 1)",
                (str(item_file).encode("utf-8"),),
            )

        plan_rejected = transaction_engine.create_existing_album_reconcile_plan(
            self.store,
            {"existing_album_id": 101, "imported_album_id": 102, "move_item_ids": [501]},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertFalse(plan_rejected.get("ok"))
        self.assertEqual(plan_rejected.get("code"), "reconcile_identity_mismatch")

        # Same request, now also carrying the payload keys a bypass would have
        # read -- still rejected. The gate does not consult these fields at all.
        plan_still_rejected = transaction_engine.create_existing_album_reconcile_plan(
            self.store,
            {
                "existing_album_id": 101, "imported_album_id": 102, "move_item_ids": [501],
                "allow_different_releasegroup": True, "force": True,
            },
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertFalse(plan_still_rejected.get("ok"))
        self.assertEqual(plan_still_rejected.get("code"), "reconcile_identity_mismatch")

        # Neither rejected plan mutated anything.
        with sqlite3.connect(self.db_path) as conn:
            irow = conn.execute("SELECT album_id FROM items WHERE id=501").fetchone()
            self.assertEqual(irow[0], 102)
            arow = conn.execute("SELECT id FROM albums WHERE id=102").fetchone()
            self.assertIsNotNone(arow)

    @mock.patch("backend.transaction_engine._read_file_audio_tags")
    def test_album_mb_track_repair_direct_tracks(self, mock_read):
        mock_read.return_value = {"ok": True, "tags": {"title": "Song 1", "artist": "Artist B"}}
        # Create album and items
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album, albumartist, year, mb_albumid, mb_releasegroupid) VALUES (201, 'Album MB', 'Artist B', 2000, '55555555-5555-5555-5555-555555555555', '66666666-6666-6666-6666-666666666666')")
            f1 = self.music_dir / "mb_track1.mp3"
            f1.write_bytes(b"audio track 1")
            conn.execute("INSERT INTO items (id, path, album_id, title, artist, album, mb_trackid, mb_albumid, disc, track) VALUES (601, ?, 201, 'Song 1', 'Artist B', 'Album MB', '', '55555555-5555-5555-5555-555555555555', 1, 1)", (str(f1).encode("utf-8"),))

        mb_tracks = [
            {"track": 1, "disc": 1, "title": "Song 1", "mb_trackid": "77777777-7777-7777-7777-777777777777"}
        ]

        plan = transaction_engine.create_album_mb_track_repair_plan(
            self.store,
            {
                "album_id": 201,
                "mb_tracks": mb_tracks,
            },
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        apply_res = transaction_engine.execute_album_mb_track_repair_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path), write_tags=False,
        )
        self.assertTrue(apply_res.get("ok"), msg=apply_res.get("error"))

        with sqlite3.connect(self.db_path) as conn:
            # Check repaired track
            r1 = conn.execute("SELECT mb_trackid, track FROM items WHERE id=601").fetchone()
            self.assertEqual(r1[0], "77777777-7777-7777-7777-777777777777")
            self.assertEqual(r1[1], 1)

    # ── album_duplicate_merge_v1 (Wave 30) ────────────────────────────────────

    def test_duplicate_merge_moves_items_inherits_fields_and_retires_source(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album, albumartist, mb_albumid, year, label) VALUES (301, 'Album', 'Artist', '', 0, '')")
            conn.execute("INSERT INTO albums (id, album, albumartist, mb_albumid, year, label) VALUES (302, 'Album', 'Artist', '88888888-8888-8888-8888-888888888888', 1999, 'Indie Label')")
            conn.execute("INSERT INTO items (id, album_id, title) VALUES (701, 302, 'Track 1')")
            conn.execute("INSERT INTO items (id, album_id, title) VALUES (702, 302, 'Track 2')")

        plan = transaction_engine.create_album_duplicate_merge_plan(
            self.store, {"target_album_id": 301, "source_album_id": 302}, db_path=str(self.db_path),
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        self.assertEqual(plan.get("moved_item_count"), 2)
        self.assertEqual(plan.get("inherit_fields"), {"mb_albumid": "88888888-8888-8888-8888-888888888888", "year": 1999, "label": "Indie Label"})
        op_id = plan["operation_id"]

        apply_res = transaction_engine.execute_album_duplicate_merge_apply(self.store, op_id, db_path=str(self.db_path))
        self.assertTrue(apply_res.get("ok"), msg=apply_res.get("error"))
        self.assertEqual(apply_res.get("moved"), 2)

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT album_id FROM items WHERE id IN (701, 702)").fetchall()
            self.assertEqual({r[0] for r in rows}, {301})
            target = conn.execute("SELECT mb_albumid, year, label FROM albums WHERE id=301").fetchone()
            self.assertEqual(target, ("88888888-8888-8888-8888-888888888888", 1999, "Indie Label"))
            source = conn.execute("SELECT id FROM albums WHERE id=302").fetchone()
            self.assertIsNone(source)

    def test_duplicate_merge_apply_rejects_toctou_item_set_change(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album, albumartist) VALUES (301, 'Album', 'Artist')")
            conn.execute("INSERT INTO albums (id, album, albumartist) VALUES (302, 'Album', 'Artist')")
            conn.execute("INSERT INTO items (id, album_id, title) VALUES (701, 302, 'Track 1')")

        plan = transaction_engine.create_album_duplicate_merge_plan(
            self.store, {"target_album_id": 301, "source_album_id": 302}, db_path=str(self.db_path),
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        # A new item lands in the source album after Plan but before Apply.
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO items (id, album_id, title) VALUES (702, 302, 'Track 2')")

        apply_res = transaction_engine.execute_album_duplicate_merge_apply(self.store, op_id, db_path=str(self.db_path))
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "album_duplicate_merge_toctou_mismatch")

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT id FROM albums WHERE id=302").fetchone()
            self.assertIsNotNone(row, "source album must not be deleted when Apply refuses a stale plan")

    def test_duplicate_merge_rollback_restores_source_album_and_items(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album, albumartist, mb_albumid) VALUES (301, 'Album', 'Artist', '')")
            conn.execute("INSERT INTO albums (id, album, albumartist, mb_albumid) VALUES (302, 'Album', 'Artist', '99999999-9999-9999-9999-999999999999')")
            conn.execute("INSERT INTO items (id, album_id, title) VALUES (701, 302, 'Track 1')")

        plan = transaction_engine.create_album_duplicate_merge_plan(
            self.store, {"target_album_id": 301, "source_album_id": 302}, db_path=str(self.db_path),
        )
        op_id = plan["operation_id"]
        apply_res = transaction_engine.execute_album_duplicate_merge_apply(self.store, op_id, db_path=str(self.db_path))
        self.assertTrue(apply_res.get("ok"), msg=apply_res.get("error"))

        rollback_res = transaction_engine.rollback_album_duplicate_merge(self.store, op_id, db_path=str(self.db_path))
        self.assertTrue(rollback_res.get("ok"), msg=rollback_res)
        self.assertEqual(rollback_res.get("status"), "Rolled Back")

        with sqlite3.connect(self.db_path) as conn:
            source = conn.execute("SELECT id FROM albums WHERE id=302").fetchone()
            self.assertIsNotNone(source)
            item = conn.execute("SELECT album_id FROM items WHERE id=701").fetchone()
            self.assertEqual(item[0], 302)
            target = conn.execute("SELECT mb_albumid FROM albums WHERE id=301").fetchone()
            self.assertEqual(target[0], "")

    def test_duplicate_merge_rejects_same_or_missing_album_ids(self):
        same = transaction_engine.create_album_duplicate_merge_plan(
            self.store, {"target_album_id": 301, "source_album_id": 301}, db_path=str(self.db_path),
        )
        self.assertFalse(same.get("ok"))
        self.assertEqual(same.get("code"), "album_duplicate_merge_invalid_payload")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album, albumartist) VALUES (301, 'Album', 'Artist')")
        missing = transaction_engine.create_album_duplicate_merge_plan(
            self.store, {"target_album_id": 301, "source_album_id": 999}, db_path=str(self.db_path),
        )
        self.assertFalse(missing.get("ok"))
        self.assertEqual(missing.get("code"), "album_duplicate_merge_album_missing")

    # ── album_maintenance_v1 filename_cleanup fix_updates / repoint_db
    # (ARCH-003 Wave 31: this payload shape had zero path validation
    # before this wave, found unused by any real caller) ─────────────────

    def test_repoint_db_accepts_relative_stored_path_and_updates_exact_row(self):
        # Real Beets libraries commonly store items.path RELATIVE to the
        # music dir; the leaked-db-paths use case this exists for scans
        # exactly that kind of row.
        real_dir = self.music_dir / "Artist" / "Album"
        real_dir.mkdir(parents=True)
        (real_dir / "01 Track.mp3").write_bytes(b"audio")
        old_rel = "Artist/Album/%the{}/01 Track.mp3"
        new_rel = "Artist/Album/01 Track.mp3"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album) VALUES (50, 'Album')")
            conn.execute("INSERT INTO items (id, path, album_id) VALUES (500, ?, 50)", (old_rel.encode("utf-8"),))

        plan = transaction_engine.create_album_maintenance_plan(
            self.store,
            {"mode": "deduplicate", "album_id": 50, "fix_updates": [{"id": 500, "old_path": old_rel, "new_path": new_rel, "rename": False}]},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        apply_res = transaction_engine.execute_album_maintenance_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path),
        )
        self.assertTrue(apply_res.get("ok"), msg=apply_res.get("error"))
        # No physical move for a repoint -- the real file stays put.
        self.assertTrue((real_dir / "01 Track.mp3").exists())

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT path FROM items WHERE id=500").fetchone()
        self.assertEqual(row[0].decode("utf-8") if isinstance(row[0], bytes) else row[0], new_rel)

    def test_repoint_db_rejects_target_outside_allowed_root(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "escaped.mp3").write_bytes(b"audio")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album) VALUES (51, 'Album')")
            conn.execute("INSERT INTO items (id, path, album_id) VALUES (501, ?, 51)", (b"Artist/broken.mp3",))

        plan = transaction_engine.create_album_maintenance_plan(
            self.store,
            {"mode": "deduplicate", "album_id": 51, "fix_updates": [{
                "id": 501, "old_path": "Artist/broken.mp3", "new_path": str(outside / "escaped.mp3"), "rename": False,
            }]},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertFalse(plan.get("ok"))
        self.assertEqual(plan.get("code"), "album_maintenance_path_out_of_root")

    def test_repoint_db_rejects_when_target_does_not_exist(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album) VALUES (52, 'Album')")
            conn.execute("INSERT INTO items (id, path, album_id) VALUES (502, ?, 52)", (b"Artist/broken.mp3",))

        plan = transaction_engine.create_album_maintenance_plan(
            self.store,
            {"mode": "deduplicate", "album_id": 52, "fix_updates": [{
                "id": 502, "old_path": "Artist/broken.mp3", "new_path": "Artist/does-not-exist.mp3", "rename": False,
            }]},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertFalse(plan.get("ok"))
        self.assertEqual(plan.get("code"), "album_maintenance_item_missing")

    def test_repoint_db_apply_toctou_rejects_when_target_changed_since_plan(self):
        real_dir = self.music_dir / "Artist"
        real_dir.mkdir()
        target_file = real_dir / "01 Track.mp3"
        target_file.write_bytes(b"audio-v1")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO albums (id, album) VALUES (53, 'Album')")
            conn.execute("INSERT INTO items (id, path, album_id) VALUES (503, ?, 53)", (b"Artist/%the{}/01 Track.mp3",))

        plan = transaction_engine.create_album_maintenance_plan(
            self.store,
            {"mode": "deduplicate", "album_id": 53, "fix_updates": [{
                "id": 503, "old_path": "Artist/%the{}/01 Track.mp3", "new_path": "Artist/01 Track.mp3", "rename": False,
            }]},
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
        )
        self.assertTrue(plan.get("ok"), msg=plan.get("error"))
        op_id = plan["operation_id"]

        # Target file content/size changes between Plan and Apply.
        target_file.write_bytes(b"audio-v2-different-size!!")

        apply_res = transaction_engine.execute_album_maintenance_apply(
            self.store, op_id, music_allowed_roots=[str(self.music_dir)], db_path=str(self.db_path),
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "album_maintenance_toctou_mismatch")
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT path FROM items WHERE id=503").fetchone()
        self.assertEqual(row[0], b"Artist/%the{}/01 Track.mp3")


if __name__ == "__main__":
    unittest.main()
