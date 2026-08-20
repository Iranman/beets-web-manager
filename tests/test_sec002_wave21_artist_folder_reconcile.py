"""SEC-002 / ARCH-003 Wave 21: Artist Folder Merge & MBID Stamping Controlled Mutation Boundary.

Tests for artist_folder_reconcile_v1 mutation family in transaction_engine.py,
beets_control_agent.py, beets_client.py, and app.py.
"""

import ast
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
    path BLOB,
    artpath BLOB
);

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
        p = self.music_root / rel_folder
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO albums (id, album, albumartist, albumartists, mb_albumartistid, mb_albumartistids, path) "
            "VALUES (?,?,?,?,?,?,?)",
            (album_id, name, artist, artist, mbid, mbid, str(p).encode("utf-8"))
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
        src = self._create_artist_folder("Bob  Marley")
        dst = self._create_artist_folder("Bob Marley")
        self._create_track_file("Bob  Marley/t.mp3")

        res = self._plan({
            "root": str(self.music_root),
            "mode": "scan_merge",
            "candidates": [{"source_path": str(src), "target_path": str(dst), "fingerprint_confirmed": True}],
        })
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("candidate_count"), 1)

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
            "candidates": [{"source_path": str(src), "target_path": str(dst), "fingerprint_confirmed": True}],
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
            "candidates": [{"source_path": str(src), "target_path": str(dst), "fingerprint_confirmed": True}],
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
            "candidates": [{"source_path": str(src), "target_path": str(dst), "fingerprint_confirmed": True}],
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
            "candidates": [{"source_path": str(src), "target_path": str(dst), "fingerprint_confirmed": True}],
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
            "candidates": [{"source_path": str(src), "target_path": str(dst), "fingerprint_confirmed": True}],
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
            "candidates": [{"source_path": str(src), "target_path": str(dst), "fingerprint_confirmed": True}],
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
            "candidates": [{"source_path": str(src), "target_path": str(dst), "fingerprint_confirmed": True}],
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
            "candidates": [{"source_path": str(src), "target_path": str(dst), "fingerprint_confirmed": True}],
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

        self._plan_patch = mock.patch.object(app_module.beets_client, "plan_artist_folder_reconcile", side_effect=mock_plan)
        self._plan_patch.start()
        self.addCleanup(self._plan_patch.stop)
        self._apply_patch = mock.patch.object(app_module.beets_client, "apply_artist_folder_reconcile", side_effect=mock_apply)
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


if __name__ == "__main__":
    unittest.main()
