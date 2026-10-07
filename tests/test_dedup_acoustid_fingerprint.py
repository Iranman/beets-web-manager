import unittest
from pathlib import Path
try:  # ARCH-001: app.py module family (works under discovery and tests.<module> runs)
    from _app_ast_cache import app_family_source, app_unit_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source, app_unit_source  # noqa: E402


class DedupAcoustidFingerprintTests(unittest.TestCase):
    """Static-analysis checks that dedup scans fingerprint-verify duplicates.

    Flask/beets aren't importable in this test environment, so — consistent
    with the rest of this test suite — we assert against the source text of
    app.py rather than executing it.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.app_source = app_family_source()
        cls.dedup_source = app_unit_source("start_dedup_scan")

    def test_fingerprint_helpers_defined(self):
        self.assertIn("def _acoustid_fingerprint_ids(", self.app_source)
        self.assertIn("def _acoustid_fingerprint_match(", self.app_source)
        # Built on top of the existing cached AcoustID lookup, not a fresh
        # network call per comparison.
        idx = self.app_source.index("def _acoustid_fingerprint_ids(")
        body = self.app_source[idx: idx + 600]
        self.assertIn("_acoustid_lookup_cached(", body)

    def test_acoustid_file_cache_uses_stable_identity(self):
        self.assertIn("def _audio_cache_file_identity(", self.app_source)
        idx = self.app_source.index("def _audio_cache_file_identity(")
        body = self.app_source[idx: idx + 800]
        self.assertIn("resolve(strict=False)", body)
        self.assertIn("st.st_size", body)
        self.assertIn("st_mtime_ns", body)

    def test_acoustid_cache_skips_missing_files(self):
        # The cache lives in the typed variant; the list API delegates to it.
        idx = self.app_source.index("def _acoustid_lookup_cached_outcome(")
        body = self.app_source[idx: idx + 1800]
        self.assertIn("_audio_cache_file_identity(file_path)", body)
        self.assertIn("file not readable", body)          # missing file -> no lookup
        self.assertIn("acoustid_lookup_outcome(str(path))", body)
    def test_scan_falls_back_to_fingerprint_when_unmatched(self):
        self.assertIn("_acoustid_fingerprint_ids(str(src))", self.dedup_source)
        self.assertIn("AcoustID fingerprint", self.dedup_source)

    def test_scan_cross_checks_weak_matches_with_fingerprint(self):
        self.assertIn("_acoustid_fingerprint_match(str(src), lib_path)", self.dedup_source)
        self.assertIn('match_type.startswith("fuzzy match")', self.dedup_source)
        self.assertIn('match_type.startswith("album+title")', self.dedup_source)
        # A disagreeing fingerprint rejects the candidate instead of trusting text similarity.
        self.assertIn("REJECTED", self.dedup_source)

    def test_scan_populates_fields_the_frontend_renders(self):
        # DedupPanel.tsx renders dup.source_artist / source_title / confidence / reason —
        # the pre-fingerprint scan never set these, leaving the UI blank.
        self.assertIn('"source_artist":', self.dedup_source)
        self.assertIn('"source_title":', self.dedup_source)
        self.assertIn('"confidence":', self.dedup_source)
        self.assertIn('"reason":', self.dedup_source)
        self.assertIn('"fingerprint_verified":', self.dedup_source)

    def test_album_deduplicate_spares_fingerprint_mismatches(self):
        # /api/albums/<id>/deduplicate deletes collision-suffix files (Song.1.flac)
        # purely by rank; verify it now fingerprint-checks before deleting so a
        # bad MB re-match that collides two *different* songs into one track slot
        # doesn't silently destroy one of them.
        start = self.app_source.index('def album_deduplicate(aid):')
        end = self.app_source.index('@app.get("/api/albums/<int:aid>/duplicate-resolver")')
        dedup_album_source = self.app_source[start:end]
        # Wave 0 (MI-3): positive same-recording proof is now REQUIRED (unknown
        # evidence spares the copy) and removal goes through the reviewed
        # duplicate cleanup (quarantine), never album maintenance.
        self.assertIn("same_recording_proof(_abs(d[\"path\"]), kept_abs", dedup_album_source)
        self.assertIn("spared", dedup_album_source)
        self.assertIn("_duplicate_cleanup.plan_reviewed_cleanup(", dedup_album_source)
        self.assertNotIn("plan_album_maintenance(", dedup_album_source)

    def test_track_relabel_rejects_fingerprint_mismatch(self):
        # ARCH-003 Wave 33 continuation: _match_tracks_from_mb_shared no
        # longer rewrites mb_trackid/track/disc/title via raw local SQL at
        # all -- it delegates to the album_mb_track_repair_v1 composite
        # workflow (backend/composite_workflows.py), which owns the actual
        # mutation and its AcoustID cross-check. What must still hold here, at
        # the app.py call site, is that this function always requests
        # that cross-check -- it is not optional for this caller, since
        # the pre-migration behavior always ran it unconditionally.
        start = self.app_source.index("def _match_tracks_from_mb_shared(")
        end = self.app_source.index("\ndef ", start + 10)
        relabel_source = self.app_source[start:end]
        self.assertIn('"acoustid_verify": True', relabel_source)
        self.assertIn("composite_workflows.plan_album_mb_track_repair(payload)", relabel_source)
        self.assertIn("composite_workflows.apply_album_mb_track_repair(op_id", relabel_source)

    def test_artist_folder_name_merge_fingerprint_verified(self):
        # The plain-named-folder -> same-named-stamped-folder merge candidate
        # is keyed on case-folded name equality alone (no per-album MB UUID
        # evidence). Verify it now samples audio and AcoustID-checks before
        # trusting the name match, and blocks (skips) on a confirmed mismatch
        # instead of silently commingling two different artists.
        self.assertIn("def _artist_folder_fingerprint_confirms(", self.app_source)
        fn_idx = self.app_source.index("def _artist_folder_fingerprint_confirms(")
        fn_body = self.app_source[fn_idx: fn_idx + 2400]
        self.assertIn("_acoustid_lookup_cached(str(p))", fn_body)
        self.assertIn("_playlist_artist_name_score(canonical_name, c_artist)", fn_body)

        merge_idx = self.app_source.index('"plain_stamped_duplicate": True')
        merge_block = self.app_source[merge_idx - 2200: merge_idx + 200]
        self.assertIn("_artist_folder_fingerprint_confirms(folder, canonical_artist)", merge_block)
        self.assertIn("fp_result is False", merge_block)
        # Only an explicit True authorizes the merge -- False and None
        # (unavailable) both block it; the code must not silently proceed
        # merely because fp_result is not the specific value False.
        self.assertIn("if fp_result is not True:", merge_block)
        self.assertIn('"fingerprint_verified": fp_result is True', merge_block)

    def test_ai_review_also_fingerprint_verifies_matches(self):
        ai_start = self.app_source.index('@app.post("/api/dedup/ai-review")')
        ai_end = self.app_source.index("\n@app.", ai_start + 10)
        ai_source = self.app_source[ai_start:ai_end]
        self.assertIn('"type": "dedup-ai-review"', ai_source)
        self.assertIn("_acoustid_fingerprint_match(str(src), lib_path)", ai_source)
        self.assertIn("REJECTED AI match", ai_source)
        self.assertIn('"source_artist":', ai_source)
        self.assertIn('"confidence":', ai_source)
        self.assertIn('"fingerprint_verified":', ai_source)


if __name__ == "__main__":
    unittest.main()

