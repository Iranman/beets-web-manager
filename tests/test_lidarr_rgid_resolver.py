"""backend.lidarr_rgid_resolver: the read-only Lidarr -> release-group-MBID planner.

Every provider response is faked (urllib.request.urlopen). No real Lidarr, no
credentials, no network. Covers each match tier, each unresolved reason, each
tie-breaker, and the full provider-failure matrix (timeout / unavailable / 401
/ 403 / 5xx / rate limit / malformed / not configured -> raised as a classified
ProviderError and NEVER downgraded to an "unresolved" result).
"""
import io
import json
import socket
import unittest
import urllib.error
import urllib.request
from unittest import mock

import app as app_module  # noqa: F401  -- loads the module family / test env in order
import backend.app_runtime as app_runtime
import backend.provider_boundary as pb
import backend.lidarr_rgid_resolver as resolver

ARTIST_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ARTIST_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
RGID_1 = "11111111-1111-1111-1111-111111111111"
RGID_2 = "22222222-2222-2222-2222-222222222222"


def album(aid, artist_mbid, title, rgid=RGID_1, *, year="2016", album_type="Album",
          monitored=True, track_files=5, path=None):
    a = {
        "id": aid,
        "title": title,
        "foreignAlbumId": rgid,
        "albumType": album_type,
        "monitored": monitored,
        "releaseDate": f"{year}-01-01T00:00:00Z",
        "statistics": {"trackFileCount": track_files},
        "artist": {"foreignArtistId": artist_mbid, "artistName": "Some Artist"},
    }
    if path is not None:
        a["path"] = path
    return a


def index(*albums):
    return resolver.index_by_artist(list(albums))


class _Resp(io.BytesIO):
    status = 200

    def __init__(self, body):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.headers = {"Content-Type": "application/json"}


def http_error(code):
    return urllib.error.HTTPError("https://lidarr.test/api/v1/album", code, "err", {}, io.BytesIO(b"{}"))


# -- tiers ----------------------------------------------------------------------

class TierTests(unittest.TestCase):
    def test_tier_a_exact_path_basename(self):
        idx = index(album(1, ARTIST_A, "The Getaway", path="/music/Artist/The Getaway (2016)"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="Totally Different",
                               album_basename="The Getaway (2016)")
        self.assertEqual(out, {"status": "resolved", "release_group_mbid": RGID_1,
                               "matched_tier": "path_exact", "lidarr_album_id": 1})

    def test_tier_b_title_plus_year_unique(self):
        idx = index(album(1, ARTIST_A, "The Getaway", year="2016"),
                    album(2, ARTIST_A, "Other Album", year="2016"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="the  getaway!", album_year="2016")
        self.assertEqual(out["status"], "resolved")
        self.assertEqual(out["matched_tier"], "title_year")
        self.assertEqual(out["lidarr_album_id"], 1)

    def test_tier_c_title_only_when_year_differs(self):
        idx = index(album(7, ARTIST_A, "Unique Title", rgid=RGID_2, year="1999"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="Unique Title", album_year="2016")
        self.assertEqual(out, {"status": "resolved", "release_group_mbid": RGID_2,
                               "matched_tier": "title_only", "lidarr_album_id": 7})

    def test_valid_mbid_only_ever_returned(self):
        idx = index(album(1, ARTIST_A, "X", rgid="not-a-uuid"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="X", album_year="2016")
        self.assertEqual(out, {"status": "unresolved", "reason": resolver.LIDARR_MISSING_RGID})


# -- unresolved reasons ---------------------------------------------------------

class UnresolvedReasonTests(unittest.TestCase):
    def test_artist_not_in_lidarr(self):
        idx = index(album(1, ARTIST_A, "X"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_B, album_title="X", album_year="2016")
        self.assertEqual(out, {"status": "unresolved", "reason": resolver.ARTIST_NOT_IN_LIDARR})

    def test_album_not_in_lidarr(self):
        idx = index(album(1, ARTIST_A, "Present Album"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="Missing Album", album_year="2016")
        self.assertEqual(out, {"status": "unresolved", "reason": resolver.ALBUM_NOT_IN_LIDARR})

    def test_ambiguous_multiple_albums(self):
        # Two reissues identical on every tie-breaker -> never pick arbitrarily.
        idx = index(album(1, ARTIST_A, "Reissued", rgid=RGID_1, year="2016"),
                    album(2, ARTIST_A, "Reissued", rgid=RGID_2, year="2016"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="Reissued", album_year="2016")
        self.assertEqual(out, {"status": "unresolved", "reason": resolver.AMBIGUOUS_MULTIPLE_ALBUMS})

    def test_lidarr_missing_rgid(self):
        idx = index(album(1, ARTIST_A, "X", rgid=""))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="X", album_year="2016")
        self.assertEqual(out, {"status": "unresolved", "reason": resolver.LIDARR_MISSING_RGID})


# -- tie-breakers (only among editions of ONE release group) --------------------

class TieBreakerTests(unittest.TestCase):
    """Tie-breakers pick *which edition's id* to report when several editions
    share a single release-group MBID. They never choose across distinct
    RGIDs (see StrictRgidTests) -- that would guess a canonical identity."""

    def test_year_is_the_first_tie_breaker(self):
        # Direct check of the year step: among same-title candidates of
        # differing years, the one matching the folder year wins.
        a2016 = album(1, ARTIST_A, "Live", rgid=RGID_1, year="2016")
        a2000 = album(2, ARTIST_A, "Live", rgid=RGID_1, year="2000")
        self.assertIs(resolver._tie_break([a2000, a2016], "2016"), a2016)
        self.assertIs(resolver._tie_break([a2000, a2016], "2000"), a2000)

    def test_album_type_picks_edition_within_one_rgid(self):
        idx = index(album(1, ARTIST_A, "Dup", rgid=RGID_1, year="2016", album_type="Album"),
                    album(2, ARTIST_A, "Dup", rgid=RGID_1, year="2016", album_type="Single"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="Dup", album_year="2016")
        self.assertEqual((out["status"], out["release_group_mbid"], out["lidarr_album_id"]),
                         ("resolved", RGID_1, 1))

    def test_monitored_picks_edition_within_one_rgid(self):
        idx = index(album(1, ARTIST_A, "Dup", rgid=RGID_1, year="2016", monitored=False),
                    album(2, ARTIST_A, "Dup", rgid=RGID_1, year="2016", monitored=True))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="Dup", album_year="2016")
        self.assertEqual((out["status"], out["release_group_mbid"], out["lidarr_album_id"]),
                         ("resolved", RGID_1, 2))

    def test_track_file_count_picks_edition_within_one_rgid(self):
        idx = index(album(1, ARTIST_A, "Dup", rgid=RGID_1, year="2016", track_files=0),
                    album(2, ARTIST_A, "Dup", rgid=RGID_1, year="2016", track_files=3))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="Dup", album_year="2016")
        self.assertEqual((out["status"], out["release_group_mbid"], out["lidarr_album_id"]),
                         ("resolved", RGID_1, 2))


# -- strict RGID identity: never tie-break across distinct release groups --------

class StrictRgidTests(unittest.TestCase):
    """finding-7: the resolver must never bake a guessed identity into a folder.
    Distinct release groups under the same title are held for review, no matter
    how cleanly year / albumType / monitored / trackFileCount would have broken
    the tie before. Several editions of ONE release group still resolve."""

    def test_two_editions_one_rgid_resolve_at_title_year(self):
        # Deluxe + standard edition, same RGID, same title+year -> that RGID.
        idx = index(album(1, ARTIST_A, "The Album", rgid=RGID_1, year="2016", album_type="Album"),
                    album(2, ARTIST_A, "The Album", rgid=RGID_1, year="2016", album_type="EP"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="The Album", album_year="2016")
        self.assertEqual((out["status"], out["matched_tier"], out["release_group_mbid"]),
                         ("resolved", "title_year", RGID_1))

    def test_distinct_rgids_same_title_year_held(self):
        # Identical title+year, DIFFERENT release groups -> HOLD (ambiguous).
        idx = index(album(1, ARTIST_A, "Dup", rgid=RGID_1, year="2016"),
                    album(2, ARTIST_A, "Dup", rgid=RGID_2, year="2016"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="Dup", album_year="2016")
        self.assertEqual(out, {"status": "unresolved", "reason": resolver.AMBIGUOUS_MULTIPLE_ALBUMS})

    def test_distinct_rgids_held_even_when_albumtype_would_break_tie(self):
        idx = index(album(1, ARTIST_A, "Dup", rgid=RGID_1, year="2016", album_type="Album"),
                    album(2, ARTIST_A, "Dup", rgid=RGID_2, year="2016", album_type="Single"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="Dup", album_year="2016")
        self.assertEqual(out, {"status": "unresolved", "reason": resolver.AMBIGUOUS_MULTIPLE_ALBUMS})

    def test_distinct_rgids_held_even_when_monitored_would_break_tie(self):
        idx = index(album(1, ARTIST_A, "Dup", rgid=RGID_1, year="2016", monitored=False),
                    album(2, ARTIST_A, "Dup", rgid=RGID_2, year="2016", monitored=True))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="Dup", album_year="2016")
        self.assertEqual(out, {"status": "unresolved", "reason": resolver.AMBIGUOUS_MULTIPLE_ALBUMS})

    def test_distinct_rgids_held_even_when_trackcount_would_break_tie(self):
        idx = index(album(1, ARTIST_A, "Dup", rgid=RGID_1, year="2016", track_files=0),
                    album(2, ARTIST_A, "Dup", rgid=RGID_2, year="2016", track_files=9))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="Dup", album_year="2016")
        self.assertEqual(out, {"status": "unresolved", "reason": resolver.AMBIGUOUS_MULTIPLE_ALBUMS})

    def test_tier_c_single_distinct_rgid_resolves(self):
        # Title-only (folder year matches neither edition), both one RGID.
        idx = index(album(1, ARTIST_A, "Live", rgid=RGID_1, year="1999"),
                    album(2, ARTIST_A, "Live", rgid=RGID_1, year="2005"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="Live", album_year="2016")
        self.assertEqual((out["status"], out["matched_tier"], out["release_group_mbid"]),
                         ("resolved", "title_only", RGID_1))

    def test_tier_c_multiple_distinct_rgids_held(self):
        # Title-only spanning two release groups -> HOLD, never tie-break.
        idx = index(album(1, ARTIST_A, "Live", rgid=RGID_1, year="1999"),
                    album(2, ARTIST_A, "Live", rgid=RGID_2, year="2005"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="Live", album_year="2016")
        self.assertEqual(out, {"status": "unresolved", "reason": resolver.AMBIGUOUS_MULTIPLE_ALBUMS})

    def test_tier_a_distinct_rgids_fall_through_to_title(self):
        # Two basename matches with distinct RGIDs: Tier A does not guess; the
        # title tier then holds them (not terminal at A, but still no guess).
        idx = index(album(1, ARTIST_A, "Dup", rgid=RGID_1, year="2016",
                          path="/music/Artist/Dup (2016)"),
                    album(2, ARTIST_A, "Dup", rgid=RGID_2, year="2016",
                          path="/music/Artist/Dup (2016)"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="Dup",
                               album_year="2016", album_basename="Dup (2016)")
        self.assertEqual(out, {"status": "unresolved", "reason": resolver.AMBIGUOUS_MULTIPLE_ALBUMS})

    def test_tier_a_single_rgid_editions_resolve(self):
        idx = index(album(1, ARTIST_A, "Dup", rgid=RGID_1, year="2016", album_type="Album",
                          path="/music/Artist/Dup (2016)"),
                    album(2, ARTIST_A, "Dup", rgid=RGID_1, year="2016", album_type="Single",
                          path="/music/Artist/Dup (2016)"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="irrelevant",
                               album_basename="Dup (2016)")
        self.assertEqual((out["status"], out["matched_tier"], out["release_group_mbid"]),
                         ("resolved", "path_exact", RGID_1))

    def test_distinct_rgid_mixed_with_missing_is_held(self):
        # One valid RGID + one missing under same title -> not a single shared
        # RGID -> held ambiguous, never resolved to the lone stamp.
        idx = index(album(1, ARTIST_A, "Dup", rgid=RGID_1, year="2016"),
                    album(2, ARTIST_A, "Dup", rgid="", year="2016"))
        out = resolver.resolve(idx, artist_mbid=ARTIST_A, album_title="Dup", album_year="2016")
        self.assertEqual(out, {"status": "unresolved", "reason": resolver.AMBIGUOUS_MULTIPLE_ALBUMS})


# -- the snapshot index ---------------------------------------------------------

class IndexTests(unittest.TestCase):
    def test_indexes_by_artist_mbid_and_drops_albums_without_one(self):
        snap = [album(1, ARTIST_A, "X"), album(2, ARTIST_B, "Y"),
                {"id": 3, "title": "no artist", "foreignAlbumId": RGID_1}]  # dropped
        idx = resolver.index_by_artist(snap)
        self.assertEqual(set(idx), {ARTIST_A, ARTIST_B})
        self.assertEqual(len(idx[ARTIST_A]), 1)


# -- provider-failure matrix (fetch raises, never "unresolved") -----------------

class FetchFailureMatrixTests(unittest.TestCase):
    def setUp(self):
        pb.reset_provider_health()
        self.addCleanup(pb.reset_provider_health)
        for patcher in (
            mock.patch.dict("os.environ", {"PROVIDER_MAX_ATTEMPTS": "1"}),  # no retries, no sleeps
            mock.patch.object(app_runtime, "LIDARR_URL", "http://127.0.0.1:8686"),
            mock.patch.object(app_runtime, "LIDARR_KEY", "unit-test-key-not-real"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fetch_with(self, effect):
        with mock.patch.object(urllib.request, "urlopen", side_effect=effect):
            return resolver.fetch_album_snapshot(timeout=5)

    def test_not_configured_missing_key(self):
        with mock.patch.object(app_runtime, "LIDARR_KEY", ""):
            with self.assertRaises(pb.ProviderError) as ctx:
                resolver.fetch_album_snapshot(timeout=5)
        self.assertEqual(ctx.exception.outcome, pb.ProviderOutcome.NOT_CONFIGURED)

    def test_not_configured_missing_url(self):
        with mock.patch.object(app_runtime, "LIDARR_URL", ""):
            with self.assertRaises(pb.ProviderError) as ctx:
                resolver.fetch_album_snapshot(timeout=5)
        self.assertEqual(ctx.exception.outcome, pb.ProviderOutcome.NOT_CONFIGURED)

    def test_timeout_is_transient(self):
        with self.assertRaises(pb.ProviderError) as ctx:
            self._fetch_with(socket.timeout("slow"))
        self.assertEqual(ctx.exception.outcome, pb.ProviderOutcome.TRANSIENT_ERROR)

    def test_provider_unavailable_connection_refused(self):
        with self.assertRaises(pb.ProviderError) as ctx:
            self._fetch_with(urllib.error.URLError("connection refused"))
        self.assertEqual(ctx.exception.outcome, pb.ProviderOutcome.UNAVAILABLE)

    def test_authentication_failure_401(self):
        with self.assertRaises(pb.ProviderError) as ctx:
            self._fetch_with(http_error(401))
        self.assertEqual(ctx.exception.outcome, pb.ProviderOutcome.AUTHENTICATION_ERROR)

    def test_authentication_failure_403(self):
        with self.assertRaises(pb.ProviderError) as ctx:
            self._fetch_with(http_error(403))
        self.assertEqual(ctx.exception.outcome, pb.ProviderOutcome.AUTHENTICATION_ERROR)

    def test_server_error_5xx_is_unavailable(self):
        with self.assertRaises(pb.ProviderError) as ctx:
            self._fetch_with(http_error(503))
        self.assertEqual(ctx.exception.outcome, pb.ProviderOutcome.UNAVAILABLE)

    def test_rate_limited_429(self):
        with self.assertRaises(pb.ProviderError) as ctx:
            self._fetch_with(http_error(429))
        self.assertEqual(ctx.exception.outcome, pb.ProviderOutcome.RATE_LIMITED)

    def test_malformed_body_is_transient(self):
        with self.assertRaises(pb.ProviderError) as ctx:
            self._fetch_with([_Resp(b"this is not json")])
        self.assertEqual(ctx.exception.outcome, pb.ProviderOutcome.TRANSIENT_ERROR)

    def test_non_list_payload_is_rejected_as_malformed(self):
        with self.assertRaises(pb.ProviderError) as ctx:
            self._fetch_with([_Resp({"error": "unexpected object"})])
        self.assertEqual(ctx.exception.outcome, pb.ProviderOutcome.TRANSIENT_ERROR)

    def test_success_returns_album_list_and_routes_through_boundary(self):
        snap = [album(1, ARTIST_A, "X"), album(2, ARTIST_B, "Y")]
        with mock.patch.object(urllib.request, "urlopen", side_effect=[_Resp(snap)]) as urlopen:
            data = resolver.fetch_album_snapshot(timeout=5)
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual([a["id"] for a in data], [1, 2])
        # The call was recorded by the provider boundary under "lidarr".
        self.assertEqual(pb.provider_health()["lidarr"]["last_outcome"], "confirmed")


class ResolverObjectTests(unittest.TestCase):
    def test_from_lidarr_then_resolve(self):
        snap = [album(1, ARTIST_A, "The Getaway", year="2016")]
        with mock.patch.object(resolver, "fetch_album_snapshot", return_value=snap):
            r = resolver.LidarrRgidResolver.from_lidarr()
        out = r.resolve(artist_mbid=ARTIST_A, album_title="The Getaway", album_year="2016")
        self.assertEqual((out["status"], out["matched_tier"], out["release_group_mbid"]),
                         ("resolved", "title_year", RGID_1))

    def test_from_lidarr_propagates_provider_failure(self):
        err = pb.ProviderError(pb.ProviderOutcome.UNAVAILABLE, "down", provider="lidarr")
        with mock.patch.object(resolver, "fetch_album_snapshot", side_effect=err):
            with self.assertRaises(pb.ProviderError):
                resolver.LidarrRgidResolver.from_lidarr()


if __name__ == "__main__":
    unittest.main()
