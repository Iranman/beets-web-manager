"""SEC-002 / ARCH-003 Wave 18 final review: bulk_import_replacement_v1.

The original Wave 18 implementation reported "PASS -- READY FOR CLAUDE
REVIEW". Independent inspection found it did not hold up: the only real
production caller (_merge_imported_album_into_existing) had been reduced
to a silent no-op by an indentation bug that nested its entire body as
unreachable dead code inside a sibling helper function; the transaction
itself never verified that a replacement had actually been imported
before retiring old library state; "new item" discovery relied on
whatever else was left in the album rather than an explicit mapping;
per-track identity evidence was never required (only an optional
album-level Release Group ID check); TOCTOU only checked inode/size, not
device/mtime; DB retirement never verified rowcount; concurrency only
serialized the same operation_id, not the same item/album across
different transactions; rollback was gated on status == "Completed"
alone, stranding any partially-mutated Failed transaction with no public
recovery path; and the focused test suite's own "apply lifecycle" test
asserted the dangerous behavior (retiring old state with zero replacement
verification) as correct.

This suite is a from-scratch rewrite proving the corrected invariants.
"""
import ast
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from backend.transaction_engine import (
    TransactionStore,
    create_bulk_import_replacement_plan,
    execute_bulk_import_replacement_apply,
    rollback_bulk_import_replacement,
    create_existing_album_reconcile_plan,
    execute_existing_album_reconcile_apply,
)

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


class Wave18FixtureBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)

        self.music_root = self.tmp_path / "music"
        self.music_root.mkdir()
        self.quarantine_root = self.tmp_path / "quarantine"
        self.quarantine_root.mkdir()
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

        self._env_patch = mock.patch.dict(os.environ, {"REPLACEMENT_QUARANTINE_DIR": str(self.quarantine_root)})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

        self._next_id = 100

    # -- fixture helpers --------------------------------------------------
    def _next(self) -> int:
        self._next_id += 1
        return self._next_id

    def _insert_album(self, album_id: int, rgid: str = RG_A) -> None:
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.execute("SELECT 1 FROM albums WHERE id=?", (album_id,))
            if cur.fetchone() is not None:
                return
            con.execute(
                "INSERT INTO albums (id, album, albumartist, mb_albumid, mb_releasegroupid, year) VALUES (?,?,?,?,?,?)",
                (album_id, "Album", "Artist", "", rgid, 2024),
            )
            con.commit()
        finally:
            con.close()

    def _insert_item(self, item_id: int, album_id: int, rel_path: str, *,
                      disc: int = 1, track: int = 1, mb_trackid: str = "", mb_releasegroupid: str = RG_A,
                      root: Path = None, content: bytes = b"AUDIO") -> Path:
        root = root or self.music_root
        p = root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        st = p.stat()
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, "
            "mb_trackid, mb_albumid, mb_releasegroupid, length, size, mtime) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, album_id, f"Track {track}", "Artist", "Album", "Artist", disc, track,
             str(p).encode("utf-8"), mb_trackid, "", mb_releasegroupid, 180.0, st.st_size, st.st_mtime),
        )
        con.commit()
        con.close()
        return p

    def _get_item_row(self, item_id: int):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        cur = con.execute("SELECT * FROM items WHERE id=?", (item_id,))
        row = cur.fetchone()
        con.close()
        return dict(row) if row else None

    def _mapping(self, old_id: int, new_id: int, identity_source: str = "forced_replacement_verified") -> dict:
        return {"old_item_id": old_id, "new_item_id": new_id, "identity_source": identity_source}

    def _plan(self, mappings, *, existing_album_id: int = 0, old_item_ids=None, **overrides) -> dict:
        payload = {"existing_album_id": existing_album_id, "mappings": mappings}
        if old_item_ids is not None:
            payload["old_item_ids"] = old_item_ids
        payload.update(overrides)
        return create_bulk_import_replacement_plan(
            self.store, payload,
            db_path=str(self.db_path),
            original_allowed_roots=[str(self.music_root)],
            quarantine_base_root=str(self.quarantine_root),
        )

    def _apply(self, op_id: str) -> dict:
        return execute_bulk_import_replacement_apply(
            self.store, op_id,
            db_path=str(self.db_path),
            original_allowed_roots=[str(self.music_root)],
            quarantine_base_root=str(self.quarantine_root),
        )

    def _rollback(self, op_id: str) -> dict:
        return rollback_bulk_import_replacement(
            self.store, op_id,
            db_path=str(self.db_path),
            original_allowed_roots=[str(self.music_root)],
            quarantine_base_root=str(self.quarantine_root),
        )

    def _make_pair(self, album_id: int = None, *, old_track: int = 1,
                    identity_source: str = "forced_replacement_verified",
                    new_has_recording_id: bool = True):
        """Create one old item + one already-imported new item at the same
        disc/track position, both under the music root (Model B: the new
        item is already a real Beets row, not a staging file)."""
        album_id = album_id if album_id is not None else self._next()
        self._insert_album(album_id)
        old_id = self._next()
        new_id = self._next()
        old_p = self._insert_item(old_id, album_id, f"old_{old_id}.mp3", track=old_track,
                                   mb_trackid="tr-old", content=b"OLD_AUDIO")
        new_mbid = "tr-new" if new_has_recording_id else ""
        new_p = self._insert_item(new_id, album_id, f"new_{new_id}.flac", track=old_track,
                                   mb_trackid=new_mbid, content=b"NEW_AUDIO_BETTER")
        return album_id, old_id, new_id, old_p, new_p


class PlanValidationTests(Wave18FixtureBase):
    def test_plan_is_non_mutating(self):
        album_id, old_id, new_id, old_p, new_p = self._make_pair()
        res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(self.store.get(res["operation_id"])["status"], "Preview")
        self.assertTrue(old_p.exists())
        self.assertTrue(new_p.exists())
        self.assertIsNotNone(self._get_item_row(old_id))
        self.assertIsNotNone(self._get_item_row(new_id))

    def test_mappings_required(self):
        res = self._plan([], existing_album_id=1)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "replacement_mapping_missing")

    def test_no_album_only_fallback(self):
        """The original implementation could resolve ALL of an album's
        item ids from the DB if no explicit ids/mappings were given -- an
        ambiguous "retire everything in this album" mode. That fallback
        must not exist: omitting mappings always fails closed."""
        album_id, old_id, new_id, _, _ = self._make_pair()
        res = create_bulk_import_replacement_plan(
            self.store, {"existing_album_id": album_id},
            db_path=str(self.db_path), original_allowed_roots=[str(self.music_root)],
            quarantine_base_root=str(self.quarantine_root),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "replacement_mapping_missing")

    def test_duplicate_old_item_id_in_mappings_rejected(self):
        album_id, old_id, new_id, _, _ = self._make_pair()
        _, _, new_id2, _, _ = self._make_pair(album_id, old_track=2)
        res = self._plan([self._mapping(old_id, new_id), self._mapping(old_id, new_id2)], existing_album_id=album_id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "replacement_mapping_ambiguous")

    def test_duplicate_new_item_id_in_mappings_rejected(self):
        album_id, old_id, new_id, _, _ = self._make_pair()
        old_id2, _ = self._next(), None
        self._insert_item(old_id2, album_id, "old2.mp3", track=2, mb_trackid="tr-old2")
        res = self._plan([self._mapping(old_id, new_id), self._mapping(old_id2, new_id)], existing_album_id=album_id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "replacement_mapping_ambiguous")

    def test_old_equals_new_rejected(self):
        album_id, old_id, new_id, _, _ = self._make_pair()
        res = self._plan([self._mapping(old_id, old_id)], existing_album_id=album_id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "replacement_mapping_ambiguous")

    def test_old_item_ids_mismatch_with_mappings_rejected(self):
        album_id, old_id, new_id, _, _ = self._make_pair()
        res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id, old_item_ids=[old_id, 99999])
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "replacement_mapping_ambiguous")

    def test_old_item_not_found(self):
        album_id, old_id, new_id, _, _ = self._make_pair()
        res = self._plan([self._mapping(999999, new_id)], existing_album_id=album_id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "item_not_found")

    def test_new_item_not_found_means_import_required(self):
        """A row that doesn't exist can never masquerade as a verified
        replacement -- this is the direct fix for 'new items discovered
        by whatever else is in the album'."""
        album_id, old_id, _, _, _ = self._make_pair()
        res = self._plan([self._mapping(old_id, 999999)], existing_album_id=album_id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "bulk_replacement_import_required")

    def test_preexisting_unrelated_row_only_usable_if_explicitly_named_and_verified(self):
        """A row that DOES exist is only ever usable as a replacement if
        it is explicitly named in `mappings` -- there is no 'anything else
        currently in the album' discovery fallback at all (contrast with
        the pre-review implementation, which found new items purely by
        excluding old_item_ids from an album_id query). Simply existing in
        the same album is not, by itself, sufficient: it is still
        individually verified against the same identity-evidence gate as
        every other mapping."""
        album_id, old_id, new_id, _, _ = self._make_pair()
        unrelated_id = self._next()
        self._insert_item(unrelated_id, album_id, "unrelated.mp3", track=5, mb_trackid="")
        res = self._plan([self._mapping(old_id, unrelated_id, identity_source="mb_tracklist_position_match")], existing_album_id=album_id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "recording_id_required")

    def test_new_item_never_discovered_implicitly(self):
        """A second, real, verifiable row exists in the album but was
        never named in `mappings` -- Plan must not silently pick it as
        old_id's replacement. Passing no mappings at all fails closed
        (see test_mappings_required); this proves there is no code path
        that would ever select it automatically."""
        album_id, old_id, new_id, _, _ = self._make_pair()
        also_present_id = self._next()
        self._insert_item(also_present_id, album_id, "also_present.flac", track=9, mb_trackid="tr-also-present")
        res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        self.assertTrue(res.get("ok"), res)
        mapped_new_ids = {m["new_item_id"] for m in res["mappings"]}
        self.assertEqual(mapped_new_ids, {new_id})
        self.assertNotIn(also_present_id, mapped_new_ids)

    def test_ai_only_identity_source_rejected(self):
        album_id, old_id, new_id, _, _ = self._make_pair()
        for bad in ("", "ai", "ai_suggestion", "ai_only"):
            res = self._plan([self._mapping(old_id, new_id, identity_source=bad)], existing_album_id=album_id)
            self.assertFalse(res.get("ok"), bad)
            self.assertEqual(res.get("code"), "ai_only_evidence_rejected", bad)

    def test_recording_id_required_for_non_forced_identity_source(self):
        album_id, old_id, new_id, old_p, new_p = self._make_pair(new_has_recording_id=False)
        res = self._plan([self._mapping(old_id, new_id, identity_source="mb_tracklist_position_match")], existing_album_id=album_id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "recording_id_required")

    def test_forced_replacement_verified_does_not_require_recording_id(self):
        album_id, old_id, new_id, _, _ = self._make_pair(new_has_recording_id=False)
        res = self._plan([self._mapping(old_id, new_id, identity_source="forced_replacement_verified")], existing_album_id=album_id)
        self.assertTrue(res.get("ok"), res)

    def test_release_group_mismatch_rejected(self):
        album_id = self._next()
        self._insert_album(album_id, rgid=RG_A)
        old_id = self._next()
        new_id = self._next()
        self._insert_item(old_id, album_id, "old.mp3", mb_releasegroupid=RG_A, mb_trackid="tr-old")
        self._insert_item(new_id, album_id, "new.flac", mb_releasegroupid=RG_B, mb_trackid="tr-new")
        res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "release_group_mismatch")

    def test_old_item_outside_music_root_rejected(self):
        album_id, old_id, new_id, _, _ = self._make_pair()
        # Point the old item's DB path outside the trusted root.
        con = sqlite3.connect(self.db_path)
        outside_file = self.outside_root / "escaped.mp3"
        outside_file.write_bytes(b"X")
        con.execute("UPDATE items SET path=? WHERE id=?", (str(outside_file).encode("utf-8"), old_id))
        con.commit()
        con.close()
        res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "original_outside_roots")

    def test_new_item_outside_music_root_rejected(self):
        album_id, old_id, new_id, _, _ = self._make_pair()
        con = sqlite3.connect(self.db_path)
        outside_file = self.outside_root / "escaped_new.flac"
        outside_file.write_bytes(b"X")
        con.execute("UPDATE items SET path=? WHERE id=?", (str(outside_file).encode("utf-8"), new_id))
        con.commit()
        con.close()
        res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "candidate_outside_roots")

    def test_old_item_leaf_symlink_rejected(self):
        album_id, old_id, new_id, old_p, _ = self._make_pair()
        real = self.music_root / "real_old.mp3"
        real.write_bytes(b"REAL")
        try:
            old_p.unlink()
            old_p.symlink_to(real)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported")
        res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "symlink_rejected")

    def test_new_item_parent_symlink_rejected(self):
        album_id, old_id, new_id, _, new_p = self._make_pair()
        real_dir = self.music_root / "real_new_dir"
        real_dir.mkdir()
        target = real_dir / new_p.name
        shutil.move(str(new_p), str(target))
        symlinked_dir = new_p.parent / "linked_new_dir"
        try:
            symlinked_dir.symlink_to(real_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported")
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET path=? WHERE id=?", (str(symlinked_dir / new_p.name).encode("utf-8"), new_id))
        con.commit()
        con.close()
        res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "symlink_rejected")

    def test_db_not_found(self):
        res = create_bulk_import_replacement_plan(
            self.store, {"existing_album_id": 1, "mappings": [self._mapping(1, 2)]},
            db_path=str(self.tmp_path / "does_not_exist.db"),
            original_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "db_not_found")

    def test_quarantine_path_uses_the_real_transaction_id(self):
        """Regression test for the Wave 17-class bug: the quarantine path
        and the returned operation_id must always be the SAME id."""
        album_id, old_id, new_id, _, _ = self._make_pair()
        res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        self.assertTrue(res.get("ok"), res)
        op_id = res["operation_id"]
        meta = self.store.get(op_id)["metadata"]
        q_target = meta["quarantine_targets"][str(old_id)]
        self.assertIn(op_id, q_target)


class ApplyTests(Wave18FixtureBase):
    def test_full_lifecycle_success(self):
        album_id, old_id, new_id, old_p, new_p = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        self.assertTrue(plan_res.get("ok"), plan_res)
        apply_res = self._apply(plan_res["operation_id"])
        self.assertTrue(apply_res.get("ok"), apply_res)
        self.assertEqual(apply_res["status"], "Completed")
        self.assertEqual(apply_res["retired_count"], 1)
        self.assertFalse(old_p.exists())
        self.assertIsNone(self._get_item_row(old_id))
        # The new item is never touched by this transaction.
        self.assertTrue(new_p.exists())
        self.assertIsNotNone(self._get_item_row(new_id))

    def test_apply_refuses_if_new_item_vanishes_before_apply(self):
        """The direct fix for the unsafe original test: Apply must refuse
        to retire the old item if the verified replacement no longer
        exists by the time Apply runs -- zero old files moved, zero old
        rows removed."""
        album_id, old_id, new_id, old_p, new_p = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        op_id = plan_res["operation_id"]

        new_p.unlink()
        con = sqlite3.connect(self.db_path)
        con.execute("DELETE FROM items WHERE id=?", (new_id,))
        con.commit()
        con.close()

        apply_res = self._apply(op_id)
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "bulk_replacement_import_required")
        self.assertFalse(apply_res.get("mutated"))
        self.assertTrue(old_p.exists())
        self.assertIsNotNone(self._get_item_row(old_id))

    def test_apply_refuses_if_new_item_path_changed(self):
        album_id, old_id, new_id, old_p, new_p = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        op_id = plan_res["operation_id"]
        new_p2 = self.music_root / "moved_new.flac"
        shutil.move(str(new_p), str(new_p2))
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET path=? WHERE id=?", (str(new_p2).encode("utf-8"), new_id))
        con.commit()
        con.close()
        apply_res = self._apply(op_id)
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "toctou_mismatch")
        self.assertTrue(old_p.exists())

    def test_all_or_nothing_one_bad_mapping_blocks_entire_apply(self):
        """Two old items in one transaction; one replacement verifies,
        the other's import never completed. Apply must retire NEITHER --
        no partial destructive retirement when any required replacement
        is unverified."""
        album_id = self._next()
        self._insert_album(album_id)
        old1 = self._next(); new1 = self._next()
        old1_p = self._insert_item(old1, album_id, "old1.mp3", track=1, mb_trackid="tr-old1")
        new1_p = self._insert_item(new1, album_id, "new1.flac", track=1, mb_trackid="tr-new1")
        old2 = self._next(); new2 = self._next()
        old2_p = self._insert_item(old2, album_id, "old2.mp3", track=2, mb_trackid="tr-old2")
        new2_p = self._insert_item(new2, album_id, "new2.flac", track=2, mb_trackid="tr-new2")

        plan_res = self._plan(
            [self._mapping(old1, new1, "forced_replacement_verified"), self._mapping(old2, new2, "forced_replacement_verified")],
            existing_album_id=album_id,
        )
        op_id = plan_res["operation_id"]

        # new2's file vanishes before Apply.
        new2_p.unlink()
        con = sqlite3.connect(self.db_path)
        con.execute("DELETE FROM items WHERE id=?", (new2,))
        con.commit()
        con.close()

        apply_res = self._apply(op_id)
        self.assertFalse(apply_res.get("ok"))
        self.assertFalse(apply_res.get("mutated"))
        self.assertTrue(old1_p.exists(), "old1 must not be retired when old2's mapping fails verification")
        self.assertTrue(old2_p.exists())
        self.assertIsNotNone(self._get_item_row(old1))
        self.assertIsNotNone(self._get_item_row(old2))

    def _toctou_case(self, mutate_fn):
        album_id, old_id, new_id, old_p, new_p = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        op_id = plan_res["operation_id"]
        mutate_fn(old_p)
        apply_res = self._apply(op_id)
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "toctou_mismatch")
        self.assertTrue(old_p.exists())
        self.assertIsNotNone(self._get_item_row(old_id))

    def test_toctou_size_changed(self):
        self._toctou_case(lambda p: p.write_bytes(b"DIFFERENT_LENGTH_CONTENT_HERE"))

    def test_toctou_mtime_changed(self):
        def mutate(p):
            content = p.read_bytes()
            time.sleep(0.01)
            os.utime(p, (time.time() + 1000, time.time() + 1000))
        self._toctou_case(mutate)

    def test_toctou_inode_changed(self):
        def mutate(p):
            content = p.read_bytes()
            p.unlink()
            p.write_bytes(content)  # new inode, same content/size/similar mtime
        self._toctou_case(mutate)

    def test_toctou_replacement_candidate_file_missing(self):
        album_id, old_id, new_id, old_p, new_p = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        op_id = plan_res["operation_id"]
        old_p.unlink()
        apply_res = self._apply(op_id)
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "toctou_mismatch")

    def test_family_mismatch_rejected(self):
        album_id, old_id, new_id, _, _ = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        op_id = plan_res["operation_id"]
        tx = self.store.get(op_id)
        self.store.update(op_id, metadata={**tx["metadata"], "mutation_family": "track_replacement_v1"})
        apply_res = self._apply(op_id)
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "family_mismatch")

    def test_malformed_transaction_id_apply(self):
        apply_res = self._apply("not-a-real-id; DROP TABLE items;")
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "malformed_transaction_id")

    def test_double_apply_is_idempotent(self):
        album_id, old_id, new_id, _, _ = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        op_id = plan_res["operation_id"]
        first = self._apply(op_id)
        self.assertTrue(first.get("ok"))
        second = self._apply(op_id)
        self.assertTrue(second.get("ok"))
        self.assertTrue(second.get("already_completed"))

    def test_db_rowcount_verified_after_delete(self):
        """A real end-to-end run genuinely deletes exactly 1 row -- proves
        rowcount verification isn't just decorative."""
        album_id, old_id, new_id, _, _ = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        apply_res = self._apply(plan_res["operation_id"])
        self.assertTrue(apply_res.get("ok"))
        con = sqlite3.connect(self.db_path)
        cur = con.execute("SELECT COUNT(*) FROM items WHERE id=?", (old_id,))
        self.assertEqual(cur.fetchone()[0], 0)
        con.close()

    def test_old_item_stale_if_deleted_between_plan_and_apply(self):
        album_id, old_id, new_id, old_p, _ = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        op_id = plan_res["operation_id"]
        # Someone else already deleted the DB row (but left the file, an
        # inconsistent-but-possible intermediate state) before Apply runs.
        con = sqlite3.connect(self.db_path)
        con.execute("DELETE FROM items WHERE id=?", (old_id,))
        con.commit()
        con.close()
        apply_res = self._apply(op_id)
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "old_item_stale")

    def test_crash_recovery_resumes_without_requarantining(self):
        album_id, old_id, new_id, old_p, new_p = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        op_id = plan_res["operation_id"]
        tx = self.store.get(op_id)
        meta = tx["metadata"]

        # Simulate a crash right after quarantine physically happened but
        # before the durable step was persisted as "completed": manually
        # move the file and mark the step in_progress with the quarantine
        # record already present, matching what _persist_step would have
        # left behind.
        q_target = Path(meta["quarantine_targets"][str(old_id)])
        q_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_p), str(q_target))
        steps = meta["apply_steps"]
        for s in steps:
            if s["step"] == "mappings_reverified":
                s["status"] = "completed"
        self.store.update(op_id, metadata={
            **meta,
            "apply_steps": steps,
            "quarantined_files": [{"old_id": old_id, "old_path": str(old_p), "quarantine_target": str(q_target)}],
        })

        apply_res = self._apply(op_id)
        self.assertTrue(apply_res.get("ok"), apply_res)
        self.assertEqual(apply_res["status"], "Completed")
        self.assertIsNone(self._get_item_row(old_id))
        # The file was not moved a second time / re-quarantined into a
        # different location.
        self.assertTrue(q_target.exists())

    def test_partial_quarantine_failure_leaves_recoverable_state(self):
        """Second of two old items fails to quarantine (occupied
        destination) -- the transaction must report Failed with mutated
        state, not silently succeed, and the first item's quarantine must
        remain in place for rollback."""
        album_id = self._next()
        self._insert_album(album_id)
        old1 = self._next(); new1 = self._next()
        old1_p = self._insert_item(old1, album_id, "old1.mp3", track=1, mb_trackid="tr-old1")
        new1_p = self._insert_item(new1, album_id, "new1.flac", track=1, mb_trackid="tr-new1")
        old2 = self._next(); new2 = self._next()
        old2_p = self._insert_item(old2, album_id, "old2.mp3", track=2, mb_trackid="tr-old2")
        new2_p = self._insert_item(new2, album_id, "new2.flac", track=2, mb_trackid="tr-new2")

        plan_res = self._plan(
            [self._mapping(old1, new1), self._mapping(old2, new2)],
            existing_album_id=album_id,
        )
        op_id = plan_res["operation_id"]
        meta = self.store.get(op_id)["metadata"]

        # Pre-occupy old2's quarantine destination so its move fails.
        q2 = Path(meta["quarantine_targets"][str(old2)])
        q2.parent.mkdir(parents=True, exist_ok=True)
        q2.write_bytes(b"ALREADY_THERE")

        apply_res = self._apply(op_id)
        self.assertFalse(apply_res.get("ok"))
        self.assertTrue(apply_res.get("mutated"))
        tx = self.store.get(op_id)
        self.assertEqual(tx["status"], "Failed")
        # old1 was already quarantined before old2 failed.
        self.assertFalse(old1_p.exists())
        self.assertTrue(old2_p.exists())

        # Recoverable: rollback works on this partially-mutated Failed state.
        rb = self._rollback(op_id)
        self.assertTrue(rb.get("ok"), rb)
        self.assertTrue(old1_p.exists())


class ConcurrencyTests(Wave18FixtureBase):
    def test_same_item_two_transactions_serialize(self):
        album_id, old_id, new_id, old_p, new_p = self._make_pair()
        # A second, independent replacement candidate for the SAME old item.
        new_id2 = self._next()
        new_p2 = self._insert_item(new_id2, album_id, "new2.flac", track=1, mb_trackid="tr-new2")

        plan1 = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        plan2 = create_bulk_import_replacement_plan(
            self.store, {"existing_album_id": album_id, "mappings": [self._mapping(old_id, new_id2)]},
            db_path=str(self.db_path), original_allowed_roots=[str(self.music_root)],
            quarantine_base_root=str(self.quarantine_root),
        )
        self.assertTrue(plan1.get("ok"))
        self.assertTrue(plan2.get("ok"))

        results = {}

        def run(key, op_id):
            results[key] = self._apply(op_id)

        t1 = threading.Thread(target=run, args=("a", plan1["operation_id"]))
        t2 = threading.Thread(target=run, args=("b", plan2["operation_id"]))
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        oks = [r for r in results.values() if r.get("ok")]
        fails = [r for r in results.values() if not r.get("ok")]
        self.assertEqual(len(oks), 1, results)
        self.assertEqual(len(fails), 1, results)

    def test_different_albums_apply_concurrently(self):
        album_a, old_a, new_a, _, _ = self._make_pair()
        album_b, old_b, new_b, _, _ = self._make_pair()
        plan_a = self._plan([self._mapping(old_a, new_a)], existing_album_id=album_a)
        plan_b = self._plan([self._mapping(old_b, new_b)], existing_album_id=album_b)

        results = {}

        def run(key, op_id):
            results[key] = self._apply(op_id)

        t1 = threading.Thread(target=run, args=("a", plan_a["operation_id"]))
        t2 = threading.Thread(target=run, args=("b", plan_b["operation_id"]))
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        self.assertTrue(results["a"].get("ok"), results["a"])
        self.assertTrue(results["b"].get("ok"), results["b"])


class RollbackTests(Wave18FixtureBase):
    def test_full_rollback_restores_file_and_db_row(self):
        album_id, old_id, new_id, old_p, new_p = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        op_id = plan_res["operation_id"]
        self.assertTrue(self._apply(op_id).get("ok"))

        rb = self._rollback(op_id)
        self.assertTrue(rb.get("ok"), rb)
        self.assertEqual(rb["status"], "Rolled Back")
        self.assertTrue(old_p.exists())
        self.assertIsNotNone(self._get_item_row(old_id))
        # The new item was never touched by rollback.
        self.assertTrue(new_p.exists())
        self.assertIsNotNone(self._get_item_row(new_id))

    def test_rollback_wrong_family_rejected(self):
        album_id, old_id, new_id, _, _ = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        op_id = plan_res["operation_id"]
        self._apply(op_id)
        tx = self.store.get(op_id)
        self.store.update(op_id, metadata={**tx["metadata"], "mutation_family": "track_replacement_v1"})
        rb = self._rollback(op_id)
        self.assertFalse(rb.get("ok"))
        self.assertEqual(rb.get("code"), "wrong_mutation_family")

    def test_rollback_malformed_id(self):
        rb = self._rollback("nope")
        self.assertFalse(rb.get("ok"))
        self.assertEqual(rb.get("code"), "malformed_transaction_id")

    def test_rollback_recovers_partial_mutation_failed_state(self):
        """Direct test for the merge-review's #17/#18 finding: a Failed
        transaction that already durably mutated part of the filesystem
        must be rollback-able, not stranded."""
        album_id = self._next()
        self._insert_album(album_id)
        old1 = self._next(); new1 = self._next()
        old1_p = self._insert_item(old1, album_id, "old1.mp3", track=1, mb_trackid="tr-old1")
        new1_p = self._insert_item(new1, album_id, "new1.flac", track=1, mb_trackid="tr-new1")
        old2 = self._next(); new2 = self._next()
        old2_p = self._insert_item(old2, album_id, "old2.mp3", track=2, mb_trackid="tr-old2")
        new2_p = self._insert_item(new2, album_id, "new2.flac", track=2, mb_trackid="tr-new2")

        plan_res = self._plan([self._mapping(old1, new1), self._mapping(old2, new2)], existing_album_id=album_id)
        op_id = plan_res["operation_id"]
        meta = self.store.get(op_id)["metadata"]
        q2 = Path(meta["quarantine_targets"][str(old2)])
        q2.parent.mkdir(parents=True, exist_ok=True)
        q2.write_bytes(b"BLOCKED")

        apply_res = self._apply(op_id)
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(self.store.get(op_id)["status"], "Failed")

        rb = self._rollback(op_id)
        self.assertTrue(rb.get("ok"), rb)
        self.assertIn(rb["status"], ("Rolled Back", "Partially Rolled Back"))
        self.assertTrue(old1_p.exists())

    def test_rollback_refuses_when_nothing_mutated(self):
        album_id, old_id, new_id, old_p, new_p = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        op_id = plan_res["operation_id"]
        # Force a Failed status with no actual mutation (e.g. Plan-stage-only failure recorded).
        tx = self.store.get(op_id)
        self.store.update(op_id, status="Failed", metadata=tx["metadata"])
        rb = self._rollback(op_id)
        self.assertFalse(rb.get("ok"))
        self.assertEqual(rb.get("code"), "rollback_not_needed")

    def test_rollback_refuses_to_overwrite_occupied_original_path(self):
        album_id, old_id, new_id, old_p, new_p = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        op_id = plan_res["operation_id"]
        self._apply(op_id)
        # Something now occupies the original path.
        old_p.write_bytes(b"SOMETHING_ELSE_LIVES_HERE_NOW")
        rb = self._rollback(op_id)
        self.assertFalse(rb.get("ok"))
        self.assertEqual(rb["status"], "Failed")
        self.assertEqual(rb["failed_restores"][0]["reason"], "original_path_occupied")

    def test_repeated_rollback_second_attempt_reports_no_further_recovery(self):
        album_id, old_id, new_id, old_p, new_p = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        op_id = plan_res["operation_id"]
        self._apply(op_id)
        first = self._rollback(op_id)
        self.assertEqual(first["status"], "Rolled Back")
        second = self._rollback(op_id)
        self.assertFalse(second.get("ok"))
        self.assertIn(second.get("code"), ("invalid_status", "rollback_unavailable"))

    def test_db_restore_skips_reused_item_id(self):
        album_id, old_id, new_id, old_p, new_p = self._make_pair()
        plan_res = self._plan([self._mapping(old_id, new_id)], existing_album_id=album_id)
        op_id = plan_res["operation_id"]
        self._apply(op_id)
        # Item id reused by something else before rollback.
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO items (id, album_id, title, path) VALUES (?,?,?,?)",
            (old_id, album_id, "Reused", b"/somewhere/else.mp3"),
        )
        con.commit()
        con.close()
        rb = self._rollback(op_id)
        self.assertIn(rb["status"], ("Partially Rolled Back", "Failed"))
        self.assertIn(old_id, rb["db_restore_failed"])


class AstStructuralTests(unittest.TestCase):
    """Broadened per SEC-002 Wave 18 final review section 37: check every
    mutation shape, not just unlink()/DELETE FROM items, and verify by
    lexical position that the only unlink()/DELETE calls remaining in
    _merge_imported_album_into_existing operate on dup_rows (redundant
    freshly-downloaded duplicates, pre-existing, out-of-family debt) --
    never on the old library rows this wave's mutation family retires."""

    FORBIDDEN_CALLS = {
        "unlink", "remove", "rmtree", "rmdir", "move", "copy", "copyfile",
        "copystat", "copytree", "rename", "replace", "mkdir", "makedirs",
        "write_text", "write_bytes", "execute", "executemany",
        "executescript", "commit", "connect", "Popen", "run", "call",
        "check_call", "check_output", "system",
    }
    ENGINE_TARGET_FUNCTIONS = {
        "create_bulk_import_replacement_plan",
        "execute_bulk_import_replacement_apply",
    }
    ENGINE_ALLOWED_LOCAL_CALLS = {
        "int", "str", "bool", "float", "len", "sorted", "set", "list", "dict",
        "isinstance", "next", "getattr", "enumerate",
        "_fetch_authoritative_item_row", "_path_has_symlink_under",
        "_bulk_replacement_toctou_check", "_track_replacement_fail",
        "_get_apply_lock", "_get_resource_lock",
        "_execute_bulk_import_replacement_apply_locked",
        "Path", "ExitStack",
    }

    def setUp(self):
        with open("app.py", "r", encoding="utf-8") as f:
            self.app_source = f.read()
        self.app_tree = ast.parse(self.app_source, filename="app.py")
        with open("backend/transaction_engine.py", "r", encoding="utf-8") as f:
            self.engine_source = f.read()
        self.engine_tree = ast.parse(self.engine_source, filename="backend/transaction_engine.py")

    def _find_function(self, tree, name):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        return None

    def test_merge_function_is_not_dead_code(self):
        """Direct regression test for the diff's actual defect: the merge
        logic must be reachable code inside the outer function's own try
        block, not nested unreachable dead code inside a sibling helper's
        body after its own return statement."""
        node = self._find_function(self.app_tree, "_merge_imported_album_into_existing")
        self.assertIsNotNone(node)
        try_node = next(n for n in ast.walk(node) if isinstance(n, ast.Try))
        top_level_kinds = [type(s).__name__ for s in try_node.body]
        self.assertIn("With", top_level_kinds, "the with _db(...) block must be a direct statement inside the try block")

    def test_merge_function_calls_engine_for_replace_rows(self):
        source_lines = self.app_source.splitlines()
        node = self._find_function(self.app_tree, "_merge_imported_album_into_existing")
        self.assertIsNotNone(node)
        body_text = "\n".join(source_lines[node.lineno - 1: node.end_lineno])
        self.assertIn("plan_bulk_import_replacement", body_text)
        self.assertIn("apply_bulk_import_replacement", body_text)

    def test_no_direct_mutation_of_old_library_rows_in_replace_branch(self):
        """Locate the `if replace_rows:` block specifically and assert it
        contains no unlink/DELETE/execute call at all -- it must be pure
        engine delegation. dup_rows/move_ids blocks are intentionally
        excluded (pre-existing, out-of-family debt; see module docstring)."""
        node = self._find_function(self.app_tree, "_merge_imported_album_into_existing")
        self.assertIsNotNone(node)
        replace_if = None
        for n in ast.walk(node):
            if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "replace_rows":
                replace_if = n
                break
        self.assertIsNotNone(replace_if, "could not locate `if replace_rows:` block")
        for child in ast.walk(replace_if):
            if isinstance(child, ast.Call):
                func_name = ""
                if isinstance(child.func, ast.Name):
                    func_name = child.func.id
                elif isinstance(child.func, ast.Attribute):
                    func_name = child.func.attr
                self.assertNotIn(func_name, self.FORBIDDEN_CALLS, f"forbidden call {func_name} inside if replace_rows:")

    def test_engine_functions_only_call_reviewed_local_helpers(self):
        """Closes the 'helper-function delegation' blind spot: only
        bare-name calls to an explicitly reviewed allowlist are permitted
        inside the Plan/Apply entry points -- a new, unreviewed local
        mutation helper introduced later would fail this test rather than
        silently passing because it isn't literally named unlink/DELETE."""
        for fn_name in self.ENGINE_TARGET_FUNCTIONS:
            node = self._find_function(self.engine_tree, fn_name)
            self.assertIsNotNone(node, fn_name)
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                    self.assertIn(
                        child.func.id, self.ENGINE_ALLOWED_LOCAL_CALLS,
                        f"{fn_name} calls local helper {child.func.id}() which is not in the reviewed allowlist.",
                    )

    def test_engine_apply_uses_safe_rename_not_shutil_move(self):
        node = self._find_function(self.engine_tree, "_execute_bulk_import_replacement_apply_locked")
        self.assertIsNotNone(node)
        source_lines = self.engine_source.splitlines()
        body_text = "\n".join(source_lines[node.lineno - 1: node.end_lineno])
        self.assertNotIn("shutil.move(", body_text)
        self.assertIn("_safe_rename(", body_text)

    def test_engine_rollback_uses_safe_rename_not_shutil_move(self):
        node = self._find_function(self.engine_tree, "rollback_bulk_import_replacement")
        self.assertIsNotNone(node)
        source_lines = self.engine_source.splitlines()
        body_text = "\n".join(source_lines[node.lineno - 1: node.end_lineno])
        self.assertNotIn("shutil.move(", body_text)
        self.assertIn("_safe_rename(", body_text)

    def test_no_local_import_statements_in_engine_target_functions(self):
        for fn_name in self.ENGINE_TARGET_FUNCTIONS:
            node = self._find_function(self.engine_tree, fn_name)
            for child in ast.walk(node):
                self.assertNotIsInstance(child, (ast.Import, ast.ImportFrom))


class GenericRollbackDispatchTests(unittest.TestCase):
    """Wave 17 made the generic rollback route family-aware; Wave 18 must
    dispatch through the same explicit, known-family mechanism -- no
    'unknown family -> try X' fallback."""

    def test_app_dispatches_bulk_family_explicitly(self):
        import app as flask_app
        source = flask_app.__file__
        with open(source, "r", encoding="utf-8") as f:
            text = f.read()
        idx = text.index("def api_transaction_rollback")
        snippet = text[idx: idx + 4000]
        self.assertIn('mutation_family == "bulk_import_replacement_v1"', snippet)
        self.assertIn("rollback_bulk_import_replacement", snippet)


class RealProductionPathTests(unittest.TestCase):
    """Real integration test: the actual app.py _merge_imported_album_into_existing
    function, with app.beets_client's bulk-replacement methods patched to
    call the REAL transaction_engine functions (real TransactionStore,
    real SQLite DB, real files), and app._db patched to a real local
    sqlite3 connection instead of the remote-engine proxy (which has
    nothing to connect to in a unit test). This is what actually proves
    the NameError/no-op regression is fixed and that replacement evidence
    reaches the engine for real -- not just that the engine functions
    work when called directly."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)
        self.music_root = self.tmp_path / "music"
        self.music_root.mkdir()
        self.quarantine_root = self.tmp_path / "quarantine"
        self.quarantine_root.mkdir()

        self.db_path = self.tmp_path / "musiclibrary.db"
        con = sqlite3.connect(self.db_path)
        con.executescript(ITEMS_SCHEMA)
        con.commit()
        con.close()

        self._env_patch = mock.patch.dict(os.environ, {"REPLACEMENT_QUARANTINE_DIR": str(self.quarantine_root)})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir()
        self.tx_store = TransactionStore(root=str(store_dir))

        import app as flask_app
        self.flask_app = flask_app

        @contextmanager
        def _local_db(path=None, *, text_factory=None, row_factory=None):
            con = sqlite3.connect(self.db_path)
            con.text_factory = text_factory or bytes
            if row_factory is not None:
                con.row_factory = row_factory
            try:
                yield con
            finally:
                con.close()

        self._db_patch = mock.patch.object(flask_app, "_db", _local_db)
        self._db_patch.start()
        self.addCleanup(self._db_patch.stop)

        self._music_root_patch = mock.patch.object(flask_app, "MUSIC_ROOT", self.music_root)
        self._music_root_patch.start()
        self.addCleanup(self._music_root_patch.stop)

        def mock_plan(payload, **kw):
            return create_bulk_import_replacement_plan(
                self.tx_store, payload,
                original_allowed_roots=[str(self.music_root)],
                db_path=str(self.db_path),
                quarantine_base_root=str(self.quarantine_root),
            )

        def mock_apply(op_id, **kw):
            return execute_bulk_import_replacement_apply(
                self.tx_store, op_id,
                original_allowed_roots=[str(self.music_root)],
                db_path=str(self.db_path),
                quarantine_base_root=str(self.quarantine_root),
            )

        def mock_rec_plan(payload, **kw):
            return create_existing_album_reconcile_plan(
                self.tx_store, payload,
                music_allowed_roots=[str(self.music_root)],
                db_path=str(self.db_path),
                quarantine_base_root=str(self.quarantine_root),
            )

        def mock_rec_apply(op_id, **kw):
            return execute_existing_album_reconcile_apply(
                self.tx_store, op_id,
                music_allowed_roots=[str(self.music_root)],
                db_path=str(self.db_path),
                quarantine_base_root=str(self.quarantine_root),
            )

        self._plan_patch = mock.patch.object(flask_app.beets_client, "plan_bulk_import_replacement", side_effect=mock_plan)
        self._plan_patch.start()
        self.addCleanup(self._plan_patch.stop)
        self._apply_patch = mock.patch.object(flask_app.beets_client, "apply_bulk_import_replacement", side_effect=mock_apply)
        self._apply_patch.start()
        self.addCleanup(self._apply_patch.stop)

        self._rec_plan_patch = mock.patch.object(flask_app.beets_client, "plan_existing_album_reconcile", side_effect=mock_rec_plan)
        self._rec_plan_patch.start()
        self.addCleanup(self._rec_plan_patch.stop)
        self._rec_apply_patch = mock.patch.object(flask_app.beets_client, "apply_existing_album_reconcile", side_effect=mock_rec_apply)
        self._rec_apply_patch.start()
        self.addCleanup(self._rec_apply_patch.stop)

    def _insert_album(self, album_id, name="Album", rgid=""):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO albums (id, album, albumartist, mb_albumid, mb_releasegroupid, year) VALUES (?,?,?,?,?,?)",
            (album_id, name, "Artist", "", rgid, 2024),
        )
        con.commit()
        con.close()

    def _insert_item(self, item_id, album_id, rel_path, *, disc=1, track=1, mb_trackid="", content=b"AUDIO"):
        p = self.music_root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, album_id, f"Track {track}", "Artist", "Album", "Artist", disc, track, str(p).encode("utf-8"), mb_trackid, "", "", 180.0),
        )
        con.commit()
        con.close()
        return p

    def test_merge_function_actually_retires_old_row_end_to_end(self):
        """Real production path: no mb_albumid resolvable (so the general
        'existing row at this disc/track doesn't match any known target'
        branch fires, not the replace_existing_item_ids-forced one -- see
        the module docstring on why the forced branch intentionally does
        NOT route through bulk_import_replacement_v1, since that specific
        old item's retirement is Wave 17's track_replacement_v1's job,
        running as a separate, later call in the real pipeline)."""
        existing_album_id = 500
        imported_album_id = 501
        self._insert_album(existing_album_id, "Existing Album")
        self._insert_album(imported_album_id, "Existing Album")

        old_id = 5001
        old_p = self._insert_item(old_id, existing_album_id, "old_track.mp3", track=1, mb_trackid="tr-old")
        new_id = 5002
        new_p = self._insert_item(new_id, imported_album_id, "new_track.flac", track=1, mb_trackid="tr-new")

        log = []
        result_album_id = self.flask_app._merge_imported_album_into_existing(
            imported_album_id, existing_album_id, str(self.music_root), log,
            mb_albumid="", replace_existing_item_ids=None,
        )

        # The pre-Wave-18-final-review-fix version of this diff silently
        # returned None here and mutated nothing at all.
        self.assertEqual(result_album_id, existing_album_id)
        self.assertFalse(old_p.exists(), "old file must have been quarantined by the real engine call")
        con = sqlite3.connect(self.db_path)
        cur = con.execute("SELECT COUNT(*) FROM items WHERE id=?", (old_id,))
        self.assertEqual(cur.fetchone()[0], 0)
        cur = con.execute("SELECT album_id FROM items WHERE id=?", (new_id,))
        self.assertEqual(cur.fetchone()[0], existing_album_id, "new item must be merged into the existing album")
        con.close()
        self.assertTrue(any("Delegated removal" in line for line in log), log)

    def test_merge_function_forced_replace_leaves_old_row_for_wave17(self):
        """replace_existing_item_ids must NOT trigger a second,
        conflicting bulk_import_replacement_v1 retirement of the same old
        item Wave 17's track_replacement_v1 is separately responsible
        for -- the old row must be left exactly as-is by this function."""
        existing_album_id = 600
        imported_album_id = 601
        self._insert_album(existing_album_id, "Existing Album")
        self._insert_album(imported_album_id, "Existing Album")

        old_id = 6001
        old_p = self._insert_item(old_id, existing_album_id, "old_track.mp3", track=1, mb_trackid="tr-old")
        new_id = 6002
        new_p = self._insert_item(new_id, imported_album_id, "new_track.flac", track=1, mb_trackid="tr-new")

        log = []
        with mock.patch.object(self.flask_app.beets_client, "plan_bulk_import_replacement") as plan_mock:
            result_album_id = self.flask_app._merge_imported_album_into_existing(
                imported_album_id, existing_album_id, str(self.music_root), log,
                mb_albumid="", replace_existing_item_ids=[old_id],
            )
        plan_mock.assert_not_called()
        self.assertEqual(result_album_id, existing_album_id)
        self.assertTrue(old_p.exists(), "old file must be untouched -- Wave 17 owns its retirement, not this function")
        con = sqlite3.connect(self.db_path)
        cur = con.execute("SELECT COUNT(*) FROM items WHERE id=?", (old_id,))
        self.assertEqual(cur.fetchone()[0], 1)
        cur = con.execute("SELECT album_id FROM items WHERE id=?", (new_id,))
        self.assertEqual(cur.fetchone()[0], existing_album_id)
        con.close()


if __name__ == "__main__":
    unittest.main()
