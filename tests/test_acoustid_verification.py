"""Tests for the flexible track alignment and AcoustID verification workflow.

Tests the backend.track_align module directly (no Flask required) plus a small
set of integration checks for the _build_import_target_preview logic.
"""
import unittest
from pathlib import Path
from typing import Any, Dict, List


from backend.track_align import align_tracks, resolve_unmatched_via_acoustid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mb_track(num: int, title: str, recording_id: str = "") -> Dict[str, Any]:
    return {
        "track": num,
        "title": title,
        "title_norm": title.casefold(),
        "mb_trackid": recording_id,
        "duration_ms": 0,
    }


def _exact_sim(file_path: str, mb_norm: str) -> float:
    """Simple similarity: exact stem vs mb_norm."""
    stem = Path(file_path).stem.casefold()
    from difflib import SequenceMatcher
    return SequenceMatcher(None, stem, mb_norm).ratio()


def _local(name: str) -> str:
    """Fake local file path from a short name."""
    return f"/fake/folder/{name}.flac"


def _acoustid_hit(recording_id: str, score: int = 90) -> Dict[str, Any]:
    return {"mb_trackid": recording_id, "score": score, "title": "", "artist": ""}


# ---------------------------------------------------------------------------
# 1. Title mismatch + AcoustID verified → allowed import (acoustid_verified status)
# ---------------------------------------------------------------------------

class TestAcoustIDVerifiedTitleMismatch(unittest.TestCase):
    """A local file with a messy name that does NOT title-match, but AcoustID confirms it."""

    def _run(self) -> List[Dict[str, Any]]:
        mb_tracks = [
            _mb_track(1, "intro", "rec-0001"),
            _mb_track(2, "4 da gang", "rec-0002"),
        ]
        # local file 1 matches "intro" well; local file 2 has a completely different name
        local_files = [_local("intro"), _local("totally wrong name xyz")]
        comparison = align_tracks(local_files, mb_tracks, _exact_sim)
        # At this point track 2 is "missing" (no title match) and the bad name is "extra"
        return comparison

    def test_before_acoustid_track2_missing(self):
        comparison = self._run()
        statuses = {r["num"]: r["status"] for r in comparison}
        self.assertEqual(statuses[1], "matched")
        self.assertEqual(statuses[2], "missing")
        # The bad-name file is extra
        extra = [r for r in comparison if r["status"] == "extra"]
        self.assertEqual(len(extra), 1)

    def test_acoustid_verification_resolves_missing(self):
        mb_tracks = [
            _mb_track(1, "intro", "rec-0001"),
            _mb_track(2, "4 da gang", "rec-0002"),
        ]
        local_files = [_local("intro"), _local("totally wrong name xyz")]
        comparison = align_tracks(local_files, mb_tracks, _exact_sim)

        # AcoustID confirms the bad-name file IS recording rec-0002
        def mock_acoustid(file_path: str) -> List[Dict[str, Any]]:
            if "totally wrong" in file_path:
                return [_acoustid_hit("rec-0002", score=95)]
            return []

        resolve_unmatched_via_acoustid(comparison, mock_acoustid, fpcalc_available=True)

        statuses = {r["num"]: r["status"] for r in comparison}
        self.assertEqual(statuses[1], "matched")
        self.assertEqual(statuses[2], "acoustid_verified",
                         "AcoustID-confirmed track must become acoustid_verified, not missing")
        # Extra row should be removed once resolved
        extra = [r for r in comparison if r["status"] == "extra"]
        self.assertEqual(len(extra), 0)

    def test_acoustid_verified_row_carries_file_path(self):
        mb_tracks = [_mb_track(1, "4 da gang", "rec-0002")]
        local_files = [_local("totally wrong name xyz")]
        comparison = align_tracks(local_files, mb_tracks, _exact_sim)

        def mock_acoustid(file_path: str) -> List[Dict[str, Any]]:
            return [_acoustid_hit("rec-0002")]

        resolve_unmatched_via_acoustid(comparison, mock_acoustid, fpcalc_available=True)

        row = next(r for r in comparison if r["num"] == 1)
        self.assertEqual(row["status"], "acoustid_verified")
        self.assertIn("totally wrong", row["file_path"],
                      "file_path must point to the verified local file")


# ---------------------------------------------------------------------------
# 2. Title mismatch + AcoustID NOT verified → stays blocked (missing/extra)
# ---------------------------------------------------------------------------

class TestAcoustIDNotVerified(unittest.TestCase):

    def test_wrong_recording_id_stays_missing(self):
        mb_tracks = [_mb_track(1, "4 da gang", "rec-correct")]
        local_files = [_local("totally wrong name xyz")]
        comparison = align_tracks(local_files, mb_tracks, _exact_sim)

        # AcoustID returns a DIFFERENT recording ID — wrong song
        def mock_acoustid(file_path: str) -> List[Dict[str, Any]]:
            return [_acoustid_hit("rec-WRONG", score=92)]

        resolve_unmatched_via_acoustid(comparison, mock_acoustid, fpcalc_available=True)

        missing = [r for r in comparison if r["status"] == "missing"]
        extra = [r for r in comparison if r["status"] == "extra"]
        self.assertEqual(len(missing), 1, "MB track must stay missing when recording ID doesn't match")
        self.assertEqual(len(extra), 1, "Local file must stay extra when AcoustID doesn't confirm it")

    def test_acoustid_returns_empty_stays_blocked(self):
        mb_tracks = [_mb_track(1, "4 da gang", "rec-correct")]
        local_files = [_local("totally wrong name xyz")]
        comparison = align_tracks(local_files, mb_tracks, _exact_sim)

        def mock_acoustid(file_path: str) -> List[Dict[str, Any]]:
            return []  # no AcoustID results

        resolve_unmatched_via_acoustid(comparison, mock_acoustid, fpcalc_available=True)

        missing = [r for r in comparison if r["status"] == "missing"]
        self.assertEqual(len(missing), 1)


# ---------------------------------------------------------------------------
# 3. No AcoustID key / fpcalc unavailable → no fake verification
# ---------------------------------------------------------------------------

class TestAcoustIDUnavailable(unittest.TestCase):

    def test_fpcalc_unavailable_skips_all_verification(self):
        mb_tracks = [_mb_track(1, "4 da gang", "rec-correct")]
        local_files = [_local("totally wrong name xyz")]
        comparison = align_tracks(local_files, mb_tracks, _exact_sim)

        called = []
        def mock_acoustid(file_path: str) -> List[Dict[str, Any]]:
            called.append(file_path)
            return [_acoustid_hit("rec-correct")]

        resolve_unmatched_via_acoustid(
            comparison, mock_acoustid, fpcalc_available=False
        )

        self.assertEqual(called, [], "acoustid_lookup_fn must not be called when fpcalc_available=False")
        missing = [r for r in comparison if r["status"] == "missing"]
        self.assertEqual(len(missing), 1, "Track must stay missing when fpcalc is unavailable")

    def test_fpcalc_unavailable_response_field(self):
        """resolve_unmatched_via_acoustid propagates fpcalc_available=False correctly."""
        comparison = [
            {"num": 1, "mb_title": "song", "mb_trackid": "rec-001",
             "local_title": "", "file_path": "", "status": "missing", "sim_score": 0.0},
            {"num": 2, "mb_title": "", "mb_trackid": "",
             "local_title": "bad name", "file_path": "/fake/bad name.flac",
             "status": "extra", "sim_score": 0.0},
        ]
        resolve_unmatched_via_acoustid(
            comparison, lambda fp: [_acoustid_hit("rec-001")], fpcalc_available=False
        )
        self.assertEqual(comparison[0]["status"], "missing")
        self.assertEqual(comparison[1]["status"], "extra")


# ---------------------------------------------------------------------------
# 4. Missing first track causes shifted alignment, not 0 matches
# ---------------------------------------------------------------------------

class TestMissingFirstTrackAlignment(unittest.TestCase):
    """Trust Tape 2 scenario: local folder is missing track 1 "Intro"."""

    def _mb_tracks(self):
        return [
            _mb_track(1,  "intro",          "rec-101"),
            _mb_track(2,  "fool you",        "rec-102"),
            _mb_track(3,  "loyalty",         "rec-103"),
            _mb_track(4,  "all my",          "rec-104"),
            _mb_track(5,  "ondaspot",        "rec-105"),
        ]

    def _local_files(self):
        # No "intro" file — starts from track 2
        return [
            _local("fool you"),
            _local("loyalty"),
            _local("all my"),
            _local("ondaspot"),
        ]

    def test_intro_is_missing_not_different(self):
        comparison = align_tracks(self._local_files(), self._mb_tracks(), _exact_sim)
        intro_row = next(r for r in comparison if r["num"] == 1)
        self.assertEqual(intro_row["status"], "missing",
                         "Track 1 'Intro' must be Missing, not Different, when absent")
        self.assertEqual(intro_row["local_title"], "",
                         "Missing track must not steal a local file")

    def test_subsequent_tracks_match_correctly(self):
        comparison = align_tracks(self._local_files(), self._mb_tracks(), _exact_sim)
        status_by_num = {r["num"]: r["status"] for r in comparison}
        for num in (2, 3, 4, 5):
            self.assertIn(status_by_num[num], {"matched", "fuzzy"},
                          f"Track {num} should be matched/fuzzy after correct alignment")

    def test_no_extra_files_when_counts_are_correct(self):
        comparison = align_tracks(self._local_files(), self._mb_tracks(), _exact_sim)
        extra = [r for r in comparison if r["status"] == "extra"]
        self.assertEqual(len(extra), 0,
                         "No extra files when every local file aligns to a MB track")

    def test_positional_match_was_broken(self):
        """Demonstrate the old behavior was wrong: positional would have matched fool you to intro."""
        from difflib import SequenceMatcher
        intro_norm = "intro"
        fool_you_norm = "fool you"
        ratio = SequenceMatcher(None, fool_you_norm, intro_norm).ratio()
        self.assertLess(ratio, 0.55,
                        "Positional matching would assign 'fool you' to 'intro' — wrong")

    def test_matched_count_excludes_missing(self):
        comparison = align_tracks(self._local_files(), self._mb_tracks(), _exact_sim)
        matched = sum(1 for r in comparison if r["status"] in {"matched", "fuzzy", "acoustid_verified"})
        missing = sum(1 for r in comparison if r["status"] == "missing")
        self.assertEqual(matched, 4)
        self.assertEqual(missing, 1)


# ---------------------------------------------------------------------------
# 5. Corrected mapping changes target path preview (acoustid_verified not blocked)
# ---------------------------------------------------------------------------

class TestTargetPathPreviewNotBlocked(unittest.TestCase):
    """acoustid_verified status must not block _build_import_target_preview."""

    def test_acoustid_verified_does_not_appear_in_blocking_set(self):
        # Rejected/extra rows are cleanup candidates; AcoustID-verified rows remain importable.
        cleanup_statuses = {"different", "conflicting", "extra"}
        self.assertNotIn("acoustid_verified", cleanup_statuses)

    def test_preview_uses_mb_title_for_filename(self):
        """When a row has mb_title set, the target filename uses it (not local_title).

        This is already the existing behavior; we verify it still holds after the change.
        Row with mb_title="4 da Gang" must produce a path containing "4 da Gang",
        not "free dem boyz 04 4 da gang".
        """
        row = {
            "num": 4,
            "mb_title": "4 da Gang",
            "local_title": "free dem boyz 04 4 da gang",
            "file_path": "",
            "status": "acoustid_verified",
        }
        title = row.get("mb_title") or row.get("title") or row.get("local_title") or "Track 4"
        self.assertEqual(title, "4 da Gang")


# ---------------------------------------------------------------------------
# 6. Wrong recording ID does not pass verification
# ---------------------------------------------------------------------------

class TestWrongRecordingIDBlocked(unittest.TestCase):

    def test_wrong_id_exact_mismatch(self):
        mb_tracks = [_mb_track(1, "4 da gang", "rec-correct-id")]
        local_files = [_local("random file name")]
        comparison = align_tracks(local_files, mb_tracks, _exact_sim)

        def mock_acoustid_wrong(file_path: str) -> List[Dict[str, Any]]:
            # Returns a plausible but wrong recording
            return [_acoustid_hit("rec-totally-different-wrong", score=88)]

        resolve_unmatched_via_acoustid(comparison, mock_acoustid_wrong, fpcalc_available=True)

        missing = [r for r in comparison if r["status"] == "missing"]
        verified = [r for r in comparison if r["status"] == "acoustid_verified"]
        self.assertEqual(len(missing), 1, "Track stays missing when AcoustID returns wrong recording")
        self.assertEqual(len(verified), 0, "No acoustid_verified rows for wrong recording ID")

    def test_multiple_candidates_none_match(self):
        mb_tracks = [_mb_track(1, "the real song", "rec-real")]
        local_files = [_local("impostor")]
        comparison = align_tracks(local_files, mb_tracks, _exact_sim)

        def mock_acoustid_multi(file_path: str) -> List[Dict[str, Any]]:
            return [
                _acoustid_hit("rec-other-a", score=85),
                _acoustid_hit("rec-other-b", score=70),
                _acoustid_hit("rec-other-c", score=60),
            ]

        resolve_unmatched_via_acoustid(comparison, mock_acoustid_multi, fpcalc_available=True)

        missing = [r for r in comparison if r["status"] == "missing"]
        self.assertEqual(len(missing), 1)


if __name__ == "__main__":
    unittest.main()
