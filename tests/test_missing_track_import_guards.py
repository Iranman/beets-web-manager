import re
import unittest
from pathlib import Path


APP_SOURCE = Path(__file__).resolve().parents[1] / "app.py"


class MissingTrackImportGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_SOURCE.read_text(encoding="utf-8")

    def test_pre_rename_uses_existing_template_token_cleaner(self):
        self.assertNotIn("_TMPL_RE", self.source)
        self.assertIn("_UNRESOLVED_TEMPLATE_TOKEN_RE.search(f.stem)", self.source)
        self.assertIn("_clean_template_token_stem(f.stem)", self.source)

    def test_mixed_missing_track_downloads_stage_safe_subset(self):
        self.assertNotIn("if useful_files and not unknown_count", self.source)
        self.assertIn("if useful_files:", self.source)
        self.assertIn("downloaded file(s) that did not", self.source)
        self.assertIn("safely match requested missing MusicBrainz tracks", self.source)

    def test_ytdlp_missing_track_search_prefers_album_qualified_query(self):
        self.assertIn("def _ytdlp_track_queries", self.source)
        self.assertIn('queries = [f"{prefix}:{artist} {album} {title}"]', self.source)
        self.assertIn('queries.append(f"{prefix}:{artist} {title}")', self.source)
        self.assertIn("_ytdlp_track_queries(source, artist, album, title, year)", self.source)

    def test_partial_slskd_missing_track_match_can_fallback_to_direct_sources(self):
        self.assertNotIn("best_wanted_count < min_wanted", self.source)
        self.assertIn("best_wanted_count < 1", self.source)
        self.assertIn("Candidate is partial; unresolved requested track(s)", self.source)
        self.assertIn("_wanted_tracks_satisfied_by_names", self.source)
        self.assertIn("SLSKD satisfied", self.source)
        self.assertIn("trying direct sources for remaining", self.source)
        self.assertIn("_try_direct_sources_after_slskd", self.source)
        self.assertIn('combined["method"] = f"slskd+', self.source)
        self.assertNotIn('"method"] = "slskd+ytdlp"', self.source)

    def test_partial_existing_album_review_reason_uses_scan_counts(self):
        self.assertIn('result["expected_count"] = int(comp.get("expected_count") or 0)', self.source)
        self.assertIn('result["in_library"] = int(comp.get("in_library") or 0)', self.source)
        self.assertIn("_partial_in_library = int(scan.get(\"in_library\") or 0)", self.source)
        self.assertIn("_partial_expected = int(", self.source)
        self.assertNotIn("Repaired {comp.get('in_library', 0)}/", self.source)

    def test_selected_release_validation_accepts_clean_partial_source(self):
        self.assertIn("clean_partial_import = bool(", self.source)
        self.assertIn("and actual < expected", self.source)
        self.assertIn("and (actual >= min(6, expected)", self.source)
        self.assertIn("and matches >= max(1, int(math.ceil(actual * 0.90)))", self.source)
        self.assertIn("and unmatched == 0", self.source)
        self.assertIn("and duplicate_matches == 0", self.source)
        self.assertIn("ok = matches >= min_required or clean_partial_import", self.source)
        self.assertIn("Clean partial import accepted:", self.source)
        self.assertIn("missing_expected_tracks", self.source)


if __name__ == "__main__":
    unittest.main()
