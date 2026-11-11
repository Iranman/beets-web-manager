"""#590: a hand-typed bare Release Group UUID sent to Match resolves through
the Release Group, and a UUID that is neither still fails closed.

All MusicBrainz answers are synthetic; provider_boundary.opened is mocked.
"""
import contextlib
import io
import json
import unittest
import urllib.error
from unittest import mock

import helpers_mb

REL = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
RG = "11111111-1111-1111-1111-111111111111"
RG_REL = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
UNKNOWN = "99999999-9999-9999-9999-999999999999"

ANSWERS = {
    f"/release/{REL}": {"id": REL, "release-group": {"id": RG}},
    f"/release-group/{RG}": {"releases": [{"id": "cccccccc-cccc-cccc-cccc-cccccccccccc", "status": "Bootleg"},
                                          {"id": RG_REL, "status": "Official", "title": "Album"}]},
}


def _fake_opened(urls, outage=False):
    @contextlib.contextmanager
    def opened(_provider, req, **_kw):
        url = req.full_url
        urls.append(url)
        if outage:
            raise urllib.error.HTTPError(url, 503, "down", {}, None)
        for key, body in ANSWERS.items():
            if key + "?" in url:
                yield io.BytesIO(json.dumps(body).encode())
                return
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)
    return opened


def _resolve(mb_input, outage=False):
    urls, log = [], []
    with mock.patch.object(helpers_mb.provider_boundary, "opened", _fake_opened(urls, outage)):
        got = helpers_mb._resolve_mb_release_id(mb_input, log)
    return got, urls, log


class BareUuidResolverTests(unittest.TestCase):
    def test_bare_release_uuid_stays_the_release(self):
        got, urls, _ = _resolve(REL)
        self.assertEqual(got, REL)
        self.assertFalse(any("/release-group/" in u for u in urls))

    def test_bare_release_group_uuid_resolves_like_the_url_form(self):
        got, _, log = _resolve(RG)
        self.assertEqual(got, RG_REL)
        self.assertEqual(got, _resolve(f"https://musicbrainz.org/release-group/{RG}")[0])
        self.assertTrue(any("not a MusicBrainz release" in line for line in log))

    def test_uuid_that_is_neither_fails_closed(self):
        got, urls, _ = _resolve(UNKNOWN)
        self.assertEqual(got, "")
        self.assertEqual(len(urls), 2)  # one Release, one Release Group lookup

    def test_outage_never_hands_a_guess_on(self):
        # The release lookup is down: the UUID is passed on unchanged and the
        # Match's own release fetch fails closed; it is never treated as a group.
        got, urls, _ = _resolve(RG, outage=True)
        self.assertEqual(got, RG)
        self.assertFalse(any("/release-group/" in u for u in urls))


class MatchUsesResolvedReleaseTests(unittest.TestCase):
    def test_match_validates_the_release_resolved_from_a_bare_group(self):
        import routes_library
        urls = []
        plan = mock.Mock(side_effect=RuntimeError("stop after validation"))
        with mock.patch.object(helpers_mb.provider_boundary, "opened", _fake_opened(urls)), \
                mock.patch.object(routes_library, "_album_mb_match_plan", plan):
            with self.assertRaises(RuntimeError):
                routes_library._match_album_run(7, RG, [])
        plan.assert_called_once()
        self.assertEqual(plan.call_args.args[:2], (7, RG_REL))

    def test_match_errors_for_a_uuid_that_is_neither(self):
        import routes_library
        plan = mock.Mock()
        with mock.patch.object(helpers_mb.provider_boundary, "opened", _fake_opened([])), \
                mock.patch.object(routes_library, "_album_mb_match_plan", plan):
            with self.assertRaises(RuntimeError):
                routes_library._match_album_run(7, UNKNOWN, [])
        plan.assert_not_called()


class ImportSearchesWholeGroupTests(unittest.TestCase):
    """QA on #598: the import path must not force-import the group's first
    Official release (RG_REL here, which does not fit) when another release
    in the group (REL_X) fits the folder."""

    def test_override_import_of_bare_group_picks_the_fitting_release(self):
        import backend.library_service as ls
        rel_x = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        log = []
        with mock.patch.object(helpers_mb.provider_boundary, "opened", _fake_opened([])), \
                mock.patch.object(ls, "_folder_import_track_count", return_value=10), \
                mock.patch.object(ls, "_mb_release_has_tracks", side_effect=lambda r: r in (RG_REL, rel_x)), \
                mock.patch.object(ls, "_resolve_release_group_to_release", return_value=RG_REL), \
                mock.patch.object(ls, "_mb_release_group_candidates",
                                  return_value=[{"mb_albumid": RG_REL, "track_count": 10},
                                                {"mb_albumid": rel_x, "track_count": 10}]), \
                mock.patch.object(ls, "_folder_release_preflight",
                                  side_effect=lambda _f, rel, **_k: {"ok": rel == rel_x, "matches": 10,
                                                                      "expected": 10, "audio_count": 10}):
            got = ls._resolve_album_release_for_import(
                RG, "Artist", "Album", "", 10, log, source_folder="/downloads/Artist - Album",
                allow_provided_release_override=True)
        self.assertEqual(got, rel_x)


if __name__ == "__main__":
    unittest.main()
