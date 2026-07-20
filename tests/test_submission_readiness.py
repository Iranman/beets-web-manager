"""Tests for the Submissions page readiness redesign: normalized backend
check data model (id/severity/stage/group/action_type) and the compact,
stage-aware SubmissionReadinessCard replacing the old flat checklist grid.

Regression coverage for the root cause found while doing this work: the old
_check() always kept its `action` text regardless of whether the check
passed, so a passed check could still render red remediation text under it.
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTES_SOURCE = (ROOT / "routes_submissions.py").read_text(encoding="utf-8")
SUBMISSIONS_SOURCE = (ROOT / "frontend" / "src" / "views" / "Submissions.tsx").read_text(encoding="utf-8")
CARD_SOURCE = (ROOT / "frontend" / "src" / "components" / "SubmissionReadinessCard.tsx").read_text(encoding="utf-8")
TYPES_SOURCE = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")


def _function_source(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class BackendCheckNormalizationTests(unittest.TestCase):
    """Passed checks must not carry remediation; the previous bug let a
    passed check keep its 'action' text and render it as a correction."""

    def setUp(self):
        self._check_fn = _function_source(ROUTES_SOURCE, "def _check(", "def _submission_preflight(")

    def test_passed_check_clears_explanation_and_action(self):
        self.assertIn('explanation, action, action_type, action_target = "", "", "", ""', self._check_fn)

    def test_severity_derived_from_ok_and_blocking(self):
        self.assertIn('status, severity = "pass", "ready"', self._check_fn)
        self.assertIn('status, severity = "fail", "blocked"', self._check_fn)
        self.assertIn('status, severity = "warning", "needs_attention"', self._check_fn)

    def test_check_exposes_normalized_fields(self):
        for field in ("id", "severity", "stage", "group", "action_type", "action_target"):
            self.assertIn(f'"{field}"', self._check_fn)


class BackendStageTaxonomyTests(unittest.TestCase):
    def setUp(self):
        self._preflight_fn = _function_source(
            ROUTES_SOURCE, "def _submission_preflight(", "def _submission_stage_id("
        )

    def test_five_stage_order_defined(self):
        # "artist" was added as its own gating stage ahead of "identify"
        # (see tests/test_submission_artist_resolution.py) after this was
        # first written as a 5-stage taxonomy; still one flat ordered list.
        self.assertIn(
            '_STAGE_ORDER = ["artist", "identify", "musicbrainz_prep", "attach_ids", "acoustid", "complete"]',
            ROUTES_SOURCE,
        )

    def test_local_file_checks_are_identify_stage(self):
        for check_id in ("local_files_found", "files_accessible", "track_positions", "disc_positions"):
            self.assertIn(f'_check("{check_id}"', self._preflight_fn)

    def test_acoustid_checks_are_acoustid_stage_only(self):
        acoustid_ids = ("recording_mbids", "acoustid_api_key", "fpcalc_available", "pyacoustid_available", "chroma_plugin")
        for check_id in acoustid_ids:
            block_start = self._preflight_fn.index(f'_check("{check_id}"')
            block = self._preflight_fn[block_start:block_start + 450]
            self.assertIn('"acoustid"', block)

    def test_beets_import_required_at_musicbrainz_prep_not_identify(self):
        # Regression: importing into Beets must not gate basic review/metadata
        # editing (identify stage); it only matters once mbsubmit/attach runs.
        block_start = self._preflight_fn.index('_check("beets_imported"')
        block = self._preflight_fn[block_start:block_start + 200]
        self.assertIn('"musicbrainz_prep"', block)

    def test_optional_fields_are_non_blocking(self):
        # "artist_entity" was superseded by the blocking "artist_resolved"
        # gate (tests/test_submission_artist_resolution.py) -- artist
        # identity is no longer treated as optional.
        for check_id in ("release_date", "release_format", "duplicates_reviewed"):
            block_start = self._preflight_fn.index(f'_check("{check_id}"')
            block = self._preflight_fn[block_start:block_start + 450]
            self.assertIn("blocking=False", block)

    def test_musicbrainz_ready_gate_unchanged_by_restaging(self):
        # The actual gating booleans (which checks block MB/AcoustID
        # readiness) must not change just because check labels/stages were
        # reorganized for display. Later extended to also gate on the
        # "artist" stage once that was added (test_submission_artist_resolution.py).
        self.assertIn(
            'mb_blocked = any(c["blocking"] and c["status"] == "fail" and c["stage"] in ("artist", "identify", "musicbrainz_prep") for c in checks)',
            ROUTES_SOURCE,
        )


class BackendCurrentStageComputationTests(unittest.TestCase):
    def test_current_stage_falls_back_to_workflow_stage_text(self):
        fn = _function_source(ROUTES_SOURCE, "def _submission_stage_id(", "def _annotate_current_stage(")
        self.assertIn('if "complete" in text:', fn)
        self.assertIn('if "acoustid" in text:', fn)

    def test_identify_stage_checked_directly_not_only_from_text(self):
        # Regression: workflow_stage text alone can stay "Needs metadata"
        # (i.e. identify) purely because of a musicbrainz_prep-only check
        # like "not imported into Beets", which would hide that real
        # blocker from the readiness card since it only shows current or
        # earlier stage checks. current_stage must advance past "identify"
        # once identify-only checks are clean, independent of that text.
        fn = _function_source(ROUTES_SOURCE, "def _submission_current_stage(", "def _annotate_current_stage(")
        self.assertIn('c["stage"] == "identify"', fn)

    def test_annotate_sets_current_stage_relevant_per_check(self):
        fn = _function_source(ROUTES_SOURCE, "def _annotate_current_stage(", "def _resolve_submission_target(")
        self.assertIn("current_stage_relevant", fn)
        self.assertIn("current_stage_label", fn)

    def test_target_route_calls_annotate_after_workflow_stage_is_known(self):
        route = _function_source(ROUTES_SOURCE, "def submission_target():", "def _draft_target_ref(")
        stage_pos = route.index("workflow_stage")
        annotate_pos = route.index("_annotate_current_stage(")
        self.assertLess(stage_pos, annotate_pos)


class ReadinessCardComponentTests(unittest.TestCase):
    def test_component_exports_readiness_summary_helper(self):
        self.assertIn("export function readinessSummary(", CARD_SOURCE)

    def test_summary_scopes_to_current_stage_relevant_checks(self):
        fn = _function_source(CARD_SOURCE, "export function readinessSummary(", "function SeverityDot(")
        self.assertIn("current_stage_relevant !== false", fn)

    def test_passed_checks_render_no_explanation_or_action(self):
        # A passed (severity 'ready') row must never show explanation/action
        # text -- this is the frontend half of the pass/fail contradiction fix.
        self.assertIn("check.severity !== 'ready' && check.explanation", CARD_SOURCE)
        self.assertIn("check.severity !== 'ready' && check.action", CARD_SOURCE)

    def test_only_user_facing_severity_labels_are_shown(self):
        self.assertIn("'Blocked'", CARD_SOURCE)
        self.assertIn("'Needs attention'", CARD_SOURCE)
        self.assertNotIn(">{check.status}<", CARD_SOURCE)

    def test_groups_with_a_blocker_open_by_default(self):
        self.assertIn("c.group === group && c.severity === 'blocked'", CARD_SOURCE)

    def test_acoustid_notice_hidden_once_acoustid_stage_reached(self):
        self.assertIn("stage !== 'acoustid' && stage !== 'complete'", CARD_SOURCE)

    def test_view_all_checks_toggle_present(self):
        self.assertIn("View all checks", CARD_SOURCE)
        self.assertIn("Hide all checks", CARD_SOURCE)


class SubmissionsPageWiringTests(unittest.TestCase):
    def test_old_flat_checklist_grid_is_removed(self):
        self.assertNotIn("Preflight Checklist", SUBMISSIONS_SOURCE)
        self.assertNotIn("target.preflight.checks || []).map((check) =>", SUBMISSIONS_SOURCE)

    def test_readiness_card_is_rendered_with_shared_primary_action(self):
        self.assertIn("<SubmissionReadinessCard", SUBMISSIONS_SOURCE)
        self.assertIn("primaryAction={primary}", SUBMISSIONS_SOURCE)

    def test_footer_blocker_count_uses_same_readiness_summary_as_card(self):
        self.assertIn(
            "const footerBlockerCount = readinessSummary(target?.preflight ?? null).blockers.length;",
            SUBMISSIONS_SOURCE,
        )

    def test_primary_action_prioritizes_current_stage_blocker(self):
        fn = _function_source(SUBMISSIONS_SOURCE, "const primary = (() => {", "const footerBlockerCount")
        self.assertIn("const { firstBlocker } = readinessSummary(target.preflight);", fn)
        self.assertIn("if (firstBlocker) {", fn)

    def test_rescan_button_is_wired_to_a_real_refetch_not_disabled_noop(self):
        # Regression: the old "Rescan folder" primary button was permanently
        # disabled with a no-op action even when a folder genuinely needed
        # re-scanning after being fixed on disk.
        self.assertIn("const refetchTarget = useCallback(() => setRefreshNonce((n) => n + 1), []);", SUBMISSIONS_SOURCE)
        self.assertIn("label: 'Scan local files', disabled: !selectedItem, action: refetchTarget", SUBMISSIONS_SOURCE)

    def test_action_dispatch_covers_every_backend_action_type(self):
        fn = _function_source(SUBMISSIONS_SOURCE, "function handleCheckAction(", "async function resetDraft()")
        for action_type in ("rescan", "open_import_review", "open_settings", "view_setup_details"):
            self.assertIn(f"case '{action_type}':", fn)

    def test_section_anchors_exist_for_scroll_targeted_actions(self):
        for anchor in ("submission-metadata", "submission-tracks", "submission-duplicates", "submission-mb-handoff"):
            self.assertIn(f'id="{anchor}"', SUBMISSIONS_SOURCE)


class SubmissionTypesTests(unittest.TestCase):
    def test_preflight_check_type_has_normalized_fields(self):
        self.assertIn("severity: SubmissionCheckSeverity;", TYPES_SOURCE)
        self.assertIn("action_type?: SubmissionActionType;", TYPES_SOURCE)
        self.assertIn("current_stage_relevant?: boolean;", TYPES_SOURCE)


if __name__ == "__main__":
    unittest.main()
