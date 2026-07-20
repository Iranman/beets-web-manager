"""Regression test: the original readiness-redesign spec explicitly required
AcoustID readiness to stay collapsed until the AcoustID stage is reached ("Do
not show API-key and runtime failures as active blockers during MusicBrainz
preparation"). That was done for the compact readiness card, but the full
"AcoustID Submission" section (per-track fingerprint status grid + Submit
Fingerprints button) was still rendered unconditionally on every stage,
showing "Missing recording MBID" for every track before recording IDs even
exist yet -- exactly the kind of premature AcoustID noise the redesign was
supposed to remove.
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUBMISSIONS_SOURCE = (ROOT / "frontend" / "src" / "views" / "Submissions.tsx").read_text(encoding="utf-8")


class AcoustidSectionStageGateTests(unittest.TestCase):
    def test_full_acoustid_section_gated_on_acoustid_or_complete_stage(self):
        gate_pos = SUBMISSIONS_SOURCE.index("target?.preflight.current_stage === 'acoustid' || target?.preflight.current_stage === 'complete'")
        section_pos = SUBMISSIONS_SOURCE.index("AcoustID Submission")
        self.assertLess(gate_pos, section_pos)

    def test_collapsed_placeholder_shown_before_that_stage(self):
        self.assertIn("Available after MusicBrainz recording IDs are attached.", SUBMISSIONS_SOURCE)

    def test_only_one_full_acoustid_submission_section_exists(self):
        self.assertEqual(SUBMISSIONS_SOURCE.count("Submit Fingerprints"), 1)


if __name__ == "__main__":
    unittest.main()
