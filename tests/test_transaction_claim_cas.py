"""Transaction status compare-and-set hardening (#206 F3/F4, PR #204 QA F-B).

Everything runs against temp directories, a temp TransactionStore and a fake
adapter."""

import inspect
import os
import tempfile
import unittest
from unittest import mock

import backend.composite_workflows as cw
from backend import resource_locks
from backend.transaction_engine import TransactionStore
from tests.test_staging_mutation_hardening import _AlbumStaysAdapter, _RouteEnv
from tests.test_wave0_s1_containment import FakeAdapter


class SharedStoreLockTests(unittest.TestCase):
    def test_stores_on_one_directory_share_one_lock(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIs(TransactionStore(d)._lock, TransactionStore(os.path.join(d, "."))._lock)


class ClaimRacesCancelTests(_RouteEnv):
    """F3: a cancel CAS landing between the claim's read and its write wins."""

    def _cancel_during_claim_read(self, tx_id, cancels):
        real_get = self.store.get

        def get(tid, *a, **kw):
            tx = real_get(tid, *a, **kw)
            if not cancels and any(f.function == "claim_approved" for f in inspect.stack(0)):
                cancels.append(self.client.post(f"/api/transactions/{tid}/cancel"))
            return tx
        return mock.patch.object(self.store, "get", side_effect=get)

    def test_claim_approved_loses_to_cancel(self):
        tx = self.store.create(operation_type="Delete", status="Approved")
        cancels = []
        with self._cancel_during_claim_read(tx["id"], cancels):
            claimed = resource_locks.claim_approved(self.store, tx["id"])
        self.assertTrue(cancels[0].get_json()["ok"])
        self.assertIsNone(claimed)
        self.assertEqual(self.store.get(tx["id"])["status"], "Cancelled")

    def test_album_cleanup_apply_never_runs_after_cancel(self):
        ad = FakeAdapter(items={7: {"id": 7, "album_id": 5, "path": self.media("A/B/01.flac")}},
                         albums={5: {"id": 5}})
        op = cw.plan_album_cleanup(5, adapter=ad, store=self.store)["operation_id"]
        self.store.transition(op, "Preview", "Approved")
        cancels = []
        with self._cancel_during_claim_read(op, cancels), mock.patch.object(cw, "beets_adapter", ad):
            resp = self.client.post(f"/api/transactions/{op}/apply")
        self.assertTrue(cancels[0].get_json()["ok"])
        self.assertEqual(resp.status_code, 409, resp.get_json())
        self.assertEqual(ad.destructive_calls(), [])
        self.assertEqual(self.store.get(op)["status"], "Cancelled")

    def test_metadata_update_job_applies_nothing_after_cancel(self):
        import backend.transaction_service as ts
        tx = self.store.create(operation_type="Metadata Update", status="Approved",
                               metadata={"item_id": 3, "pending_fields": {"title": "New"}})
        started = {}

        def start_python(fn, label="", metadata=None):
            started["fn"] = fn
            return mock.Mock(job_id="job1")

        with mock.patch.object(ts, "transactions", self.store), \
                mock.patch.object(ts.jobs, "start_python", side_effect=start_python), \
                mock.patch.object(ts.composite_workflows, "update_item_metadata") as write:
            ts._start_metadata_apply_transaction(tx["id"])
            # The cancel lands after the route's status check, before the job runs.
            self.assertTrue(self.client.post(f"/api/transactions/{tx['id']}/cancel").get_json()["ok"])
            with self.assertRaises(RuntimeError):
                started["fn"]([])
        write.assert_not_called()
        self.assertEqual(self.store.get(tx["id"])["status"], "Cancelled")


class ApproveNeverResurrectsTests(_RouteEnv):
    """F4: Preview -> Approved is a CAS; Cancelled/Failed stay put."""

    def _tx(self, status, **meta):
        return self.store.create(operation_type="Delete", status=status, metadata=meta)["id"]

    def test_approve_preview(self):
        op = self._tx("Preview")
        self.assertEqual(resource_locks.approve_preview(self.store, op, "me")["status"], "Approved")
        self.assertEqual(resource_locks.approve_preview(self.store, op, "me")["status"], "Approved")
        for status in ("Cancelled", "Failed", "Completed"):
            self.assertIsNone(resource_locks.approve_preview(self.store, self._tx(status), "me"))
        with self.assertRaises(KeyError):
            resource_locks.approve_preview(self.store, "missing", "me")

    def test_approve_and_apply_sites_refuse(self):
        import backend.album_row_merge as arm
        import backend.duplicate_cleanup as dc
        import backend.item_replacement as ir
        for status in ("Cancelled", "Failed"):
            with self.subTest(status=status), \
                    mock.patch.object(arm, "apply_album_row_merge") as a1, \
                    mock.patch.object(cw, "apply_track_replacement") as a2, \
                    mock.patch.object(dc, "apply_reviewed_cleanup") as a3:
                ops = [self._tx(status) for _ in range(2)]
                cleanup = self._tx(status, mutation_family=dc.REVIEWED_CLEANUP_FAMILY)
                self.assertEqual(arm.approve_and_apply(ops[0], approved_by="x", store=self.store)["code"],
                                 "not_preview")
                self.assertEqual(ir.approve_and_apply(ops[1], approved_by="x", store=self.store)["code"],
                                 "not_preview")
                res = cw.apply_existing_album_reconcile(cleanup, store=self.store, approve_duplicates=True)
                self.assertEqual(res["code"], "not_preview")
                for fn in (a1, a2, a3):
                    fn.assert_not_called()
                for op in ops + [cleanup]:
                    self.assertEqual(self.store.get(op)["status"], status)

    def test_dedup_sites_refuse(self):
        import backend.dedup_service as ds
        op = self._tx("Cancelled")
        with mock.patch.object(ds._duplicate_cleanup, "plan_reviewed_cleanup",
                               return_value={"ok": True, "operation_id": op, "skipped": []}), \
                mock.patch.object(ds._duplicate_cleanup, "pairs_from_proposal", return_value=[{}]), \
                mock.patch.object(ds, "_dedup_pairs_for_paths", return_value=([{}], [])), \
                mock.patch.object(ds._duplicate_cleanup, "apply_reviewed_cleanup") as apply:
            body, _ = ds.run_dedup_cleanup({"paths": ["/x.flac"], "dry_run": False})
            self.assertFalse(body["results"][0]["deleted"])
            res = ds._unattended_reviewed_cleanup([{}], [])
            self.assertFalse(res["ok"])
            self.assertEqual(res["deleted"], 0)
        apply.assert_not_called()
        self.assertEqual(self.store.get(op)["status"], "Cancelled")


class GenericAlbumCleanupApplyTests(_RouteEnv):
    """PR #204 QA F-B: the generic apply route classifies like the album route."""

    def _approved(self, ad):
        op = cw.plan_album_cleanup(5, adapter=ad, store=self.store)["operation_id"]
        self.store.transition(op, "Preview", "Approved")
        return op

    def test_lock_conflict_is_controlled_409(self):
        ad = FakeAdapter(items={7: {"id": 7, "album_id": 5, "path": self.media("A/B/01.flac")}},
                         albums={5: {"id": 5}})
        op = self._approved(ad)
        conflict = resource_locks.ResourceLockConflictError("album:5", {"owner": "/secret/holder"}, "me")
        with mock.patch.object(cw, "beets_adapter", ad), \
                mock.patch.object(resource_locks.ResourceLocks, "hold", side_effect=conflict):
            resp = self.client.post(f"/api/transactions/{op}/apply")
        body = resp.get_json()
        self.assertEqual(resp.status_code, 409, body)
        self.assertEqual((body["code"], body["error_kind"], body["mutated"]), ("resource_busy", "other", False))
        self.assertNotIn("secret", str(body))
        self.assertEqual(self.store.get(op)["status"], "Approved")

    def test_unexpected_error_is_controlled_500(self):
        ad = FakeAdapter(items={7: {"id": 7, "album_id": 5, "path": self.media("A/B/01.flac")}},
                         albums={5: {"id": 5}})
        op = self._approved(ad)
        with mock.patch.object(cw, "apply_album_cleanup", side_effect=OSError("/secret/path")):
            resp = self.client.post(f"/api/transactions/{op}/apply")
        body = resp.get_json()
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(body["error_kind"], "other")
        self.assertNotIn("secret", str(body))

    def test_failure_result_is_classified(self):
        path = self.media("A/B/01.flac")
        ad = _AlbumStaysAdapter(items={7: {"id": 7, "album_id": 5, "path": path}}, albums={5: {"id": 5}})
        op = self._approved(ad)
        with mock.patch.object(cw, "beets_adapter", ad):
            resp = self.client.post(f"/api/transactions/{op}/apply")
        body = resp.get_json()
        self.assertEqual(resp.status_code, 400, body)
        self.assertEqual(body["error_kind"], "partial_mutation")
        self.assertTrue(body["mutated"])


if __name__ == "__main__":
    unittest.main()
