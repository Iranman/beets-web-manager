from pathlib import Path
import unittest


APP_SOURCE = Path(__file__).resolve().parents[1] / "app.py"


class AcquisitionExtraOnlyCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_SOURCE.read_text(encoding="utf-8")

    def test_extra_only_complete_albums_are_not_download_candidates(self):
        self.assertIn("def _acq_is_extra_only_complete", self.source)
        self.assertIn("def _acq_is_leftover_only_complete", self.source)
        self.assertIn("if _acq_is_extra_only_complete(album) or _acq_is_leftover_only_complete(album):\n        return 0", self.source)
        self.assertIn('"extra_only_complete": extra_only_complete', self.source)
        self.assertIn('"leftover_only_complete": leftover_only_complete', self.source)
        self.assertIn('health.get("extra_only_complete") or health.get("leftover_only_complete")', self.source)

    def test_complete_local_albums_suppress_lidarr_wanted_rows(self):
        self.assertIn("complete_local_by_identity", self.source)
        self.assertIn("complete_local_by_text", self.source)
        self.assertIn("def _acq_locally_satisfies_wanted", self.source)
        self.assertIn("if complete_local_by_identity.get(identity) or complete_local_by_text.get(fallback):", self.source)

    def test_expected_count_uses_representative_tracktotal(self):
        self.assertIn("def _representative_tracktotal", self.source)
        self.assertIn("top_count = max(counts.values())", self.source)
        self.assertIn("per_disc.setdefault(disc, []).append(track_total)", self.source)
        self.assertIn("per_disc.setdefault(disc, []).append(total)", self.source)


if __name__ == "__main__":
    unittest.main()
