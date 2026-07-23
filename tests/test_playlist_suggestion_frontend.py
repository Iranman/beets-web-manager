"""Static-source assertions for the playlist suggestions frontend
migration -- same convention as tests/test_playlist_saved_discovery.py and
tests/test_playlist_backend_job.py: read the actual frontend source and
assert the properties that matter aren't silently regressed, without
needing a JS/TS test runner in this Python test suite.

Covers section 14 of the migration: the frontend displays backend-
computed decision fields, never calculates safety itself, submits only
the minimal (track_key/mb_trackid/item_id/decision_version) shape, and
handles the stale/in-progress error codes.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT_SOURCE = (ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
TYPES_SOURCE = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")
PAGE_SOURCE = (ROOT / "frontend" / "src" / "views" / "Playlists.tsx").read_text(encoding="utf-8")


class PlaylistSuggestionTypesTests(unittest.TestCase):
    def test_decision_shaped_types_are_declared(self):
        self.assertIn("interface PlaylistSuggestionDecision", TYPES_SOURCE)
        self.assertIn("action_eligibility: PlaylistActionEligibility", TYPES_SOURCE)
        self.assertIn("playlist_resolve_without_review: boolean", TYPES_SOURCE)
        self.assertIn("decision_version: string", TYPES_SOURCE)
        self.assertIn("decision: PlaylistSuggestionDecision", TYPES_SOURCE)

    def test_legacy_fields_are_marked_deprecated(self):
        region = TYPES_SOURCE[
            TYPES_SOURCE.index("export interface PlaylistTrackSuggestion"):
            TYPES_SOURCE.index("export interface PlaylistSuggestionRow")
        ]
        for field in ("confidence", "safe", "reason"):
            self.assertIn(f"@deprecated", region)
        self.assertIn("@deprecated use decision.confidence_score", region)
        self.assertIn("@deprecated use decision.action_eligibility.playlist_resolve_without_review", region)
        self.assertIn("@deprecated use decision.eligibility_reason", region)

    def test_minimal_submission_type_excludes_safety_fields(self):
        region = TYPES_SOURCE[
            TYPES_SOURCE.index("export interface PlaylistSuggestionSubmission"):
            TYPES_SOURCE.index("export interface PlaylistSuggestionOutcomeRow")
        ]
        self.assertIn("track_key: string", region)
        self.assertIn("decision_version: string", region)
        for forbidden in ("safe:", "confidence:", "action_eligibility:", "conflicts:", "warnings:"):
            self.assertNotIn(forbidden, region)

    def test_apply_response_has_bucketed_outcomes(self):
        region = TYPES_SOURCE[TYPES_SOURCE.index("export interface PlaylistApplySuggestionsResponse"):]
        region = region[:region.index("\n}\n") + 3]
        for bucket in ("applied", "unchanged", "skipped_review", "conflicts", "stale"):
            self.assertIn(bucket, region)


class PlaylistSuggestionClientTests(unittest.TestCase):
    def test_apply_endpoint_sends_only_suggestions_list(self):
        region = CLIENT_SOURCE[CLIENT_SOURCE.index("export function applySafePlaylistSuggestions"):]
        region = region[:region.index("\n}\n") + 3]
        self.assertIn("suggestions: PlaylistSuggestionSubmission[]", region)
        self.assertIn("{ suggestions }", region)
        self.assertNotIn("musicbrainz", region)


class PlaylistSuggestionPageTests(unittest.TestCase):
    def test_apply_handler_builds_minimal_submission(self):
        region = PAGE_SOURCE[
            PAGE_SOURCE.index("const handleApplySafeSuggestions"):
            PAGE_SOURCE.index("const handleRepairQualityRows")
        ]
        self.assertIn("playlist_resolve_without_review", region)
        self.assertIn("track_key: row.track_key", region)
        self.assertIn("decision_version: row.best?.decision_version", region)
        # Never recomputes or submits a client-side safety verdict.
        self.assertNotIn("safe: true", region)
        self.assertNotIn("row.best?.safe,", region)

    def test_apply_handler_reports_all_outcome_buckets(self):
        region = PAGE_SOURCE[
            PAGE_SOURCE.index("const handleApplySafeSuggestions"):
            PAGE_SOURCE.index("const handleRepairQualityRows")
        ]
        for bucket in ("result.applied", "result.unchanged", "result.skipped_review",
                      "result.conflicts", "result.stale"):
            self.assertIn(bucket, region)

    def test_apply_handler_refreshes_detail_after_apply_not_from_response(self):
        """The apply response itself no longer carries a full playlist-detail
        shape (matched/missing tracks) -- the frontend must re-fetch detail
        rather than assume the apply response has it."""
        region = PAGE_SOURCE[
            PAGE_SOURCE.index("const handleApplySafeSuggestions"):
            PAGE_SOURCE.index("const handleRepairQualityRows")
        ]
        self.assertIn("getPlaylistDetails(savedPlaylistName", region)
        self.assertIn("setParseResult(detail)", region)

    def test_safe_suggestion_count_reads_from_decision_not_legacy_flag(self):
        self.assertIn(
            "row.best?.decision?.action_eligibility?.playlist_resolve_without_review",
            PAGE_SOURCE,
        )


if __name__ == "__main__":
    unittest.main()
