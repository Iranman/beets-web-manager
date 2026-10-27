"""ARCH-030 2.5 / finding F4: the staged-folder "Find Match" route
(``_ai_suggest_folder_internal``) must apply the SAME carrier-bound protection
the album route got -- seed the MusicBrainz candidate pool from the carrier's
canonical Release Group AND veto unrelated-RG title collisions -- so a
title-collision like FLEUR "FOREVER FOREVER" (RG 2da91738) can never be
seeded or ranked primary for a carrier-bound 112 "Forever" recovery
(RG 37cb4a72). Synthetic MB data only; no real credentials.
"""
import unittest
from unittest import mock

import backend.ai_service as ai
import backend.recovery_carrier as rc
from backend.provider_boundary import ProviderOutcome, ProviderResult

RG_112 = "37cb4a72-1111-1111-1111-111111111111"
REL_112 = "083b9009-2222-2222-2222-222222222222"
RG_FLEUR = "2da91738-3333-3333-3333-333333333333"
REL_FLEUR = "4e4e4e4e-4444-4444-4444-444444444444"


def _cand(rel, rg, album, artist):
    return {"mb_albumid": rel, "mb_releasegroupid": rg,
            "mb_releasegroupurl": f"https://musicbrainz.org/release-group/{rg}",
            "release_group_primary_type": "Album", "album": album, "artist": artist,
            "year": "1996", "score": 100, "tracks": 9, "country": "US", "label": ""}


def _evidence():
    return {
        "audio_files": [],
        "folder_track_count": 9,
        "nested_audio_count": 0,
        "guessed_artist": "112",
        "guessed_artist_mbid": "",
        "guessed_album": "Forever",
        "guessed_year": "1996",
        "track_lines": [],
        "filenames": [],
    }


class CarrierBoundFolderSuggestionTests(unittest.TestCase):
    def _run(self, *, carrier_rg, search, folder_tracks=None, score=None):
        folder_tracks = folder_tracks or mock.MagicMock(return_value=[])
        score = score or (lambda ev, c: {"total": 0.9})
        src_dir = mock.MagicMock()
        src_dir.is_dir.return_value = True
        src_dir.iterdir.return_value = []
        carrier = {"mb_releasegroupid": carrier_rg} if carrier_rg else None
        with mock.patch.object(ai, "_ai_api_key", return_value=""), \
                mock.patch.object(ai, "_build_folder_evidence", return_value=_evidence()), \
                mock.patch.object(ai, "_resolve_import_review_source_path",
                                  return_value=(src_dir, None)), \
                mock.patch.object(ai, "_import_review_folder_signature", return_value=""), \
                mock.patch.object(ai, "_acoustid_multi_file", return_value={}), \
                mock.patch.object(ai, "_acoustid_lookup_cached_outcome",
                                  return_value=ProviderResult("acoustid", ProviderOutcome.NO_RESULT, data=[])), \
                mock.patch.object(ai, "_mb_release_search", search), \
                mock.patch.object(ai, "_mb_release_search_by_folder_tracks", folder_tracks), \
                mock.patch.object(ai, "_discogs_release_fallback_candidate", return_value={}), \
                mock.patch.object(ai, "_score_mb_release_candidate", side_effect=score), \
                mock.patch.object(ai, "_run_ai_release_preflight", return_value={"ok": True}), \
                mock.patch.object(ai, "_apply_ai_preflight_to_suggestion"), \
                mock.patch.object(ai, "_ai_match_evidence_packet", return_value={}), \
                mock.patch.object(rc, "load_for_folder", return_value=carrier):
            res = ai._ai_suggest_folder_internal("/downloads/112 - src missing 123")
        return res["suggestion"], search, folder_tracks

    def test_carrier_rg_seeds_pool_and_fleur_is_neither_seeded_nor_primary(self):
        def search(album_q, artist_q, limit=8, year="", track_count=0, log=None,
                   artist_mbid="", release_group_id=""):
            if release_group_id:
                self.assertEqual(release_group_id, RG_112)
                return [_cand(REL_112, RG_112, "Forever", "112")]
            # The free title path would surface the title-collision FLEUR RG.
            return [_cand(REL_FLEUR, RG_FLEUR, "FOREVER FOREVER", "FLEUR")]

        search = mock.MagicMock(side_effect=search)
        sug, search, folder_tracks = self._run(carrier_rg=RG_112, search=search)

        # Seeded from the carrier's Release Group, never the FLEUR collision.
        self.assertEqual(sug["mb_releasegroupid"], RG_112)
        self.assertEqual(sug["mb_albumid"], REL_112)
        self.assertNotEqual(sug["mb_releasegroupid"], RG_FLEUR)
        seed_calls = [c for c in search.call_args_list if c.kwargs.get("release_group_id")]
        self.assertEqual(len(seed_calls), 1)
        self.assertEqual(seed_calls[0].args[0], "")  # RG, not a title, in the query
        free_calls = [c for c in search.call_args_list if not c.kwargs.get("release_group_id")]
        self.assertEqual(free_calls, [], "a carrier-bound recovery must not run a free title search")
        folder_tracks.assert_not_called()

    def test_veto_demotes_unrelated_rg_even_if_scored_higher(self):
        # Defense in depth: even if an unrelated-RG candidate slips into the
        # pool AND the scorer ranks it first, the relatedness veto sinks it
        # below the carrier's Release Group so it never becomes primary.
        def search(album_q, artist_q, limit=8, year="", track_count=0, log=None,
                   artist_mbid="", release_group_id=""):
            return [_cand(REL_FLEUR, RG_FLEUR, "FOREVER FOREVER", "FLEUR"),
                    _cand(REL_112, RG_112, "Forever", "112")]

        def score(ev, c):
            return {"total": 0.99 if c["mb_releasegroupid"] == RG_FLEUR else 0.10}

        search = mock.MagicMock(side_effect=search)
        sug, search, _ = self._run(carrier_rg=RG_112, search=search, score=score)
        self.assertEqual(sug["mb_releasegroupid"], RG_112)
        self.assertEqual(sug["mb_albumid"], REL_112)

    def test_empty_rgid_search_falls_back_to_rg_scoped_folder_tracks(self):
        search = mock.MagicMock(return_value=[])
        folder_tracks = mock.MagicMock(return_value=[_cand(REL_112, RG_112, "Forever", "112")])
        sug, search, folder_tracks = self._run(
            carrier_rg=RG_112, search=search, folder_tracks=folder_tracks)
        self.assertEqual(folder_tracks.call_args.kwargs["release_group_id"], RG_112)
        self.assertEqual(sug["mb_releasegroupid"], RG_112)

    def test_no_carrier_new_album_path_is_unchanged(self):
        new_cand = _cand("5a5a5a5a-5555-5555-5555-555555555555",
                         "6b6b6b6b-6666-6666-6666-666666666666", "Forever", "112")

        def search(album_q, artist_q, limit=8, year="", track_count=0, log=None,
                   artist_mbid="", release_group_id=""):
            self.assertFalse(release_group_id, "no carrier RG must mean no rgid seed")
            self.assertEqual(album_q, "Forever")
            self.assertEqual(artist_q, "112")
            return [new_cand]

        search = mock.MagicMock(side_effect=search)
        sug, search, folder_tracks = self._run(carrier_rg="", search=search)
        self.assertEqual(sug["mb_albumid"], new_cand["mb_albumid"])
        self.assertEqual(sug["mb_releasegroupid"], new_cand["mb_releasegroupid"])
        self.assertTrue(search.called)
        folder_tracks.assert_not_called()


if __name__ == "__main__":
    unittest.main()
