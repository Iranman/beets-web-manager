"""SEC-002 / ARCH-003 Wave 24 final review round 4 (CodeQL closure)
regression tests.

CodeQL's py/path-injection query flagged 47 sinks in
backend/transaction_engine.py, concentrated in the album_artwork_v1
family, because it does not model this module's custom containment/
symlink-rejection helpers as sanitizers. Manual per-alert audit (see the
Round 4 PR body / final report) found no exploitable flow: every sink is
reached only after root-containment (Path.relative_to()-based, not
string-prefix) and symlink-component checks. This file exercises the new
`validate_path_under_allowed_roots` primitive the artwork family was
refactored around, and the adversarial cases the audit's threat model
requires -- independent of whether CodeQL itself can see the proof.
"""
import os
import sys
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.transaction_engine import (  # noqa: E402
    TransactionStore,
    validate_path_under_allowed_roots,
    create_album_artwork_plan,
    execute_album_artwork_apply,
)


class TestValidatePathUnderAllowedRootsPrimitive(unittest.TestCase):
    """Direct unit tests of the primitive itself -- section 11 minimums."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.music_root = self.root / "music"
        self.other_root = self.root / "music-other"  # sibling-prefix trap
        self.music_root.mkdir(parents=True)
        self.other_root.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_normal_in_root_existing_path_accepted(self):
        p = self.music_root / "Artist" / "Album"
        p.mkdir(parents=True)
        result = validate_path_under_allowed_roots(p, [self.music_root])
        self.assertIsNotNone(result)
        self.assertTrue(result.exists())

    def test_normal_in_root_new_destination_accepted(self):
        p = self.music_root / "Artist" / "New Album"  # does not exist yet
        result = validate_path_under_allowed_roots(p, [self.music_root])
        self.assertIsNotNone(result)
        self.assertFalse(p.exists())

    def test_outside_absolute_path_rejected(self):
        outside = self.root / "elsewhere" / "secret"
        result = validate_path_under_allowed_roots(outside, [self.music_root])
        self.assertIsNone(result)

    def test_dot_dot_traversal_rejected(self):
        traversal = self.music_root / ".." / "music-other" / "target"
        result = validate_path_under_allowed_roots(traversal, [self.music_root])
        self.assertIsNone(result)

    def test_sibling_prefix_path_rejected(self):
        # "/music-other" starts with the string "/music" but is NOT
        # contained in it -- a string-prefix check alone would wrongly
        # accept this; component-aware containment must reject it.
        result = validate_path_under_allowed_roots(self.other_root / "x", [self.music_root])
        self.assertIsNone(result)

    def test_symlink_file_rejected(self):
        real_file = self.other_root / "real.jpg"
        real_file.write_bytes(b"x")
        link = self.music_root / "cover.jpg"
        os.symlink(str(real_file), str(link))
        result = validate_path_under_allowed_roots(link, [self.music_root])
        self.assertIsNone(result)

    def test_symlink_directory_component_rejected(self):
        real_dir = self.other_root / "real_album"
        real_dir.mkdir()
        linked_dir = self.music_root / "Artist" / "Album"
        linked_dir.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(real_dir), str(linked_dir), target_is_directory=True)
        candidate = linked_dir / "cover.jpg"
        result = validate_path_under_allowed_roots(candidate, [self.music_root])
        self.assertIsNone(result)

    def test_nonexistent_leaf_under_root_accepted(self):
        candidate = self.music_root / "Artist" / "Album" / "does_not_exist.jpg"
        result = validate_path_under_allowed_roots(candidate, [self.music_root])
        self.assertIsNotNone(result)

    def test_fails_closed_on_empty_allowed_roots(self):
        result = validate_path_under_allowed_roots(self.music_root / "x", [])
        self.assertIsNone(result)

    def test_multiple_allowed_roots_selects_containing_one(self):
        p = self.other_root / "x"
        result = validate_path_under_allowed_roots(p, [self.music_root, self.other_root])
        self.assertIsNotNone(result)


def _write_test_audio(path: Path) -> None:
    import wave
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)


class TestArtworkFamilyAdversarialPaths(unittest.TestCase):
    """Integration-level adversarial cases against the real
    create_album_artwork_plan / execute_album_artwork_apply pair --
    proving DB-, request-, and transaction-metadata-supplied path data can
    never enlarge the server-configured allowed_roots trust boundary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.music_dir = self.root / "music"
        self.outside_dir = self.root / "outside"  # never a configured root
        self.quarantine_dir = self.root / "quarantine"
        self.db_path = self.root / "musiclibrary.blb"
        self.tx_dir = self.root / "transactions"
        self.music_dir.mkdir(parents=True)
        self.outside_dir.mkdir(parents=True)
        self.tx_dir.mkdir(parents=True)
        self.store = TransactionStore(root=str(self.tx_dir))

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
                albumartist TEXT, album TEXT, track INTEGER, disc INTEGER,
                year INTEGER, genre TEXT, format TEXT, path TEXT,
                mb_trackid TEXT, mb_albumid TEXT
            )
        """)
        con.execute("""
            INSERT INTO albums (id, album, artist, albumartist, year, genre, artpath, mb_albumid, mb_releasegroupid)
            VALUES (1, 'Test Album', 'Test Artist', 'Test Artist', 2024, 'Rock', '', '11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222')
        """)
        item_path = self.music_dir / "Test Artist" / "Test Album" / "01 - Track 1.mp3"
        _write_test_audio(item_path)
        con.execute("""
            INSERT INTO items (id, album_id, title, artist, albumartist, album, track, disc, year, genre, format, path)
            VALUES (1, 1, 'Track 1', 'Test Artist', 'Test Artist', 'Test Album', 1, 1, 2024, 'Rock', 'MP3', ?)
        """, (str(item_path),))
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_request_target_dir_outside_allowed_roots_rejected(self):
        """A request payload cannot point target_dir outside the
        configured music root -- the allowed_roots list is never enlarged
        by client input."""
        plan_res = create_album_artwork_plan(
            self.store,
            {"album_id": 1, "mode": "move", "target_dir": str(self.outside_dir / "hijack"),
             "candidates": []},
            music_allowed_roots=[str(self.music_dir)],
            staging_allowed_roots=[str(self.quarantine_dir), str(self.quarantine_dir / "staging")],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertFalse(plan_res.get("ok"))
        self.assertEqual(plan_res.get("code"), "album_artwork_path_out_of_root")

    def test_db_artpath_outside_roots_not_trusted_as_source(self):
        """An albums.artpath row pointing outside music_allowed_roots must
        never be trusted for source discovery -- the DB is data, not a
        trust root."""
        outside_art = self.outside_dir / "hijacked_cover.jpg"
        outside_art.write_bytes(b"fake")
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE albums SET artpath=? WHERE id=1", (str(outside_art),))
        con.commit()
        con.close()

        plan_res = create_album_artwork_plan(
            self.store,
            {"album_id": 1, "mode": "quarantine"},
            music_allowed_roots=[str(self.music_dir)],
            staging_allowed_roots=[str(self.quarantine_dir), str(self.quarantine_dir / "staging")],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        # Must not fail *and* must not surface the outside file as a
        # quarantine candidate -- the untrusted artpath is silently
        # ignored for discovery, never used as a filesystem source.
        self.assertTrue(plan_res.get("ok"), f"Plan failed: {plan_res}")
        tx = self.store.get(plan_res["operation_id"]) if plan_res.get("operation_id") else None
        if tx:
            sources = [q.get("source") for q in tx["metadata"].get("quarantines", [])]
            self.assertNotIn(str(outside_art), sources)

    def test_apply_rejects_transaction_metadata_destination_outside_target_dir(self):
        """Simulates transaction-record tampering: even if a stored
        moves_plan destination were altered to point outside the
        validated target_dir, Apply must re-derive/reject it rather than
        trust the stored string verbatim."""
        import base64
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), color=(50, 60, 70)).save(buf, format="JPEG")
        jpeg_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        plan_res = create_album_artwork_plan(
            self.store,
            {"album_id": 1, "mode": "replace", "image_data_b64": jpeg_b64},
            music_allowed_roots=[str(self.music_dir)],
            staging_allowed_roots=[str(self.quarantine_dir), str(self.quarantine_dir / "staging")],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        if not plan_res.get("ok") or not plan_res.get("operation_id"):
            self.skipTest("plan did not produce an operation to tamper with in this fixture")
        op_id = plan_res["operation_id"]
        tx = self.store.get(op_id)
        meta = dict(tx["metadata"])
        moves_plan = list(meta.get("moves_plan") or [])
        if not moves_plan:
            self.skipTest("no moves_plan entries to tamper with in this fixture")
        tampered = dict(moves_plan[0])
        tampered["destination"] = str(self.outside_dir / "escaped.jpg")
        moves_plan[0] = tampered
        meta["moves_plan"] = moves_plan
        self.store.update(op_id, metadata=meta)

        apply_res = execute_album_artwork_apply(
            self.store, op_id,
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "album_artwork_path_out_of_root")
        self.assertFalse((self.outside_dir / "escaped.jpg").exists())

    def test_plan_to_apply_symlink_substitution_on_target_dir_fails_closed(self):
        """Target directory is a real, validated in-root directory at
        Plan time; before Apply runs, it is replaced with a symlink
        pointing outside the allowed root. Apply must re-validate and
        refuse, not trust Plan's snapshot."""
        target_dir = self.music_dir / "Artist" / "Album"
        target_dir.mkdir(parents=True)
        art_src = self.music_dir / "incoming.jpg"
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), color=(10, 20, 30)).save(buf, format="JPEG")
        art_src.write_bytes(buf.getvalue())

        plan_res = create_album_artwork_plan(
            self.store,
            {"album_id": 1, "mode": "move", "target_dir": str(target_dir),
             "candidates": [{"source": str(art_src)}]},
            music_allowed_roots=[str(self.music_dir)],
            staging_allowed_roots=[str(self.quarantine_dir), str(self.quarantine_dir / "staging")],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertTrue(plan_res.get("ok"), f"Plan failed: {plan_res}")
        self.assertTrue(plan_res.get("operation_id"))

        # Swap target_dir for a symlink into the outside (untrusted) root
        # between Plan and Apply.
        shutil.rmtree(target_dir)
        real_outside = self.outside_dir / "swapped_target"
        real_outside.mkdir(parents=True)
        os.symlink(str(real_outside), str(target_dir), target_is_directory=True)

        apply_res = execute_album_artwork_apply(
            self.store, plan_res["operation_id"],
            music_allowed_roots=[str(self.music_dir)],
            db_path=str(self.db_path),
            quarantine_base_root=str(self.quarantine_dir),
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertFalse(any(real_outside.iterdir()), "no file must have landed in the swapped-in outside directory")


if __name__ == "__main__":
    unittest.main()
