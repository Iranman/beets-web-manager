"""QA additions for issue #260: bare release-group UUID input, manual override,
group-lookup failure after the ranked release is rejected."""
import unittest
from unittest.mock import patch

import backend.library_service as ls

RG = "11111111-1111-1111-1111-111111111111"
REL_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REL_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
OTHER = "cccccccc-cccc-cccc-cccc-cccccccccccc"
FOLDER = "/downloads/Artist - Album"


class BareReleaseGroupUuidTests(unittest.TestCase):
    def _run(self, mb_input, fitting, rg_first=REL_A, group=(REL_A, REL_B), override=False):
        candidates = [{"mb_albumid": r, "track_count": 10} for r in group]
        hit = [{"mb_albumid": OTHER, "artist": "Artist", "album": "Album", "tracks": 10}]
        log = []
        with patch.object(ls, "_folder_import_track_count", return_value=10), \
             patch.object(ls, "_resolve_release_group_to_release", return_value=rg_first), \
             patch.object(ls, "_mb_release_group_candidates", return_value=candidates), \
             patch.object(ls, "_folder_release_preflight",
                          side_effect=lambda _f, rel, **_k: {"ok": rel in fitting, "matches": 10,
                                                             "expected": 10, "audio_count": 10}), \
             patch.object(ls, "_mb_release_search", return_value=hit), \
             patch.object(ls, "_mb_release_search_by_folder_tracks", return_value=hit), \
             patch.object(ls, "_resolve_mb_release_id", side_effect=lambda s, _l: s.lower()), \
             patch.object(ls, "_mb_release_has_tracks", side_effect=lambda r: r != RG):
            got = ls._resolve_album_release_for_import(
                mb_input, "Artist", "Album", "", 10, log, source_folder=FOLDER,
                allow_provided_release_override=override)
        return got, log

    def test_bare_rg_uuid_picks_fitting_group_release(self):
        self.assertEqual(self._run(RG, fitting={REL_B, OTHER})[0], REL_B)

    def test_bare_rg_uuid_refuses_other_group(self):
        got, log = self._run(RG, fitting={OTHER})
        self.assertEqual(got, "")
        self.assertTrue(any("REFUSED" in line for line in log))

    def test_override_prefers_fitting_group_release_then_ranked(self):
        rg_url = f"https://musicbrainz.org/release-group/{RG}"
        self.assertEqual(self._run(rg_url, fitting={REL_B}, override=True)[0], REL_B)
        self.assertEqual(self._run(rg_url, fitting={OTHER}, override=True)[0], REL_A)

    def test_group_listing_fails_after_ranked_rejected(self):
        rg_url = f"https://musicbrainz.org/release-group/{RG}"
        got, _ = self._run(rg_url, fitting={OTHER}, group=())
        self.assertEqual(got, "")


if __name__ == "__main__":
    unittest.main()
