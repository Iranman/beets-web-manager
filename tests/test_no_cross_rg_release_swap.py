"""A confirmed or suggested Release is never replaced by one in another
Release Group (music-identity review of #295, F1-F3). Synthetic MB data."""
import types
import unittest
from unittest import mock

import backend.ai_service as ai
import backend.musicbrainz_service as msvc

SINGLE_REL = "55555555-5555-5555-5555-555555555555"
SINGLE_RG = "66666666-6666-6666-6666-666666666666"
ALBUM_REL = "77777777-7777-7777-7777-777777777777"
ALBUM_RG = "88888888-8888-8888-8888-888888888888"
REC = "99999999-9999-9999-9999-999999999999"


def _tracklist(primary_type):
    def fetch(rid, log=None):
        if rid == ALBUM_REL:
            return {"ok": True, "release_group": ALBUM_RG, "release_group_primary_type": "Album",
                    "tracks": [{"mb_trackid": REC, "title": "Song"}]}
        return {"ok": True, "release_group": SINGLE_RG, "release_group_primary_type": primary_type,
                "tracks": [{"mb_trackid": REC, "title": "Song"}]}
    return fetch


class SuggestionKeepsTheCandidateReleaseTests(unittest.TestCase):
    """F1 + F3: the selected candidate's Release, Release Group and URL are
    surfaced unchanged, whatever its type, even when an album containing
    the same recording exists in another Release Group."""

    def _suggest(self, primary_type):
        album = types.SimpleNamespace(id=7, album="Song", albumartist="Bwm Synthetic", year=2001,
                                      items=lambda: [])
        cand = {"mb_albumid": SINGLE_REL, "mb_releasegroupid": SINGLE_RG,
                "mb_releasegroupurl": f"https://musicbrainz.org/release-group/{SINGLE_RG}",
                "release_group_primary_type": primary_type, "album": "Song",
                "artist": "Bwm Synthetic", "year": "2001", "score": 100, "tracks": 1,
                "country": "XW", "label": ""}
        fetch = _tracklist(primary_type)
        with mock.patch.object(ai, "_ai_api_key", return_value=""), \
                mock.patch.object(ai, "_album_preflight_folder", return_value=""), \
                mock.patch.object(ai, "_acoustid_multi_file", return_value={}), \
                mock.patch.object(ai, "_acoustid_lookup_cached", return_value=[]), \
                mock.patch.object(ai, "_mb_release_search", return_value=[cand]), \
                mock.patch.object(ai, "_score_mb_release_candidate", return_value={"total": 0.9}), \
                mock.patch.object(ai, "_fetch_mb_release_tracklist", side_effect=fetch), \
                mock.patch.object(ai, "_run_ai_release_preflight", return_value={"ok": True}), \
                mock.patch.object(ai, "_apply_ai_preflight_to_suggestion"), \
                mock.patch.object(ai, "_ai_match_evidence_packet", return_value={}), \
                mock.patch.object(msvc, "_fetch_mb_release_tracklist", side_effect=fetch, create=True), \
                mock.patch.object(msvc, "_fetch_mb_recording_details", create=True,
                                  return_value={"mb_albumid": ALBUM_REL}):
            return ai._ai_suggest_album_internal(album, [])["suggestion"]

    def test_candidate_release_passes_through_for_single_ep_and_album(self):
        for primary_type in ("Single", "EP", "Album"):
            with self.subTest(primary_type):
                sug = self._suggest(primary_type)
                self.assertEqual(sug["mb_albumid"], SINGLE_REL)
                self.assertEqual(sug["mb_releasegroupid"], SINGLE_RG)
                self.assertEqual(sug["mb_releasegroupurl"],
                                 f"https://musicbrainz.org/release-group/{SINGLE_RG}")


if __name__ == "__main__":
    unittest.main()
