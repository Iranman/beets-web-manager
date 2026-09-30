"""ARCH-020: the legacy merge entry points (composite_workflows
plan/apply/rollback_album_duplicate_merge, merge_duplicate_albums,
merge_split_album_items, *_existing_album_reconcile) are thin delegations to
the one album-row merge authority (backend.album_row_merge) and, for
duplicate copies, to the reviewed duplicate cleanup.

Nothing here rewrites Release, Release Group or Recording IDs, tags or
paths; another edition, an unproven release or a filled slot is refused.
"""

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.album_row_merge as arm
import backend.composite_workflows as cw
import backend.duplicate_cleanup as dc
from backend.resource_locks import ResourceLocks, set_locks
from backend.transaction_engine import TransactionStore

RG = "ef4b6576-ac7c-4f72-bee6-e7a6b6cf019d"
REL = "45347542-db98-422a-a307-ae95d5371f60"
REL_OTHER = "11111111-2222-3333-4444-555555555555"


def rec(n):
    return f"00000000-0000-0000-0000-{n:012d}"


class FakeEngine:
    """Album ownership and quarantine the way the engine ops behave."""

    def __init__(self, root: Path):
        self.root = root
        self.albums = {10: self._album(10), 11: self._album(11)}
        self.items = {}
        # Existing row 10: tracks 1-3. Imported row 11: tracks 4-5.
        for iid, aid, track in ((1, 10, 1), (2, 10, 2), (3, 10, 3), (4, 11, 4), (5, 11, 5)):
            self.add_item(iid, aid, track)
        self.merge_requests = []
        self.removed = []

    @staticmethod
    def _album(aid, rel=REL, rg=RG):
        return {"id": aid, "album": "A", "albumartist": "X", "mb_albumid": rel, "mb_releasegroupid": rg}

    def add_item(self, iid, aid, track, recording=None):
        path = self.root / f"{iid}.flac"
        path.write_bytes(f"audio-{iid}".encode())
        self.items[iid] = {"id": iid, "album_id": aid, "disc": 1, "track": track, "format": "FLAC", "bitrate": 900000,
                           "mb_trackid": recording or rec(track), "mb_albumid": REL, "mb_releasegroupid": RG,
                           "path": str(path)}

    def get_album(self, aid, expand=True):
        return self.albums.get(int(aid))

    def get_item(self, iid):
        return self.items.get(int(iid))

    def find_all_items_by_album_id(self, aid):
        return [i for i in self.items.values() if i["album_id"] == int(aid)]

    def get_stats(self):
        return {"albums": len(self.albums), "items": len(self.items)}

    def album_row_merge(self, target, sources, items, rg, rel, idempotency_key, partial=False):
        self.merge_requests.append({"target": target, "sources": list(sources), "partial": partial,
                                    "item_ids": [i["item_id"] for i in items]})
        self._undo = ({s: dict(self.albums[s]) for s in sources}, {i["item_id"]: i["source_album_id"] for i in items})
        for i in items:
            self.items[i["item_id"]]["album_id"] = target
        retired = [s for s in sources if not self.find_all_items_by_album_id(s)]
        for s in retired:
            del self.albums[s]
        return {"success": True, "merge_id": arm.merge_id_for(idempotency_key), "retired_album_ids": retired,
                "moved_item_ids": [i["item_id"] for i in items]}

    def rollback_album_row_merge(self, merge_id, idempotency_key=None):
        rows, owners = self._undo
        self.albums.update(rows)
        for iid, aid in owners.items():
            self.items[iid]["album_id"] = aid
        return {"success": True}

    def quarantine_remove_items(self, items, idempotency_key=None):
        out = []
        for entry in items:
            row = self.items.pop(int(entry["item_id"]))
            self.removed.append(entry)
            if entry.get("retire_album_id"):
                del self.albums[int(entry["retire_album_id"])]
            out.append({"item_id": row["id"], "original_path": row["path"], "quarantine_path": "/config/q/x"})
        return {"success": True, "quarantine_id": "a" * 32, "removed": out}


class LegacyMergeCallerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.engine = FakeEngine(self.root)
        self.store = TransactionStore(str(self.root / "tx"))
        set_locks(ResourceLocks(self.root / "locks"))
        self.addCleanup(set_locks, None)
        self.shared = mock.MagicMock(side_effect=self._fingerprints)
        deps = {"fingerprint_match": self.shared, "abs_path": lambda p: p, "music_root": self.root,
                "path_under": lambda path, root: str(path).startswith(str(root))}
        for p in (mock.patch.object(arm, "_default_abs_path", lambda: (lambda p: p)),
                  mock.patch.object(dc, "_default_deps", lambda: deps)):
            p.start()
            self.addCleanup(p.stop)
        self.kw = {"adapter": self.engine, "store": self.store}

    def _fingerprints(self, drop_path, keep_path):
        """Same recording exactly when both items carry the same Recording ID."""
        by_path = {i["path"]: i["mb_trackid"] for i in self.engine.items.values()}
        a, b = by_path.get(drop_path), by_path.get(keep_path)
        return (a if a == b else "", [a], [b])

    # -- merge entry points ---------------------------------------------------

    def test_whole_row_merge_accepts_a_single_source_id_and_moves_ownership_only(self):
        before = {k: {f: v for f, v in i.items() if f != "album_id"} for k, i in self.engine.items.items()}
        res = cw.merge_duplicate_albums(10, 11, **self.kw)
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["moved"], res["retired_album_ids"]), (2, [11]))
        [req] = self.engine.merge_requests
        self.assertEqual((req["target"], req["sources"], req["partial"]), (10, [11], False))
        after = {k: {f: v for f, v in i.items() if f != "album_id"} for k, i in self.engine.items.items()}
        self.assertEqual(after, before)  # no identity, tag or path field changed
        tx = self.store.get(res["operation_id"])
        self.assertEqual((tx["status"], tx["metadata"]["approved_by"]), ("Completed", "operator merge request"))

    def test_another_edition_or_an_unproven_release_is_refused_not_rewritten(self):
        self.engine.albums[11]["mb_albumid"] = REL_OTHER
        self.assertEqual(cw.merge_duplicate_albums(10, [11], **self.kw)["code"], "edition_differs")
        self.engine.albums[11]["mb_albumid"] = REL
        self.engine.albums[11]["mb_releasegroupid"] = "other"
        self.assertEqual(cw.merge_duplicate_albums(10, [11], **self.kw)["code"], "release_group_mismatch")
        self.engine.albums[10]["mb_albumid"] = ""
        self.assertEqual(cw.merge_duplicate_albums(10, [11], **self.kw)["code"], "identity_required")
        self.assertEqual(self.engine.merge_requests, [])

    def test_a_filled_slot_is_refused_as_a_duplicate_review_case(self):
        self.engine.add_item(6, 11, 2)
        self.assertEqual(cw.merge_duplicate_albums(10, [11], **self.kw)["code"], "slot_overlap")
        self.assertEqual(self.engine.merge_requests, [])

    def test_identity_rewriting_payloads_are_refused(self):
        for extra in ({"adopt_target_fields": True}, {"item_field_overrides": {"4": {"track": 9}}},
                      {"reassign_fields": {"mb_albumid": REL_OTHER}}):
            res = cw.plan_album_duplicate_merge({"target_album_id": 10, "source_album_id": 11, **extra}, **self.kw)
            self.assertEqual(res["code"], "identity_rewrite_not_supported", extra)

    def test_split_album_move_is_partial_and_retires_the_row_only_when_emptied(self):
        res = cw.merge_split_album_items(10, 11, [4], **self.kw)
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["items_reassigned"], res["source_album_deleted"]), (1, False))
        self.assertTrue(self.engine.merge_requests[0]["partial"])
        self.assertIn(11, self.engine.albums)
        res = cw.merge_split_album_items(10, 11, [5], **self.kw)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["source_album_deleted"])
        self.assertFalse(self.engine.merge_requests[1]["partial"])  # every remaining item: a full move

    def test_split_album_move_refuses_an_item_of_another_row(self):
        self.assertEqual(cw.merge_split_album_items(10, 11, [1], **self.kw)["code"], "item_not_in_source")

    def test_rollback_restores_rows_and_ownership(self):
        res = cw.merge_duplicate_albums(10, [11], **self.kw)
        rb = cw.rollback_album_duplicate_merge(res["operation_id"], **self.kw)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(sorted(i["id"] for i in self.engine.find_all_items_by_album_id(11)), [4, 5])

    # -- existing-album reconcile (import) -------------------------------------

    def _reconcile_payload(self, moves, dups):
        return {"existing_album_id": 10, "imported_album_id": 11, "move_item_ids": moves,
                "dup_item_ids": [d for d, _ in dups],
                "dup_details": [{"dup_item_id": d, "survivor_item_ids": [k]} for d, k in dups]}

    def test_reconcile_moves_now_and_leaves_duplicates_for_approval(self):
        self.engine.add_item(6, 11, 2)  # imported copy of existing slot 2 (item 2), same recording
        plan = cw.plan_existing_album_reconcile(self._reconcile_payload([4, 5], [(6, 2)]), **self.kw)
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(self.engine.merge_requests, [])  # planning changes nothing
        res = cw.apply_existing_album_reconcile(plan["operation_id"], **self.kw)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.engine.merge_requests[0]["item_ids"], [4, 5])
        self.assertTrue(self.engine.merge_requests[0]["partial"])
        self.assertEqual(res["cleanup_status"], "Preview")
        self.assertEqual(self.engine.removed, [])  # the copy stays until an operator approves
        cleanup = self.store.get(res["cleanup_operation_id"])
        self.assertEqual(cleanup["status"], "Preview")
        self.assertEqual(cleanup["metadata"]["pairs"][0]["retire_album"]["album_id"], 11)

    def test_reviewer_decision_applies_the_cleanup_and_retires_the_emptied_row(self):
        self.engine.items.pop(4), self.engine.items.pop(5)
        self.engine.add_item(6, 11, 2)
        plan = cw.plan_existing_album_reconcile(self._reconcile_payload([], [(6, 2)]), **self.kw)
        self.assertTrue(plan["ok"], plan)
        res = cw.apply_existing_album_reconcile(plan["operation_id"], approve_duplicates=True,
                                                approved_by="reconciliation reviewer", **self.kw)
        self.assertTrue(res["ok"], res)
        [sent] = self.engine.removed
        self.assertEqual((sent["item_id"], sent["retire_album_id"], sent["sibling_keeper_item_id"]), (6, 11, 2))
        self.assertNotIn(11, self.engine.albums)
        self.assertEqual(self.store.get(plan["operation_id"])["metadata"]["approved_by"], "reconciliation reviewer")

    def test_an_unproven_duplicate_is_never_removed_even_with_a_reviewer_decision(self):
        self.engine.items.pop(4), self.engine.items.pop(5)
        self.engine.add_item(6, 11, 2, recording=rec(99))  # a different recording in the same slot
        plan = cw.plan_existing_album_reconcile(self._reconcile_payload([], [(6, 2)]), **self.kw)
        self.assertFalse(plan["ok"])
        self.assertEqual(self.engine.removed, [])
        # ...and after a move, the unproven copy keeps both sides.
        self.engine.add_item(7, 11, 7)
        plan = cw.plan_existing_album_reconcile(self._reconcile_payload([7], [(6, 2)]), **self.kw)
        res = cw.apply_existing_album_reconcile(plan["operation_id"], approve_duplicates=True, **self.kw)
        self.assertFalse(res["ok"])
        self.assertEqual(res["cleanup_status"], "not_proven")
        self.assertEqual(self.engine.removed, [])
        self.assertEqual(self.engine.items[7]["album_id"], 10)  # the proven move still happened

    def test_reconcile_with_no_moves_and_no_duplicates_merges_the_whole_row(self):
        plan = cw.plan_existing_album_reconcile({"existing_album_id": 10, "imported_album_id": 11}, **self.kw)
        res = cw.apply_existing_album_reconcile(plan["operation_id"], **self.kw)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["retired_album_ids"], [11])
        self.assertEqual(res["cleanup_status"], "none")

    def test_reconcile_rollback_dispatches_by_family(self):
        plan = cw.plan_existing_album_reconcile({"existing_album_id": 10, "imported_album_id": 11}, **self.kw)
        cw.apply_existing_album_reconcile(plan["operation_id"], **self.kw)
        self.assertTrue(cw.rollback_existing_album_reconcile(plan["operation_id"], **self.kw)["ok"])
        self.assertIn(11, self.engine.albums)


class CallerContractTests(unittest.TestCase):
    """The route callers bind to the real signatures (they used to pass an
    int where a list was iterated, and three arguments to a two-argument
    function -- both only failed at run time)."""

    def test_route_call_shapes_bind(self):
        inspect.signature(cw.merge_duplicate_albums).bind(10, 11)
        inspect.signature(cw.merge_split_album_items).bind(10, 11, [4])
        import routes_cleanup
        import routes_library
        self.assertIn("merge_duplicate_albums(target_id, source_id)", inspect.getsource(routes_cleanup))
        self.assertIn("merge_split_album_items(target_id, source_id, move_ids)", inspect.getsource(routes_library))

    def test_no_legacy_merge_path_reassigns_album_id_through_modify(self):
        source = inspect.getsource(cw)
        for name in ("plan_album_duplicate_merge", "apply_album_duplicate_merge", "rollback_album_duplicate_merge",
                     "merge_duplicate_albums", "merge_split_album_items", "plan_existing_album_reconcile",
                     "apply_existing_album_reconcile", "rollback_existing_album_reconcile"):
            body = inspect.getsource(getattr(cw, name))
            self.assertNotIn(".modify(", body, name)
            self.assertNotIn(".remove(", body, name)
        self.assertNotIn('"album_id": target_aid', source)


if __name__ == "__main__":
    unittest.main()
