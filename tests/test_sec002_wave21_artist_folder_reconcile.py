"""SEC-002 / ARCH-003 Wave 21: Artist Folder Merge & MBID Stamping Controlled Mutation Boundary.

Tests for artist_folder_reconcile_v1 mutation family in transaction_engine.py,
beets_control_agent.py, beets_client.py, and app.py.
"""

import ast
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import app as app_module
from backend.transaction_engine import (
    TransactionStore,
    create_artist_folder_reconcile_plan,
    execute_artist_folder_reconcile_apply,
    rollback_artist_folder_reconcile,
    _engine_stamp_artist_folder_scan,
    list_artist_folder_inventory,
)

ITEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    album TEXT,
    albumartist TEXT,
    albumartists TEXT,
    mb_albumartistid TEXT,
    mb_albumartistids TEXT,
    mb_albumid TEXT,
    mb_releasegroupid TEXT,
    year INTEGER,
    artpath BLOB
);
-- Real Beets schema (verified against actual `beets` package output, hotfix
-- v0.1.17 follow-up) has NO `albums.path` column -- only `items.path` and
-- `albums.artpath`. A prior version of this fixture synthetically added one,
-- which masked a real production bug: create_artist_folder_reconcile_plan()
-- querying a nonexistent `albums.path` column, raising
-- sqlite3.OperationalError against every real Beets library. Do not add it
-- back.

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    album_id INTEGER,
    title TEXT,
    artist TEXT,
    artists TEXT,
    albumartist TEXT,
    albumartists TEXT,
    album TEXT,
    disc INTEGER,
    track INTEGER,
    path BLOB,
    mb_trackid TEXT,
    mb_albumid TEXT,
    mb_artistid TEXT,
    mb_artistids TEXT,
    mb_albumartistid TEXT,
    mb_albumartistids TEXT,
    mb_releasegroupid TEXT,
    length REAL
);
"""

MBID_A = "aaaaaaaa-0000-0000-0000-000000000001"
MBID_B = "bbbbbbbb-0000-0000-0000-000000000002"


class Wave21BaseTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)

        mb_patcher = mock.patch("backend.transaction_engine._verify_mb_artist_recording_credit", return_value=True)
        mb_patcher.start()
        self.addCleanup(mb_patcher.stop)

        self.music_root = self.tmp_path / "music"
        self.music_root.mkdir()
        self.quarantine_root = self.tmp_path / "quarantine"
        self.quarantine_root.mkdir()

        self.db_path = self.tmp_path / "musiclibrary.db"
        con = sqlite3.connect(self.db_path)
        con.executescript(ITEMS_SCHEMA)
        con.commit()
        con.close()

        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir()
        self.tx_store = TransactionStore(root=str(store_dir))

    def _create_artist_folder(self, name: str) -> Path:
        p = self.music_root / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _create_track_file(self, rel_path: str, content: bytes = b"AUDIO_DATA") -> Path:
        p = self.music_root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def _insert_item(self, item_id: int, album_id: int, artist: str, album: str, rel_path: str, mbid: str = ""):
        p = self.music_root / rel_path
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO items (id, album_id, title, artist, artists, albumartist, albumartists, album, disc, track, path, mb_artistid, mb_artistids, mb_albumartistid, mb_albumartistids) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, album_id, "Song", artist, artist, artist, artist, album, 1, 1, str(p).encode("utf-8"), mbid, mbid, mbid, mbid)
        )
        con.commit()
        con.close()

    def _insert_album(self, album_id: int, name: str, artist: str, rel_folder: str, mbid: str = ""):
        # Real Beets schema has no albums.path column -- rel_folder is only
        # used to build the item path(s) inserted separately via
        # _insert_item(); the album row itself carries no path of its own.
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO albums (id, album, albumartist, albumartists, mb_albumartistid, mb_albumartistids) "
            "VALUES (?,?,?,?,?,?)",
            (album_id, name, artist, artist, mbid, mbid)
        )
        con.commit()
        con.close()

    def _plan(self, payload):
        return create_artist_folder_reconcile_plan(
            self.tx_store, payload,
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_root),
        )

    def _apply(self, op_id):
        return execute_artist_folder_reconcile_apply(
            self.tx_store, op_id,
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_root),
        )

    def _rollback(self, op_id):
        return rollback_artist_folder_reconcile(
            self.tx_store, op_id,
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_root),
        )


class PlanTests(Wave21BaseTest):
    def test_plan_is_non_mutating(self):
        src = self._create_artist_folder("aaliyah")
        dst = self._create_artist_folder(f"Aaliyah ({MBID_A})")
        self._create_track_file("aaliyah/Album 1/track1.mp3", b"TRACK1")

        res = self._plan({"root": str(self.music_root), "mode": "scan_merge"})
        self.assertTrue(res.get("ok"), res)
        self.assertTrue((self.music_root / "aaliyah/Album 1/track1.mp3").exists())
        self.assertTrue(src.exists())

    def test_plan_rejects_identity_conflict_established_ids(self):
        """SEC-002 Wave 21 final review, findings #4-#6: identity is
        independently derived from Beets DB state, not the caller-supplied
        MBID -- a caller cannot merge two folders whose ALREADY-ESTABLISHED
        DB identities genuinely conflict, even if it claims matching MBIDs."""
        src = self._create_artist_folder("Artist A Dup")
        dst = self._create_artist_folder("Artist A")
        self._create_track_file("Artist A Dup/Album 2/t.mp3")
        self._create_track_file("Artist A/Album 1/t.mp3")
        self._insert_album(1, "Album 1", "Artist A", "Artist A", mbid=MBID_A)
        self._insert_album(2, "Album 2", "Artist A Dup", "Artist A Dup", mbid=MBID_B)
        self._insert_item(10, 1, "Artist A", "Album 1", "Artist A/Album 1/t.mp3", mbid=MBID_A)
        self._insert_item(20, 2, "Artist A Dup", "Album 2", "Artist A Dup/Album 2/t.mp3", mbid=MBID_B)

        res = self._plan({
            "root": str(self.music_root),
            "mode": "scan_merge",
            "candidates": [{
                "source_path": str(src), "target_path": str(dst),
                # Caller claims they match -- this must not override the
                # independently-derived, genuinely conflicting DB identity.
                "source_mbid": MBID_A, "target_mbid": MBID_A,
                "fingerprint_confirmed": True,
            }],
        })
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("candidate_count"), 0)
        review = res.get("requires_review") or []
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["reason"], "artist_reconcile_identity_conflict")
        # Nothing touched.
        self.assertTrue(src.exists())
        self.assertTrue(dst.exists())

    def test_plan_allows_merge_when_established_ids_match(self):
        src = self._create_artist_folder("Artist A Dup")
        dst = self._create_artist_folder("Artist A")
        self._create_track_file("Artist A Dup/Album 2/t.mp3")
        self._create_track_file("Artist A/Album 1/t.mp3")
        self._insert_album(1, "Album 1", "Artist A", "Artist A", mbid=MBID_A)
        self._insert_album(2, "Album 2", "Artist A Dup", "Artist A Dup", mbid=MBID_A)
        self._insert_item(10, 1, "Artist A", "Album 1", "Artist A/Album 1/t.mp3", mbid=MBID_A)
        self._insert_item(20, 2, "Artist A Dup", "Album 2", "Artist A Dup/Album 2/t.mp3", mbid=MBID_A)

        res = self._plan({
            "root": str(self.music_root),
            "mode": "scan_merge",
            "candidates": [{"source_path": str(src), "target_path": str(dst)}],
        })
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("candidate_count"), 1)
        self.assertEqual(res.get("requires_review"), [])

    def test_plan_requires_evidence_for_blank_identity_merge(self):
        """Both sides blank -- name equality alone must not be sufficient
        (SEC-002 Wave 21 final review, finding #7)."""
        src = self._create_artist_folder("Bob  Marley")
        dst = self._create_artist_folder("Bob Marley")
        self._create_track_file("Bob  Marley/t.mp3")

        res = self._plan({
            "root": str(self.music_root),
            "mode": "scan_merge",
            "candidates": [{"source_path": str(src), "target_path": str(dst)}],
        })
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("candidate_count"), 0)
        review = res.get("requires_review") or []
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["reason"], "artist_reconcile_requires_review")

    def test_plan_allows_blank_identity_merge_with_fingerprint_evidence(self):
        """SEC-002 Wave 27 authority policy: Fingerprint agreement supports
        identity, but requires a canonical MBID verified engine-side (Case E).
        Fingerprint agreement alone without an MBID is review required (Case F)."""
        src = self._create_artist_folder("Bob  Marley")
        dst = self._create_artist_folder("Bob Marley")
        self._create_track_file("Bob  Marley/t.mp3")

        # Case F: Fingerprint confirmed without MBID -> review required
        res_no_mbid = self._plan({
            "root": str(self.music_root),
            "mode": "scan_merge",
            "candidates": [{"source_path": str(src), "target_path": str(dst), "fingerprint_confirmed": True}],
        })
        self.assertTrue(res_no_mbid.get("ok"), res_no_mbid)
        self.assertEqual(res_no_mbid.get("candidate_count"), 0)
        self.assertEqual((res_no_mbid.get("requires_review") or [])[0]["reason"], "artist_reconcile_requires_review")

        # Case E: Fingerprint confirmed + caller MBID verified engine-side -> eligible
        res_with_mbid = self._plan({
            "root": str(self.music_root),
            "mode": "scan_merge",
            "candidates": [{"source_path": str(src), "target_path": str(dst), "mbid": MBID_A, "fingerprint_confirmed": True}],
        })
        self.assertTrue(res_with_mbid.get("ok"), res_with_mbid)
        self.assertEqual(res_with_mbid.get("candidate_count"), 1)

    def test_plan_rejects_malformed_mbid_as_identity(self):
        """SEC-002 Wave 21 final review, finding #12: placeholder-shaped
        strings like 'uuid-1111' are not valid MusicBrainz Artist UUIDs and
        must never be trusted as identity evidence."""
        src = self._create_artist_folder("Bob  Marley")
        dst = self._create_artist_folder("Bob Marley")
        self._create_track_file("Bob  Marley/t.mp3")

        res = self._plan({
            "root": str(self.music_root),
            "mode": "scan_merge",
            "candidates": [{
                "source_path": str(src), "target_path": str(dst),
                "source_mbid": "uuid-1111", "target_mbid": "uuid-1111",
                # No fingerprint_confirmed -- malformed ids must not count
                # as identity evidence on their own.
            }],
        })
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("candidate_count"), 0)
        self.assertEqual((res.get("requires_review") or [])[0]["reason"], "artist_reconcile_requires_review")

    def test_plan_diff_lists_every_effect(self):
        """SEC-002 Wave 21 final review, finding #31: the Plan diff must
        show every planned effect, not only file moves."""
        src = self._create_artist_folder("Artist A Dup")
        dst = self._create_artist_folder("Artist A")
        self._create_track_file("Artist A Dup/Album 2/t.mp3")
        self._create_track_file("Artist A/Album 1/t.mp3")
        self._insert_album(1, "Album 1", "Artist A", "Artist A", mbid=MBID_A)
        self._insert_album(2, "Album 2", "Artist A Dup", "Artist A Dup", mbid=MBID_A)
        self._insert_item(10, 1, "Artist A", "Album 1", "Artist A/Album 1/t.mp3", mbid=MBID_A)
        self._insert_item(20, 2, "Artist A Dup", "Album 2", "Artist A Dup/Album 2/t.mp3", mbid=MBID_A)

        res = self._plan({
            "root": str(self.music_root),
            "mode": "scan_merge",
            "candidates": [{"source_path": str(src), "target_path": str(dst)}],
        })
        self.assertTrue(res.get("ok"), res)
        tx = self.tx_store.get(res["operation_id"])
        ops = {c["operation"] for c in tx["changes"]}
        self.assertIn("move_file", ops)
        self.assertIn("Stamp Artist Attributes", ops)


class StampMbidMajorityRuleTests(Wave21BaseTest):
    """SEC-002 Wave 21 final review, findings #11/#37: a conflicting known
    Artist ID must never be overwritten by a majority-rule threshold."""

    def test_conflicting_established_ids_produce_no_stamp_candidate(self):
        folder = self._create_artist_folder("Some Artist")
        self._create_track_file("Some Artist/Album 1/t.mp3")
        self._create_track_file("Some Artist/Album 2/t.mp3")
        self._create_track_file("Some Artist/Album 3/t.mp3")
        self._create_track_file("Some Artist/Album 4/t.mp3")
        # 3 albums with MBID_A, 1 album with a DIFFERENT established MBID_B.
        self._insert_album(1, "Album 1", "Some Artist", "Some Artist/Album 1", mbid=MBID_A)
        self._insert_album(2, "Album 2", "Some Artist", "Some Artist/Album 2", mbid=MBID_A)
        self._insert_album(3, "Album 3", "Some Artist", "Some Artist/Album 3", mbid=MBID_A)
        self._insert_album(4, "Album 4", "Some Artist", "Some Artist/Album 4", mbid=MBID_B)
        self._insert_item(10, 1, "Some Artist", "Album 1", "Some Artist/Album 1/t.mp3", mbid=MBID_A)
        self._insert_item(20, 2, "Some Artist", "Album 2", "Some Artist/Album 2/t.mp3", mbid=MBID_A)
        self._insert_item(30, 3, "Some Artist", "Album 3", "Some Artist/Album 3/t.mp3", mbid=MBID_A)
        self._insert_item(40, 4, "Some Artist", "Album 4", "Some Artist/Album 4/t.mp3", mbid=MBID_B)
        candidates = _engine_stamp_artist_folder_scan(self.music_root, str(self.db_path))
        self.assertEqual(candidates, [])

    def test_single_established_id_with_blank_albums_produces_candidate(self):
        folder = self._create_artist_folder("Some Artist")
        self._create_track_file("Some Artist/Album 1/t.mp3")
        self._create_track_file("Some Artist/Album 2/t.mp3")
        self._insert_album(1, "Album 1", "Some Artist", "Some Artist/Album 1", mbid=MBID_A)
        self._insert_album(2, "Album 2", "Some Artist", "Some Artist/Album 2", mbid="")
        self._insert_item(10, 1, "Some Artist", "Album 1", "Some Artist/Album 1/t.mp3", mbid=MBID_A)
        self._insert_item(20, 2, "Some Artist", "Album 2", "Some Artist/Album 2/t.mp3", mbid="")
        candidates = _engine_stamp_artist_folder_scan(self.music_root, str(self.db_path))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["mbid"], MBID_A)

    def test_apply_stamp_never_overwrites_conflicting_album(self):
        """End-to-end: even if a caller directly supplies a stamp candidate
        despite the conflict, Apply must never touch the conflicting
        album's own attributes via a name-based blanket update."""
        folder = self._create_artist_folder("Some Artist")
        self._create_track_file("Some Artist/Album 1/t.mp3")
        self._create_track_file("Some Artist/Album 2/t.mp3")
        self._insert_album(1, "Album 1", "Some Artist", "Some Artist/Album 1", mbid=MBID_A)
        self._insert_album(2, "Album 2", "Some Artist", "Some Artist/Album 2", mbid=MBID_B)
        self._insert_item(10, 1, "Some Artist", "Album 1", "Some Artist/Album 1/t.mp3", mbid=MBID_A)
        self._insert_item(20, 2, "Some Artist", "Album 2", "Some Artist/Album 2/t.mp3", mbid=MBID_B)

        target = self.music_root / f"Some Artist ({MBID_A})"
        res = self._plan({
            "root": str(self.music_root),
            "mode": "stamp_mbid",
            "candidates": [{
                "source_path": str(folder), "target_path": str(target),
                "mbid": MBID_A, "source_name": "Some Artist", "target_name": target.name,
            }],
        })
        # Established conflict inside the same folder -- refused.
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("candidate_count"), 0)

        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT mb_albumartistid FROM albums WHERE id=2").fetchone()
        con.close()
        self.assertEqual(row[0], MBID_B)


class ApplyTests(Wave21BaseTest):
    def test_apply_moves_files_quarantines_duplicates_and_updates_db(self):
        src = self._create_artist_folder("artist_source")
        dst = self._create_artist_folder("artist_target")

        self._create_track_file("artist_source/Album A/track1.flac", b"TRACK1_DATA")
        self._insert_album(10, "Album A", "artist_source", "artist_source/Album A")
        self._insert_item(100, 10, "artist_source", "Album A", "artist_source/Album A/track1.flac")

        plan_res = self._plan({
            "root": str(self.music_root), "mode": "scan_merge",
            "candidates": [{"source_path": str(src), "target_path": str(dst), "mbid": MBID_A, "fingerprint_confirmed": True}],
        })
        self.assertTrue(plan_res.get("ok"), plan_res)
        op_id = plan_res["operation_id"]

        apply_res = self._apply(op_id)
        self.assertTrue(apply_res.get("ok"), apply_res)
        self.assertEqual(apply_res.get("status"), "Completed")

        self.assertTrue((self.music_root / "artist_target/Album A/track1.flac").exists())
        self.assertFalse((self.music_root / "artist_source/Album A/track1.flac").exists())

        con = sqlite3.connect(self.db_path)
        cur = con.execute("SELECT path, albumartist FROM items WHERE id=100")
        row = cur.fetchone()
        con.close()
        expected_path = str(self.music_root / "artist_target/Album A/track1.flac").encode("utf-8")
        self.assertEqual(row[0], expected_path)

    def test_apply_stamps_exact_bound_rows_not_whole_library(self):
        """SEC-002 Wave 21 final review, finding #26: an unrelated item
        that happens to share the source artist's TEXT name, but lives
        somewhere else entirely (not under the merged folder), must never
        be touched."""
        src = self._create_artist_folder("artist_source")
        dst = self._create_artist_folder("artist_target")
        self._create_track_file("artist_source/Album A/track1.flac")
        self._insert_album(10, "Album A", "artist_source", "artist_source/Album A")
        self._insert_item(100, 10, "artist_source", "Album A", "artist_source/Album A/track1.flac")

        # Unrelated row, same artist text, different (unrelated) folder --
        # not part of this transaction's discovered id sets at all.
        other = self._create_artist_folder("unrelated_other_folder")
        self._create_track_file("unrelated_other_folder/Album B/other.flac")
        self._insert_album(11, "Album B", "artist_source", "unrelated_other_folder/Album B")
        self._insert_item(200, 11, "artist_source", "Album B", "unrelated_other_folder/Album B/other.flac")

        plan_res = self._plan({
            "root": str(self.music_root), "mode": "scan_merge",
            "candidates": [{"source_path": str(src), "target_path": str(dst), "mbid": MBID_A, "fingerprint_confirmed": True}],
        })
        apply_res = self._apply(plan_res["operation_id"])
        self.assertTrue(apply_res.get("ok"), apply_res)

        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT albumartist FROM items WHERE id=200").fetchone()
        con.close()
        self.assertEqual(row[0], "artist_source")

    def test_apply_quarantine_collision_free_across_subdirectories(self):
        """SEC-002 Wave 21 final review, finding #21: two different album
        subdirectories with an identically-named duplicate file must not
        collide in quarantine."""
        src = self._create_artist_folder("artist_source")
        dst = self._create_artist_folder("artist_target")
        self._create_track_file("artist_source/Album A/folder.jpg", b"IMG_A")
        self._create_track_file("artist_target/Album A/folder.jpg", b"IMG_A_DIFFERENT")
        self._create_track_file("artist_source/Album B/folder.jpg", b"IMG_B")
        self._create_track_file("artist_target/Album B/folder.jpg", b"IMG_B_DIFFERENT")

        plan_res = self._plan({
            "root": str(self.music_root), "mode": "scan_merge",
            "candidates": [{"source_path": str(src), "target_path": str(dst), "mbid": MBID_A, "fingerprint_confirmed": True}],
        })
        self.assertTrue(plan_res.get("ok"), plan_res)
        apply_res = self._apply(plan_res["operation_id"])
        self.assertTrue(apply_res.get("ok"), apply_res)
        # Both quarantined art files must be independently recoverable --
        # neither one overwrote the other.
        tx = self.tx_store.get(plan_res["operation_id"])
        q_records = tx["metadata"].get("quarantined_records") or []
        art_records = [q for q in q_records if q["type"] == "artwork"]
        self.assertEqual(len(art_records), 2)
        quarantine_paths = {r["quarantine"] for r in art_records}
        self.assertEqual(len(quarantine_paths), 2)
        for r in art_records:
            self.assertTrue(Path(r["quarantine"]).exists())

    def test_apply_idempotency_returns_already_completed(self):
        src = self._create_artist_folder("artist_x_source")
        dst = self._create_artist_folder("artist_x_target")
        self._create_track_file("artist_x_source/Song.mp3", b"SONG")

        plan_res = self._plan({
            "root": str(self.music_root), "mode": "scan_merge",
            "candidates": [{"source_path": str(src), "target_path": str(dst), "mbid": MBID_A, "fingerprint_confirmed": True}],
        })
        op_id = plan_res["operation_id"]

        apply1 = self._apply(op_id)
        self.assertTrue(apply1.get("ok"))

        apply2 = self._apply(op_id)
        self.assertTrue(apply2.get("ok"))
        self.assertTrue(apply2.get("already_completed"))

    def test_apply_toctou_full_stat_mismatch(self):
        """SEC-002 Wave 21 final review, finding #15: dev/inode are part of
        the TOCTOU check, not just size/mtime."""
        src = self._create_artist_folder("artist_toctou_source")
        dst = self._create_artist_folder("artist_toctou_target")
        f = self._create_track_file("artist_toctou_source/Song.mp3", b"SONG")

        plan_res = self._plan({
            "root": str(self.music_root), "mode": "scan_merge",
            "candidates": [{"source_path": str(src), "target_path": str(dst), "mbid": MBID_A, "fingerprint_confirmed": True}],
        })
        op_id = plan_res["operation_id"]
        # Same size/mtime is hard to force without touching the fs; replace
        # content (changes size) to prove the mismatch path fails closed.
        f.write_bytes(b"DIFFERENT_CONTENT_HERE")
        apply_res = self._apply(op_id)
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "artist_reconcile_toctou_mismatch")
        self.assertTrue(f.exists())

    def test_apply_wrong_mutation_family(self):
        tx = self.tx_store.create(
            operation_type="Merge Album", status="Preview", summary="wrong family",
            metadata={"mutation_family": "existing_album_reconcile_v1"},
        )
        res = self._apply(tx["id"])
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "artist_reconcile_family_mismatch")


class RollbackTests(Wave21BaseTest):
    def test_rollback_restores_filesystem_and_db(self):
        src = self._create_artist_folder("old_artist_dir")
        dst = self._create_artist_folder("new_artist_dir")
        self._create_track_file("old_artist_dir/Album/t.mp3", b"MUSIC")
        self._insert_album(1, "Album", "old_artist_dir", "old_artist_dir/Album")
        self._insert_item(10, 1, "old_artist_dir", "Album", "old_artist_dir/Album/t.mp3")

        plan_res = self._plan({
            "root": str(self.music_root), "mode": "scan_merge",
            "candidates": [{"source_path": str(src), "target_path": str(dst), "mbid": MBID_A, "fingerprint_confirmed": True}],
        })
        op_id = plan_res["operation_id"]
        self.assertTrue(self._apply(op_id).get("ok"))

        rb_res = self._rollback(op_id)
        self.assertTrue(rb_res.get("ok"), rb_res)
        self.assertEqual(rb_res.get("status"), "Rolled Back")

        self.assertTrue((self.music_root / "old_artist_dir/Album/t.mp3").exists())

        con = sqlite3.connect(self.db_path)
        cur = con.execute("SELECT path FROM items WHERE id=10")
        row = cur.fetchone()
        con.close()
        old_path_b = str(self.music_root / "old_artist_dir/Album/t.mp3").encode("utf-8")
        self.assertEqual(row[0], old_path_b)

    def test_rollback_restores_artist_attributes(self):
        """SEC-002 Wave 21 final review, finding #28: the original
        implementation restored paths but never the artist/MBID attribute
        stamping Apply performed."""
        src = self._create_artist_folder("Some Artist")
        self._create_track_file("Some Artist/Album 1/t.mp3")
        self._insert_album(1, "Album 1", "Some Artist", "Some Artist/Album 1", mbid="")
        self._insert_item(10, 1, "Some Artist", "Album 1", "Some Artist/Album 1/t.mp3", mbid="")

        target = self.music_root / f"Some Artist ({MBID_A})"
        plan_res = self._plan({
            "root": str(self.music_root), "mode": "stamp_mbid",
            "candidates": [{
                "source_path": str(src), "target_path": str(target),
                "mbid": MBID_A, "source_name": "Some Artist", "target_name": target.name,
                "fingerprint_confirmed": True,
            }],
        })
        self.assertTrue(plan_res.get("ok"), plan_res)
        self.assertEqual(plan_res.get("candidate_count"), 1)
        op_id = plan_res["operation_id"]
        apply_res = self._apply(op_id)
        self.assertTrue(apply_res.get("ok"), apply_res)

        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT albumartist, mb_albumartistid FROM albums WHERE id=1").fetchone()
        con.close()
        self.assertEqual(row[0], target.name)
        self.assertEqual(row[1], MBID_A)

        rb_res = self._rollback(op_id)
        self.assertTrue(rb_res.get("ok"), rb_res)

        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT albumartist, mb_albumartistid FROM albums WHERE id=1").fetchone()
        item_row = con.execute("SELECT artist, mb_artistid FROM items WHERE id=10").fetchone()
        con.close()
        self.assertEqual(row[0], "Some Artist")
        self.assertEqual(row[1], "")
        self.assertEqual(item_row[0], "Some Artist")
        self.assertEqual(item_row[1], "")

    def test_rollback_repeated_is_idempotent(self):
        src = self._create_artist_folder("repeat_src")
        dst = self._create_artist_folder("repeat_dst")
        self._create_track_file("repeat_src/Song.mp3")
        plan_res = self._plan({
            "root": str(self.music_root), "mode": "scan_merge",
            "candidates": [{"source_path": str(src), "target_path": str(dst), "mbid": MBID_A, "fingerprint_confirmed": True}],
        })
        op_id = plan_res["operation_id"]
        self.assertTrue(self._apply(op_id).get("ok"))
        first = self._rollback(op_id)
        self.assertTrue(first.get("ok"), first)
        second = self._rollback(op_id)
        self.assertTrue(second.get("ok"))
        self.assertTrue(second.get("already_rolled_back"))

    def test_rollback_unmutated_refused(self):
        src = self._create_artist_folder("nm_src")
        dst = self._create_artist_folder("nm_dst")
        self._create_track_file("nm_src/Song.mp3")
        plan_res = self._plan({
            "root": str(self.music_root), "mode": "scan_merge",
            "candidates": [{"source_path": str(src), "target_path": str(dst), "mbid": MBID_A, "fingerprint_confirmed": True}],
        })
        res = self._rollback(plan_res["operation_id"])
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "artist_reconcile_not_mutated")


class RealProductionPathTests(Wave21BaseTest):
    def setUp(self):
        super().setUp()

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

        self._db_patch = mock.patch.object(app_module, "_db", _local_db)
        self._db_patch.start()
        self.addCleanup(self._db_patch.stop)

        self._music_root_patch = mock.patch.object(app_module, "MUSIC_ROOT", self.music_root)
        self._music_root_patch.start()
        self.addCleanup(self._music_root_patch.stop)

        def mock_plan(payload, **kw):
            return self._plan(payload)

        def mock_apply(op_id, **kw):
            return self._apply(op_id)

        self._plan_patch = mock.patch.object(app_module.composite_workflows, "plan_artist_folder_reconcile", side_effect=mock_plan)
        self._plan_patch.start()
        self.addCleanup(self._plan_patch.stop)
        self._apply_patch = mock.patch.object(app_module.composite_workflows, "apply_artist_folder_reconcile", side_effect=mock_apply)
        self._apply_patch.start()
        self.addCleanup(self._apply_patch.stop)

    def test_production_path_apply_artist_folder_groups(self):
        stamped = self._create_artist_folder(f"Artist Name ({MBID_A})")
        unstamped = self._create_artist_folder("artist name")
        self._create_track_file("artist name/Album/track.flac", b"TRACK_BYTES")
        self._insert_album(1, "Album", "artist name", "artist name/Album", mbid=MBID_A)
        self._insert_item(1, 1, "artist name", "Album", "artist name/Album/track.flac", mbid=MBID_A)

        mock_groups = [{
            "key": "artistname",
            "canonical": {"path": str(stamped), "name": stamped.name},
            "sources": [{"path": str(unstamped), "name": unstamped.name}],
            "musicbrainz": {"id": MBID_A},
        }]

        with mock.patch.object(app_module, "_scan_artist_folder_groups", return_value=mock_groups):
            with mock.patch.object(app_module, "_artist_folder_fingerprint_confirms", return_value=True):
                log = []
                res = app_module._apply_artist_folder_groups(str(self.music_root), None, False, log, use_musicbrainz=False)
                self.assertEqual(res.get("groups"), 1, f"Log output: {log}")
                self.assertTrue(any("Delegated artist folder merge to engine" in line for line in log))


class WebManagerMutationProhibitionTests(unittest.TestCase):
    """AST structural inspection asserting Web Manager contains zero direct
    mutations AND zero direct references to engine mutation internals
    (SEC-002 Wave 21 final review, finding #3: the original AST test only
    checked for raw os/shutil calls, so it passed even while the function
    imported and directly executed transaction_engine's Plan/Apply
    functions in-process)."""

    def test_artist_folder_functions_contain_no_direct_mutations(self):
        with open("app.py", "r", encoding="utf-8") as f:
            source = f.read()
            tree = ast.parse(source, filename="app.py")

        target_funcs = {
            "_merge_artist_dir_contents",
            "_apply_artist_folder_groups",
            "clean_artist_folders_stamp_mbid",
        }

        prohibited_attributes = {"unlink", "rename", "replace", "rmdir", "remove"}
        prohibited_shutil = {"move", "rmtree", "copy", "copy2"}
        prohibited_names = {
            "TransactionStore",
            "create_artist_folder_reconcile_plan",
            "execute_artist_folder_reconcile_apply",
            "rollback_artist_folder_reconcile",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in target_funcs:
                fn_source = ast.get_source_segment(source, node) or ""
                for name in prohibited_names:
                    self.assertNotIn(name, fn_source, f"Prohibited reference '{name}' found in {node.name} -- engine mutation must only be reached via BeetsClient")
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and sub.attr in prohibited_attributes:
                        self.fail(f"Direct mutation .{sub.attr} found in {node.name}")
                    if isinstance(sub, ast.Call):
                        if isinstance(sub.func, ast.Attribute) and isinstance(sub.func.value, ast.Name):
                            if sub.func.value.id == "shutil" and sub.func.attr in prohibited_shutil:
                                self.fail(f"Direct shutil.{sub.func.attr} found in {node.name}")
                        if isinstance(sub.func, ast.Name) and sub.func.id == "sqlite3":
                            self.fail(f"Direct sqlite3 usage found in {node.name}")
                    if isinstance(sub, ast.ImportFrom) and sub.module == "backend.transaction_engine":
                        imported = {alias.name for alias in sub.names}
                        leaked = imported & prohibited_names
                        self.assertFalse(leaked, f"{node.name} imports engine mutation functions directly: {leaked}")


class ResilientApplyAgainstLostResponseTests(unittest.TestCase):
    """Hotfix v0.1.17 (BUG-4/BUG-5): a real TrueNAS v0.1.16 production
    incident. Web Manager's own client-side timeout fired on
    apply_artist_folder_reconcile() while the Beets Engine kept executing
    the controlled mutation normally; Web Manager logged "engine
    unavailable" and gave up, then the engine's own attempt to write back
    its now-orphaned response produced a BrokenPipeError. Production
    inspection proved the mutation continued moving from file to file
    after Web Manager had already reported failure.

    These tests exercise app._apply_artist_folder_reconcile_resilient()
    directly (the shared helper all three real apply_artist_folder_reconcile
    call sites in app.py now go through) against a mocked beets_client,
    with poll/max-wait configuration patched to small values so the tests
    run in well under a second rather than actually waiting minutes.
    """

    def setUp(self):
        self.patchers = []
        self._patch(mock.patch.object(app_module, "BEETS_LONG_OPERATION_POLL_SECONDS", 0.01))
        self._patch(mock.patch.object(app_module, "BEETS_LONG_OPERATION_MAX_SECONDS", 0.2))

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def _patch(self, patcher):
        self.patchers.append(patcher)
        return patcher.start()

    def test_apply_success_on_the_first_call_never_polls(self):
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            return_value={"ok": True, "operation_id": "op-1", "status": "Completed"},
        ))
        get_tx_mock = self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction"))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-1", log)

        self.assertTrue(result.get("ok"))
        apply_mock.assert_called_once()
        get_tx_mock.assert_not_called()

    def test_lost_apply_response_polls_and_reports_the_real_completed_outcome(self):
        """The exact production incident: apply's own HTTP response is
        lost (client-side timeout), but the engine actually completed the
        mutation. Apply must be called exactly once; the real outcome must
        come from polling the transaction, not from a second apply call."""
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ))
        statuses = iter(["Running", "Running", "Completed"])
        get_tx_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "get_transaction",
            side_effect=lambda op_id: {"ok": True, "transaction": {"status": next(statuses), "operation_id": op_id}},
        ))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-2", log)

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("status"), "Completed")
        self.assertTrue(result.get("recovered_via_poll"))
        apply_mock.assert_called_once()
        self.assertGreaterEqual(get_tx_mock.call_count, 3, "must have polled through both Running states to Completed")
        self.assertFalse(any("ENGINE_OFFLINE" in line for line in log), "must not report ENGINE_OFFLINE while the engine is genuinely still working")
        joined_log = "\n".join(log)
        self.assertIn("op-2", joined_log)

    def test_lost_apply_response_then_confirmed_failed_is_reported_as_failed(self):
        self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ))
        self._patch(mock.patch.object(
            app_module.composite_workflows, "get_transaction",
            return_value={"ok": True, "transaction": {"status": "Failed", "operation_id": "op-3"}},
        ))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-3", log)

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status"), "Failed")
        self.assertTrue(result.get("recovered_via_poll"))

    def test_apply_never_called_a_second_time_even_across_many_poll_iterations(self):
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ))
        self._patch(mock.patch.object(
            app_module.composite_workflows, "get_transaction",
            return_value={"ok": True, "transaction": {"status": "Running", "operation_id": "op-4"}},
        ))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-4", log)

        # Max-wait exceeded while still "Running" -- must report "still
        # running", never fabricate success, and never call Apply again.
        self.assertFalse(result.get("ok"))
        self.assertTrue(result.get("still_running"))
        apply_mock.assert_called_once()

    def test_transient_transaction_lookup_failures_are_retried_not_fatal(self):
        """A poll that itself fails to reach the engine (still recovering)
        must be retried within the bound, not treated as a final failure."""
        self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ))
        responses = iter([
            app_module.BeetsUnavailableError("engine still recovering"),
            app_module.BeetsUnavailableError("engine still recovering"),
            {"ok": True, "transaction": {"status": "Completed", "operation_id": "op-5"}},
        ])

        def _get_transaction(op_id):
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return item

        self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction", side_effect=_get_transaction))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-5", log)

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("status"), "Completed")

    def test_cancellation_stops_polling_without_reporting_false_success_or_failure(self):
        cancel_event = mock.MagicMock()
        cancel_event.is_set.return_value = True
        self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ))
        get_tx_mock = self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction"))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-6", log, cancel_event=cancel_event)

        self.assertFalse(result.get("ok"))
        self.assertTrue(result.get("still_running"))
        get_tx_mock.assert_not_called()

    def test_clean_artist_folders_stamp_mbid_job_does_not_reapply_on_lost_response(self):
        """End-to-end through the real production call site: clean_artist_folders_stamp_mbid()'s
        background job must call apply_artist_folder_reconcile at most
        once even when its own client-side call fails, and must recover
        the real outcome via transaction polling."""
        with mock.patch.object(app_module, "MUSIC_ROOT", Path(tempfile.mkdtemp())), \
             mock.patch.object(app_module, "_security_auth_disabled", return_value=True):
            with mock.patch.object(
                app_module, "_stamp_artist_folder_scan",
                return_value={"candidates": [{"source": "x"}], "skipped": []},
            ), mock.patch.object(
                app_module.composite_workflows, "plan_artist_folder_reconcile",
                return_value={"ok": True, "operation_id": "op-7"},
            ), mock.patch.object(
                app_module.composite_workflows, "apply_artist_folder_reconcile",
                side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
            ) as apply_mock, mock.patch.object(
                app_module.composite_workflows, "get_transaction",
                return_value={"ok": True, "transaction": {"status": "Completed", "operation_id": "op-7", "renamed": 2, "merged": 1}},
            ):
                with app_module.app.test_request_context(
                    "/api/clean/artist-folders/stamp-mbid",
                    method="POST",
                    json={"root": str(app_module.MUSIC_ROOT), "dry_run": True},
                ):
                    payload = app_module.request.get_json(silent=True) or {}
                root_path, _err = app_module._artist_folder_repair_root(payload.get("root") or str(app_module.MUSIC_ROOT))

                # Reach into the real, unexported `_do` closure the same way
                # the maintenance job does: call the route in non-dry-run
                # mode via test_request_context, but capture the job body
                # directly rather than going through the async job store.
                with app_module.app.test_request_context(
                    "/api/clean/artist-folders/stamp-mbid",
                    method="POST",
                    json={"root": str(root_path), "dry_run": False},
                ):
                    captured = {}
                    real_start_python = app_module.jobs.start_python

                    def _capture_and_run(fn, label="", metadata=None):
                        log = []
                        fn(log)
                        captured["log"] = log
                        return mock.MagicMock(job_id="job-7")

                    with mock.patch.object(app_module.jobs, "start_python", side_effect=_capture_and_run):
                        app_module.clean_artist_folders_stamp_mbid()

                apply_mock.assert_called_once()
                joined_log = "\n".join(captured.get("log") or [])
                self.assertNotIn("ENGINE_OFFLINE", joined_log)
                self.assertIn("op-7", joined_log)

    def test_bad_request_fails_immediately_without_polling(self):
        """A definite HTTP 400 means the engine already answered "no" (bad
        operation_id/payload) -- not that the response was lost. Must never
        enter the transaction poll loop, and must never call Apply again."""
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsBadRequestError(
                "Beets API bad request: operation not in Pending/Approved state",
                error_code="INVALID_STATE", status_code=400,
            ),
        ))
        get_tx_mock = self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction"))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-400", log)

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "INVALID_STATE")
        self.assertEqual(result.get("status_code"), 400)
        apply_mock.assert_called_once()
        get_tx_mock.assert_not_called()
        self.assertFalse(any("polling" in line.lower() and "response was lost" in line.lower() for line in log))

    def test_auth_error_fails_immediately_without_polling(self):
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsAuthError(
                "Authentication with Beets Control Agent failed: HTTP 401",
                error_code="ENGINE_AUTH_FAILED", status_code=401,
            ),
        ))
        get_tx_mock = self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction"))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-401", log)

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status_code"), 401)
        apply_mock.assert_called_once()
        get_tx_mock.assert_not_called()

    def test_not_found_fails_immediately_without_polling(self):
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsNotFoundError(
                "Beets API resource not found: operation_id unknown",
                error_code="NOT_FOUND", status_code=404,
            ),
        ))
        get_tx_mock = self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction"))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-404", log)

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status_code"), 404)
        apply_mock.assert_called_once()
        get_tx_mock.assert_not_called()

    def test_forbidden_403_fails_immediately_without_polling(self):
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsAuthError(
                "Access to Beets Control Agent forbidden: HTTP 403",
                error_code="FORBIDDEN", status_code=403,
            ),
        ))
        get_tx_mock = self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction"))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-403", log)

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status_code"), 403)
        apply_mock.assert_called_once()
        get_tx_mock.assert_not_called()

    def test_ambiguous_5xx_still_polls_unlike_definite_4xx(self):
        """A generic 5xx (not one of the specific 4xx rejection types) means
        the engine may have started mutating before failing to answer --
        this is transport/execution uncertainty, not a definite rejection,
        and must still recover via the transaction poll like a
        BeetsUnavailableError does."""
        apply_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsError(
                "Beets Control Agent server error: HTTP 500", error_code="ENGINE_SERVER_ERROR", status_code=500,
            ),
        ))
        get_tx_mock = self._patch(mock.patch.object(
            app_module.composite_workflows, "get_transaction",
            return_value={"ok": True, "transaction": {"status": "Completed", "operation_id": "op-500"}},
        ))

        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-500", log)

        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("recovered_via_poll"))
        apply_mock.assert_called_once()
        get_tx_mock.assert_called()


class ListArtistFolderInventoryTests(unittest.TestCase):
    """ARCH-020: the read-only Control Agent endpoint backing engine-side
    artist-folder candidate discovery for Web Manager (which has no local
    media mount in the supported two-service deployment)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name) / "music"
        self.root.mkdir()

    def test_lists_immediate_subfolders_with_audio_counts(self):
        artist = self.root / "Artist A"
        artist.mkdir()
        (artist / "track1.mp3").write_bytes(b"x")
        (artist / "track2.mp3").write_bytes(b"x")
        (artist / "subdir").mkdir()
        (self.root / ".hidden").mkdir()
        (self.root / "not-a-folder.txt").write_bytes(b"x")

        res = list_artist_folder_inventory({"root": str(self.root)}, music_allowed_roots=[str(self.root)])

        self.assertTrue(res.get("ok"), res)
        names = {f["name"] for f in res["folders"]}
        self.assertEqual(names, {"Artist A"})
        entry = res["folders"][0]
        self.assertEqual(entry["audio_files"], 2)
        self.assertEqual(entry["subfolders"], 1)
        self.assertEqual(entry["path"], str(artist))

    def test_rejects_root_outside_allowed_roots(self):
        outside = Path(self._tmpdir.name) / "outside"
        outside.mkdir()
        res = list_artist_folder_inventory({"root": str(outside)}, music_allowed_roots=[str(self.root)])
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "artist_reconcile_path_out_of_root")

    def test_rejects_nonexistent_root(self):
        res = list_artist_folder_inventory(
            {"root": str(self.root / "does-not-exist")}, music_allowed_roots=[str(self.root)],
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "artist_reconcile_invalid_root")

    def test_requires_root(self):
        res = list_artist_folder_inventory({}, music_allowed_roots=[str(self.root)])
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "artist_reconcile_invalid_root")

    def test_rejects_symlink_component(self):
        real_dir = Path(self._tmpdir.name) / "real"
        real_dir.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(real_dir, target_is_directory=True)
        except (OSError, NotImplementedError) as ex:
            self.skipTest(f"symlink creation unavailable: {ex}")
        res = list_artist_folder_inventory({"root": str(link)}, music_allowed_roots=[str(self.root)])
        self.assertFalse(res.get("ok"))


class StampArtistFolderScanFailClosedTests(unittest.TestCase):
    """Independent review follow-up: an engine inventory failure must never
    be indistinguishable from a genuine successful scan that found zero
    eligible folders. _stamp_artist_folder_scan() must report ok=False with
    the real error/error_code, not the same empty
    {"candidates": [], "skipped": []} shape a real empty scan returns."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name) / "music"
        self.root.mkdir()

    def test_beets_unavailable_reports_ok_false_not_empty_success(self):
        with mock.patch.object(
            app_module.composite_workflows, "get_artist_folder_inventory",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ):
            result = app_module._stamp_artist_folder_scan(self.root)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result.get("error"), "Beets Control Agent is unavailable.")

    def test_beets_unavailable_does_not_expose_raw_exception_text(self):
        """CodeQL: information exposure through an exception. A
        BeetsUnavailableError's own message can carry internal URLs, host
        names, or ports (it is built from the real connection failure) --
        none of that may reach the "error" field callers surface to HTTP
        responses, job logs, and job results."""
        sensitive = "http://internal-secret-host.example:9999/beets-agent?token=abc123secret"
        with mock.patch.object(
            app_module.composite_workflows, "get_artist_folder_inventory",
            side_effect=app_module.BeetsUnavailableError(f"Beets Control Agent is unavailable at {sensitive}: refused"),
        ):
            result = app_module._stamp_artist_folder_scan(self.root)
        self.assertFalse(result.get("ok"))
        self.assertNotIn(sensitive, result.get("error", ""))
        self.assertNotIn("secret", result.get("error", "").lower())
        self.assertEqual(result.get("error"), "Beets Control Agent is unavailable.")

    def test_beets_auth_error_reports_ok_false_with_error_code(self):
        with mock.patch.object(
            app_module.composite_workflows, "get_artist_folder_inventory",
            side_effect=app_module.BeetsAuthError(
                "Authentication with Beets Control Agent failed: HTTP 401",
                error_code="ENGINE_AUTH_FAILED", status_code=401,
            ),
        ):
            result = app_module._stamp_artist_folder_scan(self.root)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "ENGINE_AUTH_FAILED")
        self.assertEqual(result.get("status_code"), 401)
        # error_code/status_code are agent-controlled structured fields and
        # stay intact; the message itself is the canned safe text, not the
        # raw exception string.
        self.assertEqual(result.get("error"), "Authentication with Beets Control Agent failed.")

    def test_beets_auth_error_does_not_expose_raw_exception_text(self):
        sensitive = "/config/.beet_secret_token_file"
        with mock.patch.object(
            app_module.composite_workflows, "get_artist_folder_inventory",
            side_effect=app_module.BeetsAuthError(f"Authentication failed reading {sensitive}: permission denied"),
        ):
            result = app_module._stamp_artist_folder_scan(self.root)
        self.assertNotIn(sensitive, result.get("error", ""))
        self.assertNotIn("permission denied", result.get("error", ""))

    def test_unexpected_exception_does_not_expose_raw_text(self):
        sensitive = "/home/runner/work/secret-internal-path/credentials.json"
        with mock.patch.object(
            app_module.composite_workflows, "get_artist_folder_inventory",
            side_effect=RuntimeError(f"unexpected failure reading {sensitive}"),
        ):
            result = app_module._stamp_artist_folder_scan(self.root)
        self.assertFalse(result.get("ok"))
        self.assertNotIn(sensitive, result.get("error", ""))
        self.assertNotIn("credentials", result.get("error", "").lower())
        self.assertEqual(result.get("error"), "Artist-folder inventory failed.")

    def test_genuine_empty_inventory_reports_ok_true(self):
        with mock.patch.object(app_module.composite_workflows, "get_artist_folder_inventory", return_value=[]), \
             mock.patch.object(app_module.composite_workflows, "get_artist_folder_album_mbids", return_value=[]):
            result = app_module._stamp_artist_folder_scan(self.root)
        self.assertTrue(result.get("ok"))
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["skipped"], [])

    def test_mbid_counts_engine_failure_reports_ok_false(self):
        artist_dir = self.root / "Some Artist"
        artist_dir.mkdir()
        with mock.patch.object(
            app_module.composite_workflows, "get_artist_folder_inventory",
            return_value=[{"name": "Some Artist", "path": str(artist_dir), "audio_files": 1, "subfolders": 0}],
        ), mock.patch.object(
            app_module, "_stamp_artist_folder_album_mbid_counts",
            return_value=({}, {}, "engine unavailable"),
        ):
            result = app_module._stamp_artist_folder_scan(self.root)
        self.assertFalse(result.get("ok"))
        self.assertIn("engine unavailable", result.get("error", ""))

    def test_only_genuine_success_produces_no_folders_need_stamping_message(self):
        """End-to-end through the real production call sites: only a
        genuinely successful, empty scan may produce the "No artist folders
        need MB ID stamping" outcome -- an engine failure must be reported
        as a failure instead."""
        with mock.patch.object(app_module, "MUSIC_ROOT", self.root), \
             mock.patch.object(app_module, "_security_auth_disabled", return_value=True):
            # Failure case: the real job must raise (fail closed), not
            # report "no artist folders need MB ID stamping".
            with mock.patch.object(
                app_module.composite_workflows, "get_artist_folder_inventory",
                side_effect=app_module.BeetsUnavailableError("engine unreachable"),
            ):
                with app_module.app.test_request_context(
                    "/api/clean/artist-folders/stamp-mbid", method="POST",
                    json={"root": str(self.root), "dry_run": False},
                ):
                    captured = {}

                    def _capture_and_run(fn, label="", metadata=None):
                        log = []
                        try:
                            fn(log)
                            captured["raised"] = None
                        except Exception as ex:
                            captured["raised"] = ex
                        captured["log"] = log
                        return mock.MagicMock(job_id="job-fail")

                    with mock.patch.object(app_module.jobs, "start_python", side_effect=_capture_and_run):
                        app_module.clean_artist_folders_stamp_mbid()
                self.assertIsNotNone(captured["raised"], "an engine inventory failure must raise, not silently succeed")
                joined = "\n".join(captured["log"])
                self.assertNotIn("No artist folders need MB ID stamping", joined)

            # Genuine success case: an empty inventory legitimately produces
            # the "no folders need stamping" outcome.
            with mock.patch.object(app_module.composite_workflows, "get_artist_folder_inventory", return_value=[]), \
                 mock.patch.object(app_module.composite_workflows, "get_artist_folder_album_mbids", return_value=[]):
                with app_module.app.test_request_context(
                    "/api/clean/artist-folders/stamp-mbid", method="POST",
                    json={"root": str(self.root), "dry_run": False},
                ):
                    captured2 = {}

                    def _capture_and_run2(fn, label="", metadata=None):
                        log = []
                        fn(log)
                        captured2["log"] = log
                        return mock.MagicMock(job_id="job-ok")

                    with mock.patch.object(app_module.jobs, "start_python", side_effect=_capture_and_run2):
                        app_module.clean_artist_folders_stamp_mbid()
                self.assertIn("No artist folders need MB ID stamping", "\n".join(captured2["log"]))

    def test_dry_run_http_response_does_not_expose_raw_exception_text(self):
        """CodeQL finding: the dry-run route's error response used to
        return scan.get("error") straight from str(ex). Verify the real
        HTTP JSON response for the exact route CodeQL flagged never
        contains the raw exception text, while still returning a safe
        status code and error_code."""
        sensitive = "postgresql://internal-user:hunter2@10.0.0.55:5432/beetsdb"
        with mock.patch.object(app_module, "MUSIC_ROOT", self.root), \
             mock.patch.object(app_module, "_security_auth_disabled", return_value=True), \
             mock.patch.object(
                 app_module.composite_workflows, "get_artist_folder_inventory",
                 side_effect=app_module.BeetsAuthError(
                     f"Authentication with Beets Control Agent failed via {sensitive}: HTTP 401",
                     error_code="ENGINE_AUTH_FAILED", status_code=401,
                 ),
             ):
            with app_module.app.test_client() as client:
                resp = client.post(
                    "/api/clean/artist-folders/stamp-mbid",
                    json={"root": str(self.root), "dry_run": True},
                )
        raw_body = resp.get_data(as_text=True)
        self.assertNotIn(sensitive, raw_body)
        self.assertNotIn("hunter2", raw_body)
        data = resp.get_json()
        self.assertFalse(data.get("ok"))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(data.get("error_code"), "ENGINE_AUTH_FAILED")
        self.assertEqual(data.get("error"), "Authentication with Beets Control Agent failed.")

    def test_apply_job_does_not_expose_raw_exception_text_in_log_or_result(self):
        """The async (non-dry-run) stamp-mbid job's log and returned result
        must not expose the raw exception either -- only the job must
        raise (fail closed), never the leaked internal detail."""
        sensitive = "s3://internal-bucket/private-config.yaml?sig=abcdef123456"
        with mock.patch.object(app_module, "MUSIC_ROOT", self.root), \
             mock.patch.object(app_module, "_security_auth_disabled", return_value=True), \
             mock.patch.object(
                 app_module.composite_workflows, "get_artist_folder_inventory",
                 side_effect=app_module.BeetsUnavailableError(f"connection to {sensitive} failed"),
             ):
            with app_module.app.test_request_context(
                "/api/clean/artist-folders/stamp-mbid", method="POST",
                json={"root": str(self.root), "dry_run": False},
            ):
                captured = {}

                def _capture_and_run(fn, label="", metadata=None):
                    log = []
                    try:
                        fn(log)
                        captured["raised"] = None
                    except Exception as ex:
                        captured["raised"] = ex
                    captured["log"] = log
                    return mock.MagicMock(job_id="job-fail")

                with mock.patch.object(app_module.jobs, "start_python", side_effect=_capture_and_run):
                    app_module.clean_artist_folders_stamp_mbid()
            self.assertIsNotNone(captured["raised"])
            joined_log = "\n".join(captured["log"])
            self.assertNotIn(sensitive, joined_log)
            self.assertNotIn("sig=abcdef123456", joined_log)
            # The raised exception itself (used only for the job's internal
            # failed-status bookkeeping, never echoed to the user as JSON)
            # is allowed to carry the real detail -- what matters is that no
            # HTTP response or job-visible log line does.


class ResilientApplySanitizedErrorTests(unittest.TestCase):
    """CodeQL follow-up, audited path #2: _apply_artist_folder_reconcile_resilient()'s
    own exception handling (definite rejection, lost-response, and
    transaction-poll-retry branches) must not leak raw exception text into
    job-visible logs or result "error" fields either."""

    def setUp(self):
        self.patchers = []
        self._patch(mock.patch.object(app_module, "BEETS_LONG_OPERATION_POLL_SECONDS", 0.01))
        self._patch(mock.patch.object(app_module, "BEETS_LONG_OPERATION_MAX_SECONDS", 0.05))

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def _patch(self, patcher):
        self.patchers.append(patcher)
        return patcher.start()

    def test_rejected_apply_does_not_expose_raw_exception_text(self):
        sensitive = "http://engine-internal.local:8338/artists/reconcile/apply?token=zzz"
        self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsBadRequestError(
                f"Beets API bad request via {sensitive}: operation not in Pending/Approved state",
                error_code="INVALID_STATE", status_code=400,
            ),
        ))
        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-sanitize-1", log)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "INVALID_STATE")
        self.assertEqual(result.get("status_code"), 400)
        self.assertNotIn(sensitive, result.get("error", ""))
        self.assertNotIn("token=zzz", result.get("error", ""))
        joined_log = "\n".join(log)
        self.assertNotIn(sensitive, joined_log)
        self.assertNotIn("token=zzz", joined_log)

    def test_lost_response_poll_does_not_expose_raw_exception_text(self):
        sensitive = "/var/lib/beets/private/musiclibrary.blb"
        self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError(f"Timed out reaching {sensitive}"),
        ))
        self._patch(mock.patch.object(
            app_module.composite_workflows, "get_transaction",
            return_value={"ok": True, "transaction": {"status": "Completed", "operation_id": "op-sanitize-2"}},
        ))
        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-sanitize-2", log)
        self.assertTrue(result.get("ok"), result)
        joined_log = "\n".join(log)
        self.assertNotIn(sensitive, joined_log)

    def test_transaction_poll_failure_does_not_expose_raw_exception_text(self):
        sensitive = "postgresql://user:swordfish@10.1.2.3/beets"
        self._patch(mock.patch.object(
            app_module.composite_workflows, "apply_artist_folder_reconcile",
            side_effect=app_module.BeetsUnavailableError("Timed out communicating with Beets Control Agent"),
        ))
        responses = iter([
            app_module.BeetsUnavailableError(f"engine still recovering, tried {sensitive}"),
            {"ok": True, "transaction": {"status": "Completed", "operation_id": "op-sanitize-3"}},
        ])

        def _get_transaction(op_id):
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return item

        self._patch(mock.patch.object(app_module.composite_workflows, "get_transaction", side_effect=_get_transaction))
        log = []
        result = app_module._apply_artist_folder_reconcile_resilient("op-sanitize-3", log)
        self.assertTrue(result.get("ok"), result)
        joined_log = "\n".join(log)
        self.assertNotIn(sensitive, joined_log)
        self.assertNotIn("swordfish", joined_log)


class SavedOperationLookupSanitizedErrorTests(unittest.TestCase):
    """CodeQL follow-up, audited path #3: _maintenance_artist_folder_merge_step()'s
    saved-operation transaction-status-lookup-failure branch (Clean All
    resume) must not leak raw exception text into its job-visible log line
    or "error" field, while still preserving still_running=True and the
    operation_id (the actual fail-closed behavior under test)."""

    def test_lookup_failure_does_not_expose_raw_exception_text(self):
        sensitive = "http://internal-agent.local:8338/transactions/op-abc?key=topsecret"
        with mock.patch.object(
            app_module.composite_workflows, "get_transaction",
            side_effect=app_module.BeetsUnavailableError(f"Timed out reaching {sensitive}"),
        ):
            log = []
            result = app_module._maintenance_artist_folder_merge_step(
                log, None, "/data/media/music", resume_operation_id="op-abc",
            )
        self.assertFalse(result.get("ok"))
        self.assertTrue(result.get("still_running"))
        self.assertEqual(result.get("operation_id"), "op-abc")
        self.assertNotIn(sensitive, result.get("error", ""))
        self.assertNotIn("topsecret", result.get("error", ""))
        joined_log = "\n".join(log)
        self.assertNotIn(sensitive, joined_log)
        self.assertNotIn("topsecret", joined_log)
        self.assertEqual(result.get("error"), "Beets Control Agent is unavailable.")


if __name__ == "__main__":
    unittest.main()
