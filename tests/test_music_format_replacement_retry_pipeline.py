import tempfile
import unittest
from pathlib import Path

from backend.audio_preferences import mark_needs_replacement


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")


def function_source(name: str) -> str:
    start = APP_SOURCE.index(f"def {name}(")
    end = APP_SOURCE.find("\ndef ", start + 5)
    if end == -1:
        end = len(APP_SOURCE)
    return APP_SOURCE[start:end]


class ReplacementRetryPipelineTests(unittest.TestCase):
    def test_existing_internal_album_id_path_still_uses_replacement_mode(self):
        self.assertIn("Replacement mode", APP_SOURCE)
        self.assertIn("existing_album_id", function_source("_music_format_replacement_payload"))
        self.assertIn("wanted_tracks", function_source("_music_format_replacement_payload"))

    def test_recording_id_without_internal_album_id_can_build_payload(self):
        payload_source = function_source("_music_format_replacement_payload")
        verifier_source = function_source("_music_format_find_verified_replacement")
        self.assertIn('if mb_trackid:', verifier_source)
        self.assertIn('WHERE lower(mb_trackid)=?', verifier_source)
        self.assertIn('"replace_existing": True', payload_source)

    def test_verifier_checks_imported_rows_outside_original_album(self):
        verifier_source = function_source("_music_format_find_verified_replacement")
        self.assertIn('candidates: List[sqlite3.Row] = []', verifier_source)
        self.assertIn('seen_candidate_ids = set()', verifier_source)
        self.assertIn('WHERE lower(mb_trackid)=?', verifier_source)
        self.assertIn('FROM items WHERE album_id=?', verifier_source)
        self.assertIn('ORDER BY id DESC LIMIT 60', verifier_source)

    def test_recording_match_can_ignore_original_track_number(self):
        verifier_source = function_source("_music_format_find_verified_replacement")
        self.assertIn('candidate_matches_recording = bool(mb_trackid and candidate_mbid == mb_trackid)', verifier_source)
        self.assertIn('not candidate_matches_recording and (candidate_disc, candidate_track) != (disc, track)', verifier_source)
        self.assertIn('if not candidate_matches_recording:', verifier_source)

    def test_release_group_is_canonical_album_identity(self):
        resolver_source = function_source("_music_format_resolve_replacement_identity")
        payload_source = function_source("_music_format_replacement_payload")
        self.assertIn('resolved["mb_releasegroupid"] = rgid', resolver_source)
        self.assertIn('https://musicbrainz.org/release-group/{rgid}', payload_source)
        self.assertNotIn('missing album id, MusicBrainz release id, or track number', function_source("_music_format_replace_rows"))

    def test_artist_title_only_runs_identity_resolution_before_search(self):
        replace_source = function_source("_music_format_replace_rows")
        self.assertIn('status="Resolving identity"', replace_source)
        self.assertIn('_music_format_resolve_replacement_identity(row, log)', replace_source)
        self.assertIn('Searching replacement sources', replace_source)

    def test_uncertain_album_context_needs_review_before_download(self):
        replace_source = function_source("_music_format_replace_rows")
        payload_source = function_source("_music_format_replacement_payload")
        self.assertIn('album_context = _s(resolved.get("mb_releasegroupid") or resolved.get("mb_albumid") or "").strip()', replace_source)
        self.assertIn('stage="album_context_resolution"', replace_source)
        self.assertIn('Could not confidently identify album context', replace_source)
        self.assertNotIn('row.get("album") or row.get("title")', payload_source)

    def test_acoustid_resolved_orphan_track_uses_recording_identity(self):
        resolver_source = function_source("_music_format_resolve_replacement_identity")
        self.assertIn('_acoustid_lookup_cached(str(path))', resolver_source)
        self.assertIn('resolved["acoustid_mb_trackid"]', resolver_source)
        self.assertIn('AcoustID match found', resolver_source)

    def test_unresolved_orphan_needs_review_not_retry_loop(self):
        replace_source = function_source("_music_format_replace_rows")
        self.assertIn('status="Needs review"', replace_source)
        self.assertIn('retryable=False', replace_source)
        self.assertIn('unable to resolve MusicBrainz recording identity', function_source("_music_format_resolve_replacement_identity"))

    def test_case_variant_duplicate_requests_collapse(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_path = str(Path(tmp) / "replacements.json")
            saved = mark_needs_replacement([
                {"artist": "Release", "title": "Stranger", "path": "a.flac"},
                {"artist": "release", "title": "stranger", "path": "b.flac"},
            ], path=status_path)
        self.assertEqual(len(saved["tracks"]), 1)
        self.assertIn("text:release|stranger", saved["tracks"][0]["replacement_identity_key"])

    def test_punctuation_whitespace_duplicate_requests_collapse(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_path = str(Path(tmp) / "replacements.json")
            saved = mark_needs_replacement([
                {"artist": "Dennis  Brown", "title": "Pass-the Dutchie", "path": "a.flac"},
                {"artist": "dennis brown", "title": "Pass The   Dutchie", "path": "b.flac"},
            ], path=status_path)
        self.assertEqual(len(saved["tracks"]), 1)

    def test_retry_backoff_and_bounded_attempts_are_stored(self):
        replace_source = function_source("_music_format_replace_rows")
        state_source = function_source("_music_format_retry_state")
        allowed_source = function_source("_music_format_retry_allowed")
        self.assertIn("_MUSIC_FORMAT_REPLACEMENT_MAX_ATTEMPTS", replace_source)
        self.assertIn('"attempt_count"', state_source)
        self.assertIn('"next_retry_at"', state_source)
        self.assertIn("retry limit reached", allowed_source)

    def test_failed_verification_preserves_original(self):
        replace_source = function_source("_music_format_replace_rows")
        self.assertIn('replacement = _music_format_find_verified_replacement(resolved, prefs)', replace_source)
        self.assertIn('raise RuntimeError("replacement failed verification")', replace_source)
        self.assertLess(
            replace_source.index('replacement = _music_format_find_verified_replacement(resolved, prefs)'),
            replace_source.index('_music_format_remove_original_after_replacement('),
        )

    def test_slskd_missing_track_batch_behavior_remains(self):
        self.assertIn('Switched to @', APP_SOURCE)
        self.assertIn('Filtering candidate to', APP_SOURCE)
        self.assertIn('Queued {len(queued)} file(s)', APP_SOURCE)
        self.assertIn('requested missing track(s)', APP_SOURCE)

    def test_imported_wanted_downloads_are_fingerprint_gated_before_import(self):
        helper_source = function_source("_validate_wanted_download_identity_before_import")
        download_source = function_source("api_download_album")
        self.assertIn("_playlist_download_match", helper_source)
        self.assertIn("Downloaded audio did not fingerprint-verify", helper_source)
        self.assertIn("_validate_wanted_download_identity_before_import(", download_source)
        self.assertLess(
            download_source.index("_validate_wanted_download_identity_before_import("),
            download_source.index("_validate_import_source_audio("),
        )

    def test_replace_existing_import_skips_repair_only_shortcut(self):
        reimport_source = function_source("reimport_disk")
        self.assertIn('replace_existing = bool(payload.get("replace_existing") or replace_existing_item_ids)', reimport_source)
        self.assertIn("Replacement mode: importing verified staged file", reimport_source)
        self.assertIn(
            "if existing_album_id and wanted_tracks and not source_is_music_library and not replace_existing:",
            reimport_source,
        )
        self.assertIn(
            'if existing_album_id and not replace_existing and int(preflight.get("matches") or 0) >= int(preflight.get("expected") or 0):',
            reimport_source,
        )

if __name__ == "__main__":
    unittest.main()