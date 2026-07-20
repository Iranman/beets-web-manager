"""Static coverage for display-only Import Review recording-ID evidence."""
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

    def test_recording_candidates_display_ids_confidence_and_release_context(self):
        panel = _source_between(IMPORT_REVIEW_SOURCE, "function RecordingIdEvidencePanel", "function initialMbid")
        self.assertIn("Suggested Recording ID", panel)
        self.assertIn("Recording title", panel)
        self.assertIn("Recording artist credit", panel)
        self.assertIn("Release ID", panel)
        self.assertIn("Release Group ID", panel)
        self.assertIn("AcoustID score/confidence", panel)
        self.assertIn("Same recording appears on", panel)
        self.assertIn("matching_local_release_found", panel)
        self.assertIn("musicBrainzRecordingUrl", IMPORT_REVIEW_SOURCE)
        self.assertIn("https://musicbrainz.org/recording/", IMPORT_REVIEW_SOURCE)

    def test_item_ai_suggest_response_is_kept_for_display(self):
        fn = _source_between(IMPORT_REVIEW_SOURCE, "const handleSuggest = useCallback(", "const startApply = useCallback(")
        self.assertIn("await suggestItem(item.item_id)", fn)
        self.assertIn("setSuggestions((current) => ({ ...current, [item.id]: response }));", fn)
        self.assertIn("selectedRecordingCandidate(response, item", fn)
        self.assertIn("Review the backend recording evidence before attaching", fn)

    def test_candidate_selection_only_copies_visible_recording_id(self):
        row = _source_between(IMPORT_REVIEW_SOURCE, "function RecordingCandidateRow", "function RecordingIdEvidencePanel")
        self.assertIn("Use Recording ID", row)
        self.assertIn("onUseCandidate(candidate.mb_trackid || '')", row)
        self.assertNotIn("attachRecording", row)
        self.assertNotIn("confirmed_conflicts", row)

    def test_reduced_work_does_not_make_frontend_authoritative(self):
        self.assertNotIn("RECORDING_REJECTS_STORAGE_KEY", IMPORT_REVIEW_SOURCE)
        self.assertNotIn("recordingAttachBlockReason", IMPORT_REVIEW_SOURCE)
        self.assertNotIn("confirmedRecordingConflicts", IMPORT_REVIEW_SOURCE)
        self.assertNotIn("recordingCandidateHasHardConflict", IMPORT_REVIEW_SOURCE)
        self.assertNotIn("candidate: selectedRecordingCandidate", IMPORT_REVIEW_SOURCE)
        run_apply = _source_between(IMPORT_REVIEW_SOURCE, "const runApply = useCallback(", "const requestDismiss = useCallback(")
        self.assertIn("started = await attachRecording(item.item_id, representativeId);", run_apply)

    def test_recording_rows_stay_out_of_album_match_state(self):
        self.assertIn("if (item.target_kind === 'item') return item.mb_trackid || '';", IMPORT_REVIEW_SOURCE)
        handler = _source_between(IMPORT_REVIEW_SOURCE, "const handleMbidChange = useCallback(", "const handleUseCandidate = useCallback(")
        self.assertIn("if (item.target_kind === 'item')", handler)
        self.assertIn("delete next[item.id];", handler)
        self.assertIn("delete targetPreviewKeysRef.current[item.id];", handler)


if __name__ == "__main__":
    unittest.main()