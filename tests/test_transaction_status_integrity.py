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


if __name__ == "__main__":
    unittest.main()
