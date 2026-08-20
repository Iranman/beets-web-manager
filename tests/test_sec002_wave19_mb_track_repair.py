"""SEC-002 / ARCH-003 Wave 19: MusicBrainz Album Track Repair Controlled Mutation Boundary.

Comprehensive focused test suite for album_mb_track_repair_v1 mutation family.
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
    create_album_mb_track_repair_plan,
    execute_album_mb_track_repair_apply,
    rollback_album_mb_track_repair,
    _read_file_audio_tags,
    _write_file_audio_tags,
)
from backend.beets_client import BeetsClient
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
REL_B = "22222222-2222-2222-2222-222222222222"
REC_1 = "33333333-3333-3333-3333-333333333331"
REC_2 = "33333333-3333-3333-3333-333333333332"
REC_TARGET_1 = "44444444-4444-4444-4444-444444444441"
REC_TARGET_2 = "44444444-4444-4444-4444-444444444442"


def _fake_tracklist_a(rel_id=REL_A, rg_id=RG_A):
    return {
        "ok": True,
        "release_id": rel_id,
        "release_group": rg_id,
        "release_title": "Test Album Title",
        "tracks": [
            {
                "disc": 1,
                "track": 1,
                "title": "Track 1 Fixed Title",
                "mb_trackid": REC_TARGET_1,
                "duration_ms": 180000,
            },
            {
                "disc": 1,
                "track": 2,
                "title": "Track 2 Fixed Title",
                "mb_trackid": REC_TARGET_2,
                "duration_ms": 200000,
            },
        ],
    }


def _write_test_audio(path: Path, *, freq: float = 220.0, duration: float = 0.2,
                       mb_trackid: str = "", mb_albumid: str = "", title: str = "",
                       track: int = 0, disc: int = 1) -> None:
    """Write a real, minimal, playable WAV file with real MusicBrainz tags.

    SEC-002 Wave 19 final review: the original fixture wrote fake bytes
    (b"FLAC_DATA_1") into a .flac-named file, which no real tag reader can
    parse. That made every Apply/rollback test pass only because
    `_write_file_audio_tags` had a `Path.touch()` fallback that reported
    success without writing anything -- exactly the false-success defect
    this review exists to close. Tests that exercise real tag writes need
    real, independently-readable audio (self-synthesized sine wave, same
    technique as `scripts/seed_demo_library.py`'s demo library -- no
    copyrighted material, no external fixture download).
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

    import mediafile
    mf = mediafile.MediaFile(path)
    mf.mb_trackid = mb_trackid or ""
    mf.mb_albumid = mb_albumid or ""
    mf.title = title or ""
    mf.track = track or 0
    mf.disc = disc or 1
    mf.save()


class Wave19FixtureBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)

        self.music_root = self.tmp_path / "music"
        self.music_root.mkdir()
        self.outside_root = self.tmp_path / "outside"
        self.outside_root.mkdir()

        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir()
        self.store = TransactionStore(root=str(store_dir))

        self.db_path = self.tmp_path / "musiclibrary.db"
        con = sqlite3.connect(self.db_path)
        con.executescript(ITEMS_SCHEMA)
        con.commit()
        con.close()

        app_module.app.config["TESTING"] = True
        self._env_patch = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def _create_album_and_items(self, album_id=1, rg_id=RG_A, rel_id=REL_A, item_count=2, outside=False,
                                 blank_recording_ids=True, mb_albumid_override=None):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO albums (id, album, albumartist, mb_albumid, mb_releasegroupid, year) VALUES (?, ?, ?, ?, ?, ?)",
            (album_id, "Test Album Title", "Test Artist", rel_id, rg_id, 2024),
        )
        item_paths = []
        for i in range(1, item_count + 1):
            root = self.outside_root if outside else self.music_root
            album_dir = root / f"album_{album_id}"
            album_dir.mkdir(parents=True, exist_ok=True)
            file_path = album_dir / f"track{i}.wav"

            rec_id = "" if blank_recording_ids else (REC_1 if i == 1 else REC_2)
            title = f"Track {i} Old Title"
            item_albumid = mb_albumid_override if mb_albumid_override is not None else rel_id
            _write_test_audio(
                file_path, freq=220.0 * i, duration=0.2,
                mb_trackid=rec_id, mb_albumid=item_albumid, title=title, track=i, disc=1,
            )

            con.execute(
                "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    i + (album_id - 1) * 10,
                    album_id,
                    title,
                    "Test Artist",
                    "Test Album Title",
                    "Test Artist",
                    1,
                    i,
                    str(file_path),
                    rec_id,
                    item_albumid,
                    rg_id,
                    180.0 if i == 1 else 200.0,
                ),
            )
            item_paths.append(file_path)
        con.commit()
        con.close()
        return item_paths


class PlanTests(Wave19FixtureBase):
    def test_create_plan_valid(self):
        self._create_album_and_items(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"), res)
        op_id = res["operation_id"]
        self.assertEqual(res["updated"], 2)

        tx = self.store.get(op_id)
        self.assertEqual(tx["status"], "Preview")
        self.assertEqual(tx["metadata"]["mutation_family"], "album_mb_track_repair_v1")
        self.assertEqual(len(tx["changes"]), 2)
        self.assertEqual(tx["changes"][0]["after"]["mb_trackid"], REC_TARGET_1)
        self.assertEqual(tx["changes"][1]["after"]["mb_trackid"], REC_TARGET_2)

    def test_plan_non_mutating(self):
        item_paths = self._create_album_and_items(album_id=1)
        mtimes_before = [p.stat().st_mtime_ns for p in item_paths]

        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"))

        # Verify DB and files completely untouched after Plan
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT mb_trackid FROM items WHERE album_id=1 ORDER BY id").fetchall()
        con.close()
        self.assertEqual(rows[0]["mb_trackid"], "")
        self.assertEqual(rows[1]["mb_trackid"], "")

        mtimes_after = [p.stat().st_mtime_ns for p in item_paths]
        self.assertEqual(mtimes_before, mtimes_after)

        # File tags must be untouched too -- Plan reads on-disk tags to
        # capture rollback state but must never write.
        for p in item_paths:
            tags = _read_file_audio_tags(p)
            self.assertTrue(tags["ok"])
            self.assertEqual(tags["tags"]["mb_trackid"], "")

    def test_plan_invalid_album(self):
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 999},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertFalse(res.get("ok"))
        self.assertIn("not found", res.get("error", "").lower())

    def test_plan_invalid_item_membership(self):
        con = sqlite3.connect(self.db_path)
        con.execute("INSERT INTO albums (id, album, mb_albumid) VALUES (5, 'Empty Album', ?)", (REL_A,))
        con.commit()
        con.close()

        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 5},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertFalse(res.get("ok"))
        self.assertIn("no track items", res.get("error", "").lower())

    def test_plan_missing_target_file(self):
        item_paths = self._create_album_and_items(album_id=1)
        item_paths[0].unlink()  # delete physical file

        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "repair_item_missing")

    def test_plan_release_group_mismatch(self):
        self._create_album_and_items(album_id=1, rg_id=RG_A)
        # Pass override REL_B which returns RG_B
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1, "mb_albumid": REL_B},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(rel_id=REL_B, rg_id=RG_B),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "repair_identity_mismatch")
        self.assertIn("different Release Group", res.get("error", ""))

    def test_plan_parent_symlink(self):
        item_paths = self._create_album_and_items(album_id=1)
        # Create a symlink dir inside music_root pointing to album_1
        link_dir = self.music_root / "link_album_1"
        try:
            os.symlink(item_paths[0].parent, link_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported on this host environment")

        symlink_file = link_dir / item_paths[0].name
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET path=? WHERE id=1", (str(symlink_file),))
        con.commit()
        con.close()

        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "repair_symlink_rejected")

    def test_plan_final_symlink(self):
        item_paths = self._create_album_and_items(album_id=1)
        symlink_file = self.music_root / "album_1" / "link_track.flac"
        try:
            os.symlink(item_paths[0], symlink_file)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported on this host environment")

        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET path=? WHERE id=1", (str(symlink_file),))
        con.commit()
        con.close()

        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "repair_symlink_rejected")

    def test_plan_path_outside_root(self):
        self._create_album_and_items(album_id=1, outside=True)

        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "repair_path_out_of_root")


class ApplyTests(Wave19FixtureBase):
    def test_apply_successful_repair(self):
        item_paths = self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(plan_res.get("ok"))
        op_id = plan_res["operation_id"]

        apply_res = execute_album_mb_track_repair_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply_res.get("ok"), apply_res)
        self.assertEqual(apply_res["status"], "Completed")

        # Verify DB updated
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT id, mb_trackid, title FROM items WHERE album_id=1 ORDER BY id").fetchall()
        con.close()
        self.assertEqual(rows[0]["mb_trackid"], REC_TARGET_1)
        self.assertEqual(rows[1]["mb_trackid"], REC_TARGET_2)
        self.assertEqual(rows[0]["title"], "Track 1 Fixed Title")
        self.assertEqual(rows[1]["title"], "Track 2 Fixed Title")

        # Verify tx record status
        tx = self.store.get(op_id)
        self.assertEqual(tx["status"], "Completed")
        self.assertTrue(tx["metadata"]["mutated"])
        self.assertTrue(tx["metadata"]["db_mutated"])
        self.assertTrue(tx["metadata"]["tags_mutated"])

        # Verify the ACTUAL on-disk tags were written and are independently
        # readable back -- proving there is no fake touch()-only success
        # (SEC-002 Wave 19 final review).
        tags1 = _read_file_audio_tags(item_paths[0])
        self.assertTrue(tags1["ok"], tags1)
        self.assertEqual(tags1["tags"]["mb_trackid"], REC_TARGET_1)
        self.assertEqual(tags1["tags"]["title"], "Track 1 Fixed Title")
        tags2 = _read_file_audio_tags(item_paths[1])
        self.assertTrue(tags2["ok"], tags2)
        self.assertEqual(tags2["tags"]["mb_trackid"], REC_TARGET_2)

    def test_apply_stale_item_recording_id(self):
        self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]

        # Mutate DB recording ID after Plan before Apply
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET mb_trackid='modified-outside-plan' WHERE id=1")
        con.commit()
        con.close()

        apply_res = execute_album_mb_track_repair_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "repair_toctou_mismatch")

    def test_apply_stale_album_membership(self):
        self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]

        # Change item's album_id in DB after Plan
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET album_id=99 WHERE id=1")
        con.commit()
        con.close()

        apply_res = execute_album_mb_track_repair_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "repair_album_membership_changed")

    def test_apply_mtime_changed(self):
        item_paths = self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]

        # Modify file on disk to change mtime/size
        time.sleep(0.01)
        item_paths[0].write_bytes(b"MODIFIED_AFTER_PLAN")

        apply_res = execute_album_mb_track_repair_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "repair_toctou_mismatch")

    def test_apply_already_completed_replay(self):
        self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]

        apply1 = execute_album_mb_track_repair_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply1.get("ok"))

        apply2 = execute_album_mb_track_repair_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply2.get("ok"))
        self.assertTrue(apply2.get("already_completed"))

    def test_apply_wrong_mutation_family(self):
        tx = self.store.create(
            operation_type="Replace",
            status="Preview",
            summary="Fake family",
            metadata={"mutation_family": "track_replacement_v1"},
        )
        res = execute_album_mb_track_repair_apply(
            self.store,
            tx["id"],
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(res.get("ok"))
        self.assertIn("mismatch", res.get("error", "").lower())


class MatchingTests(Wave19FixtureBase):
    def test_matching_correct_recording_id_needs_no_repair(self):
        self._create_album_and_items(album_id=1)
        # Set item recording IDs to match target exactly
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET mb_trackid=? WHERE id=1", (REC_TARGET_1,))
        con.execute("UPDATE items SET mb_trackid=? WHERE id=2", (REC_TARGET_2,))
        con.commit()
        con.close()

        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["updated"], 0)

    def test_matching_conflicting_recording_id_requires_review(self):
        """SEC-002 Wave 19 final review, matching authority policy: an
        existing NONBLANK Recording ID is identity evidence. Title/
        position/duration fuzzy scoring alone (backend/mb_alignment.
        best_album_track_match's non-exact_mbid path) is not sufficient
        evidence to overwrite it -- these rows must be surfaced for manual
        review, never auto-applied. (Previously this test asserted the
        opposite -- that a same-title, wrong-recording-id row got silently
        repaired -- which is exactly the unsafe behavior this review
        requires closing.)"""
        self._create_album_and_items(album_id=1, blank_recording_ids=False)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["updated"], 0)
        self.assertEqual(res.get("conflicts"), 2)

        tx = self.store.get(res["operation_id"])
        payload = tx["metadata"]
        self.assertEqual(payload["tracks_to_repair"], [])
        self.assertEqual(len(payload["conflicts_requiring_review"]), 2)

        # Applying this Plan must not touch the conflicting rows.
        apply_res = execute_album_mb_track_repair_apply(
            self.store, res["operation_id"], db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply_res.get("ok"), apply_res)
        con = sqlite3.connect(self.db_path)
        rows = con.execute("SELECT mb_trackid FROM items WHERE album_id=1 ORDER BY id").fetchall()
        con.close()
        self.assertEqual(rows[0][0], REC_1)
        self.assertEqual(rows[1][0], REC_2)

    def test_matching_blank_recording_id_safely_filled(self):
        """The mirror case: a BLANK Recording ID is safe to fill from
        tracklist alignment evidence -- no conflicting identity is being
        overwritten."""
        self._create_album_and_items(album_id=1, blank_recording_ids=True)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["updated"], 2)
        self.assertEqual(res.get("conflicts"), 0)


class ConcurrencyTests(Wave19FixtureBase):
    def test_concurrency_same_operation_twice(self):
        self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]

        results = []
        def _worker():
            r = execute_album_mb_track_repair_apply(
                self.store,
                op_id,
                db_path=str(self.db_path),
                music_allowed_roots=[str(self.music_root)],
            )
            results.append(r)

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].get("ok"))
        self.assertTrue(results[1].get("ok"))
        completed_count = sum(1 for r in results if r.get("status") == "Completed")
        self.assertGreaterEqual(completed_count, 1)

    def test_concurrency_unrelated_albums(self):
        self._create_album_and_items(album_id=1)
        self._create_album_and_items(album_id=2)

        plan1 = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        plan2 = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 2},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )

        res1 = []
        res2 = []

        t1 = threading.Thread(
            target=lambda: res1.append(execute_album_mb_track_repair_apply(
                self.store, plan1["operation_id"], db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)]
            ))
        )
        t2 = threading.Thread(
            target=lambda: res2.append(execute_album_mb_track_repair_apply(
                self.store, plan2["operation_id"], db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)]
            ))
        )

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertTrue(res1[0].get("ok"))
        self.assertTrue(res2[0].get("ok"))


class RollbackTests(Wave19FixtureBase):
    def test_rollback_full_success(self):
        self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]

        apply_res = execute_album_mb_track_repair_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply_res.get("ok"))

        rollback_res = rollback_album_mb_track_repair(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(rollback_res.get("ok"))
        self.assertEqual(rollback_res["status"], "Rolled Back")

        # Verify DB restored to original (blank) recording IDs
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT mb_trackid, title FROM items WHERE album_id=1 ORDER BY id").fetchall()
        con.close()
        self.assertEqual(rows[0]["mb_trackid"], "")
        self.assertEqual(rows[1]["mb_trackid"], "")
        self.assertEqual(rows[0]["title"], "Track 1 Old Title")

        # Verify the actual ON-DISK file tags were restored too, not just
        # the DB row (SEC-002 Wave 19 final review: rollback must restore
        # the real file-tag snapshot, not the DB's before-state).
        item_paths = sorted((self.music_root / "album_1").glob("*.wav"))
        for p in item_paths:
            tags = _read_file_audio_tags(p)
            self.assertTrue(tags["ok"])
            self.assertEqual(tags["tags"]["mb_trackid"], "")

    def test_rollback_repeated_refused(self):
        self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store, {"album_id": 1},
            music_allowed_roots=[str(self.music_root)], db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]
        apply_res = execute_album_mb_track_repair_apply(
            self.store, op_id, db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply_res.get("ok"))

        first = rollback_album_mb_track_repair(
            self.store, op_id, db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(first.get("ok"))
        self.assertEqual(first["status"], "Rolled Back")

        second = rollback_album_mb_track_repair(
            self.store, op_id, db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(second.get("ok"))
        self.assertEqual(second.get("code"), "repair_already_rolled_back")

    def test_rollback_stale_after_manual_edit_refused(self):
        """A later, legitimate manual edit must not be silently overwritten
        by a stale rollback of an earlier repair (SEC-002 Wave 19 final
        review, "rollback must validate current state before overwriting
        it")."""
        self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store, {"album_id": 1},
            music_allowed_roots=[str(self.music_root)], db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]
        apply_res = execute_album_mb_track_repair_apply(
            self.store, op_id, db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply_res.get("ok"))

        # Simulate a later, legitimate manual edit to item 1's Recording ID.
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET mb_trackid=? WHERE id=1", ("55555555-5555-5555-5555-555555555555",))
        con.commit()
        con.close()

        res = rollback_album_mb_track_repair(
            self.store, op_id, db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "repair_rollback_stale")

        # The manual edit must survive untouched.
        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT mb_trackid FROM items WHERE id=1").fetchone()
        con.close()
        self.assertEqual(row[0], "55555555-5555-5555-5555-555555555555")

    def test_rollback_unmutated_refused(self):
        self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]

        # Attempt rollback on Preview status (not mutated)
        res = rollback_album_mb_track_repair(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(res.get("ok"))
        self.assertIn("unavailable", res.get("error", "").lower())


class TagWriteIntegrityTests(Wave19FixtureBase):
    """SEC-002 Wave 19 final review: prove there is no fake touch()-only
    success path, and that a genuinely unreadable file is refused rather
    than silently marked repaired."""

    def test_unreadable_file_excluded_from_repair_not_faked(self):
        item_paths = self._create_album_and_items(album_id=1)
        # Overwrite item 1's file with garbage that no real tag reader can
        # parse -- this is exactly the old fake-fixture scenario. Content
        # is a fixed byte string so mtime/size still stay valid/stable.
        item_paths[0].write_bytes(b"NOT_REAL_AUDIO_DATA")

        res = create_album_mb_track_repair_plan(
            self.store, {"album_id": 1},
            music_allowed_roots=[str(self.music_root)], db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"), res)
        # Only item 2 (still real audio) is safe to repair; item 1 is
        # excluded with a truthful reason rather than "succeeding" via a
        # touch()-only fallback.
        self.assertEqual(res["updated"], 1)
        unreadable = res.get("unreadable_items") or []
        self.assertEqual(len(unreadable), 1)
        self.assertEqual(unreadable[0]["reason"], "unreadable")

        # The garbage file must be untouched -- no touch()-driven mtime
        # bump, no fake "success".
        before_bytes = item_paths[0].read_bytes()
        self.assertEqual(before_bytes, b"NOT_REAL_AUDIO_DATA")

    def test_write_file_audio_tags_rejects_unreadable_file(self):
        bogus = self.music_root / "bogus.wav"
        bogus.write_bytes(b"NOT_REAL_AUDIO_DATA")
        result = _write_file_audio_tags(bogus, {"title": "New Title", "mb_trackid": REC_TARGET_1})
        self.assertFalse(result.get("ok"))
        self.assertNotEqual(result.get("reason"), None)
        # Must not have touched the file at all.
        self.assertEqual(bogus.read_bytes(), b"NOT_REAL_AUDIO_DATA")

    def test_write_file_audio_tags_missing_file_not_faked(self):
        missing = self.music_root / "does_not_exist.wav"
        result = _write_file_audio_tags(missing, {"title": "X"})
        self.assertFalse(result.get("ok"))
        self.assertFalse(missing.exists())

    def test_read_file_audio_tags_distinguishes_blank_from_unreadable(self):
        real_path = self.music_root / "real.wav"
        _write_test_audio(real_path, title="", mb_trackid="")
        blank = _read_file_audio_tags(real_path)
        self.assertTrue(blank["ok"])
        self.assertEqual(blank["tags"]["mb_trackid"], "")

        bogus_path = self.music_root / "bogus2.wav"
        bogus_path.write_bytes(b"GARBAGE")
        unreadable = _read_file_audio_tags(bogus_path)
        self.assertFalse(unreadable["ok"])
        self.assertIsNone(unreadable["tags"])
        self.assertEqual(unreadable["reason"], "unreadable")

        missing_path = self.music_root / "missing.wav"
        missing = _read_file_audio_tags(missing_path)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["reason"], "not_found")

    def test_rollback_restores_actual_file_tags_not_db_row(self):
        """Rollback must restore what was really on the file, which can
        legitimately differ from the Beets DB row (SEC-002 Wave 19 final
        review, "rollback does not capture the actual original file
        tags")."""
        item_paths = self._create_album_and_items(album_id=1)
        # Make the on-disk title diverge from the DB row's title, as could
        # happen in a real library where the DB and file drifted.
        import mediafile
        mf = mediafile.MediaFile(item_paths[0])
        mf.title = "Track 1 Old Title (Original Mix)"
        mf.save()

        plan_res = create_album_mb_track_repair_plan(
            self.store, {"album_id": 1},
            music_allowed_roots=[str(self.music_root)], db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]
        apply_res = execute_album_mb_track_repair_apply(
            self.store, op_id, db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply_res.get("ok"), apply_res)

        rollback_res = rollback_album_mb_track_repair(
            self.store, op_id, db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(rollback_res.get("ok"), rollback_res)

        restored = _read_file_audio_tags(item_paths[0])
        self.assertTrue(restored["ok"])
        # Restored to the ACTUAL prior file value, not the DB row's
        # "Track 1 Old Title".
        self.assertEqual(restored["tags"]["title"], "Track 1 Old Title (Original Mix)")

    def test_apply_write_tags_false_skips_file_mutation(self):
        """write_tags=False preserves the historical DB-only maintenance
        contract (_repair_album_mbid_sticking_once(write_tags=False)) --
        SEC-002 Wave 19 final review found this silently ignored."""
        item_paths = self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store, {"album_id": 1},
            music_allowed_roots=[str(self.music_root)], db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]

        apply_res = execute_album_mb_track_repair_apply(
            self.store, op_id, db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)], write_tags=False,
        )
        self.assertTrue(apply_res.get("ok"), apply_res)
        self.assertFalse(apply_res.get("tags_written"))

        con = sqlite3.connect(self.db_path)
        rows = con.execute("SELECT mb_trackid FROM items WHERE album_id=1 ORDER BY id").fetchall()
        con.close()
        self.assertEqual(rows[0][0], REC_TARGET_1)

        # File tags must remain exactly as they started -- untouched.
        tags = _read_file_audio_tags(item_paths[0])
        self.assertTrue(tags["ok"])
        self.assertEqual(tags["tags"]["mb_trackid"], "")

        tx = self.store.get(op_id)
        self.assertTrue(tx["metadata"]["db_mutated"])
        self.assertFalse(tx["metadata"].get("tags_mutated"))


class ReleaseOnlyStampingTests(Wave19FixtureBase):
    """SEC-002 Wave 19 final review: zero Recording-ID changes plus
    pending release stamping must still actually Apply -- this was a
    concrete regression (Plan created a real transaction that both
    Web Manager callers silently never Applied)."""

    def test_plan_reports_release_stamping_needed(self):
        # Recording IDs already exact-match the target tracklist, but the
        # item rows still carry the OLD release id -- classic release-only
        # stamping scenario.
        self._create_album_and_items(album_id=1, mb_albumid_override="99999999-0000-0000-0000-000000000000")
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET mb_trackid=? WHERE id=1", (REC_TARGET_1,))
        con.execute("UPDATE items SET mb_trackid=? WHERE id=2", (REC_TARGET_2,))
        con.commit()
        con.close()

        res = create_album_mb_track_repair_plan(
            self.store, {"album_id": 1},
            music_allowed_roots=[str(self.music_root)], db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["updated"], 0)
        self.assertTrue(res.get("release_stamping_needed"))
        self.assertEqual(res.get("release_stamp_rows"), 2)

    def test_apply_actually_stamps_release_id_when_updated_is_zero(self):
        self._create_album_and_items(album_id=1, mb_albumid_override="99999999-0000-0000-0000-000000000000")
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET mb_trackid=? WHERE id=1", (REC_TARGET_1,))
        con.execute("UPDATE items SET mb_trackid=? WHERE id=2", (REC_TARGET_2,))
        con.commit()
        con.close()

        plan_res = create_album_mb_track_repair_plan(
            self.store, {"album_id": 1},
            music_allowed_roots=[str(self.music_root)], db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertEqual(plan_res["updated"], 0)
        self.assertTrue(plan_res.get("release_stamping_needed"))
        op_id = plan_res["operation_id"]

        apply_res = execute_album_mb_track_repair_apply(
            self.store, op_id, db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply_res.get("ok"), apply_res)
        self.assertEqual(apply_res["status"], "Completed")
        self.assertEqual(apply_res.get("release_stamp_rows"), 2)

        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        album_row = con.execute("SELECT mb_albumid FROM albums WHERE id=1").fetchone()
        item_rows = con.execute("SELECT mb_albumid FROM items WHERE album_id=1").fetchall()
        con.close()
        self.assertEqual(album_row["mb_albumid"], REL_A)
        for row in item_rows:
            self.assertEqual(row["mb_albumid"], REL_A)

    def test_rollback_restores_every_stamped_item_and_album(self):
        self._create_album_and_items(album_id=1, mb_albumid_override="99999999-0000-0000-0000-000000000000")
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET mb_trackid=? WHERE id=1", (REC_TARGET_1,))
        con.execute("UPDATE items SET mb_trackid=? WHERE id=2", (REC_TARGET_2,))
        con.commit()
        con.close()

        plan_res = create_album_mb_track_repair_plan(
            self.store, {"album_id": 1},
            music_allowed_roots=[str(self.music_root)], db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]
        apply_res = execute_album_mb_track_repair_apply(
            self.store, op_id, db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply_res.get("ok"), apply_res)

        rollback_res = rollback_album_mb_track_repair(
            self.store, op_id, db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(rollback_res.get("ok"), rollback_res)
        self.assertEqual(rollback_res["status"], "Rolled Back")

        # The album row's OWN mb_albumid was already REL_A the whole time
        # (only the item rows had drifted) -- confirm it is unchanged, and
        # confirm every item row is back to its actual original value.
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        album_row = con.execute("SELECT mb_albumid FROM albums WHERE id=1").fetchone()
        item_rows = con.execute("SELECT mb_albumid FROM items WHERE album_id=1").fetchall()
        con.close()
        self.assertEqual(album_row["mb_albumid"], REL_A)
        for row in item_rows:
            self.assertEqual(row["mb_albumid"], "99999999-0000-0000-0000-000000000000")

    def test_rollback_restores_album_rows_own_original_value(self):
        """SEC-002 Wave 19 final review, Problem A: when release-only
        stamping happens with ZERO repaired tracks, rollback must still
        restore the album row's own original mb_albumid -- not silently
        skip it for lack of a `tracks_to_repair[0]` to infer from."""
        self._create_album_and_items(album_id=1, rel_id=REL_B, rg_id=RG_A, mb_albumid_override=REL_B)
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET mb_trackid=? WHERE id=1", (REC_TARGET_1,))
        con.execute("UPDATE items SET mb_trackid=? WHERE id=2", (REC_TARGET_2,))
        con.commit()
        con.close()

        plan_res = create_album_mb_track_repair_plan(
            self.store, {"album_id": 1, "mb_albumid": REL_A},
            music_allowed_roots=[str(self.music_root)], db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(rel_id=REL_A, rg_id=RG_A),
        )
        self.assertTrue(plan_res.get("ok"), plan_res)
        self.assertEqual(plan_res["updated"], 0)
        self.assertTrue(plan_res.get("release_stamping_needed"))
        op_id = plan_res["operation_id"]

        apply_res = execute_album_mb_track_repair_apply(
            self.store, op_id, db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply_res.get("ok"), apply_res)

        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT mb_albumid FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(row[0], REL_A)

        rollback_res = rollback_album_mb_track_repair(
            self.store, op_id, db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(rollback_res.get("ok"), rollback_res)

        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT mb_albumid FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(row[0], REL_B)

    def test_repair_album_mbid_sticking_once_applies_release_only_stamping(self):
        """Exercise the actual app.py production caller
        (_repair_album_mbid_sticking_once), not just the engine directly --
        this is the exact function the original regression was found in."""
        self._create_album_and_items(album_id=1, mb_albumid_override="99999999-0000-0000-0000-000000000000")
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET mb_trackid=? WHERE id=1", (REC_TARGET_1,))
        con.execute("UPDATE items SET mb_trackid=? WHERE id=2", (REC_TARGET_2,))
        con.commit()
        con.close()

        plan_res = create_album_mb_track_repair_plan(
            self.store, {"album_id": 1},
            music_allowed_roots=[str(self.music_root)], db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )

        def _mock_plan(payload, **kwargs):
            return plan_res

        def _mock_apply(operation_id, write_tags=True, **kwargs):
            return execute_album_mb_track_repair_apply(
                self.store, operation_id, db_path=str(self.db_path),
                music_allowed_roots=[str(self.music_root)], write_tags=write_tags,
            )

        with mock.patch.object(app_module, "beets_client") as mock_client:
            mock_client.plan_album_mb_track_repair.side_effect = _mock_plan
            mock_client.apply_album_mb_track_repair.side_effect = _mock_apply
            summary = app_module._repair_album_mbid_sticking_once(
                1, REL_A, [], repair_tracks=True, write_tags=True,
            )

        self.assertTrue(summary["changed"])
        mock_client.apply_album_mb_track_repair.assert_called_once()

        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT mb_albumid FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(row[0], REL_A)


class RealProductionPathIntegrationTests(Wave19FixtureBase):
    def test_production_path_repair_flow(self):
        self._create_album_and_items(album_id=1)

        app_module.app.config["TESTING"] = True
        with mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}):
            client = BeetsClient(base_url="http://mock-beets:8338")

        mock_plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = mock_plan_res["operation_id"]

        def _mock_plan(payload, **kwargs):
            return mock_plan_res

        def _mock_apply(operation_id, **kwargs):
            return execute_album_mb_track_repair_apply(
                self.store,
                operation_id,
                db_path=str(self.db_path),
                music_allowed_roots=[str(self.music_root)],
            )

        with mock.patch.object(app_module, "beets_client") as mock_client, \
             mock.patch.object(app_module.lib, "get_album", return_value=mock.Mock(albumartist="Test Artist", album="Test Album Title")), \
             mock.patch.object(app_module, "_invalidate_lib_cache"), \
             mock.patch.object(app_module, "_trigger_plex_refresh"):

            mock_client.plan_album_mb_track_repair.side_effect = _mock_plan
            mock_client.apply_album_mb_track_repair.side_effect = _mock_apply

            with app_module.app.test_client() as c:
                res = c.post("/api/albums/1/repair-mb-tracks", json={"mb_albumid": REL_A})
                self.assertEqual(res.status_code, 200)
                data = res.get_json()
                self.assertTrue(data.get("ok"))
                job_id = data.get("job_id")
                self.assertTrue(job_id)

                # Execute job synchronously to verify end-to-end flow
                for _ in range(50):
                    job = app_module.jobs.get(job_id)
                    if job and job.status != "running":
                        break
                    time.sleep(0.1)

                job = app_module.jobs.get(job_id)
                self.assertIsNotNone(job)
                self.assertIn(job.status, ("finished", "success"), getattr(job, "log", []))

        # Confirm DB updated via complete production path
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT mb_trackid FROM items WHERE album_id=1 ORDER BY id").fetchall()
        con.close()
        self.assertEqual(rows[0]["mb_trackid"], REC_TARGET_1)
        self.assertEqual(rows[1]["mb_trackid"], REC_TARGET_2)


class WebManagerMutationProhibitionTests(unittest.TestCase):
    """SEC-002 Wave 19 final review: AGY's own architecture scan only
    inspected `repair_album_mb_tracks`, but `_repair_album_mbid_sticking_once`
    is a second, automatic production caller of the same mutation family --
    it must be held to the identical no-local-mutation standard."""

    PROHIBITED_STRINGS = [
        "Path.unlink", "os.unlink", "os.remove", "os.rename", "os.replace",
        "shutil.move", "shutil.rmtree", "UPDATE items SET", "UPDATE albums SET",
        "_beet_run", "MediaFile(", "mutagen.File(",
    ]

    def _fn_source(self, name):
        app_path = Path(app_module.__file__)
        source = app_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn_def = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                fn_def = node
                break
        self.assertIsNotNone(fn_def, f"{name} not found in app.py")
        return ast.get_source_segment(source, fn_def)

    def test_app_py_repair_endpoint_contains_no_direct_mutations(self):
        fn_source = self._fn_source("repair_album_mb_tracks")
        for p in self.PROHIBITED_STRINGS:
            self.assertNotIn(p, fn_source, f"Prohibited call '{p}' found in repair_album_mb_tracks")

    def test_app_py_automatic_repair_helper_contains_no_direct_mutations(self):
        fn_source = self._fn_source("_repair_album_mbid_sticking_once")
        for p in self.PROHIBITED_STRINGS:
            self.assertNotIn(p, fn_source, f"Prohibited call '{p}' found in _repair_album_mbid_sticking_once")


if __name__ == "__main__":
    unittest.main()
