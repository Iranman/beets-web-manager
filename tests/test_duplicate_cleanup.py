"""Reviewed duplicate cleanup (backend.duplicate_cleanup).

Plan re-proves every reviewed pair against live Beets with the scan's own
policy and skips drifted pairs; Apply needs approval, quarantines through the
engine by content hash and verifies the keepers; Rollback goes through the
engine. The Web Manager never touches /music itself.
"""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.duplicate_cleanup as dc
from backend.transaction_engine import TransactionStore

REC = "5d39b3b9-290a-493e-93fc-30b5ec6c614a"
REL = "45347542-db98-422a-a307-ae95d5371f60"
RG = "ef4b6576-ac7c-4f72-bee6-e7a6b6cf019d"


class FakeAdapter:
    def __init__(self, items):
        self.items = items
        self.removed = []
        self.stats_items = len(items)
        self.quarantine_remove_items = mock.MagicMock(side_effect=self._remove)
        self.rollback_quarantine_remove_items = mock.MagicMock(
            return_value={"success": True, "restored": [{"old_item_id": 22577, "new_item_id": 30001, "path": "x"}]})

    def get_item(self, iid):
        return self.items.get(int(iid))

    def get_stats(self):
        return {"items": self.stats_items}

    def find_all_items_by_album_id(self, album_id):
        return [i for i in self.items.values() if i.get("album_id") == album_id]

    def _remove(self, items, idempotency_key=None):
        removed = []
        for entry in items:
            row = self.items.pop(int(entry["item_id"]))
            self.stats_items -= 1
            removed.append({"item_id": row["id"], "original_path": row["path"], "quarantine_path": "/config/q/x"})
        return {"success": True, "quarantine_id": "a" * 32, "removed": removed}


class ReviewedCleanupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        album_dir = self.root / "BossMan Dlow" / "2 Slippery"
        album_dir.mkdir(parents=True)
        self.keep_path = album_dir / "BossMan Dlow - 2 Slippery - 11 - Top Notch.1.flac"
        self.drop_path = album_dir / "bossman dlow - 2 Slippery - 11 - top notch (00).flac"
        self.keep_path.write_bytes(b"k" * 120)
        self.drop_path.write_bytes(b"d" * 138)
        self.items = {
            25264: {"id": 25264, "album_id": 1935, "mb_trackid": REC, "mb_albumid": REL, "mb_releasegroupid": RG,
                    "disc": 1, "track": 11, "format": "FLAC", "bitrate": 900000, "path": str(self.keep_path)},
            22577: {"id": 22577, "album_id": None, "mb_trackid": "", "mb_albumid": REL, "mb_releasegroupid": "",
                    "disc": 1, "track": 11, "format": "FLAC", "bitrate": 900000, "path": str(self.drop_path)},
            1: {"id": 1, "album_id": 1935, "path": str(album_dir / "other.flac")},
        }
        self.adapter = FakeAdapter(self.items)
        self.store = TransactionStore(str(self.root / "tx"))
        self.fp = mock.MagicMock(return_value=(REC, [REC], [REC]))
        self.deps = {"fingerprint_match": self.fp, "abs_path": lambda p: p, "music_root": self.root,
                     "path_under": lambda path, root: str(path).startswith(str(root))}

    def _pair(self, **expected):
        pair = {"delete_item_id": 22577, "keep_item_id": 25264}
        if expected:
            pair["expected"] = expected
        return pair

    def _plan(self, *pairs):
        return dc.plan_reviewed_cleanup(pairs or [self._pair()], adapter=self.adapter, store=self.store, deps=self.deps)

    def test_verified_pair_is_planned_with_its_content_hash(self):
        res = self._plan()
        self.assertTrue(res["ok"], res)
        [pair] = res["pairs"]
        self.assertEqual(pair["delete_sha256"], hashlib.sha256(b"d" * 138).hexdigest())
        self.assertEqual(pair["shared_recording_id"], REC)
        self.assertIn("album slot", pair["keep_reason"])
        self.assertEqual(self.store.get(res["operation_id"])["status"], "Preview")
        self.adapter.quarantine_remove_items.assert_not_called()

    def _skip_reasons(self, *pairs):
        res = self._plan(*pairs)
        self.assertFalse(res["ok"])
        return res["skipped"][0]["reasons"]

    def test_drifted_pairs_are_skipped_back_to_review(self):
        self.assertIn("delete_path_changed", self._skip_reasons(self._pair(delete={"path": "/elsewhere.flac"})))
        self.assertIn("delete_file_changed_since_proposal",
                      self._skip_reasons(self._pair(delete={"path": str(self.drop_path), "size": 1})))
        self.assertIn("keep_release_slot_changed",
                      self._skip_reasons(self._pair(keep={"album_id": 1935, "disc": 1, "track": 12})))
        self.assertIn("shared_recording_changed",
                      self._skip_reasons(self._pair(fingerprint={"shared_recording_id": "other"})))
        self.assertIn("release_relation_changed",
                      self._skip_reasons(self._pair(release_relation="same_album_position")))

    def test_missing_items_or_files_are_skipped(self):
        self.drop_path.unlink()
        self.assertIn("delete_file_missing", self._skip_reasons())
        del self.items[25264]
        self.assertIn("keep_item_missing", self._skip_reasons())

    def test_no_shared_fingerprint_is_never_deleted(self):
        self.fp.return_value = ("", [REC], ["other-recording"])
        self.assertIn("no_shared_fingerprint_recording", self._skip_reasons())

    def test_embedded_id_contradicting_fingerprint_is_skipped(self):
        self.items[22577]["mb_trackid"] = "11111111-1111-1111-1111-111111111111"
        self.assertIn("embedded_recording_id_contradicts_fingerprint", self._skip_reasons())

    def test_keeper_policy_flip_is_skipped(self):
        """Reversed pair: policy keeps the album copy, so deleting it is refused."""
        reasons = self._skip_reasons({"delete_item_id": 25264, "keep_item_id": 22577})
        self.assertIn("keeper_policy_now_prefers_the_other_copy", reasons)

    def test_album_slot_gate_holds(self):
        self.items[22577]["album_id"] = 2000  # would empty another album row's slot
        reasons = self._skip_reasons()
        self.assertTrue(any("policy_no_longer_selects" in r or "release_relation" in r for r in reasons), reasons)

    def test_lossy_keeper_with_lossless_rival_goes_to_replacement_review(self):
        self.items[25264].update(format="MP3", bitrate=128000)
        self.assertIn("replacement_review_required", self._skip_reasons())

    def test_overlapping_pairs_in_one_plan_are_refused(self):
        res = self._plan(self._pair(), {"delete_item_id": 25264, "keep_item_id": 22577})
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["pairs"]), 1)
        self.assertEqual(len(res["skipped"]), 1)

    def test_apply_requires_approval_then_quarantines_and_verifies(self):
        op = self._plan()["operation_id"]
        early = dc.apply_reviewed_cleanup(op, adapter=self.adapter, store=self.store, abs_path=lambda p: p)
        self.assertEqual(early["code"], "not_approved")
        self.adapter.quarantine_remove_items.assert_not_called()

        self.store.update(op, status="Approved")
        res = dc.apply_reviewed_cleanup(op, adapter=self.adapter, store=self.store, abs_path=lambda p: p)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "Completed")
        self.adapter.quarantine_remove_items.assert_called_once_with(
            [{"item_id": 22577, "sha256": hashlib.sha256(b"d" * 138).hexdigest()}], idempotency_key=op)
        self.assertEqual((res["items_before"], res["items_after"]), (3, 2))
        again = dc.apply_reviewed_cleanup(op, adapter=self.adapter, store=self.store, abs_path=lambda p: p)
        self.assertEqual(again["code"], "already_applied")

    def test_apply_flags_keeper_identity_drift(self):
        op = self._plan()["operation_id"]
        self.store.update(op, status="Approved")
        original = self.adapter._remove

        def remove_and_drift(items, idempotency_key=None):
            out = original(items, idempotency_key)
            self.items[25264]["track"] = 3
            return out
        self.adapter.quarantine_remove_items.side_effect = remove_and_drift
        res = dc.apply_reviewed_cleanup(op, adapter=self.adapter, store=self.store, abs_path=lambda p: p)
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "Recovery Required")
        self.assertTrue(any("identity changed: track" in p for p in res["verification_problems"]))

    def test_apply_flags_an_unexpected_library_count_change(self):
        op = self._plan()["operation_id"]
        self.store.update(op, status="Approved")
        original = self.adapter._remove

        def remove_extra(items, idempotency_key=None):
            out = original(items, idempotency_key)
            self.adapter.stats_items -= 1  # something else vanished too
            return out
        self.adapter.quarantine_remove_items.side_effect = remove_extra
        res = dc.apply_reviewed_cleanup(op, adapter=self.adapter, store=self.store, abs_path=lambda p: p)
        self.assertIn("library item count changed by 2, expected 1", res["verification_problems"])

    def test_rollback_goes_through_the_engine(self):
        op = self._plan()["operation_id"]
        self.assertEqual(dc.rollback_reviewed_cleanup(op, adapter=self.adapter, store=self.store)["code"], "not_applied")
        self.store.update(op, status="Approved")
        dc.apply_reviewed_cleanup(op, adapter=self.adapter, store=self.store, abs_path=lambda p: p)
        res = dc.rollback_reviewed_cleanup(op, adapter=self.adapter, store=self.store)
        self.assertTrue(res["ok"])
        self.adapter.rollback_quarantine_remove_items.assert_called_once_with("a" * 32, idempotency_key=f"{op}:rollback")
        self.assertEqual(self.store.get(op)["status"], "Rolled Back")

    def test_pairs_from_proposal_takes_only_delete_rows(self):
        proposal = [
            {"action": "delete", "delete": {"item_id": 22577, "path": "a"}, "keep": {"item_id": 25264},
             "fingerprint": {"shared_recording_id": REC}, "release_relation": "same_release_position"},
            {"action": "replacement_review", "delete": {"item_id": 24258}, "keep": {"item_id": 22575}},
        ]
        pairs = dc.pairs_from_proposal(proposal)
        self.assertEqual([(p["delete_item_id"], p["keep_item_id"]) for p in pairs], [(22577, 25264)])
        self.assertEqual(dc.pairs_from_proposal(proposal, [(1, 2)]), [])


class SiblingAdapter(FakeAdapter):
    """FakeAdapter with album rows: an engine that retires the named row."""

    def __init__(self, items, albums):
        super().__init__(items)
        self.albums = albums
        self.quarantine_remove_items = mock.MagicMock(side_effect=self._remove_and_retire)
        self.rollback_quarantine_remove_items = mock.MagicMock(side_effect=self._restore)
        self._gone = {}

    def get_album(self, album_id, expand=True):
        return self.albums.get(int(album_id))

    def get_stats(self):
        return {"items": self.stats_items, "albums": len(self.albums)}

    def _remove_and_retire(self, items, idempotency_key=None):
        for entry in items:
            self._gone[int(entry["item_id"])] = self.items[int(entry["item_id"])]
            if entry.get("retire_album_id"):
                self._gone[("album", entry["retire_album_id"])] = self.albums.pop(int(entry["retire_album_id"]))
        return self._remove(items, idempotency_key)

    def _restore(self, quarantine_id, idempotency_key=None):
        for key, row in list(self._gone.items()):
            if isinstance(key, tuple):
                self.albums[key[1]] = row
            else:
                self.items[key] = row
                self.stats_items += 1
        return {"success": True, "restored": []}


class SiblingRowRetirementTests(unittest.TestCase):
    """A copy that is the ONLY item of a duplicate row of the keeper's own
    release (Dennis Brown shape): reviewed cleanup may remove it and retire
    that row; the unattended path never may."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        album_dir = self.root / "Various Artists" / "Revolutionary Sounds"
        album_dir.mkdir(parents=True)
        self.keep_path = album_dir / "Dennis Brown - Revolutionary Sounds - 04 - Here I Come.flac"
        self.drop_path = album_dir / "dennis brown - Revolutionary Sounds - 04 - Here I Come (00).flac"
        self.keep_path.write_bytes(b"k" * 100)
        self.drop_path.write_bytes(b"d" * 100)
        common = {"mb_trackid": REC, "mb_albumid": REL, "mb_releasegroupid": RG, "disc": 1, "track": 4,
                  "format": "FLAC", "bitrate": 483490}
        self.items = {24301: {"id": 24301, "album_id": 1976, "path": str(self.keep_path), **common},
                      25215: {"id": 25215, "album_id": 2140, "path": str(self.drop_path), **common}}
        self.albums = {1976: {"id": 1976, "mb_albumid": REL, "mb_releasegroupid": RG, "album": "Revolutionary Sounds"},
                       2140: {"id": 2140, "mb_albumid": REL, "mb_releasegroupid": RG, "album": "Revolutionary Sounds"}}
        self.adapter = SiblingAdapter(self.items, self.albums)
        self.store = TransactionStore(str(self.root / "tx"))
        self.deps = {"fingerprint_match": mock.MagicMock(return_value=(REC, [REC], [REC])), "abs_path": lambda p: p,
                     "music_root": self.root, "path_under": lambda path, root: str(path).startswith(str(root))}
        self.pair = {"delete_item_id": 25215, "keep_item_id": 24301}

    def _plan(self, allow=True):
        return dc.plan_reviewed_cleanup([self.pair], adapter=self.adapter, store=self.store, deps=self.deps,
                                        allow_sibling_row_retire=allow)

    def test_without_the_opt_in_the_album_slot_gate_still_refuses(self):
        res = self._plan(allow=False)
        self.assertFalse(res["ok"])
        self.assertIn("policy_no_longer_selects", res["skipped"][0]["reasons"][0])

    def test_reviewed_plan_retires_the_sole_item_row(self):
        res = self._plan()
        self.assertTrue(res["ok"], res)
        [pair] = res["pairs"]
        self.assertEqual(pair["retire_album"]["album_id"], 2140)
        self.assertEqual(pair["retire_album"]["keeper_album_id"], 1976)
        change = self.store.get(res["operation_id"])["changes"][0]
        self.assertEqual(change["retire_album_id"], 2140)

    def test_refused_for_another_release_group_a_shared_row_or_another_slot(self):
        cases = (("other release", lambda: self.albums[2140].update(mb_albumid="other")),
                 ("other release group", lambda: self.albums[2140].update(mb_releasegroupid="other")),
                 ("row not sole", lambda: self.items.update({9: {"id": 9, "album_id": 2140, "path": "x"}})),
                 ("other slot", lambda: self.items[25215].update(track=5)))
        for label, mutate in cases:
            with self.subTest(label):
                self.setUp()
                mutate()
                self.assertFalse(self._plan()["ok"], label)

    def test_apply_retires_the_row_and_verifies_counts_then_rollback_verifies_restoration(self):
        op = self._plan()["operation_id"]
        self.store.update(op, status="Approved")
        res = dc.apply_reviewed_cleanup(op, adapter=self.adapter, store=self.store, abs_path=lambda p: p)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["retired_album_ids"], [2140])
        [sent] = self.adapter.quarantine_remove_items.call_args.args[0]
        self.assertEqual((sent["retire_album_id"], sent["sibling_keeper_item_id"]), (2140, 24301))
        self.assertNotIn(2140, self.albums)
        rb = dc.rollback_reviewed_cleanup(op, adapter=self.adapter, store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(self.store.get(op)["status"], "Rolled Back")

    def test_apply_flags_a_row_that_was_not_retired(self):
        op = self._plan()["operation_id"]
        self.store.update(op, status="Approved")
        self.adapter.quarantine_remove_items.side_effect = self.adapter._remove  # engine leaves the row
        res = dc.apply_reviewed_cleanup(op, adapter=self.adapter, store=self.store, abs_path=lambda p: p)
        self.assertEqual(res["status"], "Recovery Required")
        self.assertIn("duplicate album row 2140 was not retired", res["verification_problems"])

    def test_rollback_that_does_not_restore_the_row_needs_recovery(self):
        op = self._plan()["operation_id"]
        self.store.update(op, status="Approved")
        dc.apply_reviewed_cleanup(op, adapter=self.adapter, store=self.store, abs_path=lambda p: p)
        self.adapter.rollback_quarantine_remove_items.side_effect = lambda *a, **k: {"success": True, "restored": []}
        rb = dc.rollback_reviewed_cleanup(op, adapter=self.adapter, store=self.store)
        self.assertEqual(rb["status"], "Recovery Required")

    def test_only_the_operator_reviewed_route_opts_in(self):
        import inspect
        import backend.dedup_service as ds
        import routes_cleanup
        self.assertNotIn("allow_sibling_row_retire", inspect.getsource(ds))
        route = inspect.getsource(routes_cleanup.dedup_reviewed_cleanup_plan)
        self.assertIn("allow_sibling_row_retire=True", route)


class UnattendedAndManualPathTests(unittest.TestCase):
    """Both the unattended step and the manual cleanup route use the same
    reviewed-cleanup authority; nothing else can remove a duplicate."""

    def test_unattended_path_plans_approves_and_applies_through_the_authority(self):
        import backend.dedup_service as ds
        store = mock.MagicMock()
        with mock.patch.object(ds._duplicate_cleanup, "plan_reviewed_cleanup",
                               return_value={"ok": True, "operation_id": "tx1", "skipped": []}) as plan, \
                mock.patch.object(ds._duplicate_cleanup, "apply_reviewed_cleanup",
                                  return_value={"ok": True, "status": "Completed",
                                                "removed": [{"original_path": "/music/a.flac"}]}) as apply_, \
                mock.patch.object(ds.composite_workflows, "get_default_store", return_value=store):
            out = ds._unattended_reviewed_cleanup(
                [{"action": "delete", "delete": {"item_id": 2, "path": "/music/a.flac"}, "keep": {"item_id": 1}}], [])
        plan.assert_called_once()
        store.transition.assert_called_once_with(
            "tx1", "Preview", "Approved", metadata={"approved_by": "unattended duplicate deletion authorization"})
        store.update.assert_not_called()
        apply_.assert_called_once_with("tx1")
        self.assertEqual(out["deleted"], 1)

    def test_manual_live_cleanup_refuses_paths_without_a_proven_pair(self):
        import backend.dedup_service as ds
        with mock.patch.object(ds, "_dedup_pairs_for_paths", return_value=([], ["/music/lonely.flac"])), \
                mock.patch.object(ds._duplicate_cleanup, "apply_reviewed_cleanup") as apply_:
            body, status = ds.run_dedup_cleanup({"paths": ["/music/lonely.flac"], "dry_run": False})
        self.assertEqual(status, 200)
        self.assertEqual(body["deleted"], 0)
        self.assertIn("left in place", body["results"][0]["error"])
        apply_.assert_not_called()


if __name__ == "__main__":
    unittest.main()
