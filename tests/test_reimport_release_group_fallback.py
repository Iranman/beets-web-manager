"""Issue #260: an explicit release-group request to the reimport resolver must
never return a release from another release group."""
import unittest
from unittest.mock import patch

import backend.library_service as ls

RG = "11111111-1111-1111-1111-111111111111"
RG_URL = f"https://musicbrainz.org/release-group/{RG}"
REL_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"  # in RG, ranked first
REL_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"  # in RG
OTHER = "cccccccc-cccc-cccc-cccc-cccccccccccc"  # in another release group
FOLDER = "/downloads/Artist - Album"


def _pre(fits):
    return {"ok": fits, "matches": 10 if fits else 2, "expected": 10, "audio_count": 10}


class ExplicitReleaseGroupFallbackTests(unittest.TestCase):
    def _run(self, mb_input, fitting, rg_first=REL_A, group=(REL_A, REL_B)):
        candidates = [{"mb_albumid": r, "track_count": 10} for r in group]
        search_hit = [{"mb_albumid": OTHER, "artist": "Artist", "album": "Album", "tracks": 10}]
        log = []
        with patch.object(ls, "_folder_import_track_count", return_value=10), \
             patch.object(ls, "_resolve_release_group_to_release", return_value=rg_first), \
             patch.object(ls, "_mb_release_group_candidates", return_value=candidates, create=True), \
             patch.object(ls, "_folder_release_preflight",
                          side_effect=lambda _f, rel, **_k: _pre(rel in fitting)), \
             patch.object(ls, "_mb_release_search", return_value=search_hit), \
             patch.object(ls, "_mb_release_search_by_folder_tracks", return_value=search_hit), \
             patch.object(ls, "_resolve_mb_release_id",
                          side_effect=lambda s, _l: REL_A if "release/" in s else ""), \
             patch.object(ls, "_mb_release_has_tracks", return_value=True):
            got = ls._resolve_album_release_for_import(
                mb_input, "Artist", "Album", "", 10, log, source_folder=FOLDER)
        return got, log

    def test_ranked_group_release_fits(self):
        got, _ = self._run(RG_URL, fitting={REL_A, OTHER})
        self.assertEqual(got, REL_A)

    def test_other_release_in_group_fits(self):
        got, _ = self._run(RG_URL, fitting={REL_B, OTHER})
        self.assertEqual(got, REL_B)

    def test_no_release_in_group_fits_refuses(self):
        got, log = self._run(RG_URL, fitting={OTHER})
        self.assertEqual(got, "")
        self.assertNotIn(OTHER, "\n".join(log))
        self.assertTrue(any("REFUSED" in line and RG in line for line in log))

    def test_musicbrainz_down_fails_closed(self):
        got, _ = self._run(RG_URL, fitting={OTHER}, rg_first="", group=())
        self.assertEqual(got, "")

    def test_explicit_release_id_still_works(self):
        got, _ = self._run(f"https://musicbrainz.org/release/{REL_A}", fitting={REL_A})
        self.assertEqual(got, REL_A)

    def test_never_returns_release_group_uuid(self):
        for fitting in ({REL_A}, {REL_B}, {OTHER}, set()):
            got, _ = self._run(RG_URL, fitting=fitting)
            self.assertNotEqual(got, RG)


if __name__ == "__main__":
    unittest.main()
