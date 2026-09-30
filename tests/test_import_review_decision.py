"""ARCH-005: the Import Review action decision is backend-owned.

backend/import_review_decision.py is the authority; the page mirrors it in
one pure module that runs against the same cases
(frontend/tests/importReviewDecision.test.tsx), and the apply path asks the
backend for the final verdict.
"""

import copy
import json
import re
import unittest
from pathlib import Path

import app as app_module
import routes_import
from backend import import_review_decision as decision

ROOT = Path(__file__).resolve().parent.parent
CASES = json.loads((ROOT / "frontend" / "tests" / "fixtures" / "import_review_decision_cases.json")
                   .read_text(encoding="utf-8"))
PAGE = (ROOT / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx").read_text(encoding="utf-8")
MIRROR = (ROOT / "frontend" / "src" / "features" / "importReview" / "importReviewDecision.ts").read_text(encoding="utf-8")

RG = "11111111-2222-3333-4444-555555555555"
REL = "66666666-7777-8888-9999-aaaaaaaaaaaa"


def _case(name):
    return copy.deepcopy(next(c for c in CASES if c["name"] == name))


class SharedCaseTests(unittest.TestCase):
    def test_the_recorded_cases_are_what_the_authority_returns(self):
        self.assertGreaterEqual(len(CASES), 40)
        for case in CASES:
            with self.subTest(case=case["name"]):
                self.assertEqual(decision.decide(copy.deepcopy(case["input"])), case["expected"])

    def test_the_fixture_is_current_with_its_builder(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "build_cases", ROOT / "tests" / "fixtures" / "build_import_review_decision_cases.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        self.assertEqual([c["name"] for c in CASES], [c[0] for c in builder.CASES])

    def test_every_decision_carries_the_full_payload(self):
        keys = {"match_bucket", "blocked", "ready", "can_apply", "apply_block_reason", "block_reason", "next_action",
                "action_label", "selected_source_files"}
        for case in CASES:
            self.assertEqual(set(case["expected"]), keys, case["name"])


class DecisionRuleTests(unittest.TestCase):
    """Hand-written expectations, independent of the generated fixture."""

    def _decide(self, name):
        return decision.decide(_case(name)["input"])

    def test_a_verified_selection_is_ready_and_may_apply(self):
        out = self._decide("ready import")
        self.assertEqual((out["ready"], out["blocked"], out["can_apply"], out["apply_block_reason"]),
                         (True, False, True, ""))
        self.assertEqual(out["action_label"], "Import with ID")
        self.assertEqual(len(out["selected_source_files"]), 3)

    def test_an_unsafe_target_preview_blocks_and_explains_the_next_step(self):
        out = self._decide("preview unsafe with reason")
        self.assertFalse(out["can_apply"])
        self.assertEqual(out["apply_block_reason"],
                         "Import blocked by target path preview: target folder holds another release.")
        self.assertEqual(out["next_action"], "Fix the target path conflict, then retry this item.")
        self.assertEqual((out["blocked"], out["ready"]), (True, False))

    def test_the_preview_must_be_present_and_agree_with_the_selection(self):
        self.assertIn("until the target path preview is available", self._decide("preview missing")["apply_block_reason"])
        self.assertIn("until the target path preview finishes", self._decide("preview loading")["apply_block_reason"])
        self.assertEqual(self._decide("preview count differs from selection")["apply_block_reason"],
                         "Import blocked: selected file count does not match target preview.")

    def test_identity_and_preflight_gates(self):
        self.assertIn("out of sync", self._decide("field and match out of sync")["apply_block_reason"])
        self.assertEqual(self._decide("identity not validated")["action_label"], "Import blocked")
        self.assertEqual(self._decide("identity not validated")["selected_source_files"], [])
        self.assertEqual(self._decide("preflight failed with reason")["apply_block_reason"],
                         "Only 2 of 12 tracks matched.")
        self.assertIn("until preflight is refreshed", self._decide("preflight stale")["apply_block_reason"])

    def test_a_file_that_conflicts_at_the_target_is_never_selected(self):
        out = self._decide("conflicting target file is not imported")
        self.assertEqual(out["selected_source_files"], ["/downloads/a/02.flac", "/downloads/a/03.flac"])
        self.assertTrue(out["can_apply"])

    def test_an_existing_album_is_a_repair_not_gated_by_the_import_preview(self):
        out = self._decide("repair of an existing album")
        self.assertEqual((out["can_apply"], out["action_label"]), (True, "Complete Verified Repair"))
        self.assertFalse(self._decide("repair with failed preflight")["can_apply"])

    def test_stored_block_state_wins_the_bucket_but_audio_mismatch_and_skipped_are_never_blocked(self):
        stored = self._decide("stored blocked reason, no match")
        self.assertEqual((stored["match_bucket"], stored["blocked"]), ("blocked", True))
        # No match selected yet: selecting one is the next step, ahead of the stored hint.
        self.assertIn("Select the visible MusicBrainz match first", stored["apply_block_reason"])
        # When the action itself is allowed, the stored reason and its stored hint are shown.
        repair = decision.decide({"item": {"type": "pending_ai", "existing_album_id": 42,
                                           "blocked_reason": "Format policy rejected this source.",
                                           "blocked_next_action": "Pick another source."},
                                  "mbid": RG, "selected_match": None, "target_preview_state": None})
        self.assertEqual((repair["can_apply"], repair["block_reason"], repair["next_action"]),
                         (True, "Format policy rejected this source.", "Pick another source."))
        self.assertEqual(self._decide("blocked status key")["match_bucket"], "blocked")
        mismatch = self._decide("audio mismatch evidence")
        self.assertEqual((mismatch["match_bucket"], mismatch["blocked"], mismatch["ready"]),
                         ("audio_mismatch", False, False))
        skipped = self._decide("skipped item")
        self.assertEqual((skipped["blocked"], skipped["ready"]), (False, False))

    def test_an_id_is_required(self):
        self.assertEqual(self._decide("no ID entered, album")["apply_block_reason"],
                         "Enter or select a MusicBrainz Release Group ID first.")
        self.assertEqual(self._decide("no ID entered, item target")["apply_block_reason"],
                         "Enter or select a MusicBrainz recording ID first.")

    def test_decide_is_pure(self):
        entry = _case("ready import")["input"]
        before = copy.deepcopy(entry)
        decision.decide(entry)
        self.assertEqual(entry, before)


class DecisionRouteTests(unittest.TestCase):
    def _post(self, body):
        with app_module.app.test_request_context("/api/import-review/decision", method="POST", json=body):
            out = routes_import.import_review_decision()
        response, status = out if isinstance(out, tuple) else (out, 200)
        return status, response.get_json()

    def test_returns_one_decision_per_entry_in_order(self):
        entries = [_case("ready import")["input"], _case("preview loading")["input"]]
        status, body = self._post({"entries": entries})
        self.assertEqual(status, 200)
        self.assertEqual([d["can_apply"] for d in body["decisions"]], [True, False])
        self.assertEqual(body["decisions"][0], _case("ready import")["expected"])

    def test_rejects_malformed_bodies(self):
        for body in ({}, {"entries": []}, {"entries": "x"}, {"entries": [{"mbid": RG}]},
                     {"entries": [{"item": "not an object"}]}, {"entries": [{"item": {}}] * 201}):
            with self.subTest(body=str(body)[:60]):
                self.assertEqual(self._post(body)[0], 400)

    def test_the_route_is_a_pure_read(self):
        import inspect
        source = inspect.getsource(routes_import.import_review_decision)
        for forbidden in ("jobs.start_python", "composite_workflows", "beets_adapter", "open(", "_ur."):
            self.assertNotIn(forbidden, source)


class FrontendOwnershipTests(unittest.TestCase):
    """The page displays decisions; it does not define the rules."""

    RULES = ("itemMatchBucket", "targetPreviewBlockReason", "applyBlockReason", "storedBlockedReason",
             "shouldShowBlockedBucket", "shouldShowReadyBucket", "blockedActionHint", "actionLabel",
             "selectedImportSourceFiles", "hasAudioMismatchEvidence")

    def test_no_decision_rule_is_defined_in_the_page(self):
        for name in self.RULES:
            with self.subTest(rule=name):
                self.assertIsNone(re.search(rf"^(export )?function {name}\(", PAGE, re.M))
                self.assertIsNotNone(re.search(rf"^export function {name}\(", MIRROR, re.M))

    def test_the_mirror_is_pure(self):
        imports = [line for line in MIRROR.splitlines() if line.startswith("import ")]
        self.assertEqual(imports, ["import type { ImportTargetPreviewResponse, ReviewEvidence, ReviewItem } "
                                   "from '../../api/types';"])
        for forbidden in ("fetch(", "apiJson", "useState", "useEffect", "document.", "window."):
            self.assertNotIn(forbidden, MIRROR)

    def test_the_apply_path_asks_the_backend_and_fails_closed(self):
        start = PAGE.index("const runApply = useCallback(async (item: ReviewItem, mbid: string) => {")
        body = PAGE[start:PAGE.index("const representativeId", start)]
        self.assertIn("await decideImportReviewRemote([{", body)
        self.assertIn("if (!verdict || !verdict.can_apply) {", body)
        self.assertIn("nothing was started", body)
        self.assertEqual(body.count("return;"), 3)  # already running, blocked verdict, unreachable backend
        self.assertLess(body.index("await decideImportReviewRemote"), body.index("const representativeId")
                        if "const representativeId" in body else len(body))
        client = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        self.assertIn("'/api/import-review/decision'", client)

    def test_mirror_and_authority_share_their_constants(self):
        for status in decision.IMPORTABLE_TRACK_STATUSES:
            self.assertIn(f"'{status}'", MIRROR)
        for key in decision.FAILED_STATUS_KEYS | decision.BLOCKED_STATUS_KEYS:
            self.assertIn(f"'{key}'", MIRROR)


if __name__ == "__main__":
    unittest.main()
