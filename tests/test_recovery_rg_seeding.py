"""ARCH-030 2.5: a carrier-bound recovery seeds the MusicBrainz candidate pool
from the carrier's canonical Release Group, so generic title matching can never
replace a known target album (requirement 4). Synthetic MB data only.

Scenario anchor (shared with test_mtr_deterministic_slot): 112 "Forever"
(RG 37cb4a72) vs the title-collision FLEUR "FOREVER FOREVER" (RG 2da91738), an
unrelated real release that a folder/tag title match would otherwise surface.
"""
import types
import unittest
import urllib.parse
from unittest import mock

import backend.ai_service as ai
import backend.musicbrainz_service as msvc
from backend.provider_boundary import ProviderOutcome, ProviderResult

RG_112 = "37cb4a72-1111-1111-1111-111111111111"
REL_112 = "083b9009-2222-2222-2222-222222222222"
RG_FLEUR = "2da91738-3333-3333-3333-333333333333"
REL_FLEUR = "4e4e4e4e-4444-4444-4444-444444444444"
RG_COMPILATION = "5f5f5f5f-5555-5555-5555-555555555555"
REL_COMPILATION = "6a6a6a6a-6666-6666-6666-666666666666"


def _cand(rel, rg, album, artist):
    return {"mb_albumid": rel, "mb_releasegroupid": rg,
            "mb_releasegroupurl": f"https://musicbrainz.org/release-group/{rg}",
            "release_group_primary_type": "Album", "album": album, "artist": artist,
            "year": "1996", "score": 100, "tracks": 9, "country": "US", "label": ""}


def _tracklist(rid, log=None):
    if rid == REL_FLEUR:
        return {"ok": True, "release_group": RG_FLEUR, "release_group_primary_type": "Album",
                "tracks": [{"mb_trackid": "rec-fleur-1", "title": "Forever Forever"}]}
    return {"ok": True, "release_group": RG_112, "release_group_primary_type": "Album",
            "tracks": [{"mb_trackid": "rec-112-1", "title": "Forever (Intro)"}]}


class CarrierBoundSuggestionSeedsFromReleaseGroupTests(unittest.TestCase):
    def _run(self, album, search):
        """Drive _ai_suggest_album_internal with _mb_release_search replaced by
        ``search`` (a MagicMock). Returns (suggestion, search, folder_tracks_mock)."""
        folder_tracks = mock.MagicMock(return_value=[])
        with mock.patch.object(ai, "_ai_api_key", return_value=""), \
                mock.patch.object(ai, "_album_preflight_folder", return_value=""), \
                mock.patch.object(ai, "_acoustid_multi_file", return_value={}), \
                mock.patch.object(ai, "_acoustid_lookup_cached_outcome",
                                  return_value=ProviderResult("acoustid", ProviderOutcome.NO_RESULT, data=[])), \
                mock.patch.object(ai, "_mb_release_search", search), \
                mock.patch.object(ai, "_mb_release_search_by_folder_tracks", folder_tracks), \
                mock.patch.object(ai, "_score_mb_release_candidate", return_value={"total": 0.9}), \
                mock.patch.object(ai, "_fetch_mb_release_tracklist", side_effect=_tracklist), \
                mock.patch.object(ai, "_run_ai_release_preflight", return_value={"ok": True}), \
                mock.patch.object(ai, "_apply_ai_preflight_to_suggestion"), \
                mock.patch.object(ai, "_ai_match_evidence_packet", return_value={}), \
                mock.patch.object(msvc, "_fetch_mb_release_tracklist", side_effect=_tracklist, create=True), \
                mock.patch.object(msvc, "_fetch_mb_recording_details", create=True,
                                  return_value={"mb_albumid": REL_FLEUR}):
            sug = ai._ai_suggest_album_internal(album, [])["suggestion"]
        return sug, search, folder_tracks

    def test_carrier_rg_seeds_pool_and_fleur_is_neither_seeded_nor_primary(self):
        # The carrier binds RG 37cb4a72 onto the album. A title search would
        # return the unrelated FLEUR release; the rgid-seeded search returns 112.
        album = types.SimpleNamespace(id=112, album="Forever", albumartist="112",
                                      year=1996, mb_releasegroupid=RG_112, items=lambda: [])

        def search(album_q, artist_q, limit=8, year="", track_count=0, log=None,
                   artist_mbid="", release_group_id=""):
            if release_group_id:
                self.assertEqual(release_group_id, RG_112)
                return [_cand(REL_112, RG_112, "Forever", "112")]
            # The free title path would surface the title-collision FLEUR RG.
            return [_cand(REL_FLEUR, RG_FLEUR, "FOREVER FOREVER", "FLEUR")]

        search = mock.MagicMock(side_effect=search)
        sug, search, folder_tracks = self._run(album, search)

        # Seeded from the carrier's Release Group...
        self.assertEqual(sug["mb_releasegroupid"], RG_112)
        self.assertEqual(sug["mb_albumid"], REL_112)
        # ...and the FLEUR RG was NEITHER seeded (no free title search ran) NOR
        # surfaced as the suggestion.
        self.assertNotEqual(sug["mb_releasegroupid"], RG_FLEUR)
        seed_calls = [c for c in search.call_args_list if c.kwargs.get("release_group_id")]
        self.assertEqual(len(seed_calls), 1)
        self.assertEqual(seed_calls[0].args[0], "")  # album positional empty: RG, not title
        free_calls = [c for c in search.call_args_list if not c.kwargs.get("release_group_id")]
        self.assertEqual(free_calls, [], "a carrier-bound recovery must not run a free title search")
        folder_tracks.assert_not_called()

    def test_empty_rgid_search_falls_back_to_rg_scoped_folder_tracks(self):
        # RG search index lag: the rgid search returns nothing. The fallback
        # must stay bound to the RG (folder-track search scoped to it), never a
        # free title match that could land on another Release Group.
        album = types.SimpleNamespace(id=112, album="Forever", albumartist="112",
                                      year=1996, mb_releasegroupid=RG_112, items=lambda: [])
        search = mock.MagicMock(return_value=[])
        folder_tracks = mock.MagicMock(return_value=[_cand(REL_112, RG_112, "Forever", "112")])
        with mock.patch.object(ai, "_ai_api_key", return_value=""), \
                mock.patch.object(ai, "_album_preflight_folder", return_value=""), \
                mock.patch.object(ai, "_acoustid_multi_file", return_value={}), \
                mock.patch.object(ai, "_acoustid_lookup_cached_outcome",
                                  return_value=ProviderResult("acoustid", ProviderOutcome.NO_RESULT, data=[])), \
                mock.patch.object(ai, "_mb_release_search", search), \
                mock.patch.object(ai, "_mb_release_search_by_folder_tracks", folder_tracks), \
                mock.patch.object(ai, "_score_mb_release_candidate", return_value={"total": 0.9}), \
                mock.patch.object(ai, "_fetch_mb_release_tracklist", side_effect=_tracklist), \
                mock.patch.object(ai, "_run_ai_release_preflight", return_value={"ok": True}), \
                mock.patch.object(ai, "_apply_ai_preflight_to_suggestion"), \
                mock.patch.object(ai, "_ai_match_evidence_packet", return_value={}), \
                mock.patch.object(msvc, "_fetch_mb_release_tracklist", side_effect=_tracklist, create=True), \
                mock.patch.object(msvc, "_fetch_mb_recording_details", create=True,
                                  return_value={"mb_albumid": REL_112}):
            sug = ai._ai_suggest_album_internal(album, [])["suggestion"]
        self.assertEqual(folder_tracks.call_args.kwargs["release_group_id"], RG_112)
        self.assertEqual(sug["mb_releasegroupid"], RG_112)

    def test_no_carrier_new_album_path_is_unchanged(self):
        # A genuine new-album import has no carrier RG: the text search runs
        # exactly as before and the rgid seed is never used.
        album = types.SimpleNamespace(id=5, album="Some New Album", albumartist="New Artist",
                                      year=2020, items=lambda: [])
        new_cand = _cand("5a5a5a5a-5555-5555-5555-555555555555",
                         "6b6b6b6b-6666-6666-6666-666666666666", "Some New Album", "New Artist")

        def search(album_q, artist_q, limit=8, year="", track_count=0, log=None,
                   artist_mbid="", release_group_id=""):
            self.assertFalse(release_group_id, "no carrier RG must mean no rgid seed")
            self.assertEqual(album_q, "Some New Album")
            self.assertEqual(artist_q, "New Artist")
            return [new_cand]

        search = mock.MagicMock(side_effect=search)
        sug, search, folder_tracks = self._run(album, search)
        self.assertEqual(sug["mb_albumid"], new_cand["mb_albumid"])
        self.assertEqual(sug["mb_releasegroupid"], new_cand["mb_releasegroupid"])
        self.assertTrue(search.called)
        folder_tracks.assert_not_called()


class FolderTrackSearchScopesToReleaseGroupTests(unittest.TestCase):
    """_mb_release_search_by_folder_tracks (the generic title matcher) binds to
    a carrier RG when given one: every recording query is rgid-scoped and the
    resulting candidates carry that RG; without one, behavior is unchanged."""

    def _run(self, release_group_id):
        captured_queries = []

        class _Resp:
            def __init__(self, body):
                self._body = body
            def read(self):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def opened(service, req, timeout=0):
            captured_queries.append(urllib.parse.unquote_plus(req.full_url))
            # The matched recording appears on TWO releases: the target-RG
            # "Forever" release AND a foreign compilation release in a DIFFERENT
            # Release Group. A real MB recording search returns release-group
            # per release; the rgid: filter scoped the recording, not the
            # releases it lists.
            body = ('{"recordings": [{"artist-credit": [{"artist": {"name": "112"}}],'
                    ' "releases": ['
                    '{"id": "' + REL_112 + '", "title": "Forever", "date": "1996",'
                    ' "country": "US", "release-group": {"id": "' + RG_112 + '"}},'
                    '{"id": "' + REL_COMPILATION + '", "title": "90s R&B Hits",'
                    ' "date": "2005", "country": "US",'
                    ' "release-group": {"id": "' + RG_COMPILATION + '"}}'
                    ']}]}').encode()
            return _Resp(body)

        with mock.patch.object(msvc, "_folder_track_search_titles", return_value=["Forever (Intro)"]), \
                mock.patch.object(msvc.provider_boundary, "opened", side_effect=opened):
            cands = msvc._mb_release_search_by_folder_tracks(
                "unused", existing_album_id=112, artist="112",
                release_group_id=release_group_id)
        return cands, captured_queries

    def test_rg_scoped_query_and_binding_stamp(self):
        cands, queries = self._run(RG_112)
        self.assertTrue(cands)
        self.assertTrue(all(f"rgid:{RG_112}" in q for q in queries),
                        f"recording query not scoped to RG: {queries}")
        self.assertTrue(all(c["mb_releasegroupid"] == RG_112 for c in cands))

    def test_foreign_compilation_release_is_not_surfaced_as_target_rg(self):
        # C6: a recording that also appears on a compilation in another Release
        # Group must NOT be surfaced (and never relabeled) as a target-RG
        # candidate -- the exact "incorrect album suggestions" this fixes.
        cands, _ = self._run(RG_112)
        rel_ids = {c["mb_albumid"] for c in cands}
        self.assertIn(REL_112, rel_ids)
        self.assertNotIn(REL_COMPILATION, rel_ids)
        self.assertFalse(any(c["mb_albumid"] == REL_COMPILATION for c in cands))

    def test_release_without_verifiable_rg_is_dropped_when_scoped(self):
        # Fail closed: a release whose own release-group id is missing cannot be
        # verified as belonging to the target RG, so it is dropped (never
        # fabricated into it).
        class _Resp:
            def __init__(self, body):
                self._body = body
            def read(self):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def opened(service, req, timeout=0):
            body = ('{"recordings": [{"artist-credit": [{"artist": {"name": "112"}}],'
                    ' "releases": [{"id": "' + REL_112 + '", "title": "Forever",'
                    ' "date": "1996", "country": "US"}]}]}').encode()
            return _Resp(body)

        with mock.patch.object(msvc, "_folder_track_search_titles", return_value=["Forever (Intro)"]), \
                mock.patch.object(msvc.provider_boundary, "opened", side_effect=opened):
            cands = msvc._mb_release_search_by_folder_tracks(
                "unused", existing_album_id=112, artist="112", release_group_id=RG_112)
        self.assertEqual(cands, [], "an unverifiable release must be dropped under rgid scope")

    def test_no_rg_is_unchanged_generic_matching(self):
        cands, queries = self._run("")
        self.assertTrue(cands)
        self.assertFalse(any("rgid:" in q for q in queries))
        self.assertFalse(any(c.get("mb_releasegroupid") for c in cands))


if __name__ == "__main__":
    unittest.main()
