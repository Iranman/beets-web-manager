"""QA (#447): in a mixed fill, one proven requested track never carries an
unproven file into the merge -- even with the recovery slot authorized."""
import types
import unittest
from unittest import mock

from backend import import_service as isvc

WANTED = [{"disc": 1, "track": 2, "mb_trackid": "rec-2", "title": "Two"},
          {"disc": 1, "track": 3, "mb_trackid": "rec-3", "title": "Three"}]
TRACKS = {"ok": True, "tracks": [
    {"disc": 1, "track": 2, "mb_trackid": "rec-2", "title": "Two", "artist": "A", "length": 180},
    {"disc": 1, "track": 3, "mb_trackid": "rec-3", "title": "Three", "artist": "A", "length": 200}]}
PROVEN = {"id": 1, "disc": 1, "track": 2, "mb_trackid": "rec-2", "title": "Two",
          "path": "/m/proven.flac", "length": 180.0}


def _row(**kw):
    r = {"id": 2, "disc": 1, "track": 3, "mb_trackid": "rec-3", "title": "Three",
         "path": "/m/other.flac", "length": 200.0}
    r.update(kw)
    return r


class MixedFillTests(unittest.TestCase):
    def _validate(self, other, *, other_cands, other_failure="", other_verdict="accept"):
        cw = isvc.composite_workflows
        cands = {"/m/proven.flac": [{"mb_trackid": "rec-2"}], "/m/other.flac": other_cands}
        fail = {"/m/proven.flac": "", "/m/other.flac": other_failure}

        def lookup(p):
            return types.SimpleNamespace(data=cands[p], path=p)

        def verdict(c, expected_title, expected_artist, expected_recording_id):
            if c == cands["/m/proven.flac"]:
                return {"decision": "accept", "recording_id": "rec-2"}
            return {"decision": other_verdict, "recording_id": "rec-x"}
        with mock.patch.object(isvc, "_fetch_mb_release_tracklist", return_value=TRACKS), \
                mock.patch.object(cw, "find_all_items_by_album_id", return_value=[PROVEN, other]), \
                mock.patch.object(isvc, "_album_item_abs_path", side_effect=lambda p: p), \
                mock.patch.object(isvc, "acoustid_failure_status", side_effect=lambda lk: fail[lk.path]), \
                mock.patch.object(isvc, "_acoustid_lookup_cached_outcome", side_effect=lookup), \
                mock.patch.object(isvc, "verify_audio_against_request", side_effect=verdict):
            return isvc._validate_wanted_album_items_with_acoustid(7, "rel", WANTED, [], slot_authorized=True)

    def test_proven_plus_mismatch_refuses(self):
        res = self._validate(_row(), other_cands=[{"mb_trackid": "rec-x"}], other_verdict="reject")
        self.assertFalse(res["ok"], res)

    def test_proven_plus_not_checked_refuses(self):
        res = self._validate(_row(), other_cands=[], other_failure="lookup_failed")
        self.assertFalse(res["ok"], res)

    def test_proven_plus_unknown_refuses(self):
        res = self._validate(_row(track=9, mb_trackid="rec-x", title="Nope"), other_cands=[])
        self.assertFalse(res["ok"], res)

    def test_proven_plus_fuzzy_only_no_result_refuses(self):
        res = self._validate(_row(track=9, mb_trackid=""), other_cands=[])
        self.assertFalse(res["ok"], res)
        self.assertEqual([u["item_id"] for u in res["unverified"]], [2])

    def test_proven_plus_slot_row_tagged_as_other_recording_refuses_even_if_fp_matches(self):
        # Slot 1/3 is requested, but the row carries a different recording id.
        res = self._validate(_row(mb_trackid="rec-other"), other_cands=[{"mb_trackid": "rec-3"}])
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["confirmed"], 1)

    def test_proven_plus_review_verdict_refuses(self):
        res = self._validate(_row(), other_cands=[{"mb_trackid": "rec-3"}], other_verdict="review")
        self.assertFalse(res["ok"], res)

    def test_proven_plus_slot_matched_no_result_is_honored_when_authorized(self):
        # ADR 0007 policy unchanged: a requested slot with no AcoustID result.
        res = self._validate(_row(), other_cands=[])
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["confirmed"], res["no_result"]), (1, 1))

    def test_both_proven_passes(self):
        res = self._validate(_row(), other_cands=[{"mb_trackid": "rec-3"}])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["confirmed"], 2)


if __name__ == "__main__":
    unittest.main()
