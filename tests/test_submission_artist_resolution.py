"""Tests for the Submissions page artist-resolution gate: auto MusicBrainz
artist search (reusing the existing _mb_artist_search_one helper), the new
blocking "artist" stage, manual-ID override, and the frontend gate that hides
the rest of the page until the artist is resolved or explicitly skipped.
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTES_SOURCE = (ROOT / "routes_submissions.py").read_text(encoding="utf-8")
SUBMISSIONS_SOURCE = (ROOT / "frontend" / "src" / "views" / "Submissions.tsx").read_text(encoding="utf-8")
CARD_SOURCE = (ROOT / "frontend" / "src" / "components" / "ArtistResolutionCard.tsx").read_text(encoding="utf-8")
TYPES_SOURCE = (ROOT / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")


def _function_source(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class BackendArtistMatchTests(unittest.TestCase):
    def setUp(self):
        self._fn = _function_source(
            ROUTES_SOURCE, "def _submission_artist_match(", "def _check("
        )

    def test_reuses_existing_mb_artist_search_helper(self):
        # Must not duplicate MusicBrainz artist-search logic that already
        # exists for artist-folder cleanup.
        self.assertIn("_mb_artist_search_one(name)", self._fn)
        self.assertNotIn("musicbrainz.org/ws/2/artist", ROUTES_SOURCE.replace(self._fn, ""))

    def test_requires_key_match_or_high_score(self):
        self.assertIn("_artist_folder_key(_s(match.get(\"name\"))) == _artist_folder_key(name)", self._fn)
        self.assertIn("_ARTIST_MATCH_MIN_SCORE", self._fn)

    def test_empty_name_short_circuits(self):
        self.assertIn("if not name:\n        return {}", self._fn)


class BackendArtistStageTests(unittest.TestCase):
    def test_artist_is_first_stage(self):
        self.assertIn(
            '_STAGE_ORDER = ["artist", "identify", "musicbrainz_prep", "attach_ids", "acoustid", "complete"]',
            ROUTES_SOURCE,
        )

    def test_artist_resolved_check_is_blocking(self):
        preflight_fn = _function_source(ROUTES_SOURCE, "def _submission_preflight(", "def _submission_stage_id(")
        block_start = preflight_fn.index('_check("artist_resolved"')
        block = preflight_fn[block_start:block_start + 260]
        self.assertIn('"artist"', block)
        self.assertNotIn("blocking=False", block)

    def test_old_low_priority_artist_entity_check_is_removed(self):
        # Regression: superseded by the blocking artist_resolved check above;
        # keeping both would show the same fact twice with different weight.
        self.assertNotIn('_check("artist_entity"', ROUTES_SOURCE)

    def test_musicbrainz_ready_gate_includes_artist_stage(self):
        self.assertIn(
            'mb_blocked = any(c["blocking"] and c["status"] == "fail" and c["stage"] in ("artist", "identify", "musicbrainz_prep") for c in checks)',
            ROUTES_SOURCE,
        )

    def test_current_stage_checks_artist_before_identify(self):
        fn = _function_source(ROUTES_SOURCE, "def _submission_current_stage(", "def _annotate_current_stage(")
        artist_pos = fn.index('c["stage"] == "artist"')
        identify_pos = fn.index('c["stage"] == "identify"')
        self.assertLess(artist_pos, identify_pos)

    def test_dismissal_treated_as_resolved(self):
        preflight_fn = _function_source(ROUTES_SOURCE, "def _submission_preflight(", "def _submission_stage_id(")
        self.assertIn("artist_resolved or artist_dismissed", preflight_fn)


class BackendManualArtistIdTests(unittest.TestCase):
    def test_manual_draft_id_short_circuits_live_search(self):
        route = _function_source(ROUTES_SOURCE, "def submission_target():", "def _draft_target_ref(")
        self.assertIn('manual_artist_id = _s((draft.get("published") or {}).get("artistId")', route)
        self.assertIn("elif manual_artist_id and _MB_UUID_RE.match(manual_artist_id):", route)

    def test_artist_match_computed_before_preflight_and_included_in_response(self):
        route = _function_source(ROUTES_SOURCE, "def submission_target():", "def _draft_target_ref(")
        match_pos = route.index("artist_match = _submission_artist_match(")
        preflight_pos = route.index("preflight = _submission_preflight(")
        response_pos = route.index('"artist_match": artist_match')
        self.assertLess(match_pos, preflight_pos)
        self.assertLess(preflight_pos, response_pos)

    def test_already_attached_mbid_skips_search_entirely(self):
        route = _function_source(ROUTES_SOURCE, "def submission_target():", "def _draft_target_ref(")
        self.assertIn('if _s(summary.get("mb_albumartistid")).strip():\n        artist_match', route)


class ArtistResolutionCardTests(unittest.TestCase):
    def test_create_on_musicbrainz_action_present(self):
        self.assertIn("musicbrainz.org/artist/create", CARD_SOURCE)

    def test_manual_id_and_dismiss_actions_present(self):
        self.assertIn("onSaveManualId", CARD_SOURCE)
        self.assertIn("onDismiss", CARD_SOURCE)
        self.assertIn("Skip for now", CARD_SOURCE)

    def test_manual_input_requires_a_uuid_before_enabling_use_button(self):
        self.assertIn("disabled={!extracted || saving}", CARD_SOURCE)


class SubmissionsArtistWiringTests(unittest.TestCase):
    def test_artist_stage_gate_defined(self):
        self.assertIn("const artistStageActive = target?.preflight.current_stage === 'artist';", SUBMISSIONS_SOURCE)

    def test_rest_of_page_hidden_while_artist_stage_active(self):
        # A loading check was added ahead of this gate in a later pass (see
        # tests/test_submission_artist_stage_polish.py) to avoid flashing
        # stale layout while switching review items.
        self.assertIn("artistStageActive ? (", SUBMISSIONS_SOURCE)
        self.assertIn("<ArtistResolutionCard", SUBMISSIONS_SOURCE)

    def test_found_artist_id_silently_saved_to_draft(self):
        self.assertIn("const foundArtistId = data.artist_match?.id;", SUBMISSIONS_SOURCE)
        self.assertIn("artistId: foundArtistId", SUBMISSIONS_SOURCE)

    def test_dismiss_and_manual_id_handlers_present(self):
        self.assertIn("function dismissArtistStep()", SUBMISSIONS_SOURCE)
        self.assertIn("function saveManualArtistId(mbid: string)", SUBMISSIONS_SOURCE)
        self.assertIn("artist_dismissed: true", SUBMISSIONS_SOURCE)


class SubmissionArtistTypesTests(unittest.TestCase):
    def test_artist_match_type_exposed(self):
        self.assertIn("export interface SubmissionArtistMatch {", TYPES_SOURCE)
        self.assertIn("artist_match?: SubmissionArtistMatch", TYPES_SOURCE)


if __name__ == "__main__":
    unittest.main()
