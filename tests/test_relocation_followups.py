"""#328 album relocation follow-ups against real Beets + the webmanager
plugin: a one-shot move whose lock is busy is Cancelled, not left Approved
(N1); an interrupted apply tells the operator to move again (F2); a sweep
that completes a move says so in its own log (F9); rollback reports adopted
tracks separately (N4)."""

import os
import time

import backend.composite_workflows as cw
import backend.transaction_recovery as recovery
from backend.resource_locks import ResourceLockConflictError, locks
from tests.test_album_relocation_transaction import _Engine


class RelocationFollowupTests(_Engine):
    def setUp(self):
        super().setUp()
        self.album = self.make_album(os.path.join(self.music, "incoming", "x"))

    def test_busy_album_lock_cancels_the_one_shot_move(self):
        before = self.snapshot(self.album.id)
        with locks().hold([f"album:{self.album.id}"], "someone-else"):
            with self.assertRaises(ResourceLockConflictError):
                cw.relocate_album(self.album.id, adapter=self.ad, store=self.store)
        rows, _ = self.store.list(limit=10)
        self.assertEqual([r["status"] for r in rows], ["Cancelled"])
        self.assertIn("nothing was changed", str(self.store.get(rows[0]["id"])["logs"]))
        self.assertEqual(self.snapshot(self.album.id), before)

    def test_interrupted_move_tells_the_operator_to_move_again(self):
        plan = cw.plan_album_relocation(album_id=self.album.id, adapter=self.ad, store=self.store)
        op = plan["operation_id"]
        self.store.transition(op, "Preview", "Approved")
        self.store.transition(op, "Approved", "Running", metadata={"engine_request": {"operation_id": op}})
        item = next(iter(self.lib.get_album(self.album.id).items()))
        dest = os.fsdecode(item.destination())
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.replace(os.fsdecode(item.path), dest)  # Beets died between file move and row store
        recovery.sweep(adapter=self.ad, store=self.store, before=time.time() + 1)
        self.assertEqual(self.store.get(op)["status"], "Recovery Required")
        self.assertIn("move the album to the library again", str(self.store.get(op)["logs"]))
        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertEqual(rb["code"], "not_applied")
        self.assertIn("move the album to the library again", rb["error"])

    def test_sweep_that_finishes_a_move_logs_it(self):
        plan = cw.plan_album_relocation(album_id=self.album.id, adapter=self.ad, store=self.store)
        op = plan["operation_id"]
        planned = self.store.get(op)["metadata"]["before"]["items"]
        self.store.transition(op, "Preview", "Approved")
        self.store.transition(op, "Approved", "Running", metadata={"engine_request": {"operation_id": op}})
        self.ad.relocate_album(self.album.id, planned, idempotency_key=op)  # Web Manager died here
        recovery.sweep(adapter=self.ad, store=self.store, before=time.time() + 1)
        tx = self.store.get(op)
        self.assertEqual(tx["status"], "Completed")
        self.assertIn("Recovered after restart: the engine finished", str(tx["logs"]))

    def test_rollback_reports_adopted_tracks(self):
        res = cw.relocate_album(self.album.id, adapter=self.ad, store=self.store)
        rb = cw.rollback_album_relocation(res["operation_id"], adapter=self.ad, store=self.store)
        self.assertEqual((rb["ok"], rb["adopted"]), (True, 0), rb)
