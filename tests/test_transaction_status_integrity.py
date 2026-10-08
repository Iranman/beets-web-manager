"""Transaction status integrity (#218, #224, #300). Each test fails on
da2a717: an apply or rollback that a cancel (or an unapplied status) must
stop wrote anyway, or marked the transaction Completed / Rolled Back.
Temp stores, temp directories and fake adapters only."""

import unittest
from unittest import mock

import backend.album_row_merge as album_row_merge
import backend.composite_workflows as cw
import backend.duplicate_cleanup as duplicate_cleanup
import backend.untracked_recovery_service as untracked
from backend import transaction_engine as te
from backend.beets_adapter import BeetsAdapterError, BeetsAdapterTimeoutError
from backend.transaction_engine import TransactionStore
from tests._folder_ops_local import LocalFolderOps
from tests.test_wave0_s1_containment import _Env

_WRITES = ("modify", "move", "lastgenre", "fetch_art", "embed_art", "remove")


class _CancelOnRead(TransactionStore):
    """The operator's cancel lands right after the apply read the
    transaction and before it claimed it: the race #218 describes."""

    armed = False

    def get(self, transaction_id):
        tx = super().get(transaction_id)
        if self.armed and tx.get("status") == "Preview":
            self.armed = False
            self.transition(transaction_id, "Preview", "Cancelled")
        return tx


def _adapter():
    ad = mock.Mock()
    ad.get_album.return_value = {"id": 1, "artpath": "/music/a/cover.jpg"}
    ad.find_all_items_by_album_id.return_value = []
    return ad


#: (name, plan(store) -> operation_id, apply(op, adapter, store))
_FAMILIES = [
    ("album_relocation", lambda st: cw.plan_album_relocation({"album_id": 1}, store=st)["operation_id"],
     lambda op, ad, st: cw.apply_album_relocation(op, adapter=ad, store=st)),
    ("item_metadata", lambda st: cw.plan_item_metadata({"item_id": 5, "updates": {"title": "T"}},
                                                        store=st)["operation_id"],
     lambda op, ad, st: cw.apply_item_metadata(op, adapter=ad, store=st)),
    ("album_genre_repair", lambda st: cw.plan_album_genre_repair({"album_id": 1}, store=st)["operation_id"],
     lambda op, ad, st: cw.apply_album_genre_repair(op, adapter=ad, store=st)),
    ("album_artwork", lambda st: cw.plan_album_artwork({"album_id": 1}, store=st)["operation_id"],
     lambda op, ad, st: cw.apply_album_artwork(op, adapter=ad, store=st)),
    ("album_artwork_fetch", lambda st: cw.plan_album_artwork_fetch({"album_id": 1}, store=st)["operation_id"],
     lambda op, ad, st: cw.apply_album_artwork_fetch(op, adapter=ad, store=st)),
    ("artist_folder_reconcile",
     lambda st: st.create(operation_type="Metadata Update", status="Preview",
                          metadata={"canonical_name": "A", "album_ids": [1]})["id"],
     lambda op, ad, st: cw.apply_artist_folder_reconcile(op, adapter=ad, store=st)),
    ("album_maintenance", lambda st: cw.plan_album_maintenance({"mode": "remove_album", "album_id": 1},
                                                                store=st)["operation_id"],
     lambda op, ad, st: cw.apply_album_maintenance(op, adapter=ad, store=st)),
]


class CompositeApplyCasTests(_Env):
    """#218: the plain composite apply families claim Running with a CAS."""

    def setUp(self):
        super().setUp()
        self.store = _CancelOnRead(str(self.data / "tx"))

    def test_cancel_between_read_and_claim_writes_nothing(self):
        for name, plan, apply in _FAMILIES:
            with self.subTest(family=name):
                op, ad = plan(self.store), _adapter()
                self.store.armed = True
                res = apply(op, ad, self.store)
                self.assertFalse(res["ok"], res)
                self.assertEqual(self.store.get(op)["status"], "Cancelled")
                self.assertEqual([c for c in ad.method_calls if c[0] in _WRITES], [])

    def test_preview_applies_and_completes(self):
        for name, plan, apply in _FAMILIES:
            with self.subTest(family=name):
                op = plan(self.store)
                res = apply(op, _adapter(), self.store)
                self.assertEqual((res["ok"], res["status"]), (True, "Completed"), res)
                self.assertEqual(self.store.get(op)["status"], "Completed")

    def test_adapter_failure_is_never_left_running(self):
        op = cw.plan_album_relocation({"album_id": 1}, store=self.store)["operation_id"]
        ad = _adapter()
        ad.move.side_effect = RuntimeError("refused")
        with self.assertRaises(RuntimeError):
            cw.apply_album_relocation(op, adapter=ad, store=self.store)
        self.assertEqual(self.store.get(op)["status"], "Failed")
        op = cw.plan_album_genre_repair({"album_id": 1}, store=self.store)["operation_id"]
        ad.lastgenre.side_effect = BeetsAdapterTimeoutError("timed out")
        with self.assertRaises(BeetsAdapterTimeoutError):
            cw.apply_album_genre_repair(op, adapter=ad, store=self.store)
        self.assertEqual(self.store.get(op)["status"], "Recovery Required")

    def test_second_apply_is_refused(self):
        op = cw.plan_album_relocation({"album_id": 1}, store=self.store)["operation_id"]
        ad = _adapter()
        self.assertTrue(cw.apply_album_relocation(op, adapter=ad, store=self.store)["ok"])
        self.assertFalse(cw.apply_album_relocation(op, adapter=ad, store=self.store)["ok"])
        self.assertEqual(ad.move.call_count, 1)


#: (name, rollback fn, adapter rollback method, metadata of an applied tx)
_ENGINE_ROLLBACKS = [
    ("track_replacement", cw.rollback_track_replacement, "rollback_replace_item_file",
     {"mutation_family": cw.ITEM_FILE_REPLACEMENT_FAMILY, "engine_result": {"quarantine_id": "q1"},
      "target_item_id": 1, "source_item_id": 2}),
    ("track_quarantine", cw.rollback_track_quarantine, "rollback_quarantine_remove_items",
     {"mutation_family": cw.TRACK_QUARANTINE_FAMILY, "engine_result": {"quarantine_id": "q1"}}),
    ("reviewed_cleanup", duplicate_cleanup.rollback_reviewed_cleanup, "rollback_quarantine_remove_items",
     {"mutation_family": duplicate_cleanup.REVIEWED_CLEANUP_FAMILY, "engine_result": {"quarantine_id": "q1"}}),
    ("album_row_merge", album_row_merge.rollback_album_row_merge, "rollback_album_row_merge",
     {"mutation_family": album_row_merge.ALBUM_ROW_MERGE_FAMILY, "engine_result": {"merge_id": "m1"}}),
    ("untracked_recovery", untracked.rollback_recovery, "untracked_rollback",
     {"mutation_family": untracked.ATTACH_FAMILY, "engine_result": {"record_id": "r1"}}),
]


class EngineRollbackStatusTests(_Env):
    """#224 item 3: an engine-family rollback never runs for, or marks Rolled
    Back, a transaction no apply left behind."""

    def test_unapplied_status_is_refused_without_an_engine_call(self):
        for name, fn, method, meta in _ENGINE_ROLLBACKS:
            for status in ("Cancelled", "Preview", "Approved", "Running"):
                with self.subTest(family=name, status=status):
                    op = self.store.create(operation_type="Replace", status=status, metadata=meta)["id"]
                    ad = mock.Mock()
                    res = fn(op, adapter=ad, store=self.store)
                    self.assertEqual((res["ok"], res["code"]), (False, "rollback_not_eligible"), res)
                    getattr(ad, method).assert_not_called()
                    self.assertEqual(self.store.get(op)["status"], status)

    def test_track_replacement_rollback_still_runs_when_applied(self):
        meta = _ENGINE_ROLLBACKS[0][3]
        op = self.store.create(operation_type="Replace", status="Completed", metadata=meta)["id"]
        ad = mock.Mock()
        ad.rollback_replace_item_file.return_value = {"result": {"restored_target_path": "/m/a.flac"}}
        self.assertTrue(cw.rollback_track_replacement(op, adapter=ad, store=self.store)["ok"])
        self.assertEqual(self.store.get(op)["status"], "Rolled Back")

    def test_track_replacement_rollback_does_not_overwrite_a_concurrent_change(self):
        meta = _ENGINE_ROLLBACKS[0][3]
        op = self.store.create(operation_type="Replace", status="Completed", metadata=meta)["id"]
        ad = mock.Mock()

        def other_rollback_finishes_first(*_a, **_k):
            self.store.transition(op, "Completed", "Recovery Required")
            return {"result": {}}

        ad.rollback_replace_item_file.side_effect = other_rollback_finishes_first
        res = cw.rollback_track_replacement(op, adapter=ad, store=self.store)
        self.assertEqual((res["ok"], res["code"]), (False, "conflict"))
        self.assertEqual(self.store.get(op)["status"], "Recovery Required")


class ImportReviewCleanupRollbackTests(_Env):
    """#224 item 3: rollback_import_review_cleanup claims with a CAS."""

    def _tx(self, status):
        src, dest = self.dl / "Album" / "a.flac", self.data / "q" / "a.flac"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"audio")
        op = self.store.create(operation_type="Delete", status=status, metadata={
            "mutation_family": cw.IMPORT_REVIEW_CLEANUP_FAMILY, "rollback_available": True,
            "steps": [{"type": "move_quarantine", "status": "completed", "source": str(src),
                       "destination": str(dest)}]})["id"]
        return op, src, dest

    def test_cancelled_is_refused_and_nothing_moves(self):
        for status in ("Cancelled", "Preview", "Approved"):
            with self.subTest(status=status):
                op, src, dest = self._tx(status)
                res = cw.rollback_import_review_cleanup(op, store=self.store)
                self.assertEqual((res["ok"], res["code"]), (False, "rollback_not_eligible"), res)
                self.assertEqual(self.store.get(op)["status"], status)
                self.assertTrue(dest.exists() and not src.exists())

    def test_completed_restores_and_rolls_back(self):
        op, src, dest = self._tx("Completed")
        res = cw.rollback_import_review_cleanup(op, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.store.get(op)["status"], "Rolled Back")
        self.assertTrue(src.exists() and not dest.exists())



if __name__ == "__main__":
    unittest.main()
