"""Static structural checks for display-only Import Review recording-ID evidence.

These checks protect source-level invariants only. They are not executable React
component interaction tests.
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMPORT_REVIEW_SOURCE = (ROOT / "frontend" / "src" / "features" / "importReview" / "ImportReviewPage.tsx").read_text(encoding="utf-8")


def _source_between(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class ImportReviewRecordingIdFrontendTests(unittest.TestCase):
    def test_recording_id_panel_renders_backend_evidence(self):
        panel = _source_between(IMPORT_REVIEW_SOURCE, "function RecordingIdEvidencePanel", "function initialMbid")
        self.assertIn("Local file evidence", panel)
        self.assertIn("Current title tag", panel)
        self.assertIn("Current artist tag", panel)
        self.assertIn("Current album tag", panel)
        self.assertIn("Current album artist tag", panel)
        self.assertIn("Current year/date tag", panel)
        self.assertIn("Current track number", panel)
        self.assertIn("Fingerprint status", panel)
        self.assertIn("Suggested MusicBrainz / AcoustID match", panel)
        self.assertIn("Backend match evidence", panel)
        self.assertIn("Backend recording candidates", panel)

    def test_candidate_labels_separate_backend_form_and_fallback_states(self):
        self.assertNotIn("index === 0 ? 'Backend selected candidate'", IMPORT_REVIEW_SOURCE)
        label_fn = _source_between(IMPORT_REVIEW_SOURCE, "function recordingCandidateRowLabel", "function RecordingCandidateRow")
        self.assertIn("if (isBackendSelected) return 'Backend selected candidate';", label_fn)
        self.assertIn("if (matchesEnteredId) return 'Candidate matching entered ID';", label_fn)
        self.assertIn("if (isFirstBackendCandidate) return 'First backend candidate';", label_fn)
        self.assertIn("return 'Backend alternate candidate';", label_fn)
        self.assertNotIn("index", label_fn)
        panel = _source_between(IMPORT_REVIEW_SOURCE, "function RecordingIdEvidencePanel", "function initialMbid")
        self.assertIn("const backendSelectedCandidate = backendSelectedRecordingCandidate(response, item);", panel)
        self.assertIn("const candidateMatchingEnteredId = recordingCandidateMatchingEnteredId(candidates, mbid);", panel)
        self.assertIn("const displayCandidate = backendSelectedCandidate || candidateMatchingEnteredId || candidates[0];", panel)
        self.assertIn("isBackendSelected={isBackendSelected}", panel)
        self.assertIn("matchesEnteredId={matchesEnteredId}", panel)
        self.assertIn("isFirstBackendCandidate={index === 0}", panel)

    def test_backend_selection_is_explicit_not_array_order(self):
        helper = _source_between(IMPORT_REVIEW_SOURCE, "function backendSelectedRecordingCandidate", "function recordingCandidateMatchingEnteredId")
        self.assertIn("suggestion?.selected_recording_candidate", helper)
        self.assertIn("response?.evidence?.selected_recording_candidate", helper)
        self.assertIn("response?.selected_candidate", helper)
        self.assertIn("confirmedCandidateEvidence(suggestion, explicitCandidates)", helper)
        self.assertIn("isMusicBrainzUuid(suggestion?.mb_trackid)", helper)
        self.assertNotIn("candidates[0]", helper)
        self.assertNotIn("index", helper)

    def test_candidate_source_precedence_does_not_mix_stale_item_evidence(self):
        adapter = _source_between(IMPORT_REVIEW_SOURCE, "// Temporary compatibility adapter", "function backendSelectedRecordingCandidate")
        self.assertIn("prefer the newest AI-suggest response", adapter)
        self.assertIn("fall back to persisted item evidence only", adapter)
        self.assertIn("const responseCandidates = compactRecordingCandidates(responseRecordingCandidateInputs(response));", adapter)
        self.assertIn("if (responseCandidates.length) return responseCandidates;", adapter)
        self.assertIn("return compactRecordingCandidates(itemEvidenceRecordingCandidateInputs(item));", adapter)

    def test_recording_candidates_display_ids_metric_and_release_context(self):
        panel = _source_between(IMPORT_REVIEW_SOURCE, "function RecordingIdEvidencePanel", "function initialMbid")
        self.assertIn("Suggested Recording ID", panel)
        self.assertIn("Recording title", panel)
        self.assertIn("Recording artist credit", panel)
        self.assertIn("Release ID", panel)
        self.assertIn("Release Group ID", panel)
        self.assertIn("Same recording appears on", panel)
        self.assertIn("matching_local_release_found", panel)
        self.assertIn("musicBrainzRecordingUrl", IMPORT_REVIEW_SOURCE)
        self.assertIn("https://musicbrainz.org/recording/", IMPORT_REVIEW_SOURCE)

    def test_metric_labels_are_honest_about_score_sources(self):
        metric = _source_between(IMPORT_REVIEW_SOURCE, "function recordingCandidateMetric", "function recordingCandidateRowLabel")
        self.assertNotIn("AcoustID score/confidence", IMPORT_REVIEW_SOURCE)
        self.assertIn("candidate.confidence_score", metric)
        self.assertIn("return { label: 'Match confidence', ...normalizedMetricValue(candidate.confidence_score) };", metric)
        self.assertIn("candidate.match_total", metric)
        self.assertIn("return { label: 'Match confidence', ...normalizedMetricValue(candidate.match_total) };", metric)
        self.assertIn("candidate.acoustid_score", metric)
        self.assertIn("return { label: 'AcoustID score', ...normalizedMetricValue(candidate.acoustid_score) };", metric)
        self.assertIn("candidate.score", metric)
        self.assertIn("return { label: 'Candidate score', ...rawMetricValue(candidate.score) };", metric)
        self.assertIn("return { label: 'Confidence', ...rawMetricValue(candidate.confidence) };", metric)
        self.assertIn("score >= 0 && score <= 1", metric)
        self.assertNotIn("Math.round(score)}%", metric)

    def test_item_ai_suggest_response_is_kept_for_display(self):
        fn = _source_between(IMPORT_REVIEW_SOURCE, "const handleSuggest = useCallback(", "const startApply = useCallback(")
        self.assertIn("await suggestItem(item.item_id)", fn)
        self.assertIn("setSuggestions((current) => ({ ...current, [item.id]: response }));", fn)
        self.assertIn("backendSelectedRecordingCandidate(response, item)", fn)
        self.assertNotIn("candidates[0]", fn)
        self.assertIn("Review the backend recording evidence before attaching", fn)

    def test_candidate_selection_only_copies_visible_recording_id(self):
        row = _source_between(IMPORT_REVIEW_SOURCE, "function RecordingCandidateRow", "function RecordingIdEvidencePanel")
        self.assertIn("Use Recording ID", row)
        self.assertIn("onUseCandidate(candidate.mb_trackid || '')", row)
        self.assertIn("disabled={!canUseRecordingId}", row)
        self.assertNotIn("attachRecording", row)
        self.assertNotIn("confirmed_conflicts", row)

    def test_reduced_work_does_not_make_frontend_authoritative(self):
        self.assertNotIn("RECORDING_REJECTS_STORAGE_KEY", IMPORT_REVIEW_SOURCE)
        self.assertNotIn("recordingAttachBlockReason", IMPORT_REVIEW_SOURCE)
        self.assertNotIn("confirmedRecordingConflicts", IMPORT_REVIEW_SOURCE)
        self.assertNotIn("recordingCandidateHasHardConflict", IMPORT_REVIEW_SOURCE)
        self.assertNotIn("candidate: selectedRecordingCandidate", IMPORT_REVIEW_SOURCE)
        self.assertNotIn("candidate: backendSelectedCandidate", IMPORT_REVIEW_SOURCE)
        self.assertNotIn("confirmed_conflicts", IMPORT_REVIEW_SOURCE)
        run_apply = _source_between(IMPORT_REVIEW_SOURCE, "const runApply = useCallback(", "const requestDismiss = useCallback(")
        self.assertIn("started = await attachRecording(item.item_id, representativeId);", run_apply)
        self.assertNotIn("started = await attachRecording(item.item_id, representativeId, {", run_apply)

    def test_recording_rows_stay_out_of_album_match_state(self):
        self.assertIn("if (item.target_kind === 'item') return item.mb_trackid || '';", IMPORT_REVIEW_SOURCE)
        handler = _source_between(IMPORT_REVIEW_SOURCE, "const handleMbidChange = useCallback(", "const handleUseCandidate = useCallback(")
        self.assertIn("if (item.target_kind === 'item')", handler)
        self.assertIn("delete next[item.id];", handler)
        self.assertIn("delete targetPreviewKeysRef.current[item.id];", handler)


if __name__ == "__main__":
    unittest.main()