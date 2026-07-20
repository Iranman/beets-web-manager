"""Tests for the Discogs fallback: when MusicBrainz search finds nothing for
a folder, try Discogs, retry MusicBrainz with Discogs' cleaner artist/album
text, and if MB still has nothing, surface the Discogs match as evidence
instead of a bare "no candidates found" (Import Review AI matching flow;
Library repair reaches the same code path via the shared review queue when
a repair failure gets AI-suggested). Also covers the Submissions page
Duplicate Detection panel showing that same fallback for review items with
no MusicBrainz candidates at all.
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
SUBMISSIONS_SOURCE = (ROOT / "frontend" / "src" / "views" / "Submissions.tsx").read_text(encoding="utf-8")
TYPES_SOURCE = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")


def _function_source(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class DiscogsFallbackHelperTests(unittest.TestCase):
    def setUp(self):
        self._fn = _function_source(
            APP_SOURCE, "def _discogs_release_fallback_candidate(", "def _discogs_artist_discography("
        )

    def test_reuses_existing_discogs_search_not_a_new_api_call_site(self):
        self.assertIn("_discogs_track_search(album, artist, limit=5)", self._fn)

    def test_requires_token_and_at_least_one_query_term(self):
        self.assertIn("if not DISCOGS_TOKEN or not (artist or album):", self._fn)

    def test_rejects_unrelated_artist_match(self):
        self.assertIn("SequenceMatcher", self._fn)
        self.assertIn("if similarity < 0.55:", self._fn)


class DiscogsFallbackImportReviewWiringTests(unittest.TestCase):
    def setUp(self):
        self._fn = _function_source(
            APP_SOURCE, "def _ai_suggest_folder_internal(", "def _candidate_track_local_candidates("
        )

    def test_only_triggers_after_all_musicbrainz_attempts_are_exhausted(self):
        # The Discogs call must come after the folder-tracks last-resort MB
        # search, not instead of any of the MB attempts.
        last_resort_pos = self._fn.index("_mb_release_search_by_folder_tracks(")
        discogs_pos = self._fn.index("_discogs_release_fallback_candidate(")
        self.assertLess(last_resort_pos, discogs_pos)

    def test_discogs_match_retries_musicbrainz_with_cleaner_text(self):
        self.assertIn("retry_artist = _s(discogs_fallback.get(\"artist\")) or guessed_artist", self._fn)
        self.assertIn("mb_candidates = _mb_release_search(retry_album, retry_artist,", self._fn)

    def test_still_empty_after_retry_surfaces_discogs_candidate_not_silent_failure(self):
        self.assertIn('"discogs_candidate": discogs_fallback or None,', self._fn)
        self.assertIn("a possible match exists on Discogs", self._fn)

    def test_does_not_auto_import_from_discogs_alone(self):
        # MusicBrainz stays the source of truth: a Discogs-only match must
        # still return confidence "low" / mb_valid False, never trigger an
        # automatic import.
        block_start = self._fn.index('elif discogs_fallback:')
        block = self._fn[block_start:block_start + 900]
        self.assertIn('"confidence": "low"', block)
        self.assertIn('"mb_valid": False', block)


class DiscogsFallbackSubmissionsWiringTests(unittest.TestCase):
    def test_discogs_candidate_type_exposed(self):
        self.assertIn("discogs_candidate?: {", TYPES_SOURCE)

    def test_shown_only_when_no_musicbrainz_candidates(self):
        self.assertIn("const discogsCandidate = selectedItem?.suggestion?.discogs_candidate || null;", SUBMISSIONS_SOURCE)
        self.assertIn("{!selectedCandidateCount ? (discogsCandidate ? (", SUBMISSIONS_SOURCE)

    def test_reuses_existing_reference_url_pipeline_not_a_new_endpoint(self):
        # Must funnel through the already-built Discogs reference-URL
        # parser instead of adding a second, parallel Discogs-attach path.
        self.assertIn("reprocessReferenceUrl(discogsCandidate.discogs_url)", SUBMISSIONS_SOURCE)


if __name__ == "__main__":
    unittest.main()
