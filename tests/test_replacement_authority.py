"""The one replacement authority (backend.item_replacement).

Every caller -- item route, music-format pipeline, import album merge,
reconciliation review -- plans through plan_verified_replacement: AcoustID
decides, text never does, the slot keeps its identity, an occupied
canonical destination is displaced only for identical audio, and anything
unproven fails closed with both files kept. Engine-level behaviour (the
file swap, idempotent retry, rollback, partial-failure recovery,
request-thread paths) is covered by tests/test_webmanager_replace_item_file.py.
"""

import ast
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as composite_workflows
import backend.item_replacement as item_replacement
from backend.import_reconciliation import album_identity, plan_reconciliation
from backend.transaction_engine import TransactionStore

REC = "2513c401-c500-42fe-9113-ce9d9c3295d5"
OTHER = "99999999-9999-9999-9999-999999999999"
RG = "ef4b6576-ac7c-4f72-bee6-e7a6b6cf019d"
ROOT = Path(__file__).resolve().parents[1]


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "Artist" / "Album").mkdir(parents=True)
        self.mp3 = self.root / "Artist" / "Album" / "17 Exotic.mp3"
        self.flac = self.root / "Artist" / "Album" / "exotic (00).flac"
        self.mp3.write_bytes(b"mp3")
        self.flac.write_bytes(b"flac")
        # Library-relative paths, as the stock Beets web API reports them.
        self.items = {
            1: {"id": 1, "album_id": 1935, "mb_trackid": REC, "mb_albumid": "rel", "mb_releasegroupid": RG,
                "disc": 1, "track": 17, "format": "MP3", "title": "Exotic", "path": "Artist/Album/17 Exotic.mp3"},
            2: {"id": 2, "album_id": None, "mb_trackid": "", "disc": 0, "track": 17, "format": "FLAC",
                "title": "exotic", "path": "Artist/Album/exotic (00).flac"},
        }
        self.adapter = mock.MagicMock()
        self.adapter.get_item.side_effect = lambda iid: self.items.get(int(iid))
        self.store = TransactionStore(str(self.root / "tx"))
        self.fp_match = mock.MagicMock(return_value=(REC, [REC], [REC]))
        self.fp_ids = mock.MagicMock(return_value=[REC])
        self.dest = mock.MagicMock(return_value={"ok": True, "displace": None})
        self.deps = {
            "fingerprint_match": self.fp_match, "fingerprint_ids": self.fp_ids,
            "abs_path": lambda p: p if p.startswith("/") or ":" in p else str(self.root / p),
            "destination_check": self.dest,
            "matching_contract": lambda resolved, repl: {"fingerprint": repl["fingerprint_validation"]},
        }

    def _plan(self, target=1, source=2, **kw):
        return item_replacement.plan_verified_replacement(
            target, source, reason="test", adapter=self.adapter, store=self.store, deps=self.deps, **kw)

    def test_tracked_flac_replaces_tracked_lossy_album_item(self):
        res = self._plan()
        self.assertTrue(res["ok"], res)
        tx = self.store.get(res["operation_id"])
        self.assertEqual(tx["status"], "Preview")
        self.assertEqual(tx["metadata"]["mutation_family"], composite_workflows.ITEM_FILE_REPLACEMENT_FAMILY)
        self.assertEqual((tx["metadata"]["target_item_id"], tx["metadata"]["source_item_id"]), (1, 2))
        self.assertEqual(res["fingerprint_validation"]["mb_recording_id_candidate"], REC)

    def test_relative_beets_paths_are_resolved_for_fingerprinting(self):
        self._plan()
        self.fp_match.assert_called_once_with(str(self.flac), str(self.mp3))
        self.dest.assert_called_once_with(str(self.mp3), str(self.flac))

    def test_identical_audio_occupant_is_carried_into_the_plan(self):
        displace = {"path": str(self.mp3.with_suffix(".flac")), "sha256": "a" * 64}
        self.dest.return_value = {"ok": True, "displace": displace}
        res = self._plan()
        self.assertEqual(res["displace_destination"], displace)
        self.assertEqual(self.store.get(res["operation_id"])["metadata"]["displace_destination"], displace)

    def test_occupant_with_different_audio_fails_closed(self):
        self.dest.return_value = {"ok": False, "code": "destination_occupied", "error": "differs"}
        res = self._plan()
        self.assertEqual(res["code"], "destination_occupied")
        self.assertEqual(list(Path(self.store.root).glob("txn_*.json")), [])

    def test_fingerprint_disagreement_fails_closed(self):
        self.fp_match.return_value = ("", [OTHER], [REC])
        res = self._plan()
        self.assertEqual(res["code"], "fingerprint_disagreement")
        self.dest.assert_not_called()

    def test_no_fingerprint_fails_closed(self):
        self.fp_match.return_value = ("", [], [])
        self.assertEqual(self._plan()["code"], "fingerprint_unavailable")

    def test_text_similarity_alone_never_counts(self):
        """Same title, same position, no audio proof: refused."""
        self.fp_match.return_value = ("", [], [REC])
        self.items[2]["title"] = "Exotic"
        self.assertFalse(self._plan()["ok"])

    def test_expected_recording_proves_a_replacement_for_a_wrong_audio_slot(self):
        """The slot's file is a different recording; the replacement is the
        expected one by AcoustID -- accepted."""
        self.fp_match.return_value = ("", [REC], [OTHER])
        res = self._plan(expected_recording_id=REC)
        self.assertTrue(res["ok"], res)

    def test_dangling_slot_needs_the_expected_recording(self):
        self.mp3.unlink()
        res = self._plan()
        self.assertTrue(res["ok"], res)
        self.fp_ids.assert_called_once_with(str(self.flac))
        self.dest.assert_not_called()
        self.fp_ids.return_value = [OTHER]
        self.assertEqual(self._plan()["code"], "fingerprint_disagreement")

    def test_slot_identity_mismatch_fails_closed(self):
        res = self._plan(expected_recording_id=OTHER)
        self.assertEqual(res["code"], "target_identity_mismatch")
        self.fp_match.assert_not_called()

    def test_structural_refusals(self):
        self.assertEqual(self._plan(1, 1)["code"], "invalid_items")
        self.assertEqual(self._plan(1, 99)["code"], "item_not_found")
        self.assertEqual(self._plan(2, 1)["code"], "target_not_in_album")
        self.flac.unlink()
        self.assertEqual(self._plan()["code"], "replacement_file_missing")

    def test_approve_and_apply_records_the_approver(self):
        res = self._plan()
        with mock.patch.object(composite_workflows, "apply_track_replacement", return_value={"ok": True}) as apply_:
            item_replacement.approve_and_apply(res["operation_id"], approved_by="reviewer", store=self.store)
        tx = self.store.get(res["operation_id"])
        self.assertEqual((tx["status"], tx["metadata"]["approved_by"]), ("Approved", "reviewer"))
        apply_.assert_called_once()

    def test_tracked_item_id_for_path(self):
        self.adapter.get_items.return_value = list(self.items.values())
        self.assertEqual(item_replacement.tracked_item_id_for_path(
            str(self.flac), adapter=self.adapter, abs_path=self.deps["abs_path"]), 2)
        self.assertEqual(item_replacement.tracked_item_id_for_path(
            str(self.root / "untracked.flac"), adapter=self.adapter, abs_path=self.deps["abs_path"]), 0)


def _row(iid, track=1, rid="", album=10):
    return {"id": iid, "disc": 1, "track": track, "mb_trackid": rid, "album_id": album, "path": f"/music/{iid}.flac"}


class ImportReconciliationPlanTests(unittest.TestCase):
    def _plan(self, existing, imported, hits):
        ident = album_identity({"mb_releasegroupid": RG}, {"mb_releasegroupid": RG})
        return plan_reconciliation(existing, imported, {(1, 1): {"mb_trackid": REC}}, ident,
                                   hits_fn=lambda row: hits.get(row["id"]))

    def test_keep_imported_becomes_one_canonical_replacement_and_the_import_does_not_move(self):
        wrong = [{"recording_id": OTHER, "score": 0.95}]
        right = [{"recording_id": REC, "score": 0.95}]
        plan = self._plan([_row(1, rid=REC)], [_row(2, album=11)], {1: wrong, 2: right})
        self.assertEqual(plan.mapping_pairs, [{"old_item_id": 1, "new_item_id": 2, "expected_recording_id": REC,
                                               "identity_source": "canonical_recording_identity"}])
        self.assertNotIn(2, plan.move_ids)

    def test_two_occupants_cannot_share_one_replacement_file(self):
        wrong = [{"recording_id": OTHER, "score": 0.95}]
        right = [{"recording_id": REC, "score": 0.95}]
        plan = self._plan([_row(1, rid=REC), _row(3, rid=REC)], [_row(2, album=11)], {1: wrong, 3: wrong, 2: right})
        self.assertEqual(plan.mapping_pairs, [])
        self.assertIn(2, plan.held_item_ids)


class CallerWiringTests(unittest.TestCase):
    """Every production replacement caller goes through the authority."""

    def _function(self, relpath, name):
        tree = ast.parse((ROOT / relpath).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment((ROOT / relpath).read_text(encoding="utf-8"), node)
        raise AssertionError(name)

    def test_import_merge_plans_through_the_authority_and_never_applies(self):
        src = self._function("backend/import_service.py", "_merge_imported_album_into_existing")
        self.assertIn("_item_replacement.plan_verified_replacement(", src)
        self.assertNotIn("bulk_import_replacement", src)
        self.assertNotIn("apply_track_replacement", src)

    def test_music_format_pipeline_plans_through_the_authority(self):
        src = self._function("backend/replacement_service.py", "_music_format_remove_original_after_replacement")
        self.assertIn("_item_replacement.plan_verified_replacement(", src)
        self.assertNotIn("apply_track_replacement", src)

    def test_route_and_review_use_the_authority(self):
        self.assertIn("_item_replacement.plan_verified_replacement(", self._function("routes_library.py", "item_replacement_plan"))
        self.assertIn("replacer.plan_verified_replacement(", self._function("backend/import_reconciliation.py", "resolve_review"))

    def test_the_obsolete_replacement_paths_are_gone(self):
        for relpath, names in (("backend/composite_workflows.py", ("plan_bulk_import_replacement", "apply_library_cleanup")),
                               ("backend/transaction_engine.py", ("create_track_replacement_plan",
                                                                  "execute_bulk_import_replacement_apply"))):
            text = (ROOT / relpath).read_text(encoding="utf-8")
            for name in names:
                self.assertNotIn(f"def {name}(", text, f"{relpath}: {name}")


if __name__ == "__main__":
    unittest.main()
