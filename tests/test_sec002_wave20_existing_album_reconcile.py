"""SEC-002 / ARCH-003 Wave 20: Existing-Album Duplicate & Move Bookkeeping Controlled Mutation Boundary.

Comprehensive focused test suite for existing_album_reconcile_v1 mutation family.
"""
import ast
import math
import os
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
import wave
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from backend.transaction_engine import (
    TransactionStore,
    create_existing_album_reconcile_plan,
    execute_existing_album_reconcile_apply,
    rollback_existing_album_reconcile,
    _read_file_audio_tags,
)
import app as app_module

ITEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    album TEXT,
    albumartist TEXT,
    mb_albumid TEXT,
    mb_releasegroupid TEXT,
    year INTEGER
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    album_id INTEGER,
    title TEXT,
    artist TEXT,
    album TEXT,
    albumartist TEXT,
    disc INTEGER,
    track INTEGER,
    path BLOB,
    mb_trackid TEXT,
    mb_albumid TEXT,
    mb_releasegroupid TEXT,
    length REAL,
    size INTEGER,
    mtime REAL
);
"""

RG_A = "aaaaaaaa-0000-0000-0000-000000000000"
RG_B = "bbbbbbbb-0000-0000-0000-000000000000"
REL_A = "11111111-1111-1111-1111-111111111111"
REC_1 = "33333333-3333-3333-3333-333333333331"
REC_2 = "33333333-3333-3333-3333-333333333332"
REC_3 = "33333333-3333-3333-3333-333333333333"


def _write_test_audio(path: Path, *, freq: float = 220.0, duration: float = 0.2) -> None:
    """Write a real, minimal, playable WAV file. SEC-002 Wave 20 final
    review: the original fixture wrote fake b"AUDIO_DATA_N" bytes, which the
    new survivor-readability check (_read_file_audio_tags) correctly
    refuses to treat as usable media -- exactly the "survivor must be
    readable / valid media" requirement this review closes. Real tests need
    real, independently-readable audio (self-synthesized sine wave, same
    technique as scripts/seed_demo_library.py's demo library)."""
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


class Wave20FixtureBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

        self.music_root = self.tmp_path / "music"
        self.music_root.mkdir()
        self.staging_root = self.tmp_path / "staging"
        self.staging_root.mkdir()
        self.quarantine_root = self.tmp_path / "quarantine"
        self.quarantine_root.mkdir()
        self.outside_root = self.tmp_path / "outside"
        self.outside_root.mkdir()

        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir()
        self.store = TransactionStore(root=str(store_dir))

        self.db_path = self.tmp_path / "musiclibrary.db"
        with sqlite3.connect(self.db_path) as con:
            con.executescript(ITEMS_SCHEMA)
            con.commit()

        app_module.app.config["TESTING"] = True

        @contextmanager
        def _mock_db_cm(*args, **kwargs):
            con = sqlite3.connect(self.db_path)
            if "text_factory" in kwargs:
                con.text_factory = kwargs["text_factory"]
            if "row_factory" in kwargs:
                con.row_factory = kwargs["row_factory"]
            try:
                yield con
            finally:
                con.close()

        self._db_patch = mock.patch.object(app_module, "_db", side_effect=_mock_db_cm)
        self._db_patch.start()

        self._env_patch = mock.patch.dict(os.environ, {
            "BEETS_WEB_AUTH_DISABLED": "1",
            "RECONCILE_QUARANTINE_DIR": str(self.quarantine_root),
        })
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._db_patch.stop()
        try:
            self._tmpdir.cleanup()
        except Exception:
            pass

    # Convenience: Plan/Apply wrappers that always pass staging_allowed_roots
    # (SEC-002 Wave 20 final review: source_folder/staging paths are only
    # ever authorized against a SERVER-configured staging root list, never
    # accepted merely because the request claims one -- tests must supply
    # that root explicitly, the same way the real control-agent route does).
    def _plan(self, payload, *, roots=None, staging_roots=None):
        return create_existing_album_reconcile_plan(
            self.store, payload,
            music_allowed_roots=roots if roots is not None else [str(self.music_root)],
            staging_allowed_roots=staging_roots if staging_roots is not None else [str(self.staging_root)],
            db_path=str(self.db_path),
        )

    def _apply(self, op_id, *, roots=None, staging_roots=None):
        return execute_existing_album_reconcile_apply(
            self.store, op_id,
            db_path=str(self.db_path),
            music_allowed_roots=roots if roots is not None else [str(self.music_root)],
            staging_allowed_roots=staging_roots if staging_roots is not None else [str(self.staging_root)],
        )

    def _rollback(self, op_id, *, roots=None):
        return rollback_existing_album_reconcile(
            self.store, op_id,
            db_path=str(self.db_path),
            music_allowed_roots=roots if roots is not None else [str(self.music_root)],
        )

    def _create_album(self, album_id: int, title: str, artist: str = "Test Artist", rg_id: str = RG_A, rel_id: str = REL_A):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO albums (id, album, albumartist, mb_albumid, mb_releasegroupid, year) VALUES (?, ?, ?, ?, ?, ?)",
                (album_id, title, artist, rel_id, rg_id, 2024),
            )
            con.commit()

    def _create_item(
        self,
        item_id: int,
        album_id: int,
        title: str,
        disc: int,
        track: int,
        filename: str,
        staging: bool = False,
        outside: bool = False,
        rec_id: str = REC_1,
        rg_id: str = RG_A,
        write_audio: bool = True,
    ) -> Path:
        root = self.outside_root if outside else (self.staging_root if staging else self.music_root)
        album_dir = root / f"album_{album_id}"
        album_dir.mkdir(parents=True, exist_ok=True)
        file_path = album_dir / filename
        if write_audio:
            _write_test_audio(file_path, freq=220.0 + item_id)
        else:
            file_path.write_bytes(b"NOT_REAL_AUDIO_" + str(item_id).encode())

        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    album_id,
                    title,
                    "Test Artist",
                    "Test Album",
                    "Test Artist",
                    disc,
                    track,
                    str(file_path),
                    rec_id,
                    REL_A,
                    rg_id,
                    180.0,
                ),
            )
            con.commit()
        return file_path


class PlanTests(Wave20FixtureBase):
    def test_create_plan_valid(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        surv_path = self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        move_path = self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.wav", staging=True)
        dup_path = self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.wav", staging=True)

        res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "move_item_ids": [20],
            "dup_item_ids": [21],
            "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
            "source_folder": str(self.staging_root),
        })
        self.assertTrue(res.get("ok"), res)
        op_id = res["operation_id"]
        self.assertEqual(res["reconciled_moves"], 1)
        self.assertEqual(res["reconciled_duplicates"], 1)

        tx = self.store.get(op_id)
        self.assertEqual(tx["status"], "Preview")
        self.assertEqual(tx["metadata"]["mutation_family"], "existing_album_reconcile_v1")

    def test_plan_non_mutating(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        move_path = self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.wav", staging=True)
        dup_path = self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.wav", staging=True)

        mtime_dup_before = dup_path.stat().st_mtime_ns

        res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "move_item_ids": [20],
            "dup_item_ids": [21],
            "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
            "source_folder": str(self.staging_root),
        })
        self.assertTrue(res.get("ok"), res)

        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            move_row = con.execute("SELECT album_id FROM items WHERE id=20").fetchone()
            dup_row = con.execute("SELECT album_id FROM items WHERE id=21").fetchone()
            alb_row = con.execute("SELECT id FROM albums WHERE id=2").fetchone()

        self.assertEqual(move_row["album_id"], 2)
        self.assertEqual(dup_row["album_id"], 2)
        self.assertIsNotNone(alb_row)

        self.assertTrue(dup_path.exists())
        self.assertEqual(dup_path.stat().st_mtime_ns, mtime_dup_before)

    def test_plan_release_group_mismatch(self):
        self._create_album(1, "Existing Album", rg_id=RG_A)
        self._create_album(2, "Imported Temp Album", rg_id=RG_B)

        self._create_item(20, 2, "Track 1", 1, 1, "track1.wav", staging=True, rg_id=RG_B)

        res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "move_item_ids": [20],
            "source_folder": str(self.staging_root),
        })
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "reconcile_identity_mismatch")

    def test_plan_no_candidate_survivor_requires_review(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.wav", staging=True)

        res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "dup_item_ids": [21],
            "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [999]}],
            "source_folder": str(self.staging_root),
        })
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("reconciled_duplicates"), 0)
        review = res.get("dup_requires_review") or []
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["item_id"], 21)

    def test_plan_symlink_rejected(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        target_file = self._create_item(20, 2, "Track 1", 1, 1, "real_track1.wav", staging=True)
        symlink_file = self.staging_root / "album_2" / "link_track1.wav"
        try:
            os.symlink(target_file, symlink_file)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported on host environment")

        with sqlite3.connect(self.db_path) as con:
            con.execute("UPDATE items SET path=? WHERE id=20", (str(symlink_file),))
            con.commit()

        res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "move_item_ids": [20],
            "source_folder": str(self.staging_root),
        })
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "reconcile_symlink_rejected")

    def test_plan_hardlink_handling(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        surv_path = self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        dup_path = self.staging_root / "album_2" / "track1_hardlink.wav"
        dup_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(surv_path, dup_path)
        except (OSError, NotImplementedError):
            self.skipTest("Hardlinks not supported on host environment")

        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length) "
                "VALUES (21, 2, 'Track 1', 'Test Artist', 'Test Album', 'Test Artist', 1, 1, ?, ?, ?, ?, 180.0)",
                (str(dup_path), REC_1, REL_A, RG_A),
            )
            con.commit()

        res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "dup_item_ids": [21],
            "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
            "source_folder": str(self.staging_root),
        })
        self.assertTrue(res.get("ok"), res)
        tx = self.store.get(res["operation_id"])
        dup_change = [c for c in tx["changes"] if c.get("item_id") == 21][0]
        self.assertEqual(dup_change.get("physical_class"), "hardlink")

    def test_plan_same_path_duplicate_classified_db_only(self):
        """SEC-002 Wave 20 final review: two Beets rows pointing at the
        exact same path must never result in a physical unlink -- that
        would destroy the survivor's only copy too."""
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        surv_path = self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length) "
                "VALUES (21, 2, 'Track 1', 'Test Artist', 'Test Album', 'Test Artist', 1, 1, ?, ?, ?, ?, 180.0)",
                (str(surv_path), REC_1, REL_A, RG_A),
            )
            con.commit()

        res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "dup_item_ids": [21],
            "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
        })
        self.assertTrue(res.get("ok"), res)
        tx = self.store.get(res["operation_id"])
        dup_change = [c for c in tx["changes"] if c.get("item_id") == 21][0]
        self.assertEqual(dup_change.get("physical_class"), "db_only")

    def test_plan_conflicting_recording_id_fails_closed(self):
        """SEC-002 Wave 20 final review, critical finding #3: different
        KNOWN Recording IDs must never be treated as a safe duplicate
        relation, even when disc/track position matches exactly."""
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav", rec_id=REC_1)
        self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.wav", staging=True, rec_id=REC_2)

        res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "dup_item_ids": [21],
            "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
            "source_folder": str(self.staging_root),
        })
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("reconciled_duplicates"), 0)
        review = res.get("dup_requires_review") or []
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["reason"], "identity_conflict")

        # The duplicate row must be completely untouched.
        with sqlite3.connect(self.db_path) as con:
            row = con.execute("SELECT COUNT(*) FROM items WHERE id=21").fetchone()[0]
        self.assertEqual(row, 1)

    def test_plan_blank_recording_ids_require_review(self):
        """Blank Recording ID on either side is not enough evidence for
        automatic retirement on its own -- surfaced for review instead."""
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav", rec_id="")
        self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.wav", staging=True, rec_id="")

        res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "dup_item_ids": [21],
            "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
            "source_folder": str(self.staging_root),
        })
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("reconciled_duplicates"), 0)
        review = res.get("dup_requires_review") or []
        self.assertEqual(len(review), 1)

    def test_plan_unreadable_survivor_requires_review(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav", write_audio=False)
        self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.wav", staging=True)

        res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "dup_item_ids": [21],
            "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
            "source_folder": str(self.staging_root),
        })
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("reconciled_duplicates"), 0)
        review = res.get("dup_requires_review") or []
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["reason"], "survivor_unverified")

    def test_plan_source_folder_cannot_expand_authorization(self):
        """SEC-002 Wave 20 final review, critical finding #8/#49: an
        unvalidated source_folder must never widen what paths are
        authorized. A move item outside every server-configured root
        (library or staging) must be refused even if the client claims
        source_folder points right at it."""
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        outside_item = self._create_item(20, 2, "Track 2", 1, 2, "track2.wav", outside=True)

        res = self._plan(
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "move_item_ids": [20],
                # Attacker-controlled: claims the outside root is the
                # authorized source folder. It is NOT one of the server's
                # configured staging roots, so it must not be trusted.
                "source_folder": str(self.outside_root),
            },
            staging_roots=[str(self.staging_root)],
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "reconcile_path_out_of_root")

    def test_plan_payload_rejects_duplicate_ids(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")
        self._create_item(20, 2, "Track 2", 1, 2, "track2.wav", staging=True)

        res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "move_item_ids": [20, 20],
        })
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "reconcile_payload_overlap")

    def test_plan_payload_rejects_dup_move_overlap(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")
        self._create_item(20, 2, "Track 2", 1, 2, "track2.wav", staging=True)

        res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "move_item_ids": [20],
            "dup_item_ids": [20],
        })
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "reconcile_payload_overlap")


class ApplyTests(Wave20FixtureBase):
    def test_apply_successful_reconciliation(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        move_path = self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.wav", staging=True)
        dup_path = self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.wav", staging=True)

        plan_res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "move_item_ids": [20],
            "dup_item_ids": [21],
            "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
            "source_folder": str(self.staging_root),
        })
        self.assertTrue(plan_res.get("ok"), plan_res)
        op_id = plan_res["operation_id"]

        apply_res = self._apply(op_id)
        self.assertTrue(apply_res.get("ok"), apply_res)
        self.assertEqual(apply_res["status"], "Completed")

        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            move_row = con.execute("SELECT album_id FROM items WHERE id=20").fetchone()
            dup_row = con.execute("SELECT COUNT(*) FROM items WHERE id=21").fetchone()[0]
            alb_row = con.execute("SELECT COUNT(*) FROM albums WHERE id=2").fetchone()[0]

        self.assertEqual(move_row["album_id"], 1)
        self.assertEqual(dup_row, 0)
        self.assertEqual(alb_row, 0)

        # The duplicate's original path is gone, but -- unlike the
        # original implementation -- it was quarantined, not permanently
        # unlinked, so rollback_available is truthful.
        self.assertFalse(dup_path.exists())
        tx = self.store.get(op_id)
        quarantined = tx["metadata"].get("quarantined_files") or []
        self.assertEqual(len(quarantined), 1)
        self.assertTrue(Path(quarantined[0]["quarantine_path"]).exists())
        self.assertTrue(tx["metadata"]["db_mutated"])
        self.assertTrue(tx["metadata"]["filesystem_mutated"])

    def test_apply_same_path_duplicate_preserves_file(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        surv_path = self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length) "
                "VALUES (21, 2, 'Track 1', 'Test Artist', 'Test Album', 'Test Artist', 1, 1, ?, ?, ?, ?, 180.0)",
                (str(surv_path), REC_1, REL_A, RG_A),
            )
            con.commit()

        plan_res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "dup_item_ids": [21],
            "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
        })
        self.assertTrue(plan_res.get("ok"), plan_res)
        apply_res = self._apply(plan_res["operation_id"])
        self.assertTrue(apply_res.get("ok"), apply_res)

        # The shared file must still exist -- it backs the survivor too.
        self.assertTrue(surv_path.exists())
        with sqlite3.connect(self.db_path) as con:
            cnt = con.execute("SELECT COUNT(*) FROM items WHERE id=21").fetchone()[0]
            surv_cnt = con.execute("SELECT COUNT(*) FROM items WHERE id=10").fetchone()[0]
        self.assertEqual(cnt, 0)
        self.assertEqual(surv_cnt, 1)

    def test_apply_stale_source_row(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.wav", staging=True)

        plan_res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "move_item_ids": [20],
            "source_folder": str(self.staging_root),
        })
        op_id = plan_res["operation_id"]

        with sqlite3.connect(self.db_path) as con:
            con.execute("UPDATE items SET album_id=99 WHERE id=20")
            con.commit()

        apply_res = self._apply(op_id)
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "reconcile_album_membership_changed")

    def test_apply_stale_survivor(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.wav", staging=True)

        plan_res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "dup_item_ids": [21],
            "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
            "source_folder": str(self.staging_root),
        })
        op_id = plan_res["operation_id"]

        with sqlite3.connect(self.db_path) as con:
            con.execute("DELETE FROM items WHERE id=10")
            con.commit()

        apply_res = self._apply(op_id)
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "reconcile_survivor_stale")

    def test_apply_survivor_identity_changed(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav", rec_id=REC_1)
        self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.wav", staging=True, rec_id=REC_1)

        plan_res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "dup_item_ids": [21],
            "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
            "source_folder": str(self.staging_root),
        })
        op_id = plan_res["operation_id"]

        with sqlite3.connect(self.db_path) as con:
            con.execute("UPDATE items SET mb_trackid=? WHERE id=10", (REC_3,))
            con.commit()

        apply_res = self._apply(op_id)
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "reconcile_survivor_stale")

    def test_apply_mtime_changed(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        move_path = self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.wav", staging=True)

        plan_res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "move_item_ids": [20],
            "source_folder": str(self.staging_root),
        })
        op_id = plan_res["operation_id"]

        time.sleep(0.01)
        move_path.write_bytes(b"MODIFIED_AFTER_PLAN")

        apply_res = self._apply(op_id)
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "reconcile_toctou_mismatch")

    def test_apply_already_completed_replay(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.wav", staging=True)

        plan_res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "move_item_ids": [20],
            "source_folder": str(self.staging_root),
        })
        op_id = plan_res["operation_id"]

        apply1 = self._apply(op_id)
        self.assertTrue(apply1.get("ok"))

        apply2 = self._apply(op_id)
        self.assertTrue(apply2.get("ok"))
        self.assertTrue(apply2.get("already_completed"))

    def test_apply_wrong_mutation_family(self):
        tx = self.store.create(
            operation_type="Merge Album",
            status="Preview",
            summary="Fake family",
            metadata={"mutation_family": "track_replacement_v1"},
        )
        res = self._apply(tx["id"])
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "reconcile_wrong_family")

    def test_apply_album_rgid_changed_since_plan(self):
        self._create_album(1, "Existing Album", rg_id=RG_A)
        self._create_album(2, "Imported Temp Album", rg_id=RG_A)
        self._create_item(20, 2, "Track 2", 1, 2, "track2.wav", staging=True, rg_id=RG_A)

        plan_res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "move_item_ids": [20],
            "source_folder": str(self.staging_root),
        })
        op_id = plan_res["operation_id"]

        with sqlite3.connect(self.db_path) as con:
            con.execute("UPDATE albums SET mb_releasegroupid=? WHERE id=1", (RG_B,))
            con.commit()

        apply_res = self._apply(op_id)
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "reconcile_identity_mismatch")


class ReleaseOnlyAndRetirementTests(Wave20FixtureBase):
    def test_album_retirement_exact_set_logic(self):
        """SEC-002 Wave 20 final review, finding #26: retirement must be
        based on the exact affected-ID set matching current membership,
        not a count comparison."""
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        self._create_item(20, 2, "Track 2", 1, 2, "track2.wav", staging=True)
        # A second, unrelated item remains in the imported album and is
        # NOT part of this reconciliation -- the album must NOT retire.
        self._create_item(22, 2, "Track 3", 1, 3, "track3.wav", staging=True)

        res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "move_item_ids": [20],
            "source_folder": str(self.staging_root),
        })
        self.assertTrue(res.get("ok"), res)
        tx = self.store.get(res["operation_id"])
        self.assertFalse(tx["metadata"]["retire_imported_album"])

        apply_res = self._apply(res["operation_id"])
        self.assertTrue(apply_res.get("ok"), apply_res)
        with sqlite3.connect(self.db_path) as con:
            alb_cnt = con.execute("SELECT COUNT(*) FROM albums WHERE id=2").fetchone()[0]
        self.assertEqual(alb_cnt, 1)


class ConcurrencyTests(Wave20FixtureBase):
    def test_concurrency_same_operation_twice(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.wav", staging=True)

        plan_res = self._plan({
            "imported_album_id": 2,
            "existing_album_id": 1,
            "move_item_ids": [20],
            "source_folder": str(self.staging_root),
        })
        op_id = plan_res["operation_id"]

        results = []
        def _worker():
            results.append(self._apply(op_id))

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].get("ok"))
        self.assertTrue(results[1].get("ok"))

    def test_concurrency_unrelated_albums(self):
        self._create_album(1, "Existing Album 1")
        self._create_album(2, "Imported Temp Album 1")
        self._create_album(3, "Existing Album 2")
        self._create_album(4, "Imported Temp Album 2")

        self._create_item(10, 1, "Track 1", 1, 1, "alb1_track1.wav")
        self._create_item(20, 2, "Track 2", 1, 2, "alb2_track2.wav", staging=True)
        self._create_item(30, 3, "Track 1", 1, 1, "alb3_track1.wav")
        self._create_item(40, 4, "Track 2", 1, 2, "alb4_track2.wav", staging=True)

        plan1 = self._plan({
            "imported_album_id": 2, "existing_album_id": 1, "move_item_ids": [20],
            "source_folder": str(self.staging_root),
        })
        plan2 = self._plan({
            "imported_album_id": 4, "existing_album_id": 3, "move_item_ids": [40],
            "source_folder": str(self.staging_root),
        })

        res1, res2 = [], []
        t1 = threading.Thread(target=lambda: res1.append(self._apply(plan1["operation_id"])))
        t2 = threading.Thread(target=lambda: res2.append(self._apply(plan2["operation_id"])))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertTrue(res1[0].get("ok"), res1[0])
        self.assertTrue(res2[0].get("ok"), res2[0])

    def test_concurrency_apply_vs_rollback_serializes(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")
        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        self._create_item(20, 2, "Track 2", 1, 2, "track2.wav", staging=True)

        plan_res = self._plan({
            "imported_album_id": 2, "existing_album_id": 1, "move_item_ids": [20],
            "source_folder": str(self.staging_root),
        })
        op_id = plan_res["operation_id"]
        apply_res = self._apply(op_id)
        self.assertTrue(apply_res.get("ok"), apply_res)

        results = []
        def _rollback_worker():
            results.append(self._rollback(op_id))

        t1 = threading.Thread(target=_rollback_worker)
        t2 = threading.Thread(target=_rollback_worker)
        t1.start(); t2.start(); t1.join(); t2.join()

        # Exactly one rollback should succeed; the other must see the
        # already-rolled-back state and refuse cleanly, never race.
        ok_count = sum(1 for r in results if r.get("ok"))
        self.assertEqual(ok_count, 1)


class RollbackTests(Wave20FixtureBase):
    def test_rollback_full_success(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.wav", staging=True)
        dup_path = self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.wav", outside=True)

        plan_res = self._plan(
            {
                "imported_album_id": 2,
                "existing_album_id": 1,
                "move_item_ids": [20],
                "dup_item_ids": [21],
                "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
                "source_folder": str(self.staging_root),
            },
            roots=[str(self.music_root), str(self.outside_root)],
        )
        self.assertTrue(plan_res.get("ok"), plan_res)
        op_id = plan_res["operation_id"]

        apply_res = self._apply(op_id, roots=[str(self.music_root), str(self.outside_root)])
        self.assertTrue(apply_res.get("ok"), apply_res)

        rollback_res = self._rollback(op_id, roots=[str(self.music_root), str(self.outside_root)])
        self.assertTrue(rollback_res.get("ok"), rollback_res)
        self.assertEqual(rollback_res["status"], "Rolled Back")

        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            move_row = con.execute("SELECT album_id FROM items WHERE id=20").fetchone()
            dup_row = con.execute("SELECT album_id, title FROM items WHERE id=21").fetchone()
            alb_row = con.execute("SELECT id FROM albums WHERE id=2").fetchone()

        self.assertEqual(move_row["album_id"], 2)
        self.assertIsNotNone(dup_row)
        self.assertEqual(dup_row["album_id"], 2)
        self.assertEqual(dup_row["title"], "Track 1")
        self.assertIsNotNone(alb_row)
        self.assertTrue(dup_path.exists())

    def test_rollback_repeated_refused(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")
        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        self._create_item(20, 2, "Track 2", 1, 2, "track2.wav", staging=True)

        plan_res = self._plan({
            "imported_album_id": 2, "existing_album_id": 1, "move_item_ids": [20],
            "source_folder": str(self.staging_root),
        })
        op_id = plan_res["operation_id"]
        self.assertTrue(self._apply(op_id).get("ok"))

        first = self._rollback(op_id)
        self.assertTrue(first.get("ok"), first)
        second = self._rollback(op_id)
        self.assertFalse(second.get("ok"))
        self.assertEqual(second.get("code"), "reconcile_already_rolled_back")

    def test_rollback_unmutated_refused(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")
        self._create_item(20, 2, "Track 2", 1, 2, "track2.wav", staging=True)

        plan_res = self._plan({
            "imported_album_id": 2, "existing_album_id": 1, "move_item_ids": [20],
            "source_folder": str(self.staging_root),
        })
        res = self._rollback(plan_res["operation_id"])
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "reconcile_not_mutated")

    def test_rollback_stale_after_manual_edit_refused(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")
        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        self._create_item(20, 2, "Track 2", 1, 2, "track2.wav", staging=True)

        plan_res = self._plan({
            "imported_album_id": 2, "existing_album_id": 1, "move_item_ids": [20],
            "source_folder": str(self.staging_root),
        })
        op_id = plan_res["operation_id"]
        self.assertTrue(self._apply(op_id).get("ok"))

        # A later, legitimate manual edit reassigns the moved item again.
        with sqlite3.connect(self.db_path) as con:
            con.execute("UPDATE items SET album_id=99 WHERE id=20")
            con.commit()

        res = self._rollback(op_id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "reconcile_rollback_stale")

        with sqlite3.connect(self.db_path) as con:
            row = con.execute("SELECT album_id FROM items WHERE id=20").fetchone()
        self.assertEqual(row[0], 99)

    def test_rollback_destination_collision_refused(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")
        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        dup_path = self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.wav", staging=True)

        plan_res = self._plan({
            "imported_album_id": 2, "existing_album_id": 1,
            "dup_item_ids": [21],
            "dup_details": [{"dup_item_id": 21, "survivor_item_ids": [10]}],
            "source_folder": str(self.staging_root),
        })
        op_id = plan_res["operation_id"]
        apply_res = self._apply(op_id)
        self.assertTrue(apply_res.get("ok"), apply_res)
        self.assertFalse(dup_path.exists())

        # Something else now occupies the original duplicate path.
        dup_path.parent.mkdir(parents=True, exist_ok=True)
        dup_path.write_bytes(b"UNRELATED_NEW_FILE")

        rollback_res = self._rollback(op_id)
        self.assertFalse(rollback_res.get("ok"))
        self.assertIn(rollback_res.get("status"), ("Partially Rolled Back", "Failed"))
        # The unrelated file must survive untouched.
        self.assertEqual(dup_path.read_bytes(), b"UNRELATED_NEW_FILE")


class Wave18InteractionTests(Wave20FixtureBase):
    def test_merge_job_routes_replace_rows_to_wave18_and_dup_move_to_wave20(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1 Old", 1, 1, "track1_existing.wav")
        self._create_item(20, 2, "Track 1 New", 1, 1, "track1_imported.wav", staging=True)
        self._create_item(21, 2, "Track 2 New", 1, 2, "track2_imported.wav", staging=True)

        plan_bulk_mock = mock.MagicMock(return_value={"ok": True, "operation_id": "op_bulk_18"})
        apply_bulk_mock = mock.MagicMock(return_value={"ok": True})
        plan_rec_mock = mock.MagicMock(return_value={"ok": True, "operation_id": "op_rec_20"})
        apply_rec_mock = mock.MagicMock(return_value={"ok": True})

        with mock.patch.object(app_module.beets_client, "plan_bulk_import_replacement", plan_bulk_mock), \
             mock.patch.object(app_module.beets_client, "apply_bulk_import_replacement", apply_bulk_mock), \
             mock.patch.object(app_module.beets_client, "plan_existing_album_reconcile", plan_rec_mock), \
             mock.patch.object(app_module.beets_client, "apply_existing_album_reconcile", apply_rec_mock):

            res = app_module._merge_imported_album_into_existing(
                2, 1, str(self.staging_root), [], mb_albumid=""
            )

        plan_rec_mock.assert_called_once()
        apply_rec_mock.assert_called_once_with("op_rec_20")
        self.assertEqual(res, 1)

    def test_merge_job_reports_failure_truthfully(self):
        """SEC-002 Wave 20 final review, findings #45/#46: a failed engine
        Apply must not be reported as a successful merge upstream."""
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")
        self._create_item(10, 1, "Track 1 Old", 1, 1, "track1_existing.wav")
        self._create_item(20, 2, "Track 1 New", 1, 1, "track1_imported.wav", staging=True)

        plan_rec_mock = mock.MagicMock(return_value={"ok": True, "operation_id": "op_rec_20"})
        apply_rec_mock = mock.MagicMock(return_value={"ok": False, "error": "simulated engine failure"})

        with mock.patch.object(app_module.beets_client, "plan_existing_album_reconcile", plan_rec_mock), \
             mock.patch.object(app_module.beets_client, "apply_existing_album_reconcile", apply_rec_mock):
            res = app_module._merge_imported_album_into_existing(
                2, 1, str(self.staging_root), [], mb_albumid=""
            )

        # Apply failed -- the function must NOT claim the merge succeeded.
        self.assertEqual(res, 2)


class RealProductionPathIntegrationTests(Wave20FixtureBase):
    def test_production_path_existing_album_reconciliation(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1", 1, 1, "track1_existing.wav")
        self._create_item(20, 2, "Track 2", 1, 2, "track2_imported.wav", staging=True)
        self._create_item(21, 2, "Track 1", 1, 1, "track1_imported.wav", staging=True)

        def _mock_plan(payload, **kwargs):
            return self._plan(payload)

        def _mock_apply(operation_id, **kwargs):
            return self._apply(operation_id)

        mb_mock = mock.MagicMock(return_value={
            "ok": True,
            "tracks": [
                {"disc": 1, "track": 1, "mb_trackid": REC_1, "title": "Track 1"},
                {"disc": 1, "track": 2, "mb_trackid": REC_2, "title": "Track 2"},
            ],
        })

        with mock.patch.object(app_module.beets_client, "plan_existing_album_reconcile", side_effect=_mock_plan), \
             mock.patch.object(app_module.beets_client, "apply_existing_album_reconcile", side_effect=_mock_apply), \
             mock.patch.object(app_module, "_fetch_mb_release_tracklist", mb_mock), \
             mock.patch.object(app_module, "_guard_existing_track_can_block_downloaded_replacement", return_value=True):

            log = []
            res_aid = app_module._merge_imported_album_into_existing(
                2, 1, str(self.staging_root), log, mb_albumid=REL_A
            )
            self.assertEqual(res_aid, 1)

        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            move_row = con.execute("SELECT album_id FROM items WHERE id=20").fetchone()
            dup_cnt = con.execute("SELECT COUNT(*) FROM items WHERE id=21").fetchone()[0]
            alb_cnt = con.execute("SELECT COUNT(*) FROM albums WHERE id=2").fetchone()[0]

        self.assertEqual(move_row["album_id"], 1)
        self.assertEqual(dup_cnt, 0)
        self.assertEqual(alb_cnt, 0)


class WebManagerMutationProhibitionTests(unittest.TestCase):
    def test_merge_imported_album_into_existing_contains_no_direct_mutations(self):
        app_path = Path(app_module.__file__)
        source = app_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        merge_fn_def = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_merge_imported_album_into_existing":
                merge_fn_def = node
                break

        self.assertIsNotNone(merge_fn_def, "_merge_imported_album_into_existing not found in app.py")
        fn_source = ast.get_source_segment(source, merge_fn_def)

        prohibited_strings = [
            "Path.unlink", "os.unlink", "os.remove", "os.rename", "os.replace",
            "shutil.move", "shutil.rmtree", "DELETE FROM items", "UPDATE items SET album_id",
            "DELETE FROM albums", "_beet_run",
        ]
        for p in prohibited_strings:
            self.assertNotIn(p, fn_source, f"Prohibited call '{p}' found in _merge_imported_album_into_existing")


if __name__ == "__main__":
    unittest.main()
