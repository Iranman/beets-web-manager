"""A confirmed or suggested Release is never replaced by one in another
Release Group (music-identity review of #295, F1-F3). Synthetic MB data."""
import types
import unittest
from unittest import mock

import backend.ai_service as ai
import backend.library_service as lsvc
import backend.musicbrainz_service as msvc

try:
    import test_import_paths_beets_native as base
except ImportError:  # pragma: no cover
    from tests import test_import_paths_beets_native as base

SINGLE_REL = "55555555-5555-5555-5555-555555555555"
SINGLE_RG = "66666666-6666-6666-6666-666666666666"
ALBUM_REL = "77777777-7777-7777-7777-777777777777"
ALBUM_RG = "88888888-8888-8888-8888-888888888888"
REC = "99999999-9999-9999-9999-999999999999"
SAME_RG_REL = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"  # another Release of SINGLE_RG


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


class ProvidedReleaseStaysInItsReleaseGroupTests(base.ConfirmedImportJobTests):
    """F2: a provided Release that fails the folder preflight is replaced only
    by a Release of its own Release Group; the free search (which would find
    one in another group) is not run, nothing is imported, and the folder
    goes to review. The same holds when the provided ID cannot be resolved."""

    def _reimport(self, *, passing, has_tracks=True, group_candidates=(), album=None, aldir=None):
        """reimport-disk with SINGLE_REL provided; ``passing`` are the Releases
        whose folder preflight passes. The free search would offer ALBUM_REL
        (another Release Group)."""
        isvc = self.isvc
        self.ad = base._adapter(album or {"id": 9, "mb_albumid": ALBUM_REL, "mb_releasegroupid": ALBUM_RG})
        aldir = aldir or str(isvc.MUSIC_ROOT) + "/Artist/Song"

        def preflight(folder, rid, **k):
            ok = rid in passing
            return {"ok": ok, "matches": 2 if ok else 0, "expected": 2, "audio_count": 2,
                    "release_group": ALBUM_RG if rid == ALBUM_REL else SINGLE_RG}

        self.free_search = mock.MagicMock(return_value=[{"mb_albumid": ALBUM_REL, "tracks": 2,
                                                         "artist": "Artist", "album": "Song"}])
        fetch = _tracklist("Single")
        with mock.patch.object(isvc.composite_workflows, "inspect_import_source",
                               return_value={"ok": True, "path": aldir, "audio_file_count": 2}), \
                mock.patch.object(isvc, "_folder_release_preflight", side_effect=preflight), \
                mock.patch.object(isvc, "_fetch_mb_release_tracklist", side_effect=fetch), \
                mock.patch.object(lsvc, "_folder_release_preflight", side_effect=preflight), \
                mock.patch.object(lsvc, "_folder_import_track_count", return_value=2), \
                mock.patch.object(lsvc, "_resolve_mb_release_id", return_value=SINGLE_REL), \
                mock.patch.object(lsvc, "_mb_release_has_tracks", return_value=has_tracks), \
                mock.patch.object(lsvc, "_resolve_release_group_to_release", return_value=""), \
                mock.patch.object(lsvc, "_fetch_mb_release_tracklist", side_effect=fetch), \
                mock.patch.object(lsvc, "_mb_release_group_candidates", return_value=list(group_candidates)), \
                mock.patch.object(lsvc, "_mb_release_search", self.free_search), \
                mock.patch.object(lsvc, "_mb_release_search_by_folder_tracks", self.free_search):
            body, code = isvc.start_reimport_disk({"aldir": aldir, "mb_albumid": SINGLE_REL,
                                                   "skip_import_lock": True})
        self.assertEqual(code, 200, body)
        return self.result

    def _assert_review_without_import(self, res):
        self.assertEqual(res["status"], "failed", res)
        self.free_search.assert_not_called()
        self.ad.run_import.assert_not_called()
        self.assertTrue(self.isvc._queue_folder_for_manual_review.called)
        log = "\n".join(res.get("log") or [])
        self.assertNotIn("Searching MusicBrainz", log)
        self.assertIn("REFUSED", log)

    def test_failed_preflight_goes_to_review_not_to_another_release_group(self):
        self._assert_review_without_import(self._reimport(passing={ALBUM_REL}))

    def test_unresolvable_provided_release_goes_to_review_not_to_free_search(self):
        # _mb_release_has_tracks is False on a transient MB timeout/rate limit.
        self._assert_review_without_import(self._reimport(passing={ALBUM_REL}, has_tracks=False))

    def test_downloads_source_goes_to_review_too(self):
        # QA F-B: review was queued only for in-library / existing-album sources.
        aldir = str(self.isvc.DOWNLOADS_ROOT) + "/Artist - Song"
        res = self._reimport(passing={ALBUM_REL}, album={}, aldir=aldir)
        self._assert_review_without_import(res)
        args = self.isvc._queue_folder_for_manual_review.call_args.args
        self.assertEqual(args[0], aldir)
        # The rejected Release is handed to the store, which keeps it as rejected_mb_albumid.
        self.assertEqual(args[1]["mb_albumid"], SINGLE_REL)
        self.assertIn("queued for Review", res.get("error") or str(res))

    def test_same_release_group_replacement_is_imported(self):
        res = self._reimport(
            passing={SAME_RG_REL, ALBUM_REL},
            group_candidates=[{"mb_albumid": SAME_RG_REL, "track_count": 2}],
            album={"id": 9, "mb_albumid": SAME_RG_REL, "mb_releasegroupid": SINGLE_RG})
        self.assertEqual(res["status"], "completed", res)
        self.free_search.assert_not_called()
        self.assertEqual(self.ad.run_import.call_args.kwargs["search_ids"], [SAME_RG_REL])
        (tx,) = [t for t in self.store.list()[0] if t.get("operation_type") == "Import"]
        self.assertEqual(tx["status"], "Completed")
        self.assertEqual(tx["metadata"]["engine_result"]["mb_releasegroupid"], SINGLE_RG)


if __name__ == "__main__":
    unittest.main()
