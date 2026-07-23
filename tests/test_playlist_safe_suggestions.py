"""Route-level tests for the playlist missing-track-suggestions migration
to the shared backend-authoritative matching contract (the same contract
PR #19 enforces for Import Review's attach-recording endpoint):

  GET  /api/playlists/<name>/suggestions
  POST /api/playlists/<name>/apply-safe-suggestions

Imports the real app.py (same isolated-temp-environment pattern as
tests/test_import_review_attach_enforcement.py) and drives the actual
Flask routes. The Beets library is a fake object (no real Beets/SQLite
item semantics needed for these matching-decision-shaped tests); AcoustID
lookups and MusicBrainz recording search are patched out so tests are
deterministic and never touch the network.

Concurrency (reservation), rollback, and secret-redaction coverage live in
tests/test_playlist_suggestion_integrity.py, which imports this module's
harness rather than re-deriving it.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock as mock
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="beets_playlist_suggestions_"))
unittest.addModuleCleanup(shutil.rmtree, str(_TMP_ROOT), ignore_errors=True)

_ENV_OVERRIDES = {
    "BEETSDIR": str(_TMP_ROOT / "config"),
    "LIB_PATH": str(_TMP_ROOT / "config" / "musiclibrary.blb"),
    "AI_BATCH_STATE_DIR": str(_TMP_ROOT / "ai_batch_jobs"),
    "METADATA_CACHE_DIR": str(_TMP_ROOT / "cache"),
    "BEETS_TRANSACTION_DIR": str(_TMP_ROOT / "transactions"),
    "PLAYLIST_DIR": str(_TMP_ROOT / "playlists"),
    "PLAYLIST_DOWNLOAD_ROOT": str(_TMP_ROOT / "playlist_downloads"),
    "BEETS_WEB_AUTH_DISABLED": "1",
}
(_TMP_ROOT / "config").mkdir(parents=True, exist_ok=True)
(_TMP_ROOT / "playlists").mkdir(parents=True, exist_ok=True)
_env_patcher = mock.patch.dict(os.environ, _ENV_OVERRIDES, clear=False)
_env_patcher.start()
unittest.addModuleCleanup(_env_patcher.stop)


def setUpModule():
    # Other test modules sharing this process may mutate BEETS_WEB_AUTH_*
    # without reverting (see test_import_review_attach_enforcement.py's
    # identical comment for the incident this guards against).
    os.environ.update(_ENV_OVERRIDES)


def _import_app():
    sys.path.insert(0, str(ROOT))
    import app as app_module
    return app_module


try:
    APP = _import_app()
    _APP_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment-dependent
    APP = None
    _APP_IMPORT_ERROR = exc


def _fake_lib_item(**overrides):
    # The playlist matcher also derives artist/title guesses from the file
    # path (_playlist_item_text_variants), so the default path must stay in
    # sync with title/artist -- otherwise a test overriding only `title`
    # would still exact-match via the stale default path's filename text.
    title = overrides.get("title", "Midnight City")
    artist = overrides.get("artist", "M83")
    base = dict(
        id=1, title=title, artist=artist, albumartist=artist,
        album="Hurry Up, We're Dreaming", path=f"/music/{artist}/{title}.flac",
        mb_trackid="", mb_albumid="", mb_releasegroupid="",
        length=244.0, bitrate=320000, format="FLAC",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class PlaylistSuggestionsRouteTestCase(unittest.TestCase):
    """Shared harness: isolated per-test playlist, a fake Beets library the
    test controls directly, and no real AcoustID/MusicBrainz network
    access. Subclasses/other modules should use self._set_library_items()
    and self._seed_missing_tracks() rather than touching APP globals
    directly."""

    def setUp(self):
        if APP is None:  # pragma: no cover - environment-dependent
            self.skipTest(f"app import failed: {_APP_IMPORT_ERROR}")
        self.client = APP.app.test_client()
        self.playlist_name = f"Test Playlist {uuid.uuid4().hex[:8]}"
        self.clean_name = APP._clean_playlist_name(self.playlist_name)
        self._lib_items = []
        self._mb_candidates = []

        fake_lib = SimpleNamespace(
            items=lambda q=None: list(self._lib_items),
            get_item=lambda iid: next((i for i in self._lib_items if i.id == iid), None),
        )
        self._patch(APP, "lib", fake_lib)
        self._patch(APP, "_acoustid_fingerprint_ids", lambda path, limit=5: [])
        self._patch(
            APP, "_playlist_recording_search_candidates",
            lambda title, artist, current_mbid="": list(self._mb_candidates),
        )
        APP._invalidate_lib_cache()

    def _patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _set_library_items(self, items):
        self._lib_items = items
        APP._invalidate_lib_cache()

    def _set_mb_candidates(self, candidates):
        self._mb_candidates = candidates

    def _seed_missing_tracks(self, tracks):
        """Writes tracks directly into the playlist's desired-track
        manifest (the real, atomic manifest-write path) so the route
        under test sees them as "missing" for any track that doesn't
        fuzzy-match a current fake library item."""
        APP._playlist_write_manifest(self.clean_name, tracks, source="test")

    def _get_suggestions(self, **params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"/api/playlists/{self.clean_name}/suggestions"
        if qs:
            url += f"?{qs}"
        resp = self.client.get(url)
        return resp.status_code, resp.get_json()

    def _apply_suggestions(self, suggestions):
        resp = self.client.post(
            f"/api/playlists/{self.clean_name}/apply-safe-suggestions",
            data=json.dumps({"suggestions": suggestions}),
            content_type="application/json",
        )
        return resp.status_code, resp.get_json()

    def _manifest_tracks(self):
        manifest = APP._playlist_read_manifest(self.clean_name)
        return APP._playlist_clean_track_list(manifest.get("desired_tracks") or [])


class SuggestionsEndpointTests(PlaylistSuggestionsRouteTestCase):
    def test_playlist_not_found_returns_structured_code(self):
        resp = self.client.get("/api/playlists/does-not-exist-xyz/suggestions")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["code"], "playlist_not_found")

    def test_exact_library_match_is_reported_safe_with_decision_fields(self):
        # "Midnight  City" (double space) fails app.py's own strict, exact-
        # normalized matched-vs-missing classifier (so this track stays
        # "missing" going into the suggestions endpoint), while the shared
        # matching contract's whitespace-collapsing similarity treats it as
        # identical to the library item's real title -- exactly the "video
        # title needs light cleanup" case suggestions exist to fix.
        self._set_library_items([_fake_lib_item()])
        self._seed_missing_tracks([{"artist": "M83", "title": "Midnight  City"}])

        status, body = self._get_suggestions()
        self.assertEqual(status, 200)
        self.assertEqual(body["total_missing"], 1)
        self.assertEqual(body["safe_count"], 1)
        row = body["rows"][0]
        self.assertTrue(row["track_key"])
        best = row["best"]
        self.assertEqual(best["source"], "beets")
        self.assertEqual(best["item_id"], 1)
        self.assertTrue(best["decision_version"].startswith("drv2:"))
        self.assertTrue(best["decision"]["action_eligibility"]["playlist_resolve_without_review"])
        self.assertFalse(best["decision"]["action_eligibility"]["attach_without_review"])
        # Deprecated legacy fields stay in sync with the decision.
        self.assertTrue(best["safe"])
        self.assertEqual(best["reason"], best["decision"]["eligibility_reason"])

    def test_no_candidates_reports_review_required_not_safe(self):
        self._set_library_items([])
        self._seed_missing_tracks([{"artist": "Nobody Known", "title": "Totally Obscure Song"}])
        status, body = self._get_suggestions()
        self.assertEqual(status, 200)
        self.assertEqual(body["safe_count"], 0)
        self.assertEqual(body["rows"][0]["suggestions"], [])
        self.assertIsNone(body["rows"][0]["best"])

    def test_fuzzy_title_mismatch_is_not_safe(self):
        self._set_library_items([_fake_lib_item(title="A Totally Different Song")])
        self._seed_missing_tracks([{"artist": "M83", "title": "Midnight City"}])
        status, body = self._get_suggestions()
        self.assertEqual(body["safe_count"], 0)

    def test_duplicate_titles_get_distinct_track_keys(self):
        # The desired-track manifest itself deduplicates identical
        # (artist, title) entries (_playlist_merge_desired_tracks) -- a
        # playlist that intentionally repeats a song surfaces as repeated
        # rows in the *missing* list this function is given, not as
        # duplicate manifest entries. Exercise the key generator directly
        # against two such rows, exactly as the routes call it.
        rows = APP._playlist_missing_rows_with_keys(self.clean_name, [
            {"artist": "Repeat Artist", "title": "Same Song"},
            {"artist": "Repeat Artist", "title": "Same Song"},
        ])
        keys = [key for key, _track in rows]
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])
        # Deterministic: recomputing for the identical input list reproduces
        # the same two keys in the same order.
        rows_again = APP._playlist_missing_rows_with_keys(self.clean_name, [
            {"artist": "Repeat Artist", "title": "Same Song"},
            {"artist": "Repeat Artist", "title": "Same Song"},
        ])
        self.assertEqual(keys, [key for key, _track in rows_again])

    def test_client_cannot_disable_musicbrainz_and_get_a_safer_result(self):
        """musicbrainz=0 only narrows the *displayed* candidate set; it must
        never change the safety outcome for an otherwise-identical apply."""
        self._set_library_items([_fake_lib_item()])
        self._seed_missing_tracks([{"artist": "M83", "title": "Midnight  City"}])
        _, with_mb = self._get_suggestions(musicbrainz=1)
        _, without_mb = self._get_suggestions(musicbrainz=0)
        self.assertEqual(with_mb["safe_count"], without_mb["safe_count"])


class ApplySafeSuggestionsTests(PlaylistSuggestionsRouteTestCase):
    def _safe_submission(self):
        self._set_library_items([_fake_lib_item()])
        self._seed_missing_tracks([{"artist": "M83", "title": "Midnight  City"}])
        _, body = self._get_suggestions()
        best = body["rows"][0]["best"]
        return body["rows"][0]["track_key"], best

    def test_playlist_not_found_returns_404(self):
        resp = self.client.post(
            "/api/playlists/does-not-exist-xyz/apply-safe-suggestions",
            data=json.dumps({"suggestions": []}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["code"], "playlist_not_found")

    def test_empty_batch_is_a_no_op(self):
        self._seed_missing_tracks([{"artist": "Solo Artist", "title": "Solo Song"}])
        status, body = self._apply_suggestions([])
        self.assertEqual(status, 200)
        self.assertFalse(body["changed"])
        self.assertEqual(body["applied"], [])

    def test_safe_row_is_applied_and_manifest_verified(self):
        track_key, best = self._safe_submission()
        status, body = self._apply_suggestions([{
            "track_key": track_key,
            "mb_trackid": best.get("mb_trackid") or "",
            "item_id": best.get("item_id"),
            "decision_version": best["decision_version"],
        }])
        self.assertEqual(status, 200)
        self.assertTrue(body["changed"])
        self.assertEqual(len(body["applied"]), 1)
        self.assertTrue(body["audit_id"])
        tracks = self._manifest_tracks()
        self.assertTrue(any(t.get("artist") == "M83" and t.get("title") == "Midnight City" for t in tracks))
        tx = APP.transactions.get(body["audit_id"])
        self.assertEqual(tx["status"], "Completed")
        self.assertEqual(tx["operation_type"], "Playlist Match")

    def test_mixed_batch_applies_safe_row_and_reports_stale_row_at_200(self):
        """A batch with at least one non-stale outcome returns 200 with
        per-row buckets -- only a batch where every row is stale (nothing
        else to report) escalates to a whole-batch 409."""
        track_key, best = self._safe_submission()
        status, body = self._apply_suggestions([
            {
                "track_key": track_key,
                "mb_trackid": best.get("mb_trackid") or "",
                "item_id": best.get("item_id"),
                "decision_version": best["decision_version"],
            },
            {"track_key": "ptk_unrelated_missing", "decision_version": "drv2:x"},
        ])
        self.assertEqual(status, 200)
        self.assertEqual(len(body["applied"]), 1)
        self.assertEqual(len(body["stale"]), 1)
        self.assertEqual(body["stale"][0]["track_key"], "ptk_unrelated_missing")

    def test_reapplying_the_same_row_is_unchanged_not_duplicated(self):
        track_key, best = self._safe_submission()
        row = {
            "track_key": track_key,
            "mb_trackid": best.get("mb_trackid") or "",
            "item_id": best.get("item_id"),
            "decision_version": best["decision_version"],
        }
        status1, body1 = self._apply_suggestions([row])
        self.assertEqual(len(body1["applied"]), 1)
        first_audit_id = body1["audit_id"]

        # Recompute suggestions fresh -- the track has moved from "missing"
        # into "matched" now that its manifest identity equals the library
        # item, so a second identical apply request has nothing left to
        # find under the *original* track_key. With no other rows in the
        # batch this is reported as a whole-batch stale 409 (never silently
        # re-applied or re-audited), matching the single-row-batch case.
        status2, body2 = self._apply_suggestions([row])
        self.assertEqual(status2, 409)
        self.assertEqual(body2["code"], "playlist_suggestions_stale")
        self.assertEqual(body2["stale"][0]["track_key"], track_key)

    def test_untrusted_candidate_is_rejected_without_mutation(self):
        track_key, best = self._safe_submission()
        before = self._manifest_tracks()
        status, body = self._apply_suggestions([{
            "track_key": track_key,
            "mb_trackid": "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "decision_version": best["decision_version"],
        }])
        self.assertEqual(status, 200)
        self.assertEqual(body["applied"], [])
        self.assertEqual(len(body["conflicts"]), 1)
        self.assertEqual(body["conflicts"][0]["code"], "candidate_not_in_trusted_set")
        self.assertEqual(before, self._manifest_tracks())

    def test_unknown_track_key_is_reported_stale_without_mutation(self):
        self._seed_missing_tracks([{"artist": "Someone", "title": "Some Song"}])
        status, body = self._apply_suggestions([{
            "track_key": "ptk_doesnotexist",
            "decision_version": "drv2:whatever",
        }])
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "playlist_suggestions_stale")
        self.assertEqual(body["stale"][0]["code"], "playlist_track_not_found")

    def test_stale_decision_version_is_rejected_without_mutation(self):
        track_key, best = self._safe_submission()
        before = self._manifest_tracks()
        status, body = self._apply_suggestions([{
            "track_key": track_key,
            "mb_trackid": best.get("mb_trackid") or "",
            "item_id": best.get("item_id"),
            "decision_version": "drv2:0000000000000000000000",
        }])
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "playlist_suggestions_stale")
        self.assertEqual(before, self._manifest_tracks())

    def test_review_required_row_is_skipped_not_applied(self):
        # "Midnight City Anthem" scores just high enough (~0.79) to surface
        # as a candidate but well below the deterministic-match bar
        # (library bypass needs >=0.94) -- exactly the "fuzzy title with no
        # further corroboration must require review" case from the spec.
        self._set_library_items([_fake_lib_item(title="Midnight City Anthem")])
        self._seed_missing_tracks([{"artist": "M83", "title": "Midnight City"}])
        _, body = self._get_suggestions()
        row = body["rows"][0]
        candidate = row["suggestions"][0] if row["suggestions"] else None
        self.assertIsNotNone(candidate, "expected a (review-required) candidate for this fixture")
        self.assertFalse(candidate["decision"]["action_eligibility"]["playlist_resolve_without_review"])
        status, apply_body = self._apply_suggestions([{
            "track_key": row["track_key"],
            "item_id": candidate.get("item_id"),
            "mb_trackid": candidate.get("mb_trackid") or "",
            "decision_version": candidate["decision_version"],
        }])
        self.assertEqual(status, 200)
        self.assertEqual(apply_body["applied"], [])
        self.assertEqual(len(apply_body["skipped_review"]), 1)
        self.assertEqual(apply_body["skipped_review"][0]["code"], "playlist_review_required")

    def test_client_supplied_fields_beyond_the_minimal_shape_are_ignored(self):
        """Submitting extra safety-shaped fields (safe/confidence/conflicts)
        must have zero effect -- only track_key/mb_trackid/item_id/
        decision_version are ever read."""
        track_key, best = self._safe_submission()
        status, body = self._apply_suggestions([{
            "track_key": track_key,
            "mb_trackid": best.get("mb_trackid") or "",
            "item_id": best.get("item_id"),
            "decision_version": best["decision_version"],
            "safe": True,
            "confidence": 1.0,
            "conflicts": [],
            "action_eligibility": {"playlist_resolve_without_review": True},
        }])
        self.assertEqual(status, 200)
        self.assertEqual(len(body["applied"]), 1)

    def test_untrusted_candidate_with_forged_safety_still_rejected(self):
        """A row claiming an arbitrary UUID plus forged safety fields must
        still be rejected -- the backend never trusts client safety claims."""
        track_key, _ = self._safe_submission()
        status, body = self._apply_suggestions([{
            "track_key": track_key,
            "mb_trackid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "decision_version": "drv2:forged",
            "safe": True,
            "action_eligibility": {"playlist_resolve_without_review": True, "attach_without_review": True},
        }])
        self.assertEqual(status, 200)
        self.assertEqual(body["applied"], [])
        self.assertEqual(body["conflicts"][0]["code"], "candidate_not_in_trusted_set")


if __name__ == "__main__":
    unittest.main()
