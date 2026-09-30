"""Album-row merge workflow (backend/album_row_merge.py) and restart recovery
(backend/transaction_recovery.py): Plan -> Approve -> Apply -> Verify ->
Rollback, and the ARCH-004 restart matrix -- no completed mutation ever runs
twice; engine evidence finishes an interrupted Apply or it becomes
Recovery Required."""

import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import backend.album_row_merge as arm
import backend.composite_workflows as composite_workflows
import backend.transaction_recovery as recovery
from backend.beets_adapter import BeetsNotFoundError
from backend.resource_locks import ResourceLocks, set_locks
from backend.transaction_engine import TransactionStore

RG = "ef4b6576-ac7c-4f72-bee6-e7a6b6cf019d"
REL = "45347542-db98-422a-a307-ae95d5371f60"


def rec(n):
    return f"00000000-0000-0000-0000-{n:012d}"


class FakeEngine:
    """Models album ownership the way the engine op does, and counts calls."""

    def __init__(self, root: Path):
        self.albums = {10: {"id": 10, "album": "A", "albumartist": "X", "mb_albumid": REL, "mb_releasegroupid": RG},
                       11: {"id": 11, "album": "A", "albumartist": "X", "mb_albumid": REL, "mb_releasegroupid": RG}}
        self.items = {}
        for iid, aid, track in ((1, 10, 1), (2, 10, 2), (3, 10, 3), (4, 11, 4), (5, 11, 5)):
            path = root / f"{iid}.flac"
            path.write_bytes(f"audio-{iid}".encode())
            self.items[iid] = {"id": iid, "album_id": aid, "disc": 1, "track": track, "mb_trackid": rec(track),
                               "mb_albumid": REL, "mb_releasegroupid": RG, "path": str(path)}
        self.manifests = {}
        self.registry = {}
        self.merge_calls = 0
        self.crash_after_apply = False

    def find_all_albums_by_releasegroupid(self, rg):
        return [a for a in self.albums.values() if a["mb_releasegroupid"] == rg]

    def get_items(self, query=None):
        return list(self.items.values())

    def get_item(self, iid):
        return self.items.get(int(iid))

    def get_album(self, aid):
        return self.albums.get(int(aid))

    def get_stats(self):
        return {"albums": len(self.albums), "items": len(self.items)}

    def album_row_merge(self, target, sources, items, rg, rel, idempotency_key):
        mid = arm.merge_id_for(idempotency_key)
        if mid in self.manifests:
            return {"replayed": True, **self.manifests[mid]["result"]}
        self.merge_calls += 1
        self.manifests[mid] = {"status": "applying", "sources": {s: dict(self.albums[s]) for s in sources},
                               "items": {i["item_id"]: i["source_album_id"] for i in items}}
        for i in items:
            self.items[i["item_id"]]["album_id"] = target
        for s in sources:
            del self.albums[s]
        result = {"success": True, "merge_id": mid, "retired_album_ids": list(sources),
                  "moved_item_ids": [i["item_id"] for i in items]}
        self.manifests[mid].update(status="applied", result=result)
        self.registry[idempotency_key] = {"status": "succeeded", "result": result}
        if self.crash_after_apply:
            raise ConnectionError("web manager died before recording the outcome")
        return result

    def get_album_row_merge(self, merge_id):
        m = self.manifests.get(merge_id)
        if m is None:
            raise BeetsNotFoundError("no record")
        return {"merge_id": merge_id, "status": m["status"], "result": m.get("result")}

    def get_operation(self, op):
        if op not in self.registry:
            raise BeetsNotFoundError("no op")
        return {"operation_id": op, **self.registry[op]}

    def rollback_album_row_merge(self, merge_id, idempotency_key=None):
        m = self.manifests[merge_id]
        if m["status"] == "rolled_back":
            return {"replayed": True}
        for sid, snap in m["sources"].items():
            self.albums[sid] = snap
        for iid, aid in m["items"].items():
            self.items[iid]["album_id"] = aid
        m["status"] = "rolled_back"
        return {"success": True}


class AlbumRowMergeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.engine = FakeEngine(root)
        self.store = TransactionStore(str(root / "tx"))
        set_locks(ResourceLocks(root / "locks"))
        self.addCleanup(set_locks, None)
        for p in (mock.patch.object(arm, "beets_adapter", self.engine),
                  mock.patch.object(recovery, "beets_adapter", self.engine),
                  mock.patch.object(composite_workflows, "_default_store", self.store)):
            p.start()
            self.addCleanup(p.stop)

    def _plan(self):
        return arm.plan_album_row_merge(RG, store=self.store, abs_path=lambda p: p, cached_ids=lambda p: None)

    def _approved(self):
        op = self._plan()["operation_id"]
        self.store.update(op, status="Approved")
        return op

    def _snapshot(self):
        return ({k: dict(v) for k, v in self.engine.items.items()}, {k: dict(v) for k, v in self.engine.albums.items()})

    def test_plan_records_sha_and_identity_and_mutates_nothing(self):
        before = self._snapshot()
        res = self._plan()
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["target_album_id"], res["source_album_ids"]), (10, [11]))
        meta = self.store.get(res["operation_id"])["metadata"]
        self.assertEqual([m["item_id"] for m in meta["items"]], [4, 5])
        self.assertEqual(meta["items"][0]["sha256"], hashlib.sha256(b"audio-4").hexdigest())
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.engine.merge_calls, 0)

    def test_a_stale_retained_row_is_refused_until_it_is_recovered(self):
        """The 311 - Dammit! shape: the analysis calls the group deterministic,
        but the retained row's own files are gone."""
        Path(self.engine.items[2]["path"]).unlink()
        res = self._plan()
        self.assertEqual((res["ok"], res["code"], res["item_ids"]), (False, "retained_row_file_missing", [2]))
        self.assertEqual(self.engine.merge_calls, 0)

    def test_non_deterministic_groups_are_review_only(self):
        self.engine.albums[11]["mb_albumid"] = "11111111-2222-3333-4444-555555555555"
        res = self._plan()
        self.assertEqual(res["code"], "review_only")
        self.assertTrue(any("differing editions" in b for b in res["blockers"]))

    def test_apply_needs_approval_verifies_and_rolls_back_exactly(self):
        before = self._snapshot()
        op = self._plan()["operation_id"]
        self.assertEqual(arm.apply_album_row_merge(op, store=self.store)["code"], "not_approved")
        self.store.update(op, status="Approved")
        res = arm.apply_album_row_merge(op, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["albums_before"], res["albums_after"]), (2, 1))
        self.assertEqual(arm.apply_album_row_merge(op, store=self.store)["code"], "already_applied")
        rb = arm.rollback_album_row_merge(op, store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.engine.merge_calls, 1)

    # -- ARCH-004 restart matrix ------------------------------------------
    def test_A_B_restart_before_mutation_or_after_plan_changes_nothing(self):
        op = self._plan()["operation_id"]
        self.assertEqual(recovery.sweep(store=self.store), [])
        self.assertEqual(self.store.get(op)["status"], "Preview")
        self.assertEqual(self.engine.merge_calls, 0)

    def test_C_restart_after_approval_leaves_it_approved_and_applies_once(self):
        op = self._approved()
        recovery.sweep(store=self.store)
        self.assertEqual(self.store.get(op)["status"], "Approved")
        arm.apply_album_row_merge(op, store=self.store)
        self.assertEqual(self.engine.merge_calls, 1)

    def test_D_crash_after_engine_apply_is_finished_from_the_manifest_not_replayed(self):
        op = self._approved()
        self.engine.crash_after_apply = True
        with self.assertRaises(ConnectionError):
            arm.apply_album_row_merge(op, store=self.store)
        self.store.update(op, status="Running")  # what a crashed process leaves behind
        self.engine.crash_after_apply = False
        [result] = recovery.sweep(store=self.store)
        self.assertEqual(result["action"], "finished")
        self.assertEqual(self.store.get(op)["status"], "Completed")
        self.assertEqual(self.engine.merge_calls, 1)

    def test_D_no_engine_record_means_nothing_changed(self):
        op = self._approved()
        self.store.update(op, status="Running", metadata={"engine_request": {"merge_id": arm.merge_id_for(op)}})
        [result] = recovery.sweep(store=self.store)
        self.assertEqual(result["action"], "Failed")
        self.assertEqual(self.engine.merge_calls, 0)

    def test_D_engine_record_mid_apply_with_no_live_operation_needs_recovery(self):
        op = self._approved()
        self.store.update(op, status="Running", metadata={"engine_request": {}})
        self.engine.manifests[arm.merge_id_for(op)] = {"status": "applying"}
        [result] = recovery.sweep(store=self.store)
        self.assertEqual(result["action"], "Recovery Required")

    def test_D_engine_still_running_is_left_running(self):
        op = self._approved()
        self.store.update(op, status="Running", metadata={"engine_request": {}})
        self.engine.manifests[arm.merge_id_for(op)] = {"status": "applying"}
        self.engine.registry[op] = {"status": "running"}
        [result] = recovery.sweep(store=self.store)
        self.assertEqual(result["action"], "still_running")
        self.assertEqual(self.store.get(op)["status"], "Running")

    def test_E_restart_after_completed_apply_changes_nothing(self):
        op = self._approved()
        arm.apply_album_row_merge(op, store=self.store)
        self.assertEqual(recovery.sweep(store=self.store), [])
        self.assertEqual(arm.apply_album_row_merge(op, store=self.store)["code"], "already_applied")
        self.assertEqual(self.engine.merge_calls, 1)

    def test_F_running_without_an_engine_request_was_never_sent(self):
        op = self._approved()
        self.store.update(op, status="Running")  # cancelled/crashed before the engine call
        [result] = recovery.sweep(store=self.store)
        self.assertEqual(result["action"], "failed_never_sent")
        self.assertEqual(self.engine.merge_calls, 0)

    def test_G_duplicate_concurrent_apply_runs_the_engine_once(self):
        op = self._approved()
        outcomes = []
        barrier = threading.Barrier(4)

        def go():
            barrier.wait()
            try:
                outcomes.append(arm.apply_album_row_merge(op, store=self.store).get("status") or "refused")
            except Exception as exc:  # lock conflict
                outcomes.append(type(exc).__name__)

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.engine.merge_calls, 1)
        self.assertEqual(outcomes.count("Completed"), 1)

    def test_registry_backed_family_is_finished_or_marked_recovery_required(self):
        tx = self.store.create(operation_type="Replace", status="Running", summary="r",
                               metadata={"mutation_family": composite_workflows.ITEM_FILE_REPLACEMENT_FAMILY,
                                         "engine_request": {}})
        [result] = recovery.sweep(store=self.store)
        self.assertEqual(result["action"], "Recovery Required")  # registry lost: cannot prove
        self.assertEqual(self.store.get(tx["id"])["status"], "Recovery Required")


if __name__ == "__main__":
    unittest.main()
