"""Static regression checks for Import Review refresh and manual-MBID UX.

These assertions protect source structure only. The repository does not
currently include executable React component tests for this page.
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
CLIENT_SOURCE = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
TYPES_SOURCE = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")
IMPORT_REVIEW_SOURCE = (
    ROOT / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx"
).read_text(encoding="utf-8")


def _section(source: str, start: str, end: str) -> str:
    start_idx = source.index(start)
    end_idx = source.index(end, start_idx)
    return source[start_idx:end_idx]


class ImportReviewRefreshUxTests(unittest.TestCase):
    def test_queue_polling_no_longer_replaces_visible_state(self):
        self.assertNotIn("if (!document.hidden) void loadQueue(true);", IMPORT_REVIEW_SOURCE)
        self.assertIn("setQueueUpdatesAvailable(true)", IMPORT_REVIEW_SOURCE)
        self.assertIn("Review queue updates are available.", IMPORT_REVIEW_SOURCE)
        self.assertIn(">Load updates</Button>", IMPORT_REVIEW_SOURCE)

    def test_background_check_does_not_apply_queue_payload(self):
        effect = _section(
            IMPORT_REVIEW_SOURCE,
            "const id = window.setInterval(() => {",
            "return () => window.clearInterval(id);",
        )
        self.assertIn("getReviewQueue({ limit: REVIEW_QUEUE_LIMIT, origin_type: sourceFilter })", effect)
        self.assertIn("reviewQueueSnapshot(response.items ?? [], response.counts ?? {})", effect)
        self.assertIn("setQueueUpdatesAvailable(true)", effect)
        self.assertNotIn("setItems(", effect)
        self.assertNotIn("setMbids(", effect)
        self.assertNotIn("loadQueue(", effect)
        self.assertIn("isEditableTarget(document.activeElement)", effect)
        self.assertIn("confirmIntent", effect)

    def test_active_item_is_restored_by_review_item_id(self):
        self.assertIn("const [activeItemId, setActiveItemId] = useState('')", IMPORT_REVIEW_SOURCE)
        self.assertIn("visibleItems.findIndex((item) => item.id === activeItemId)", IMPORT_REVIEW_SOURCE)
        self.assertIn("setActiveItemId(visibleItems[clamped]?.id || '')", IMPORT_REVIEW_SOURCE)
        self.assertIn("const activeItem = activeIndex >= 0 ? visibleItems[activeIndex] ?? null : null", IMPORT_REVIEW_SOURCE)

    def test_per_item_state_survives_nonremoving_refresh(self):
        load_queue = _section(IMPORT_REVIEW_SOURCE, "const loadQueue = useCallback(", "useEffect(() => {")
        self.assertIn("for (const id of Object.keys(next)) if (!liveIds.has(id)) delete next[id]", load_queue)
        self.assertIn("setManualValidations((current) => {", load_queue)
        self.assertIn("function hasUnsavedManualId", IMPORT_REVIEW_SOURCE)
        self.assertIn("function reviewInteractionIsExpanded", IMPORT_REVIEW_SOURCE)
        selected_block = _section(load_queue, "setSelectedMatches((current) => {", "setTargetPreviews((current) => {")
        self.assertNotIn("for (const id of changedIds) delete next[id]", selected_block)
        mbid_block = _section(load_queue, "setMbids((current) => {", "    } catch (err) {")
        self.assertNotIn("for (const id of changedIds) delete next[id]", mbid_block)
        self.assertIn("for (const id of changedIds) delete targetPreviewKeysRef.current[id]", load_queue)


class ImportReviewManualIdBackendTests(unittest.TestCase):
    def test_manual_validation_route_and_parser_exist(self):
        self.assertIn('@app.post("/api/import-review/manual-id/validate")', APP_SOURCE)
        self.assertIn("def _parse_manual_musicbrainz_identifier", APP_SOURCE)
        self.assertIn("release-group|release|recording", APP_SOURCE)
        self.assertIn("This is not a valid MusicBrainz UUID or URL.", APP_SOURCE)
        self.assertIn("uuid_match = re.fullmatch(", APP_SOURCE)

    def test_album_manual_ids_use_backend_musicbrainz_validation(self):
        fn = _section(APP_SOURCE, "def _manual_review_validate_album_identifier", "def _manual_review_validate_recording_identifier")
        self.assertIn("_fetch_mb_release_tracklist(mbid, log)", fn)
        self.assertIn("_mb_release_group_candidates(release_group_id, log)", fn)
        self.assertIn("_resolve_release_group_to_release(", fn)
        self.assertIn("_candidate_track_comparison_payload(", fn)
        self.assertIn("_import_review_build_revalidated_match(", fn)
        self.assertIn("_manual_review_wrong_type_response(\"album\", \"Recording\")", fn)

    def test_recording_manual_ids_use_backend_recording_evidence(self):
        fn = _section(APP_SOURCE, "def _manual_review_validate_recording_identifier", '@app.post("/api/import-review/manual-id/validate")')
        self.assertIn("_fetch_mb_recording_details(mbid)", fn)
        self.assertIn("_enrich_track_ai_candidate(current, candidate, details)", fn)
        self.assertIn("_compact_track_ai_candidate(candidate)", fn)
        self.assertIn("selected_recording_candidate", fn)
        self.assertIn("recording_candidates", fn)

    def test_manual_match_reuses_review_selected_match_contract(self):
        fn = _section(APP_SOURCE, "def _manual_review_validate_album_identifier", "def _manual_review_validate_recording_identifier")
        self.assertIn("_import_review_revalidation_preflight(comparison, acoustic_preflight)", fn)
        self.assertIn("_import_review_build_revalidated_match(", fn)
        self.assertIn("selected_match[\"source\"] = \"manual\"", fn)
        self.assertNotIn("_manual_review_selected_match_from_comparison", APP_SOURCE)

        builder = _section(APP_SOURCE, "def _import_review_build_revalidated_match", "def _update_pending_review_revalidation")
        for field in (
            '"release_group_id"',
            '"representative_release_id"',
            '"track_mapping"',
            '"preflight_status"',
            '"preflight_reason"',
            '"identity_validated"',
            '"is_importable"',
            '"confidence_score"',
            '"confidence_level"',
            '"auto_fix_eligible"',
            '"source"',
        ):
            self.assertIn(field, builder)


class ImportReviewManualIdFrontendTests(unittest.TestCase):
    def test_client_and_types_expose_manual_validation_endpoint(self):
        self.assertIn("export interface ImportReviewManualIdPayload", TYPES_SOURCE)
        self.assertIn("export interface ImportReviewManualIdResponse", TYPES_SOURCE)
        self.assertIn("validateManualMusicBrainzId", CLIENT_SOURCE)
        self.assertIn("'/api/import-review/manual-id/validate'", CLIENT_SOURCE)

    def test_manual_id_controls_and_shortcut_are_present(self):
        self.assertIn("Enter MusicBrainz ID", IMPORT_REVIEW_SOURCE)
        self.assertIn("MusicBrainz Release or Release Group ID/URL", IMPORT_REVIEW_SOURCE)
        self.assertIn("MusicBrainz Recording ID or URL", IMPORT_REVIEW_SOURCE)
        self.assertIn("Validate ID", IMPORT_REVIEW_SOURCE)
        self.assertIn("Clear Manual ID", IMPORT_REVIEW_SOURCE)
        self.assertIn("Open in MusicBrainz", IMPORT_REVIEW_SOURCE)
        self.assertIn("Use This Release", IMPORT_REVIEW_SOURCE)
        self.assertIn("Choose Another Release", IMPORT_REVIEW_SOURCE)
        shortcut_pos = IMPORT_REVIEW_SOURCE.index("if (e.key.toLowerCase() === 'i')")
        start = IMPORT_REVIEW_SOURCE.rindex("const handler = (e: KeyboardEvent) => {", 0, shortcut_pos)
        end = IMPORT_REVIEW_SOURCE.index("window.addEventListener('keydown', handler);", shortcut_pos)
        keyboard = IMPORT_REVIEW_SOURCE[start:end]
        self.assertIn("if (confirmIntent || isEditableTarget(e.target)) return;", keyboard)
        self.assertIn("if (e.key.toLowerCase() === 'i')", keyboard)
        self.assertIn("handleFocusManualEntry()", keyboard)

    def test_manual_validation_updates_backend_owned_selected_match(self):
        fn = _section(IMPORT_REVIEW_SOURCE, "const handleValidateManualId = useCallback(", "useEffect(() => {")
        self.assertIn("validateManualMusicBrainzId({", fn)
        self.assertIn("musicbrainz_id: value", fn)
        self.assertIn("target_kind: item.target_kind", fn)
        self.assertIn("setSelectedMatches((current) => ({ ...current, [item.id]: match }))", fn)
        self.assertIn("setMbids((current) => ({ ...current, [item.id]: match.release_group_id", fn)
        self.assertIn("selected_recording_candidate", fn)
        self.assertIn("recording_candidates", fn)

    def test_manual_match_flows_to_target_preview_and_apply(self):
        self.assertNotIn("if (!selectedMatch || selectedMatch.source === 'manual') return '';", IMPORT_REVIEW_SOURCE)
        self.assertNotIn("if (!importLike || !selectedMatch || selectedMatch.source === 'manual') return '';", IMPORT_REVIEW_SOURCE)
        self.assertNotIn("if (!selectedMatch || selectedMatch.source === 'manual') return false", IMPORT_REVIEW_SOURCE)
        self.assertNotIn("selectedMatch && selectedMatch.source !== 'manual'", IMPORT_REVIEW_SOURCE)
        preview_effect = _section(IMPORT_REVIEW_SOURCE, "// Target path preview", "// Auto-start Find Match")
        self.assertIn("if (!selectedMatch) return;", preview_effect)
        self.assertIn("previewImportTarget({", preview_effect)
        run_apply = _section(IMPORT_REVIEW_SOURCE, "const runApply = useCallback(", "const requestDismiss = useCallback(")
        self.assertIn("mb_albumid: representativeId", run_apply)
        self.assertIn("mb_releasegroupid: releaseGroupId || undefined", run_apply)
        self.assertIn("track_mapping: sm?.track_mapping", run_apply)
        self.assertIn("started = await attachRecording(item.item_id, representativeId);", run_apply)



class ImportReviewMatchingSafetyUiTests(unittest.TestCase):
    def test_api_types_include_backend_matching_safety_contract(self):
        for field in (
            "ai_available?: boolean",
            "ai_unavailable_reason?: string",
            "matching_method?: string",
            "warnings?: string[]",
            "action_eligibility?: unknown",
            "eligibility_reason?: string",
            "matching_contract?: Record<string, unknown>",
            "fingerprint_conflicts?: string[]",
            "recording_id_conflicts?: string[]",
            "title_mismatch_warnings?: string[]",
            "required_review?: boolean",
            "selected_match?: ImportReviewSelectedMatch",
        ):
            self.assertIn(field, TYPES_SOURCE)

    def test_review_page_displays_backend_safety_without_recomputing_authority(self):
        self.assertIn("function MatchingSafetyPanel", IMPORT_REVIEW_SOURCE)
        self.assertIn("data-import-review-matching-safety", IMPORT_REVIEW_SOURCE)
        for label in (
            "Backend matching safety",
            "Matching source",
            "AI availability",
            "Eligibility decision",
            "Eligibility reason",
            "Required review",
            "AcoustID corroboration",
            "Warnings",
        ):
            self.assertIn(label, IMPORT_REVIEW_SOURCE)
        self.assertIn("selectedMatch?.action_eligibility ?? suggestion?.action_eligibility ?? response?.action_eligibility", IMPORT_REVIEW_SOURCE)
        self.assertIn("contract?.fingerprint_conflicts", IMPORT_REVIEW_SOURCE)
        self.assertIn("contract?.recording_id_conflicts", IMPORT_REVIEW_SOURCE)
        self.assertIn("contract?.title_mismatch_warnings", IMPORT_REVIEW_SOURCE)
        self.assertNotIn("setSelectedMatches((current) => ({ ...current, [item.id]: buildCandidateSelectedMatch", IMPORT_REVIEW_SOURCE)

    def test_find_match_consumes_deterministic_selected_match_and_ai_warning(self):
        fn = _section(IMPORT_REVIEW_SOURCE, "const handleSuggest = useCallback(", "const startApply = useCallback(")
        self.assertIn("const backendSelectedMatch = response.selected_match as unknown as SelectedMatch | undefined", fn)
        self.assertIn("setSelectedMatches((current) => ({ ...current, [item.id]: backendSelectedMatch }))", fn)
        self.assertIn("aiAvailable === false ? 'warning' : 'success'", fn)
        self.assertIn("Matched using MusicBrainz and AcoustID", fn)
        self.assertIn("optionalAiWarning(message)", fn)
        self.assertIn("AI ranking was skipped because no provider is configured.", IMPORT_REVIEW_SOURCE)
if __name__ == "__main__":
    unittest.main()
