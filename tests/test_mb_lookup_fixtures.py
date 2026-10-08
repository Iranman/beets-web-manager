"""#299 F2: MusicBrainz lookups parsed from synthetic WS2 responses.

The fake server answers 400 for an include WS2 does not accept (as
MusicBrainz does for ``label-info``), so a wrong ``inc`` fails these tests."""

import contextlib
import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import backend.provider_boundary as provider_boundary
import helpers_mb
from backend import matching_service

REL = "11111111-1111-1111-1111-111111111111"
RG = "22222222-2222-2222-2222-222222222222"
ART = "33333333-3333-3333-3333-333333333333"
REC = "44444444-4444-4444-4444-444444444444"
WS2_INCLUDES = {"artist-credits", "labels", "recordings", "release-groups", "media",
                "releases", "genres", "isrcs", "tags", "url-rels"}
CREDIT = [{"name": "Bwm Synthetic", "joinphrase": "",
           "artist": {"id": ART, "name": "Bwm Synthetic", "sort-name": "Synthetic, Bwm"}}]
RELEASE = {
    "id": REL, "title": "Plain LP", "date": "2001-02-03", "country": "XW", "status": "Official",
    "artist-credit": CREDIT,
    "release-group": {"id": RG, "primary-type": "Album", "secondary-types": []},
    "label-info": [{"catalog-number": "BWM-001", "label": {"name": "Synthetic Records"}}],
    "media": [{"position": 1, "format": "CD", "track-count": 1, "tracks": [
        {"position": 1, "number": "1", "title": "Track One", "length": 180000,
         "recording": {"id": REC, "title": "Track One", "length": 180000}}]}],
}


def _fake_ws2(payload):
    @contextlib.contextmanager
    def opened(provider, req, **kwargs):
        url = urllib.parse.urlsplit(req.full_url)
        inc = urllib.parse.parse_qs(url.query).get("inc", [""])[0]  # "+" decodes to " "
        bad = set(inc.split()) - WS2_INCLUDES - {""}
        if bad:
            raise urllib.error.HTTPError(req.full_url, 400, f"bad inc {bad}", {}, None)
        yield io.BytesIO(json.dumps(payload).encode())
    return mock.patch.object(provider_boundary, "opened", opened)


class MbLookupFixtureTests(unittest.TestCase):
    def test_release_candidate_has_labels_catalog_and_release_group(self):
        with _fake_ws2(RELEASE):
            cand = helpers_mb._fetch_mb_release_candidate(REL)
        self.assertEqual(cand["label"], "Synthetic Records")
        self.assertEqual(cand["catalog_numbers"], ["BWM-001"])
        self.assertEqual(cand["mb_releasegroupid"], RG)

    def test_tracklist_artist_credit_gives_release_artist_and_ids(self):
        with _fake_ws2(RELEASE), \
             mock.patch.object(matching_service, "_mb_release_tracklist_read_disk", return_value=None), \
             mock.patch.object(matching_service, "_mb_release_tracklist_write_disk"), \
             mock.patch.dict(matching_service._MB_RELEASE_TRACKLIST_CACHE, clear=True):
            tl = matching_service._fetch_mb_release_tracklist(REL)
        self.assertTrue(tl["ok"])
        self.assertEqual(tl["release_artist"], "Bwm Synthetic")
        self.assertEqual(tl["release_artist_id"], ART)
        self.assertIn(ART, tl["release_artistids"])
        self.assertEqual(tl["release_group"], RG)

    def test_recording_without_labels_still_has_release_group(self):
        rec = {"id": REC, "title": "Track One", "length": 180000, "artist-credit": CREDIT,
               "releases": [{k: v for k, v in RELEASE.items() if k != "label-info"}]}
        with _fake_ws2(rec):
            det = helpers_mb._fetch_mb_recording_details(REC)
        self.assertEqual(det["mb_albumid"], REL)
        self.assertEqual(det["mb_releasegroupid"], RG)
        self.assertNotIn("label", det)


if __name__ == "__main__":
    unittest.main()
