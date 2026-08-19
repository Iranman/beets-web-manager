"""
Wave 17 track replacement controlled mutation boundary tests (SEC-002 / ARCH-003).

This is a from-scratch rewrite matching the from-scratch rewrite of
backend/transaction_engine.py's track-replacement functions -- the
original implementation this replaces had a signature shape
(allowed_roots + a client-suppliable quarantine_root) this review found
to be unsafe and removed. See the final-review commit message /
docs/operations/wave17_track_replacement_design.md for the corrected
architecture: role-specific roots (original = music root only,
candidate = staging/acquisition roots only), a server-derived quarantine
root, item-id-bound original resolution, and required (not optional)
matching-authority evidence.
"""

import ast
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import app as flask_app
from backend.beets_client import BeetsError, BeetsUnavailableError
from backend.transaction_engine import (
    TransactionStore,
    create_track_replacement_plan,
    execute_track_replacement_apply,
    rollback_track_replacement,
)


ITEMS_SCHEMA = (
    "CREATE TABLE items ("
    "id INTEGER PRIMARY KEY, path TEXT, title TEXT, artist TEXT, album TEXT, "
    "track INTEGER, disc INTEGER, mb_trackid TEXT, mb_albumid TEXT, "
    "mb_releasegroupid TEXT, size INTEGER, mtime REAL)"
)


class TrackReplacementFixtureBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)

        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir(parents=True, exist_ok=True)
        self.tx_store = TransactionStore(root=str(store_dir))

        self.music_root = self.tmp_path / "music"
        self.music_root.mkdir(parents=True, exist_ok=True)
        self.staging_root = self.tmp_path / "downloads"
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.other_staging_root = self.tmp_path / "torrents"
        self.other_staging_root.mkdir(parents=True, exist_ok=True)
        self.quarantine_root = self.tmp_path / "quarantine"
        self.quarantine_root.mkdir(parents=True, exist_ok=True)
        self.outside_root = self.tmp_path / "outside"
        self.outside_root.mkdir(parents=True, exist_ok=True)

        self.original_allowed_roots = [str(self.music_root)]
        self.candidate_allowed_roots = [str(self.staging_root), str(self.other_staging_root)]

        # create_track_replacement_plan derives its quarantine root from
        # this env var directly (never from client input -- see
        # test_client_quarantine_root_is_ignored) rather than taking it as
        # a parameter, exactly matching how beets_control_agent.py wires
        # both Plan and Apply from the SAME env var in production. Tests
        # must keep this in sync with quarantine_allowed_root passed to
        # Apply/Rollback, or Plan and Apply would disagree about where the
        # quarantine root actually is.
        self._env_patch = mock.patch.dict(os.environ, {"REPLACEMENT_QUARANTINE_DIR": str(self.quarantine_root)})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

        self.db_path = self.tmp_path / "musiclibrary.blb"
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(ITEMS_SCHEMA)
            con.commit()
        finally:
            con.close()

    def _insert_item(self, path: Path, *, mb_trackid: str = "", mb_releasegroupid: str = "",
                      title: str = "Test Track", artist: str = "Test Artist") -> int:
        st = path.stat()
        con = sqlite3.connect(self.db_path)
        try:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO items (path, title, artist, album, track, disc, mb_trackid, mb_albumid, "
                "mb_releasegroupid, size, mtime) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(path), title, artist, "Test Album", 1, 1, mb_trackid, "", mb_releasegroupid,
                 st.st_size, st.st_mtime),
            )
            item_id = cur.lastrowid
            con.commit()
        finally:
            con.close()
        return item_id

    def _get_item_row(self, item_id: int):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            return con.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        finally:
            con.close()

    RECORDING_ID = "aaaaaaaa-1111-1111-1111-111111111111"
    RGID = "bbbbbbbb-2222-2222-2222-222222222222"

    def _valid_contract(self, *, recording_id: str = None, rgid: str = None) -> dict:
        rid = self.RECORDING_ID if recording_id is None else recording_id
        rg = self.RGID if rgid is None else rgid
        return {
            "identity_source": "acoustid_fingerprint",
            "original_recording_id": rid,
            "replacement_recording_id": rid,
            "original_release_group_id": rg,
            "replacement_release_group_id": rg,
            "decision_reason": "Replacement AcoustID fingerprint matches original recording.",
        }

    def _plan(self, _original_path_ref: Path, candidate_path: Path, item_id: int, *, contract=None, **payload_overrides):
        # _original_path_ref is not sent to the engine by default -- the
        # item id alone is authoritative for the original's location (see
        # test_cross_item_path_mismatch_rejected). It exists purely so
        # call sites read naturally as _plan(original, candidate, item_id).
        payload = {
            "original_item_id": item_id,
            "replacement_path": str(candidate_path),
            "matching_contract": self._valid_contract() if contract is None else contract,
        }
        payload.update(payload_overrides)
        return create_track_replacement_plan(
            self.tx_store, payload,
            original_allowed_roots=self.original_allowed_roots,
            candidate_allowed_roots=self.candidate_allowed_roots,
            db_path=str(self.db_path),
        )

    def _apply(self, op_id: str):
        return execute_track_replacement_apply(
            self.tx_store, op_id, db_path=str(self.db_path),
            original_allowed_roots=self.original_allowed_roots,
            candidate_allowed_roots=self.candidate_allowed_roots,
            quarantine_allowed_root=str(self.quarantine_root),
        )

    def _rollback(self, op_id: str):
        return rollback_track_replacement(
            self.tx_store, op_id, db_path=str(self.db_path),
            quarantine_allowed_root=str(self.quarantine_root),
        )

    def _make_pair(self, name="track1", orig_content=b"ORIGINAL_AUDIO", cand_content=b"REPLACEMENT_AUDIO"):
        orig = self.music_root / f"{name}.mp3"
        orig.write_bytes(orig_content)
        cand = self.staging_root / f"{name}_hq.flac"
        cand.write_bytes(cand_content)
        item_id = self._insert_item(orig, mb_trackid=self.RECORDING_ID, mb_releasegroupid=self.RGID)
        return orig, cand, item_id


class PlanValidationTests(TrackReplacementFixtureBase):
    def test_plan_is_non_mutating(self):
        orig, cand, item_id = self._make_pair()
        res = self._plan(orig, cand, item_id)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertTrue(orig.exists())
        self.assertTrue(cand.exists())
        row = self._get_item_row(item_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["path"], str(orig))

    def test_requires_item_id(self):
        _orig, cand, _item_id = self._make_pair()
        res = self._plan(Path("unused"), cand, 0)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "item_id_required")

    def test_item_not_found(self):
        _orig, cand, _item_id = self._make_pair()
        res = self._plan(Path("unused"), cand, 999999)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "item_not_found")

    def test_cross_item_path_mismatch_rejected(self):
        """A client supplying item A's id together with item B's path must
        be rejected -- the engine derives the path from the item id, and
        any client-supplied original_path is only expected-state evidence
        that must match exactly."""
        orig_a, cand, item_id_a = self._make_pair("track_a")
        orig_b = self.music_root / "track_b.mp3"
        orig_b.write_bytes(b"OTHER TRACK")
        self._insert_item(orig_b)

        res = self._plan(orig_a, cand, item_id_a, original_path=str(orig_b))
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "item_path_mismatch")
        # Neither file should have been touched by a mere Plan attempt.
        self.assertTrue(orig_a.exists())
        self.assertTrue(orig_b.exists())

    def test_candidate_requires_recording_id_evidence(self):
        orig, cand, item_id = self._make_pair()
        res = self._plan(orig, cand, item_id, contract={"identity_source": "acoustid_fingerprint"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "recording_id_required")

    def test_ai_only_evidence_rejected(self):
        orig, cand, item_id = self._make_pair()
        contract = self._valid_contract()
        contract["identity_source"] = "ai_suggestion"
        res = self._plan(orig, cand, item_id, contract=contract)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "ai_only_evidence_rejected")

    def test_missing_identity_source_rejected(self):
        orig, cand, item_id = self._make_pair()
        contract = self._valid_contract()
        contract["identity_source"] = ""
        res = self._plan(orig, cand, item_id, contract=contract)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "ai_only_evidence_rejected")

    def test_recording_id_mismatch_rejected(self):
        orig, cand, item_id = self._make_pair()
        contract = self._valid_contract(recording_id="cccccccc-3333-3333-3333-333333333333")
        # original_recording_id still matches the DB, but replacement doesn't match original.
        contract["original_recording_id"] = self.RECORDING_ID
        res = self._plan(orig, cand, item_id, contract=contract)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "recording_id_mismatch")

    def test_original_recording_id_claim_mismatch_rejected(self):
        """A matching_contract whose claimed original_recording_id doesn't
        match what the DB actually has for this item is stale/wrong
        evidence and must be rejected, not silently accepted."""
        orig, cand, item_id = self._make_pair()
        contract = self._valid_contract()
        contract["original_recording_id"] = "dddddddd-4444-4444-4444-444444444444"
        res = self._plan(orig, cand, item_id, contract=contract)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "original_recording_id_mismatch")

    def test_release_group_mismatch_rejected(self):
        orig, cand, item_id = self._make_pair()
        contract = self._valid_contract()
        contract["replacement_release_group_id"] = "eeeeeeee-5555-5555-5555-555555555555"
        res = self._plan(orig, cand, item_id, contract=contract)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "release_group_mismatch")

    def test_original_outside_music_root_rejected(self):
        orig = self.outside_root / "sneaky.mp3"
        orig.write_bytes(b"X")
        item_id = self._insert_item(orig)
        cand = self.staging_root / "cand.flac"
        cand.write_bytes(b"Y")
        res = self._plan(orig, cand, item_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "original_outside_roots")

    def test_candidate_outside_staging_roots_rejected(self):
        """A candidate under the MUSIC root (not a staging root) must be
        rejected even though the music root is a generally-trusted root --
        role separation, not "any trusted root"."""
        orig, _cand, item_id = self._make_pair()
        rogue_candidate = self.music_root / "other_track.flac"
        rogue_candidate.write_bytes(b"SNEAKY")
        res = self._plan(orig, rogue_candidate, item_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "candidate_outside_roots")

    def test_client_quarantine_root_is_ignored(self):
        orig, cand, item_id = self._make_pair()
        attacker_quarantine = str(self.outside_root / "attacker-quarantine")
        res = self._plan(orig, cand, item_id, quarantine_root=attacker_quarantine)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertNotIn(attacker_quarantine, res["quarantine_target"])
        self.assertTrue(res["quarantine_target"].startswith(str(self.quarantine_root) + os.sep) or
                         res["quarantine_target"].startswith(str(self.quarantine_root) + "/"))

    def test_same_original_and_candidate_rejected(self):
        """A candidate that resolves to the exact same underlying file as
        the original (e.g. via a hardlink placed under an approved staging
        root) must be rejected, even though it passes root-containment
        for both roles independently."""
        orig, _cand, item_id = self._make_pair()
        hardlinked_candidate = self.staging_root / "same_as_original.mp3"
        try:
            os.link(str(orig), str(hardlinked_candidate))
        except OSError:
            self.skipTest("Hardlinks not supported on this platform/filesystem")
        res = self._plan(orig, hardlinked_candidate, item_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "same_file")

    def test_original_leaf_symlink_rejected(self):
        target = self.outside_root / "secret.mp3"
        target.write_bytes(b"SECRET")
        link = self.music_root / "linked.mp3"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported on this platform/privilege level")
        item_id = self._insert_item(link)
        cand = self.staging_root / "cand.flac"
        cand.write_bytes(b"C")
        res = self._plan(link, cand, item_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "symlink_rejected")

    def test_original_parent_symlink_rejected(self):
        real_dir = self.outside_root / "real_album_dir"
        real_dir.mkdir()
        linked_dir = self.music_root / "linked_album_dir"
        try:
            linked_dir.symlink_to(real_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported on this platform/privilege level")
        orig = linked_dir / "track.mp3"
        orig.write_bytes(b"X")
        item_id = self._insert_item(orig)
        cand = self.staging_root / "cand.flac"
        cand.write_bytes(b"C")
        res = self._plan(orig, cand, item_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "symlink_rejected")

    def test_candidate_leaf_symlink_rejected(self):
        target = self.outside_root / "secret.flac"
        target.write_bytes(b"SECRET")
        link = self.staging_root / "linked.flac"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported on this platform/privilege level")
        orig, _cand, item_id = self._make_pair()
        res = self._plan(orig, link, item_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "symlink_rejected")

    def test_candidate_parent_symlink_rejected(self):
        real_dir = self.outside_root / "real_staging_dir"
        real_dir.mkdir()
        linked_dir = self.staging_root / "linked_staging_dir"
        try:
            linked_dir.symlink_to(real_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported on this platform/privilege level")
        cand = linked_dir / "cand.flac"
        cand.write_bytes(b"C")
        orig, _c, item_id = self._make_pair()
        res = self._plan(orig, cand, item_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "symlink_rejected")

    def test_db_not_found(self):
        orig, cand, _item_id = self._make_pair()
        res = create_track_replacement_plan(
            self.tx_store, {"original_item_id": 1, "replacement_path": str(cand), "matching_contract": self._valid_contract()},
            original_allowed_roots=self.original_allowed_roots,
            candidate_allowed_roots=self.candidate_allowed_roots,
            db_path=str(self.tmp_path / "does-not-exist.blb"),
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "db_not_found")

    def test_quarantine_path_uses_the_real_transaction_id(self):
        """Regression test for the exact Wave 17 bug this review found: a
        separately-invented id used to build the quarantine path while
        store.create() generates a DIFFERENT durable id."""
        orig, cand, item_id = self._make_pair()
        res = self._plan(orig, cand, item_id)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertIn(res["operation_id"], res["quarantine_target"])


class ApplyTests(TrackReplacementFixtureBase):
    def test_full_lifecycle_success(self):
        orig, cand, item_id = self._make_pair()
        plan_res = self._plan(orig, cand, item_id)
        self.assertTrue(plan_res["ok"], plan_res.get("error"))
        op_id = plan_res["operation_id"]

        apply_res = self._apply(op_id)
        self.assertTrue(apply_res["ok"], apply_res.get("error"))
        self.assertEqual(apply_res["status"], "Completed")

        # Original is quarantined (moved away, not on-disk at its old path).
        self.assertFalse(orig.exists())
        quarantined = Path(apply_res["quarantined_to"])
        self.assertTrue(quarantined.exists())
        self.assertEqual(quarantined.read_bytes(), b"ORIGINAL_AUDIO")

        # Candidate is left exactly where it was -- Wave 17's original
        # implementation moved it onto the original's path, which this
        # review determined was a real behavioral regression from the
        # pre-Wave-17 semantics (see task section 9 / commit message).
        self.assertTrue(cand.exists())
        self.assertEqual(cand.read_bytes(), b"REPLACEMENT_AUDIO")

        # DB row for the original item is gone.
        self.assertIsNone(self._get_item_row(item_id))

    def test_original_inode_changed_toctou(self):
        orig, cand, item_id = self._make_pair()
        plan_res = self._plan(orig, cand, item_id)
        op_id = plan_res["operation_id"]
        orig.unlink()
        orig.write_bytes(b"ORIGINAL_AUDIO")  # same content/size, new inode
        res = self._apply(op_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "original_toctou_failed")
        self.assertFalse(res["mutated"])

    def test_original_size_changed_toctou(self):
        orig, cand, item_id = self._make_pair()
        plan_res = self._plan(orig, cand, item_id)
        op_id = plan_res["operation_id"]
        orig.write_bytes(b"ORIGINAL_AUDIO_LONGER_NOW")
        res = self._apply(op_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "original_toctou_failed")

    def test_original_mtime_changed_toctou(self):
        orig, cand, item_id = self._make_pair()
        plan_res = self._plan(orig, cand, item_id)
        op_id = plan_res["operation_id"]
        new_time = time.time() + 120
        os.utime(orig, (new_time, new_time))
        res = self._apply(op_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "original_toctou_failed")

    def test_candidate_inode_changed_toctou(self):
        orig, cand, item_id = self._make_pair()
        plan_res = self._plan(orig, cand, item_id)
        op_id = plan_res["operation_id"]
        cand.unlink()
        cand.write_bytes(b"REPLACEMENT_AUDIO")
        res = self._apply(op_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "candidate_toctou_failed")

    def test_candidate_size_changed_toctou(self):
        orig, cand, item_id = self._make_pair()
        plan_res = self._plan(orig, cand, item_id)
        op_id = plan_res["operation_id"]
        cand.write_bytes(b"REPLACEMENT_AUDIO_LONGER")
        res = self._apply(op_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "candidate_toctou_failed")

    def test_candidate_mtime_changed_toctou(self):
        orig, cand, item_id = self._make_pair()
        plan_res = self._plan(orig, cand, item_id)
        op_id = plan_res["operation_id"]
        new_time = time.time() + 120
        os.utime(cand, (new_time, new_time))
        res = self._apply(op_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "candidate_toctou_failed")

    def test_quarantine_destination_already_exists_refused(self):
        orig, cand, item_id = self._make_pair()
        plan_res = self._plan(orig, cand, item_id)
        op_id = plan_res["operation_id"]
        tx = self.tx_store.get(op_id)
        q_target = Path(tx["metadata"]["quarantine_target"])
        q_target.parent.mkdir(parents=True, exist_ok=True)
        q_target.write_bytes(b"already here")
        res = self._apply(op_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "quarantine_destination_exists")
        self.assertTrue(orig.exists())  # nothing touched

    def test_apply_rejects_wrong_transaction_family(self):
        store_dir = self.tmp_path / "unrelated_tx"
        tx = self.tx_store.create(
            operation_type="Library Cleanup", status="Preview", summary="x",
            metadata={"mutation_family": "album_cleanup_v1"},
        )
        res = self._apply(tx["id"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "wrong_mutation_family")

    def test_apply_malformed_transaction_id_fails_safely(self):
        res = self._apply("../../etc/passwd")
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "not_found")
        self.assertFalse(res["mutated"])

    def test_apply_unknown_transaction_id_fails_safely(self):
        res = self._apply("txn_1700000000_deadbeef0000")
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "not_found")

    def test_double_apply_is_idempotent(self):
        orig, cand, item_id = self._make_pair()
        plan_res = self._plan(orig, cand, item_id)
        op_id = plan_res["operation_id"]
        first = self._apply(op_id)
        self.assertTrue(first["ok"])
        second = self._apply(op_id)
        self.assertTrue(second["ok"])
        self.assertTrue(second.get("already_completed"))
        self.assertEqual(second["status"], "Completed")

    def test_db_row_vanished_between_plan_and_apply(self):
        orig, cand, item_id = self._make_pair()
        plan_res = self._plan(orig, cand, item_id)
        op_id = plan_res["operation_id"]
        con = sqlite3.connect(self.db_path)
        con.execute("DELETE FROM items WHERE id = ?", (item_id,))
        con.commit()
        con.close()
        res = self._apply(op_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "item_vanished")
        # Original was already (correctly) quarantined by this point --
        # that is real, reported mutation, must not be hidden.
        self.assertTrue(res["mutated"])

    def test_db_exception_after_quarantine_reports_mutated_and_fails_closed(self):
        """If the original is already quarantined but the DB step then
        fails, this must never report Completed -- exactly the class of
        bug already fixed once in Wave 15 and reintroduced here."""
        orig, cand, item_id = self._make_pair()
        plan_res = self._plan(orig, cand, item_id)
        op_id = plan_res["operation_id"]

        # Point Apply at a DB path that exists but isn't a valid SQLite DB,
        # simulating a DB failure occurring strictly after the file
        # mutation already succeeded.
        bad_db = self.tmp_path / "not_a_database.blb"
        bad_db.write_text("not a database")
        res = execute_track_replacement_apply(
            self.tx_store, op_id, db_path=str(bad_db),
            original_allowed_roots=self.original_allowed_roots,
            candidate_allowed_roots=self.candidate_allowed_roots,
            quarantine_allowed_root=str(self.quarantine_root),
        )
        self.assertFalse(res["ok"])
        self.assertTrue(res["mutated"])
        self.assertFalse(orig.exists())  # file mutation genuinely happened
        tx = self.tx_store.get(op_id)
        self.assertNotEqual(tx["status"], "Completed")

    def test_crash_recovery_resumes_without_re_quarantining(self):
        """Simulates a crash: the original was already durably quarantined
        (step persisted) but the transaction was never marked Completed.
        A retry must not attempt to quarantine a file that no longer
        exists at its original path -- it should recognize the step as
        already done and proceed to the DB step."""
        orig, cand, item_id = self._make_pair()
        plan_res = self._plan(orig, cand, item_id)
        op_id = plan_res["operation_id"]
        tx = self.tx_store.get(op_id)
        meta = tx["metadata"]
        q_target = Path(meta["quarantine_target"])
        q_target.parent.mkdir(parents=True, exist_ok=True)
        orig.rename(q_target)  # simulate the crashed attempt's own successful quarantine move
        steps = meta["apply_steps"]
        for s in steps:
            if s["step"] == "original_quarantined":
                s["status"] = "completed"
        self.tx_store.update(op_id, metadata={**meta, "apply_steps": steps})

        res = self._apply(op_id)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["status"], "Completed")
        self.assertIsNone(self._get_item_row(item_id))

    def test_quarantine_allowed_root_not_configured_fails_closed(self):
        orig, cand, item_id = self._make_pair()
        plan_res = self._plan(orig, cand, item_id)
        op_id = plan_res["operation_id"]
        res = execute_track_replacement_apply(
            self.tx_store, op_id, db_path=str(self.db_path),
            original_allowed_roots=self.original_allowed_roots,
            candidate_allowed_roots=self.candidate_allowed_roots,
            quarantine_allowed_root=None,
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "quarantine_root_missing")
        self.assertTrue(orig.exists())


class ConcurrencyTests(TrackReplacementFixtureBase):
    def test_same_item_two_transactions_serialize(self):
        """Two SEPARATE Plans for the SAME item, both applied concurrently,
        must be serialized by the item-level lock, not just the
        operation-level lock (which only protects one operation_id against
        itself)."""
        orig, cand1, item_id = self._make_pair("shared")
        cand2 = self.staging_root / "shared_alt.flac"
        cand2.write_bytes(b"ALT_REPLACEMENT_AUDIO")

        plan1 = self._plan(orig, cand1, item_id)
        self.assertTrue(plan1["ok"], plan1.get("error"))
        # Re-fetch the original's current stat for a second, independently
        # valid plan against the same still-untouched original.
        plan2 = self._plan(orig, cand2, item_id)
        self.assertTrue(plan2["ok"], plan2.get("error"))

        results = {}

        def run(name, op_id):
            results[name] = self._apply(op_id)

        t1 = threading.Thread(target=run, args=("A", plan1["operation_id"]))
        t2 = threading.Thread(target=run, args=("B", plan2["operation_id"]))
        t1.start()
        t1.join(timeout=10)
        t2.start()
        t2.join(timeout=10)

        # Exactly one of the two must have succeeded (the first to
        # actually run its TOCTOU check against the still-present
        # original); the other must fail closed once the original is gone
        # -- never both silently "succeeding" against the same file.
        oks = [r["ok"] for r in results.values()]
        self.assertEqual(oks.count(True), 1, results)
        self.assertEqual(oks.count(False), 1, results)

    def test_different_items_apply_concurrently_without_blocking(self):
        orig1, cand1, item_id1 = self._make_pair("item1")
        orig2, cand2, item_id2 = self._make_pair("item2")
        plan1 = self._plan(orig1, cand1, item_id1)
        plan2 = self._plan(orig2, cand2, item_id2)
        self.assertTrue(plan1["ok"])
        self.assertTrue(plan2["ok"])

        results = {}

        def run(name, op_id):
            results[name] = self._apply(op_id)

        t1 = threading.Thread(target=run, args=("A", plan1["operation_id"]))
        t2 = threading.Thread(target=run, args=("B", plan2["operation_id"]))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertTrue(results["A"]["ok"], results["A"])
        self.assertTrue(results["B"]["ok"], results["B"])


class RollbackTests(TrackReplacementFixtureBase):
    def _apply_ok(self, orig, cand, item_id):
        plan_res = self._plan(orig, cand, item_id)
        self.assertTrue(plan_res["ok"], plan_res.get("error"))
        op_id = plan_res["operation_id"]
        apply_res = self._apply(op_id)
        self.assertTrue(apply_res["ok"], apply_res.get("error"))
        return op_id

    def test_full_rollback_restores_file_and_db_row(self):
        orig, cand, item_id = self._make_pair()
        op_id = self._apply_ok(orig, cand, item_id)

        res = self._rollback(op_id)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["status"], "Rolled Back")
        self.assertTrue(orig.exists())
        self.assertEqual(orig.read_bytes(), b"ORIGINAL_AUDIO")
        row = self._get_item_row(item_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["mb_trackid"], self.RECORDING_ID)
        # Candidate was never touched by Apply and is not touched by
        # rollback either.
        self.assertTrue(cand.exists())

    def test_rollback_wrong_family_rejected(self):
        tx = self.tx_store.create(
            operation_type="Library Cleanup", status="Completed", summary="x",
            metadata={"mutation_family": "album_cleanup_v1"},
        )
        res = self._rollback(tx["id"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "wrong_mutation_family")

    def test_rollback_malformed_transaction_id(self):
        res = self._rollback("../../etc/passwd")
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "not_found")

    def test_rollback_requires_completed_status(self):
        orig, cand, item_id = self._make_pair()
        plan_res = self._plan(orig, cand, item_id)
        res = self._rollback(plan_res["operation_id"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "wrong_status")

    def test_repeated_rollback_second_attempt_refused(self):
        orig, cand, item_id = self._make_pair()
        op_id = self._apply_ok(orig, cand, item_id)
        first = self._rollback(op_id)
        self.assertTrue(first["ok"])
        second = self._rollback(op_id)
        self.assertFalse(second["ok"])

    def test_rollback_missing_quarantine_backup(self):
        orig, cand, item_id = self._make_pair()
        op_id = self._apply_ok(orig, cand, item_id)
        tx = self.tx_store.get(op_id)
        Path(tx["metadata"]["quarantine_target"]).unlink()
        res = self._rollback(op_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "quarantine_missing")

    def test_rollback_refuses_to_overwrite_occupied_original_path(self):
        orig, cand, item_id = self._make_pair()
        op_id = self._apply_ok(orig, cand, item_id)
        orig.write_bytes(b"SOMETHING ELSE NOW LIVES HERE")
        res = self._rollback(op_id)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "original_path_occupied")
        # The quarantined original must not have been destroyed just
        # because the destination was occupied.
        tx = self.tx_store.get(op_id)
        self.assertTrue(Path(tx["metadata"]["quarantine_target"]).exists())


class AstStructuralTests(unittest.TestCase):
    """Structural (not just two-call-name) regression guard, matching the
    approach established in Wave 16 review -- broad enough to catch
    mutation hidden behind a local helper function, not just a bare
    unlink()/shutil.move() call in the target function's own body."""

    TARGET_FUNCTIONS = {
        "_music_format_remove_original_after_replacement",
        "item_replacement_plan",
        "item_replacement_apply",
    }
    FORBIDDEN_CALLS = {
        "unlink", "remove", "rmtree", "rmdir", "move", "copy", "copyfile",
        "copystat", "copytree", "rename", "replace", "mkdir", "makedirs",
        "write_text", "write_bytes", "execute", "executemany",
        "executescript", "commit", "connect", "Popen", "run", "call",
        "check_call", "check_output", "system",
    }
    ALLOWED_LOCAL_CALLS = {
        "jsonify", "int", "str", "bool", "float", "_s", "len", "getattr",
        "_music_format_replacement_matching_contract",
        "Path",
        # Reviewed: read-only AcoustID fingerprint lookups (no filesystem
        # mutation, no DB write) -- see their own definitions/docstrings.
        # Reused deliberately rather than building a second fingerprint
        # stack (SEC-002 Wave 17 final review, task section 5).
        "_acoustid_fingerprint_match", "_acoustid_fingerprint_ids",
        # Reviewed: read-only path validation (containment + symlink-walk
        # + existence check, no mutation) -- the same hardened, already-
        # tested helper SEC-002 Waves 6/7/8 established for Import Review
        # source paths, reused here instead of a second ad hoc check
        # (CodeQL py/path-injection fix, final review).
        "_resolve_import_review_source_path",
    }

    def setUp(self):
        app_path = flask_app.__file__
        with open(app_path, "r", encoding="utf-8") as f:
            self.source = f.read()
        self.tree = ast.parse(self.source, filename=app_path)

    def test_no_direct_mutation_calls(self):
        found = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.TARGET_FUNCTIONS:
                found.add(node.name)
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        func_name = ""
                        if isinstance(child.func, ast.Name):
                            func_name = child.func.id
                        elif isinstance(child.func, ast.Attribute):
                            func_name = child.func.attr
                        self.assertNotIn(
                            func_name, self.FORBIDDEN_CALLS,
                            f"Function {node.name} contains a direct mutation-shaped call: {func_name}",
                        )
        self.assertEqual(found, self.TARGET_FUNCTIONS, f"Missing function definitions: {self.TARGET_FUNCTIONS - found}")

    def test_only_calls_known_local_helpers(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.TARGET_FUNCTIONS:
                for child in ast.walk(node):
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                        self.assertIn(
                            child.func.id, self.ALLOWED_LOCAL_CALLS,
                            f"Function {node.name} calls local helper {child.func.id}() which is not in "
                            "the reviewed allowlist.",
                        )

    def test_no_local_import_statements(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.TARGET_FUNCTIONS:
                for child in ast.walk(node):
                    self.assertNotIsInstance(child, (ast.Import, ast.ImportFrom),
                                              f"Function {node.name} must not contain a local import statement.")


class RealEndToEndRouteTests(unittest.TestCase):
    """Real integration tests: actual Flask routes with app.beets_client's
    track-replacement methods patched to call the REAL transaction_engine
    functions (real TransactionStore, real SQLite DB, real files) --
    matching the established pattern from Wave 16's
    Wave16RealEngineIntegrationTests. This is what actually proves the
    Web Manager -> BeetsClient -> control-agent-shaped call chain preserves
    the engine's security semantics, not just that the engine functions
    work when called directly."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)
        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir(parents=True, exist_ok=True)
        self.tx_store = TransactionStore(root=str(store_dir))

        self.music_root = self.tmp_path / "music"
        self.music_root.mkdir()
        self.staging_root = self.tmp_path / "downloads"
        self.staging_root.mkdir()
        self.quarantine_root = self.tmp_path / "quarantine"
        self.quarantine_root.mkdir()

        # See the identical note in TrackReplacementFixtureBase.setUp:
        # create_track_replacement_plan reads this env var directly, so it
        # must be kept in sync with the quarantine_allowed_root passed to
        # Apply/Rollback below.
        self._env_patch = mock.patch.dict(os.environ, {"REPLACEMENT_QUARANTINE_DIR": str(self.quarantine_root)})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

        self.db_path = self.tmp_path / "musiclibrary.blb"
        con = sqlite3.connect(self.db_path)
        con.execute(ITEMS_SCHEMA)
        con.commit()
        con.close()

        self.rid = "aaaaaaaa-1111-1111-1111-111111111111"
        self.orig = self.music_root / "track.mp3"
        self.orig.write_bytes(b"ORIGINAL_AUDIO")
        self.cand = self.staging_root / "track_hq.flac"
        self.cand.write_bytes(b"REPLACEMENT_AUDIO")

        # item_replacement_plan (final review fix) now validates
        # candidate_path against app.py's own trusted candidate/staging
        # roots via _resolve_import_review_source_path(allow_music=False)
        # BEFORE ever reaching beets_client.plan_track_replacement --
        # patch DOWNLOADS_ROOT to this test's staging_root rather than
        # relying on the OS temp dir incidentally matching one of the real
        # default roots, which would silently mask whether the containment
        # check is actually being exercised (the identical Wave 6/7
        # pitfall this suite's sibling fixtures already avoid).
        self._downloads_root_patch = mock.patch.object(flask_app, "DOWNLOADS_ROOT", self.staging_root)
        self._downloads_root_patch.start()
        self.addCleanup(self._downloads_root_patch.stop)
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute(
            "INSERT INTO items (path, title, artist, mb_trackid, size, mtime) VALUES (?, ?, ?, ?, ?, ?)",
            (str(self.orig), "Track", "Artist", self.rid, self.orig.stat().st_size, self.orig.stat().st_mtime),
        )
        self.item_id = cur.lastrowid
        con.commit()
        con.close()

        def mock_plan(payload, **kw):
            return create_track_replacement_plan(
                self.tx_store, payload,
                original_allowed_roots=[str(self.music_root)],
                candidate_allowed_roots=[str(self.staging_root)],
                db_path=str(self.db_path),
            )

        def mock_apply(op_id, **kw):
            return execute_track_replacement_apply(
                self.tx_store, op_id, db_path=str(self.db_path),
                original_allowed_roots=[str(self.music_root)],
                candidate_allowed_roots=[str(self.staging_root)],
                quarantine_allowed_root=str(self.quarantine_root),
            )

        def mock_rollback(op_id, **kw):
            return rollback_track_replacement(
                self.tx_store, op_id, db_path=str(self.db_path),
                quarantine_allowed_root=str(self.quarantine_root),
            )

        def mock_get_transaction(op_id, **kw):
            try:
                return {"ok": True, "transaction": self.tx_store.get(op_id)}
            except KeyError:
                return {"ok": False, "error": "not found"}

        self.mock_plan = mock_plan
        self.mock_apply = mock_apply
        self.mock_rollback = mock_rollback
        self.mock_get_transaction = mock_get_transaction

        flask_app.app.config["TESTING"] = True
        self._env_patch = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        self.client = flask_app.app.test_client()

        # Item lookup used by item_replacement_plan and the AcoustID
        # verification step -- avoid depending on a real Beets Library
        # instance in a unit test; this is the one seam mocked in this
        # suite, everything downstream of it (Plan/Apply/Rollback) is real.
        fake_item = mock.MagicMock()
        fake_item.path = str(self.orig)
        fake_item.mb_trackid = self.rid
        fake_item.mb_releasegroupid = ""
        self._lib_patch = mock.patch.object(flask_app.lib, "get_item", return_value=fake_item)
        self._lib_patch.start()
        self.addCleanup(self._lib_patch.stop)

    def test_plan_route_performs_no_mutation(self):
        with patch.object(flask_app.beets_client, "plan_track_replacement", side_effect=self.mock_plan), \
             patch.object(flask_app, "_acoustid_fingerprint_match", return_value=(self.rid, [self.rid], [self.rid])):
            resp = self.client.post(f"/api/items/{self.item_id}/replacement/plan", json={"candidate_path": str(self.cand)})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])
        self.assertTrue(self.orig.exists())
        self.assertTrue(self.cand.exists())

    def test_plan_refuses_when_fingerprint_unverified(self):
        with patch.object(flask_app.beets_client, "plan_track_replacement", side_effect=self.mock_plan), \
             patch.object(flask_app, "_acoustid_fingerprint_match", return_value=("", [], [])), \
             patch.object(flask_app, "_acoustid_fingerprint_ids", return_value=[]):
            resp = self.client.post(f"/api/items/{self.item_id}/replacement/plan", json={"candidate_path": str(self.cand)})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["code"], "candidate_not_verified")

    def test_plan_route_rejects_candidate_path_outside_trusted_roots(self):
        """Regression test for the CodeQL-flagged finding this final review
        fixed: item_replacement_plan used to build cand_p from the raw
        client string and stat() it directly (with no containment check at
        all, and a wrong MUSIC_ROOT-relative fallback for relative input),
        letting an authenticated caller probe for the existence of
        arbitrary files on the web-manager host before the engine ever saw
        the request. It must now be rejected via the same hardened
        _resolve_import_review_source_path() containment check Import
        Review already uses, without ever reaching beets_client at all."""
        outside = self.tmp_path / "outside_all_roots"
        outside.mkdir()
        probe = outside / "not_a_staging_file.flac"
        probe.write_bytes(b"whatever")
        with patch.object(flask_app.beets_client, "plan_track_replacement", side_effect=self.mock_plan) as plan_mock, \
             patch.object(flask_app, "_acoustid_fingerprint_match", return_value=(self.rid, [self.rid], [self.rid])):
            resp = self.client.post(f"/api/items/{self.item_id}/replacement/plan", json={"candidate_path": str(probe)})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["ok"])
        plan_mock.assert_not_called()

    def test_end_to_end_plan_apply_rollback_through_real_routes(self):
        with patch.object(flask_app.beets_client, "plan_track_replacement", side_effect=self.mock_plan), \
             patch.object(flask_app.beets_client, "apply_track_replacement", side_effect=self.mock_apply), \
             patch.object(flask_app.beets_client, "rollback_track_replacement", side_effect=self.mock_rollback), \
             patch.object(flask_app.beets_client, "get_transaction", side_effect=self.mock_get_transaction), \
             patch.object(flask_app, "_acoustid_fingerprint_match", return_value=(self.rid, [self.rid], [self.rid])):
            plan_resp = self.client.post(f"/api/items/{self.item_id}/replacement/plan", json={"candidate_path": str(self.cand)})
            self.assertEqual(plan_resp.status_code, 200)
            op_id = plan_resp.get_json()["operation_id"]

            apply_resp = self.client.post(f"/api/items/{self.item_id}/replacement/apply", json={"operation_id": op_id})
            self.assertEqual(apply_resp.status_code, 200)
            self.assertTrue(apply_resp.get_json()["ok"])
            self.assertFalse(self.orig.exists())

            rollback_resp = self.client.post(f"/api/transactions/{op_id}/rollback")
            self.assertEqual(rollback_resp.status_code, 200)
            data = rollback_resp.get_json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["status"], "Rolled Back")
            self.assertTrue(self.orig.exists())

    def test_generic_rollback_dispatches_by_family_not_hardcoded(self):
        """The generic /api/transactions/<id>/rollback route must consult
        the transaction's own mutation_family (via get_transaction) and
        call the matching executor -- not assume Import Review cleanup for
        every unknown-locally id, which was the Wave 16 anti-pattern this
        review is re-confirming stays fixed for a third mutation family."""
        with patch.object(flask_app.beets_client, "plan_track_replacement", side_effect=self.mock_plan), \
             patch.object(flask_app.beets_client, "apply_track_replacement", side_effect=self.mock_apply), \
             patch.object(flask_app.beets_client, "rollback_track_replacement", side_effect=self.mock_rollback) as mock_rb, \
             patch.object(flask_app.beets_client, "rollback_import_review_cleanup") as mock_ir_rb, \
             patch.object(flask_app.beets_client, "get_transaction", side_effect=self.mock_get_transaction), \
             patch.object(flask_app, "_acoustid_fingerprint_match", return_value=(self.rid, [self.rid], [self.rid])):
            plan_resp = self.client.post(f"/api/items/{self.item_id}/replacement/plan", json={"candidate_path": str(self.cand)})
            op_id = plan_resp.get_json()["operation_id"]
            self.client.post(f"/api/items/{self.item_id}/replacement/apply", json={"operation_id": op_id})

            resp = self.client.post(f"/api/transactions/{op_id}/rollback")
            self.assertEqual(resp.status_code, 200)
            mock_rb.assert_called_once()
            mock_ir_rb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
