"""Canonical import reconciliation (ARCH-002 / ARCH-009).

Text-only evidence must never discard either file; album identity must be
proven by Release Group before any slot is reconciled."""

import json
import tempfile
import unittest
from pathlib import Path

from backend.import_reconciliation import (
    ReconciliationOutcome as O,
    album_identity,
    decide_slot,
    plan_reconciliation,
    record_reviews,
)

RG = "11111111-1111-1111-1111-111111111111"
RG2 = "55555555-5555-5555-5555-555555555555"
REL = "44444444-4444-4444-4444-444444444444"
TARGET = "22222222-2222-2222-2222-222222222222"
OTHER = "33333333-3333-3333-3333-333333333333"


def _row(item_id, *, rid="", title="Song", path=None, track=1):
    return {"id": item_id, "title": title, "disc": 1, "track": track, "length": 200.0,
            "mb_trackid": rid, "path": path or f"/music/a/{item_id}.flac"}


TARGET_TRACK = {"disc": 1, "track": 1, "title": "Song", "mb_trackid": TARGET, "duration_ms": 200000}


def _hits(mapping):
    def fn(row):
        return mapping.get(row["id"], [])
    return fn


def _hit(rid, score=95):
    return [{"mb_trackid": rid, "score": score}]


class SlotDecisionTests(unittest.TestCase):
    def test_identical_text_alone_keeps_both(self):
        d = decide_slot(_row(1), _row(2), TARGET_TRACK, hits_fn=_hits({}))
        self.assertEqual(d.outcome, O.KEEP_BOTH_REVIEW)
        self.assertFalse(d.outcome.destructive)

    def test_matching_disc_track_title_only_keeps_both(self):
        d = decide_slot(_row(1, title="Song"), _row(2, title="Song", rid=TARGET), TARGET_TRACK)
        self.assertEqual(d.outcome, O.KEEP_BOTH_REVIEW, "imported embedded ID is circular, not proof")

    def test_both_deterministically_expected_recording_keeps_existing(self):
        d = decide_slot(_row(1, rid=TARGET), _row(2), TARGET_TRACK,
                        hits_fn=_hits({1: _hit(TARGET), 2: _hit(TARGET)}))
        self.assertEqual(d.outcome, O.KEEP_EXISTING)

    def test_existing_fingerprint_wrong_and_import_confirmed_keeps_imported(self):
        d = decide_slot(_row(1, rid=OTHER), _row(2), TARGET_TRACK,
                        hits_fn=_hits({1: _hit(OTHER), 2: _hit(TARGET)}))
        self.assertEqual(d.outcome, O.KEEP_IMPORTED)

    def test_existing_recording_id_mismatch_alone_is_review(self):
        # Another edition of the same release group may use another Recording ID.
        d = decide_slot(_row(1, rid=OTHER), _row(2), TARGET_TRACK, hits_fn=_hits({2: _hit(TARGET)}))
        self.assertEqual(d.outcome, O.KEEP_BOTH_REVIEW)
        self.assertIn("existing_recording_id_differs_from_expected", d.reasons)

    def test_imported_fingerprint_wrong_is_conflict(self):
        d = decide_slot(_row(1, rid=TARGET), _row(2), TARGET_TRACK,
                        hits_fn=_hits({1: _hit(TARGET), 2: _hit(OTHER)}))
        self.assertEqual(d.outcome, O.CONFLICT)

    def test_ambiguous_fingerprint_is_review(self):
        d = decide_slot(_row(1, rid=TARGET), _row(2), TARGET_TRACK,
                        hits_fn=_hits({1: _hit(TARGET), 2: _hit(TARGET, 90) + _hit(OTHER, 89)}))
        self.assertEqual(d.outcome, O.KEEP_BOTH_REVIEW)

    def test_no_expected_recording_different_ids_is_conflict(self):
        d = decide_slot(_row(1, rid=OTHER), _row(2, rid=TARGET), None)
        self.assertEqual(d.outcome, O.CONFLICT)

    def test_no_expected_recording_otherwise_review(self):
        d = decide_slot(_row(1, rid=TARGET), _row(2, rid=TARGET), None)
        self.assertEqual(d.outcome, O.KEEP_BOTH_REVIEW)

    def test_missing_existing_file_is_replaced(self):
        d = decide_slot(_row(1, rid=TARGET), _row(2), TARGET_TRACK, exists_fn=lambda r: False)
        self.assertEqual(d.outcome, O.KEEP_IMPORTED)

    def test_missing_existing_file_but_import_is_wrong_song_is_conflict(self):
        d = decide_slot(_row(1), _row(2), TARGET_TRACK, exists_fn=lambda r: False,
                        hits_fn=_hits({2: _hit(OTHER)}))
        self.assertEqual(d.outcome, O.CONFLICT)

    def test_review_record_carries_canonical_evidence(self):
        d = decide_slot(_row(1), _row(2), TARGET_TRACK, hits_fn=_hits({}))
        payload = d.to_dict()
        for key in ("identity_proof", "confidence_state", "acoustid", "hard_conflicts", "review_reasons",
                    "recording_id", "path", "item_id"):
            self.assertIn(key, payload["existing"])
            self.assertIn(key, payload["imported"])
        self.assertEqual(payload["target_recording_id"], TARGET)


class AlbumIdentityTests(unittest.TestCase):
    def test_same_release_group(self):
        self.assertTrue(album_identity({"mb_releasegroupid": RG}, {"mb_releasegroupid": RG}).same_album)

    def test_release_group_from_authoritative_release(self):
        self.assertTrue(album_identity({"mb_releasegroupid": RG}, {}, RG).same_album)

    def test_different_release_groups(self):
        self.assertFalse(album_identity({"mb_releasegroupid": RG}, {"mb_releasegroupid": RG2}).same_album)

    def test_unknown_existing_release_group_is_not_inherited(self):
        ident = album_identity({"mb_releasegroupid": ""}, {"mb_releasegroupid": RG}, RG)
        self.assertFalse(ident.same_album)
        self.assertEqual(ident.reason, "existing_album_release_group_unknown")

    def test_release_id_never_substitutes_for_release_group(self):
        ident = album_identity({"mb_releasegroupid": "", "mb_albumid": REL}, {"mb_albumid": REL})
        self.assertFalse(ident.same_album)
        self.assertEqual(ident.release_group_id, "")


class PlanTests(unittest.TestCase):
    def test_unproven_album_moves_nothing(self):
        plan = plan_reconciliation([_row(1)], [_row(2), _row(3, track=2)], {},
                                   album_identity({}, {"mb_releasegroupid": RG}))
        self.assertEqual(plan.move_ids, [])
        self.assertEqual(sorted(plan.held_item_ids), [2, 3])

    def test_empty_slots_move_and_contested_text_only_slot_is_held(self):
        plan = plan_reconciliation([_row(1)], [_row(2), _row(3, track=2)], {(1, 1): TARGET_TRACK},
                                   album_identity({"mb_releasegroupid": RG}, {"mb_releasegroupid": RG}),
                                   hits_fn=_hits({}))
        self.assertEqual(plan.move_ids, [3])
        self.assertEqual(plan.held_item_ids, [2])
        self.assertEqual(plan.duplicate_rows, [])
        self.assertEqual(plan.replace_rows, [])

    def test_text_scores_never_produce_destructive_rows(self):
        ident = album_identity({"mb_releasegroupid": RG}, {"mb_releasegroupid": RG})
        for title in ("Song", "Song!", "Totally Different"):
            plan = plan_reconciliation([_row(1, title=title)], [_row(2)], {(1, 1): TARGET_TRACK}, ident,
                                       hits_fn=_hits({}))
            self.assertEqual(plan.duplicate_rows, [], title)
            self.assertEqual(plan.replace_rows, [], title)

    def test_forced_replacement_is_bookkeeping_only(self):
        plan = plan_reconciliation([_row(1)], [_row(2)], {(1, 1): TARGET_TRACK},
                                   album_identity({"mb_releasegroupid": RG}, {"mb_releasegroupid": RG}),
                                   forced_replace_ids=[1])
        self.assertEqual(plan.move_ids, [2])
        self.assertEqual(plan.replace_rows, [])
        self.assertEqual(plan.decisions, [])

    def test_review_records_persist_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviews.json"
            plan = plan_reconciliation([_row(1)], [_row(2)], {(1, 1): TARGET_TRACK},
                                       album_identity({"mb_releasegroupid": RG}, {"mb_releasegroupid": RG}),
                                       hits_fn=_hits({}))
            records = record_reviews(plan, existing_album_id=10, imported_album_id=11, release_id=REL,
                                     source_folder="/downloads/x", path=path)
            self.assertEqual(len(records), 1)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored[0]["outcome"], "keep_both_review")
            self.assertEqual(stored[0]["release_group_id"], RG)
            self.assertEqual(stored[0]["release_id"], REL)


class _FakeEngine:
    def __init__(self):
        self.calls = []

    def plan_existing_album_reconcile(self, payload):
        self.calls.append(("plan_reconcile", payload))
        return {"ok": True, "operation_id": f"rec{len(self.calls)}"}

    def apply_existing_album_reconcile(self, op):
        self.calls.append(("apply_reconcile", op))
        return {"ok": True}

    def plan_bulk_import_replacement(self, payload):
        self.calls.append(("plan_replace", payload))
        return {"ok": True, "operation_id": "rep1"}

    def apply_bulk_import_replacement(self, op):
        self.calls.append(("apply_replace", op))
        return {"ok": True}


class ResolveReviewTests(unittest.TestCase):
    def _store(self, tmp, album_proven=True):
        from backend.import_reconciliation import record_reviews
        path = Path(tmp) / "reviews.json"
        ident = album_identity({"mb_releasegroupid": RG}, {"mb_releasegroupid": RG if album_proven else RG2})
        plan = plan_reconciliation([_row(1)], [_row(2)], {(1, 1): TARGET_TRACK}, ident, hits_fn=_hits({}))
        record_reviews(plan, existing_album_id=10, imported_album_id=11, release_id=REL,
                       source_folder="/downloads/x", path=path)
        return path, json.loads(path.read_text(encoding="utf-8"))[0]["review_id"]

    def test_keep_both_changes_nothing(self):
        from backend.import_reconciliation import resolve_review
        with tempfile.TemporaryDirectory() as tmp:
            path, rid = self._store(tmp)
            engine = _FakeEngine()
            self.assertTrue(resolve_review(rid, "keep_both", engine, path=path)["ok"])
            self.assertEqual(engine.calls, [])
            self.assertEqual(json.loads(path.read_text())[0]["status"], "resolved")

    def test_keep_existing_retires_import_via_engine(self):
        from backend.import_reconciliation import resolve_review
        with tempfile.TemporaryDirectory() as tmp:
            path, rid = self._store(tmp)
            engine = _FakeEngine()
            self.assertTrue(resolve_review(rid, "keep_existing", engine, path=path)["ok"])
            plan = engine.calls[0][1]
            self.assertEqual(plan["dup_item_ids"], [2])
            self.assertEqual(plan["dup_details"][0]["survivor_item_ids"], [1])
            self.assertEqual(plan["move_item_ids"], [])

    def test_keep_imported_replaces_existing_then_moves_import(self):
        from backend.import_reconciliation import resolve_review
        with tempfile.TemporaryDirectory() as tmp:
            path, rid = self._store(tmp)
            engine = _FakeEngine()
            out = resolve_review(rid, "keep_imported", engine, path=path)
            self.assertTrue(out["ok"])
            self.assertTrue(out["moved_into_existing_album"])
            names = [c[0] for c in engine.calls]
            self.assertEqual(names, ["plan_replace", "apply_replace", "plan_reconcile", "apply_reconcile"])
            self.assertEqual(engine.calls[0][1]["mappings"][0]["identity_source"], "user_reviewed_reconciliation")

    def test_resolution_is_one_shot(self):
        from backend.import_reconciliation import resolve_review
        with tempfile.TemporaryDirectory() as tmp:
            path, rid = self._store(tmp)
            resolve_review(rid, "keep_both", _FakeEngine(), path=path)
            self.assertEqual(resolve_review(rid, "keep_existing", _FakeEngine(), path=path)["code"], "already_resolved")

    def test_unproven_album_can_only_be_dismissed(self):
        from backend.import_reconciliation import resolve_review
        with tempfile.TemporaryDirectory() as tmp:
            path, rid = self._store(tmp, album_proven=False)
            engine = _FakeEngine()
            self.assertEqual(resolve_review(rid, "keep_imported", engine, path=path)["code"], "album_identity_unproven")
            self.assertEqual(engine.calls, [])

    def test_invalid_choice(self):
        from backend.import_reconciliation import resolve_review
        with tempfile.TemporaryDirectory() as tmp:
            path, rid = self._store(tmp)
            self.assertEqual(resolve_review(rid, "delete_everything", _FakeEngine(), path=path)["code"], "invalid_choice")

if __name__ == "__main__":
    unittest.main()
