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

    def test_duplicate_titles_get_distinct_stable_row_ids(self):
        # Two structurally-identical entries in the *same* merge group
        # (e.g. a playlist's own M3U genuinely repeating a song) each get
        # their own stable row_id -- _playlist_merge_desired_tracks only
        # dedupes a fresh (no-row_id) entry against an *earlier* group,
        # never against a same-group sibling.
        merged = APP._playlist_merge_desired_tracks([
            {"artist": "Repeat Artist", "title": "Same Song"},
            {"artist": "Repeat Artist", "title": "Same Song"},
        ])
        self.assertEqual(len(merged), 2)
        ids = [t.get("row_id") for t in merged]
        self.assertTrue(all(ids))
        self.assertNotEqual(ids[0], ids[1])

        # Once assigned, row_ids are stable across a second merge pass
        # (the normal read -> merge -> maybe-rewrite cycle) rather than
        # being re-minted every time.
        merged_again = APP._playlist_merge_desired_tracks(merged)
        self.assertEqual(ids, [t.get("row_id") for t in merged_again])

    def test_missing_rows_with_keys_uses_the_manifest_row_id(self):
        self._seed_missing_tracks([
            {"artist": "Repeat Artist", "title": "Same Song"},
            {"artist": "Repeat Artist", "title": "Same Song"},
        ])
        status, body = self._get_suggestions()
        self.assertEqual(status, 200)
        keys = [row["track_key"] for row in body["rows"]]
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])
        tracks = self._manifest_tracks()
        self.assertEqual(sorted(keys), sorted(t.get("row_id") for t in tracks))

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
        self.assertEqual(tx["changes"][0]["persisted_identity"]["artist"], "M83")
        self.assertEqual(tx["changes"][0]["persisted_identity"]["title"], "Midnight City")
        # Truthful derived-state accounting: only the manifest was
        # rewritten here -- the M3U was not regenerated.
        self.assertTrue(body["manifest_updated"])
        self.assertFalse(body["m3u_updated"])
        self.assertTrue(body["sync_required"])

    def test_mixed_batch_with_one_stale_row_applies_nothing(self):
        """Atomic batch semantics: a batch containing one otherwise-safe
        row and one stale/unresolvable row applies ZERO rows and creates
        NO transaction -- the whole batch is rejected, never partially
        applied."""
        track_key, best = self._safe_submission()
        before = self._manifest_tracks()
        status, body = self._apply_suggestions([
            {
                "track_key": track_key,
                "mb_trackid": best.get("mb_trackid") or "",
                "item_id": best.get("item_id"),
                "decision_version": best["decision_version"],
            },
            {"track_key": "ptr_unrelated_missing", "decision_version": "drv2:x"},
        ])
        self.assertEqual(status, 409)
        self.assertEqual(body["ok"], False)
        self.assertEqual(body["changed"], False)
        # Every blocking row shares one cause here (the single unrelated
        # stale row), so the top-level code is that specific cause rather
        # than the generic fallback.
        self.assertEqual(body["code"], "playlist_track_not_found")
        self.assertEqual(len(body["stale"]), 1)
        self.assertEqual(body["stale"][0]["track_key"], "ptr_unrelated_missing")
        self.assertEqual(body["conflicts"], [])
        # The otherwise-safe row was never written, and no transaction was
        # created for it.
        self.assertEqual(before, self._manifest_tracks())
        _rows, total_for_playlist = APP.transactions.list(
            operation="Playlist Match", query=self.clean_name)
        self.assertEqual(total_for_playlist, 0)

    def test_replay_of_an_accepted_submission_is_idempotent(self):
        track_key, best = self._safe_submission()
        row = {
            "track_key": track_key,
            "mb_trackid": best.get("mb_trackid") or "",
            "item_id": best.get("item_id"),
            "decision_version": best["decision_version"],
        }
        status1, body1 = self._apply_suggestions([row])
        self.assertEqual(status1, 200)
        self.assertEqual(len(body1["applied"]), 1)
        first_audit_id = body1["audit_id"]

        # Exact replay of the identical, already-applied submission: the
        # row (looked up by its stable row_id, not by "is it still
        # missing") already has exactly this resolved identity, so this
        # must be a true idempotent no-op -- 200, changed:false,
        # already_resolved -- never a stale rejection and never a second
        # write or transaction.
        status2, body2 = self._apply_suggestions([row])
        self.assertEqual(status2, 200)
        self.assertEqual(body2["applied"], [])
        self.assertEqual(len(body2["unchanged"]), 1)
        self.assertEqual(body2["unchanged"][0]["reason"], "already_resolved")
        self.assertIsNone(body2["audit_id"])
        self.assertFalse(body2["changed"])

        # No duplicate transaction was created for the replay.
        second_tx = APP.transactions.get(first_audit_id)
        self.assertEqual(second_tx["status"], "Completed")

    def test_replay_after_row_changed_again_is_rejected_not_reapplied(self):
        track_key, best = self._safe_submission()
        row = {
            "track_key": track_key,
            "mb_trackid": best.get("mb_trackid") or "",
            "item_id": best.get("item_id"),
            "decision_version": best["decision_version"],
        }
        status1, _ = self._apply_suggestions([row])
        self.assertEqual(status1, 200)

        # Something else (e.g. manual resolve-track) changes the row's
        # identity again after our apply.
        APP._playlist_replace_rows_by_id(self.clean_name, [{
            "row_id": track_key, "artist": "Someone Else", "title": "Something Else",
        }])

        status2, body2 = self._apply_suggestions([row])
        self.assertEqual(status2, 409)
        # No longer an idempotent no-op -- the row's current identity no
        # longer matches either the old or the newly-submitted target, so
        # it is correctly rejected (stale/conflict), never silently
        # overwritten back to the old resolution.
        self.assertTrue(body2["stale"] or body2["conflicts"])

    def test_untrusted_candidate_is_rejected_without_mutation(self):
        track_key, best = self._safe_submission()
        before = self._manifest_tracks()
        status, body = self._apply_suggestions([{
            "track_key": track_key,
            "mb_trackid": "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "decision_version": best["decision_version"],
        }])
        self.assertEqual(status, 409)
        self.assertEqual(body["applied"] if "applied" in body else [], [])
        self.assertEqual(len(body["conflicts"]), 1)
        self.assertEqual(body["conflicts"][0]["code"], "candidate_not_in_trusted_set")
        self.assertEqual(before, self._manifest_tracks())

    def test_unknown_track_key_is_reported_stale_without_mutation(self):
        self._seed_missing_tracks([{"artist": "Someone", "title": "Some Song"}])
        status, body = self._apply_suggestions([{
            "track_key": "ptr_doesnotexist",
            "decision_version": "drv2:whatever",
        }])
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "playlist_track_not_found")
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
        before = self._manifest_tracks()
        status, apply_body = self._apply_suggestions([{
            "track_key": row["track_key"],
            "item_id": candidate.get("item_id"),
            "mb_trackid": candidate.get("mb_trackid") or "",
            "decision_version": candidate["decision_version"],
        }])
        self.assertEqual(status, 409)
        self.assertEqual(apply_body["code"], "playlist_review_required")
        self.assertEqual(len(apply_body["skipped_review"]), 1)
        self.assertEqual(apply_body["skipped_review"][0]["code"], "playlist_review_required")
        self.assertEqual(before, self._manifest_tracks())

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
        self.assertEqual(status, 409)
        self.assertEqual(body["conflicts"][0]["code"], "candidate_not_in_trusted_set")

    def test_missing_decision_version_is_rejected_as_malformed(self):
        track_key, best = self._safe_submission()
        before = self._manifest_tracks()
        status, body = self._apply_suggestions([{
            "track_key": track_key,
            "mb_trackid": best.get("mb_trackid") or "",
            "item_id": best.get("item_id"),
        }])
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "decision_version_required")
        self.assertEqual(body["invalid"][0]["code"], "decision_version_required")
        self.assertEqual(before, self._manifest_tracks())
        self.assertFalse(body.get("changed", False))
        _rows, total_for_playlist = APP.transactions.list(
            operation="Playlist Match", query=self.clean_name)
        self.assertEqual(total_for_playlist, 0)

    def test_empty_decision_version_is_rejected_as_malformed(self):
        track_key, best = self._safe_submission()
        status, body = self._apply_suggestions([{
            "track_key": track_key,
            "mb_trackid": best.get("mb_trackid") or "",
            "decision_version": "   ",
        }])
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "decision_version_required")

    def test_forged_decision_version_cannot_bypass_the_requirement(self):
        """A row that supplies *some* decision_version string (even a
        forged one) is not malformed -- it must be rejected for staleness,
        never silently trusted."""
        track_key, best = self._safe_submission()
        status, body = self._apply_suggestions([{
            "track_key": track_key,
            "mb_trackid": best.get("mb_trackid") or "",
            "item_id": best.get("item_id"),
            "decision_version": "drv2:0000000000000000000000",
        }])
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "playlist_suggestions_stale")

    def test_duplicate_submission_with_different_candidates_is_rejected(self):
        track_key, best = self._safe_submission()
        before = self._manifest_tracks()
        status, body = self._apply_suggestions([
            {
                "track_key": track_key,
                "mb_trackid": best.get("mb_trackid") or "",
                "item_id": best.get("item_id"),
                "decision_version": best["decision_version"],
            },
            {
                "track_key": track_key,
                "mb_trackid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "decision_version": "drv2:different",
            },
        ])
        self.assertEqual(status, 409)
        self.assertEqual(body["conflicts"][0]["code"], "playlist_duplicate_submission")
        self.assertEqual(before, self._manifest_tracks())

    def test_duplicate_identical_submission_is_deduplicated_not_double_applied(self):
        track_key, best = self._safe_submission()
        row = {
            "track_key": track_key,
            "mb_trackid": best.get("mb_trackid") or "",
            "item_id": best.get("item_id"),
            "decision_version": best["decision_version"],
        }
        status, body = self._apply_suggestions([dict(row), dict(row)])
        self.assertEqual(status, 200)
        self.assertEqual(len(body["applied"]), 1)


class AcoustIdEvidenceStateTests(unittest.TestCase):
    """Direct unit coverage of APP._playlist_acoustid_evidence's truthful,
    non-contradictory fingerprint state model (no Flask needed)."""

    def setUp(self):
        if APP is None:
            self.skipTest(f"app import failed: {_APP_IMPORT_ERROR}")

    def test_no_path_is_not_attempted(self):
        result = APP._playlist_acoustid_evidence("", "some-mbid")
        self.assertEqual(result, {"attempted": False, "matched": False, "status": "not_attempted", "mapped_recording_id": ""})

    def test_lookup_exception_is_lookup_failed_not_no_evidence(self):
        with mock.patch.object(APP, "_acoustid_fingerprint_ids", side_effect=RuntimeError("fpcalc missing")):
            result = APP._playlist_acoustid_evidence("/music/track.flac", "some-mbid")
        self.assertEqual(result["attempted"], True)
        self.assertEqual(result["matched"], False)
        self.assertEqual(result["status"], "lookup_failed")
        self.assertEqual(result["mapped_recording_id"], "")

    def test_no_returned_ids_is_no_match(self):
        with mock.patch.object(APP, "_acoustid_fingerprint_ids", return_value=[]):
            result = APP._playlist_acoustid_evidence("/music/track.flac", "some-mbid")
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["mapped_recording_id"], "")

    def test_matching_id_is_matched(self):
        with mock.patch.object(APP, "_acoustid_fingerprint_ids", return_value=["mbid-a", "mbid-b"]):
            result = APP._playlist_acoustid_evidence("/music/track.flac", "mbid-a")
        self.assertEqual(result["status"], "matched")
        self.assertTrue(result["matched"])
        self.assertEqual(result["mapped_recording_id"], "mbid-a")

    def test_disagreeing_id_is_conflict_not_silently_matched(self):
        with mock.patch.object(APP, "_acoustid_fingerprint_ids", return_value=["mbid-other"]):
            result = APP._playlist_acoustid_evidence("/music/track.flac", "mbid-a")
        self.assertEqual(result["status"], "conflict")
        self.assertFalse(result["matched"])
        self.assertEqual(result["mapped_recording_id"], "mbid-other")

    def test_ids_with_no_local_recording_id_is_mapped_unverified(self):
        with mock.patch.object(APP, "_acoustid_fingerprint_ids", return_value=["mbid-x", "mbid-y"]):
            result = APP._playlist_acoustid_evidence("/music/track.flac", "")
        self.assertEqual(result["status"], "mapped_unverified")
        self.assertFalse(result["matched"])
        self.assertEqual(result["mapped_recording_id"], "mbid-x")

    def test_conflict_state_feeds_into_the_contract_as_a_hard_mismatch(self):
        """The 'conflict' status must reach build_recording_matching_decision
        as a genuine fingerprint conflict (never neutral/no-evidence),
        blocking both attach and playlist-library eligibility."""
        from backend.matching_contract import build_recording_matching_decision
        candidate = {
            "artist": "M83", "title": "Midnight City", "source": "beets", "item_id": 1,
            "mb_trackid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "fingerprint_attempted": True, "fingerprint_matched": False,
            "fingerprint_status": "conflict",
            "mapped_recording_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        }
        decision = build_recording_matching_decision(
            current={"title": "Midnight City", "artist": "M83"},
            candidate=candidate,
            library_identity_verified=True,
        ).to_dict()
        self.assertIn("fingerprint_conflict", decision["decision"]["conflicts"])
        self.assertFalse(decision["decision"]["action_eligibility"]["playlist_resolve_without_review"])


class ExactRowIdentityTests(PlaylistSuggestionsRouteTestCase):
    """Section 6: persisted-state verification and rollback must key off
    the exact manifest row_id, never "any row with this text" -- which
    could double-count one persisted row as proof of two different
    changes."""

    def test_two_distinct_rows_resolving_to_the_same_identity_both_verify_independently(self):
        self._set_library_items([_fake_lib_item()])
        self._seed_missing_tracks([
            {"artist": "M83", "title": "Midnight  City"},
            {"artist": "M83", "title": "Midnight   City"},
        ])
        _, body = self._get_suggestions()
        self.assertEqual(body["total_missing"], 2)
        submissions = []
        for row in body["rows"]:
            best = row["best"]
            self.assertIsNotNone(best)
            submissions.append({
                "track_key": row["track_key"],
                "mb_trackid": best.get("mb_trackid") or "",
                "item_id": best.get("item_id"),
                "decision_version": best["decision_version"],
            })
        status, apply_body = self._apply_suggestions(submissions)
        self.assertEqual(status, 200)
        self.assertEqual(len(apply_body["applied"]), 2)
        tracks = self._manifest_tracks()
        resolved = [t for t in tracks if t.get("artist") == "M83" and t.get("title") == "Midnight City"]
        self.assertEqual(len(resolved), 2)
        # Both rows kept their own distinct row_id through the resolution.
        self.assertEqual(len({t.get("row_id") for t in resolved}), 2)
        tx = APP.transactions.get(apply_body["audit_id"])
        self.assertEqual(len(tx["changes"]), 2)
        row_ids_in_changes = {c["row_id"] for c in tx["changes"]}
        self.assertEqual(row_ids_in_changes, {s["track_key"] for s in submissions})

    def test_pre_existing_unrelated_row_with_target_identity_does_not_confuse_verification(self):
        self._set_library_items([_fake_lib_item()])
        # Seed a missing row we intend to resolve, PLUS an already-matched
        # row that happens to already carry the exact target identity
        # ("Midnight City") -- this pre-existing row must never be mistaken
        # for proof that the *other* row's replacement landed.
        self._seed_missing_tracks([
            {"artist": "M83", "title": "Midnight City"},
            {"artist": "M83", "title": "Midnight  City"},
        ])
        manifest_before = self._manifest_tracks()
        preexisting_row_id = next(
            t["row_id"] for t in manifest_before if t.get("title") == "Midnight City"
        )
        _, body = self._get_suggestions()
        target_row = next(r for r in body["rows"] if r["track_key"] != preexisting_row_id)
        best = target_row["best"]
        self.assertIsNotNone(best)
        status, apply_body = self._apply_suggestions([{
            "track_key": target_row["track_key"],
            "mb_trackid": best.get("mb_trackid") or "",
            "item_id": best.get("item_id"),
            "decision_version": best["decision_version"],
        }])
        self.assertEqual(status, 200)
        self.assertEqual(len(apply_body["applied"]), 1)
        self.assertEqual(apply_body["applied"][0]["track_key"], target_row["track_key"])
        tracks = self._manifest_tracks()
        resolved_target = next(t for t in tracks if t["row_id"] == target_row["track_key"])
        self.assertEqual(resolved_target["title"], "Midnight City")
        # The pre-existing row is untouched and still has its own row_id.
        preexisting_after = next(t for t in tracks if t["row_id"] == preexisting_row_id)
        self.assertEqual(preexisting_after["title"], "Midnight City")


class LibraryIdentityFreshReadTests(PlaylistSuggestionsRouteTestCase):
    """Section 10: library-identity verification must re-read the exact
    item by id and confirm it still exists -- never trust a cached index
    payload alone."""

    def test_deleted_item_cannot_stay_safe_from_cached_candidate_data(self):
        item = _fake_lib_item()
        self._set_library_items([item])
        self._seed_missing_tracks([{"artist": "M83", "title": "Midnight  City"}])
        _, body = self._get_suggestions()
        self.assertEqual(body["safe_count"], 1, "expected the exact match to be safe before deletion")

        # The item is deleted from the library (e.g. file removed) but the
        # cached library index (built before the deletion) hasn't been
        # invalidated yet -- a stale candidate must not be offered as safe.
        self._lib_items = []
        status, body2 = self._get_suggestions()
        self.assertEqual(status, 200)
        self.assertEqual(body2["safe_count"], 0)
        best = body2["rows"][0]["best"]
        if best is not None:
            self.assertFalse(best["decision"]["action_eligibility"]["playlist_resolve_without_review"])


if __name__ == "__main__":
    unittest.main()
