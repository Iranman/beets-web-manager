"""Provider failure handling for the non-Beets integrations (wave-5 IA-* / BA-5).

Every provider here is faked: urllib.request.urlopen, open_public_url or the
service's own request helper. No real credentials, no network.
An outage, timeout, 401 or 5xx must be reported as unavailable -- never as an
empty result or "no match" -- and a non-idempotent request is never re-sent.
"""
import datetime
import email.utils
import http.client
import io
import json
import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import app as app_module  # noqa: F401  -- loads the module family in order
import backend.acquisition_service as acq
import backend.artwork_service as artwork_service
import backend.musicbrainz_service as mbs
import backend.playlist_service as ps
import backend.plex_service as plex_service
import backend.provider_boundary as pb
import backend.slskd_service as slskd
import helpers_mb
from backend.artwork_service import AlbumArtRequestError

RG = "11111111-1111-1111-1111-111111111111"
REL = "22222222-2222-2222-2222-222222222222"
OTHER_RG = "33333333-3333-3333-3333-333333333333"


def http_error(code, headers=None, body=b""):
    return urllib.error.HTTPError("https://provider.test/x", code, "err", headers or {}, io.BytesIO(body))


class _Resp(io.BytesIO):
    status = 200

    def __init__(self, body=b"", headers=None):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.headers = headers or {"Content-Type": "application/json"}


class _BoundaryCase(unittest.TestCase):
    """No backoff sleeps, policy attempts as shipped."""

    def setUp(self):
        pb.reset_provider_health()
        self.addCleanup(pb.reset_provider_health)
        for patcher in (
            mock.patch.dict("os.environ", {"PROVIDER_MAX_ATTEMPTS": ""}),
            mock.patch.dict(pb.POLICIES, {name: pb.ProviderPolicy(pol.max_attempts, 0.0)
                                          for name, pol in pb.POLICIES.items()}),
            mock.patch.object(helpers_mb.time, "sleep"),  # main's nested loops slept for real
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


# -- provider boundary: IA-10, IA-17 -----------------------------------------------

class ProviderHealthTests(_BoundaryCase):
    def _open(self, provider, effect):
        with mock.patch.object(urllib.request, "urlopen", side_effect=effect):
            with pb.opened(provider, "https://provider.test/x", timeout=5) as r:
                return r.read()

    def test_a_4xx_answer_is_not_a_provider_failure(self):  # IA-10
        with self.assertRaises(urllib.error.HTTPError):
            self._open("musicbrainz", http_error(404))
        row = pb.provider_health()["musicbrainz"]
        self.assertEqual((row["last_outcome"], row["failures"]), ("rejected", 0))
        self.assertIsNotNone(row["last_success_at"])
        self.assertIsNone(row["last_failure_at"])

    def test_outage_and_auth_failures_still_count(self):
        for effect in (http_error(503), http_error(401), socket.timeout("slow")):
            with self.subTest(effect=effect), self.assertRaises(Exception):
                self._open("lidarr", effect)
        self.assertEqual(pb.provider_health()["lidarr"]["failures"], 3)

    def test_a_body_that_fails_mid_read_is_recorded_as_a_failure(self):  # IA-17
        resp = _Resp(b"x")
        resp.read = mock.Mock(side_effect=http.client.IncompleteRead(b"par", 10))
        with self.assertRaises(http.client.IncompleteRead):
            self._open("plex", [resp])
        row = pb.provider_health()["plex"]
        self.assertNotEqual(row["last_outcome"], "confirmed")
        self.assertEqual(row["failures"], 1)

    def test_a_good_body_is_confirmed_after_it_is_read(self):
        self.assertEqual(self._open("plex", [_Resp(b"ok")]), b"ok")
        self.assertEqual(pb.provider_health()["plex"]["last_outcome"], "confirmed")

    def test_retry_after_http_date_is_honoured(self):  # IA-17
        when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=20)
        delay = pb._retry_after({"Retry-After": email.utils.format_datetime(when, usegmt=True)})
        self.assertIsNotNone(delay)
        self.assertTrue(15 <= delay <= 21, delay)
        self.assertEqual(pb._retry_after({"Retry-After": "7"}), 7.0)
        self.assertIsNone(pb._retry_after({"Retry-After": "soon"}))


# -- MusicBrainz: BA-5 -------------------------------------------------------------

class MusicBrainzOutageTests(_BoundaryCase):
    def test_release_search_outage_raises_and_is_not_retried_twice(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=http_error(503)) as urlopen:
            with self.assertRaises(pb.ProviderError) as ctx:
                helpers_mb._mb_release_search("Album", "Artist")
        self.assertEqual(urlopen.call_count, pb.POLICIES["musicbrainz"].max_attempts)
        self.assertEqual(ctx.exception.outcome, pb.ProviderOutcome.UNAVAILABLE)

    def test_release_search_refused_query_is_an_empty_answer(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=http_error(400)):
            self.assertEqual(helpers_mb._mb_release_search("Album", "Artist"), [])

    def test_recording_search_timeout_raises(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=socket.timeout("slow")):
            with self.assertRaises(pb.ProviderError):
                helpers_mb._mb_recording_search("Song", "Artist")

    def test_tracklist_outage_is_reported_unavailable_once(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=http_error(502)) as urlopen:
            result = helpers_mb.fetch_mb_release_tracklist(REL, [])
        self.assertEqual(urlopen.call_count, pb.POLICIES["musicbrainz"].max_attempts)
        self.assertFalse(result["ok"])
        self.assertTrue(result["unavailable"])

    def test_folder_track_search_outage_raises(self):
        with mock.patch.object(mbs, "_folder_track_search_titles", return_value=["One", "Two"]), \
                mock.patch.object(mbs.time, "sleep"), \
                mock.patch.object(urllib.request, "urlopen", side_effect=http_error(503)) as urlopen:
            with self.assertRaises(pb.ProviderError):
                mbs._mb_release_search_by_folder_tracks("/x", artist="Artist", log=[])
        self.assertEqual(urlopen.call_count, pb.POLICIES["musicbrainz"].max_attempts)

    def test_release_group_outage_never_returns_the_rg_as_a_release(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=http_error(503)):
            self.assertEqual(
                helpers_mb._resolve_mb_release_id(f"https://musicbrainz.org/release-group/{RG}", []), "")


# -- Cover Art Archive: IA-01 ---------------------------------------------------------

class ReleaseArtOutageTests(_BoundaryCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (mock.patch.object(mbs, "RELEASE_ART_CACHE_DIR", Path(tmp.name)),
                        mock.patch.object(artwork_service, "RELEASE_ART_CACHE_DIR", Path(tmp.name))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_outage_is_not_cached_as_a_miss(self):
        for effect in (socket.timeout("slow"), http_error(503), http_error(429)):
            with self.subTest(effect=effect):
                with mock.patch.object(pb, "open_public_url", side_effect=effect):
                    result = mbs._ensure_release_group_art(RG)
                self.assertFalse(result["ok"])
                self.assertTrue(result.get("unavailable"))
                self.assertEqual(artwork_service._release_art_cache_info(RG), {})

    def test_a_404_is_cached_as_a_miss(self):
        with mock.patch.object(pb, "open_public_url", side_effect=http_error(404)):
            self.assertEqual(mbs._ensure_release_group_art(RG), {"ok": False, "error": "no art found"})
        self.assertEqual(artwork_service._release_art_cache_info(RG), {"miss": True})


# -- album art from a URL: IA-20 ------------------------------------------------------

class AlbumArtDownloadErrorTests(_BoundaryCase):
    def _status(self, effect):
        with mock.patch.object(artwork_service, "resolve_public_target"), \
                mock.patch.object(pb, "open_public_url", side_effect=effect):
            with self.assertRaises(AlbumArtRequestError) as ctx:
                artwork_service._download_album_art_bytes("https://img.example.test/c.png")
        return ctx.exception.status, ctx.exception.message

    def test_failures_are_told_apart(self):
        self.assertEqual(self._status(socket.timeout("timed out"))[0], 504)
        self.assertEqual(self._status(http_error(429))[0], 503)
        self.assertEqual(self._status(http_error(503))[0], 502)
        status, message = self._status(http_error(404))
        self.assertEqual(status, 400)
        self.assertIn("404", message)


# -- Plex: IA-02, IA-05 ---------------------------------------------------------------

_PLEX_SETTINGS = {"url": "http://plex.test:32400", "token": "t", "section": "", "plex_music_roots": "",
                  "beets_music_root": "/music", "plex_scan_timeout": "60", "plex_index_timeout": "60"}


class PlexRequestTests(_BoundaryCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(plex_service, "_plex_settings", return_value=dict(_PLEX_SETTINGS))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_timed_out_post_is_never_resent(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=socket.timeout("slow")) as urlopen:
            with self.assertRaises(socket.timeout):
                plex_service._plex_request("/playlists", {"title": "x"}, method="POST")
        self.assertEqual(urlopen.call_count, 1)

    def test_a_put_is_never_resent(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=socket.timeout("slow")) as urlopen:
            with self.assertRaises(socket.timeout):
                plex_service._plex_request("/playlists/9/items", {"uri": "u"}, method="PUT")
        self.assertEqual(urlopen.call_count, 1)

    def test_a_get_is_retried_within_the_policy_only(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=socket.timeout("slow")) as urlopen:
            with self.assertRaises(socket.timeout):
                plex_service._plex_request("/identity")
        self.assertEqual(urlopen.call_count, pb.POLICIES["plex"].max_attempts)


class PlexPathMapTests(unittest.TestCase):
    def _map(self, locations, configured=""):
        settings = dict(_PLEX_SETTINGS, plex_music_roots=configured)
        return plex_service._plex_selected_path_map(settings, locations)["plex_root"]

    def test_same_root_in_both_containers_is_the_identity(self):  # IA-05
        self.assertEqual(self._map(["/music"]), "/music")
        self.assertEqual(self._map(["/music", "/elsewhere"]), "/music")

    def test_nothing_known_is_the_identity_not_a_builtin_alias(self):
        self.assertEqual(self._map([]), "/music")

    def test_a_different_plex_root_is_used(self):
        self.assertEqual(self._map(["/data/music"]), "/data/music")
        self.assertEqual(self._map([], configured="/plexmusic"), "/plexmusic")
        self.assertEqual(self._map(["/a", "/plexmusic"], configured="/plexmusic"), "/plexmusic")


# -- Plex playlist replace: IA-03, IA-16 ----------------------------------------------

class _FakePlex:
    def __init__(self, playlists=None, fail_post=None, fail_put=None, fail_delete=None):
        self.playlists = dict(playlists or {})  # ratingKey -> {"title", "items"}
        self.fail_post, self.fail_put, self.fail_delete = fail_post, fail_put, fail_delete
        self.calls = []
        self.next_key = 500

    def request(self, path, params=None, *, method="GET", timeout=None, attempts=2):
        self.calls.append((method, path))
        params = params or {}
        if method == "GET" and path == "/playlists":
            return {"MediaContainer": {"Metadata": [
                {"ratingKey": k, "key": f"/playlists/{k}/items", "title": v["title"], "smart": "0"}
                for k, v in self.playlists.items()]}}
        if method == "GET" and path.endswith("/items"):
            key = path.split("/")[2]
            return {"MediaContainer": {"Metadata": [{"ratingKey": i} for i in self.playlists[key]["items"]]}}
        if method == "POST" and path == "/playlists":
            if self.fail_post:
                raise self.fail_post
            self.next_key += 1
            key = str(self.next_key)
            self.playlists[key] = {"title": params["title"], "items": params["uri"].rsplit("/", 1)[1].split(",")}
            return {"MediaContainer": {"Metadata": [{"ratingKey": key}]}}
        if method == "PUT":
            if self.fail_put:
                raise self.fail_put
            key = path.split("/")[2]
            self.playlists[key]["items"] += params["uri"].rsplit("/", 1)[1].split(",")
            return {}
        if method == "DELETE":
            if self.fail_delete:
                raise self.fail_delete
            key = path.split("/")[2]
            if key not in self.playlists:
                raise http_error(404)
            del self.playlists[key]
            return {}
        raise AssertionError(f"unexpected Plex call {method} {path}")


class PlexPlaylistReplaceTests(unittest.TestCase):
    ITEMS = [{"id": i, "artist": "A", "title": f"T{i}", "path": f"/music/a/{i}.flac"} for i in (1, 2, 3)]

    def _sync(self, fake, manifest):
        stored = {}

        def replace_manifest(name, m):
            stored.update(m)
            return m

        patches = {
            "_playlist_ensure_state_dirs": mock.Mock(),
            "_playlist_ensure_stable_id": mock.Mock(return_value="pid1"),
            "_playlist_resolve_stable_id": mock.Mock(return_value="pid1"),
            "_playlist_key": mock.Mock(return_value="key1"),
            "_playlist_write_manifest": mock.Mock(return_value=dict(manifest)),
            "_playlist_read_manifest": mock.Mock(return_value=dict(manifest)),
            "_playlist_replace_manifest": mock.Mock(side_effect=replace_manifest),
            "_playlist_manifest_path": mock.Mock(return_value=Path("m.json")),
            "_playlist_store_track_state": mock.Mock(),
            "_playlist_manifest_track_states": mock.Mock(return_value={}),
            "_playlist_other_live_pids_with_name": mock.Mock(return_value=[]),
            "_plex_settings": mock.Mock(return_value=dict(_PLEX_SETTINGS)),
            "_plex_find_music_section": mock.Mock(return_value=("machine", "3", "Music")),
            "_plex_track_keys_for_items": mock.Mock(return_value=(
                ["11", "12", "13"], {"matched_track_ids": [], "pending_plex_count": 0,
                                     "path_mapping_verified": True})),
            "_plex_request": fake.request,
            "PLEX_PLAYLIST_CHUNK_SIZE": 1,
        }
        with mock.patch.multiple(ps, **patches), \
                mock.patch.object(ps.composite_workflows, "export_playlist_m3u", return_value={"ok": True}):
            result = ps._create_playlist_outputs("Mix", self.ITEMS, log=[])
        return result["plex"], stored.get("last_plex") or {}

    def test_failed_create_keeps_the_old_playlist_and_its_identity(self):  # IA-03
        fake = _FakePlex({"100": {"title": "Mix", "items": ["1"]}}, fail_post=socket.timeout("slow"))
        plex, last = self._sync(fake, {"last_plex": {"rating_key": "100"}})
        self.assertEqual(plex["status"], "failed")
        self.assertIn("100", fake.playlists)
        self.assertNotIn(("DELETE", "/playlists/100"), fake.calls)
        self.assertEqual(last.get("rating_key"), "100")
        self.assertEqual(plex["issue_reason"], "Plex did not answer in time")
        self.assertEqual([c for c in fake.calls if c[0] == "POST"], [("POST", "/playlists")])

    def test_failed_chunk_append_removes_the_partial_copy(self):  # IA-16
        fake = _FakePlex({"100": {"title": "Mix", "items": ["1"]}}, fail_put=http_error(500))
        plex, last = self._sync(fake, {"last_plex": {"rating_key": "100"}})
        self.assertEqual(plex["status"], "failed")
        self.assertEqual(list(fake.playlists), ["100"])  # partial new one deleted, old one intact
        self.assertEqual(last.get("rating_key"), "100")

    def test_success_replaces_the_old_playlist_after_creating_the_new_one(self):
        fake = _FakePlex({"100": {"title": "Mix", "items": ["1"]}})
        plex, last = self._sync(fake, {"last_plex": {"rating_key": "100"}})
        self.assertEqual(plex["status"], "success")
        self.assertEqual(plex["replaced"], 1)
        self.assertNotIn("100", fake.playlists)
        self.assertLess(fake.calls.index(("POST", "/playlists")), fake.calls.index(("DELETE", "/playlists/100")))
        self.assertEqual(last["rating_key"], plex["rating_key"])
        self.assertEqual(fake.playlists[plex["rating_key"]]["items"], ["11", "12", "13"])

    def test_title_fallback_replaces_the_single_same_title_playlist(self):
        fake = _FakePlex({"100": {"title": "Mix", "items": ["1"]}})
        plex, _ = self._sync(fake, {})
        self.assertEqual(plex["replaced"], 1)
        self.assertEqual(list(fake.playlists), [plex["rating_key"]])

    def test_old_playlist_delete_failure_is_reported(self):
        fake = _FakePlex({"100": {"title": "Mix", "items": ["1"]}}, fail_delete=http_error(500))
        plex, _ = self._sync(fake, {"last_plex": {"rating_key": "100"}})
        self.assertEqual(plex["status"], "partial_success")
        self.assertEqual(plex["issue_reason"], "previous Plex playlist could not be removed")


# -- Spotify: IA-19 -------------------------------------------------------------------

class SpotifyTests(_BoundaryCase):
    def _fetch(self, effects):
        with mock.patch.object(urllib.request, "urlopen", side_effect=effects):
            return ps._fetch_spotify_playlist_tracks("pl", "cid", "secret")

    def _error(self, effects):
        with self.assertRaises(ps._SpotifyFetchError) as ctx:
            self._fetch(effects)
        return str(ctx.exception)

    def test_malformed_artist_objects_do_not_crash(self):
        page = {"items": [{"track": {"name": "Song", "artists": [{"id": "x"}, {"name": "B"}, "junk"]}},
                          {"track": None}, "junk"], "next": None}
        self.assertEqual(self._fetch([_Resp({"access_token": "tok"}), _Resp(page)]),
                         [{"artist": "B", "title": "Song"}])

    def test_failures_are_told_apart(self):
        self.assertIn("client ID/secret", self._error([http_error(401)]))
        self.assertIn("rate limiting", self._error([_Resp({"access_token": "tok"})] + [http_error(429)] * 3))
        self.assertIn("not found", self._error([_Resp({"access_token": "tok"}), http_error(404)]))
        self.assertIn("refused access", self._error([_Resp({"access_token": "tok"}), http_error(403)]))
        self.assertIn("unavailable", self._error([_Resp({"access_token": "tok"})] + [http_error(503)] * 3))
        self.assertEqual(self._error([_Resp({"nope": 1})]), "Spotify authentication failed.")
        self.assertEqual(self._error([_Resp({"access_token": "tok"}), _Resp(["list"])]),
                         "Spotify playlist fetch failed.")


# -- slskd: IA-06, IA-08 --------------------------------------------------------------

class SlskdTests(_BoundaryCase):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(slskd, "SLSKD_API_KEY", "k"),
                        mock.patch.object(slskd.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _search_with_poll_error(self, error):
        def req(method, path, body=None):
            if method == "POST":
                return {}
            raise error
        with mock.patch.object(slskd, "_slskd_req", side_effect=req):
            with self.assertRaises(RuntimeError) as ctx:
                slskd._slskd_search_and_queue("Artist", "Album", "", [])
        return str(ctx.exception)

    def test_poll_outage_is_not_no_results(self):  # IA-06
        for error in (RuntimeError("slskd GET /searches/x -> HTTP 401: "), socket.timeout("timed out"),
                      urllib.error.URLError("refused")):
            with self.subTest(error=error):
                message = self._search_with_poll_error(error)
                self.assertIn("unavailable", message)
                self.assertNotIn("No Soulseek results", message)

    def test_a_real_empty_search_is_still_no_results(self):
        def req(method, path, body=None):
            return {} if method == "POST" else {"state": "Completed", "responseCount": 0, "responses": []}
        with mock.patch.object(slskd, "_slskd_req", side_effect=req):
            with self.assertRaisesRegex(RuntimeError, "No Soulseek results"):
                slskd._slskd_search_and_queue("Artist", "Album", "", [])

    def test_request_failure_modes(self):  # IA-08
        cases = [(http_error(401, body=b"Unauthorized"), RuntimeError, "HTTP 401"),
                 (http_error(503), RuntimeError, "HTTP 503"),
                 (http_error(429), RuntimeError, "HTTP 429"),
                 (socket.timeout("slow"), socket.timeout, ""),
                 (urllib.error.URLError("Name or service not known"), urllib.error.URLError, "")]
        for effect, exc_type, text in cases:
            with self.subTest(effect=effect):
                with mock.patch.object(urllib.request, "urlopen", side_effect=effect):
                    with self.assertRaises(exc_type) as ctx:
                        slskd._slskd_req("GET", "searches/x")
                self.assertIn(text, str(ctx.exception))
                self.assertNotIn("X-API-Key", str(ctx.exception))
        with mock.patch.object(urllib.request, "urlopen", side_effect=[_Resp(b"{not json")]):
            with self.assertRaises(ValueError):
                slskd._slskd_req("GET", "searches/x")

    def test_provider_body_is_never_echoed(self):
        body = b"secret-peer-token /home/someone/private"
        with mock.patch.object(urllib.request, "urlopen", side_effect=http_error(500, body=body)):
            with self.assertRaises(RuntimeError) as ctx:
                slskd._slskd_req("POST", "searches", {"id": "x"})
        self.assertTrue(str(ctx.exception).endswith("HTTP 500"), str(ctx.exception))
        self.assertNotIn("secret-peer-token", str(ctx.exception))

    def test_a_post_is_not_resent_by_the_boundary(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=http_error(503)) as urlopen:
            with self.assertRaises(RuntimeError):
                slskd._slskd_req("POST", "searches", {"id": "x"})
        self.assertEqual(urlopen.call_count, 1)


# -- Lidarr acquisition: IA-07, IA-08 -------------------------------------------------

def _wanted_page(records):
    return _Resp({"page": 1, "records": records})


_WANTED = {"id": 7, "title": "Album", "releaseDate": "1999-01-01T00:00:00Z", "albumType": "Album",
           "monitored": True, "foreignAlbumId": RG, "artist": {"artistName": "Artist"}}


class LidarrWantedTests(_BoundaryCase):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(acq, "LIDARR_KEY", "k"),
                        mock.patch.object(acq, "LIDARR_URL", "http://lidarr.test:8686")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fetch(self, effects):
        with mock.patch.object(urllib.request, "urlopen", side_effect=effects):
            return acq._acq_fetch_lidarr_wanted()

    def test_foreign_album_id_is_a_release_group(self):  # IA-07
        rows, error = self._fetch([_wanted_page([_WANTED])])
        self.assertEqual(error, "")
        self.assertEqual(rows[0]["mb_releasegroupid"], RG)
        self.assertEqual(rows[0]["mb_albumid"], "")

    def test_failure_modes_are_reported_not_empty(self):  # IA-08
        cases = [([http_error(401)], "Lidarr rejected the API key"),
                 ([http_error(429)] * 2, "Lidarr is rate limiting requests"),
                 ([http_error(503)] * 2, "Could not reach Lidarr"),
                 ([socket.timeout("slow")] * 2, "Could not reach Lidarr"),
                 ([urllib.error.URLError("Name or service not known")] * 2, "Could not reach Lidarr"),
                 ([_Resp(b"<html>")], "Lidarr returned an unexpected response"),
                 ([_Resp({"records": "nope"})], "Lidarr returned an unexpected response"),
                 ([_Resp(["x"])], "Lidarr returned an unexpected response")]
        for effects, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self._fetch(effects), ([], reason))

    def test_a_failed_later_page_is_not_a_partial_success(self):
        full = [dict(_WANTED, id=i) for i in range(100)]
        self.assertEqual(self._fetch([_wanted_page(full)] + [socket.timeout("slow")] * 2),
                         ([], "Could not reach Lidarr"))

    def test_bad_rows_are_skipped_not_fatal(self):
        rows, error = self._fetch([_wanted_page(["junk", dict(_WANTED, id="abc", foreignAlbumId="x")])])
        self.assertEqual(error, "")
        self.assertEqual((rows[0]["lidarr_id"], rows[0]["mb_releasegroupid"]), (0, ""))


class AcquisitionQueueIdentityTests(unittest.TestCase):
    LOCAL = {"albumartist": "Artist", "album": "Album", "year": 2003, "album_id": 5,
             "mb_albumid": REL, "mb_releasegroupid": RG}

    def _queue(self, wanted, *, local_complete=True):
        wanted_row = {"artist": "Artist", "album": "Album", "year": "1999", "lidarr_id": 7, "mb_albumid": "",
                      "mb_releasegroupid": wanted, "mb_url": "", "monitored": True}
        library = {"ok": True, "artists": [{"name": "Artist", "albums": [dict(self.LOCAL)]}]}
        with mock.patch.object(acq, "get_library_payload", return_value=library), \
                mock.patch.object(acq, "_acq_fetch_lidarr_wanted", return_value=([wanted_row], "")), \
                mock.patch.object(acq, "_acq_needs_acquisition", return_value=not local_complete), \
                mock.patch.object(acq, "_acq_locally_satisfies_wanted", return_value=local_complete), \
                mock.patch.object(acq, "_acq_health", return_value={"missing": 1}):
            return acq._build_acquisition_queue_payload()["items"]

    def test_complete_local_album_suppresses_the_same_release_group(self):  # IA-07
        self.assertEqual(self._queue(RG), [])

    def test_a_different_release_group_with_the_same_title_is_still_wanted(self):
        items = self._queue(OTHER_RG)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["mbid"], f"https://musicbrainz.org/release-group/{OTHER_RG}")

    def test_incomplete_local_album_merges_by_release_group(self):
        items = self._queue(RG, local_complete=False)
        self.assertEqual(len(items), 1)
        self.assertEqual(sorted(items[0]["sources"]), ["beets", "lidarr"])
        self.assertEqual(items[0]["mbid"], REL)  # the local release, never the RG as a release

    def test_download_payload_accepts_a_release_group(self):
        self.assertEqual(acq._acq_release_group_url(RG.upper()), f"https://musicbrainz.org/release-group/{RG}")
        self.assertEqual(acq._acq_release_group_url(""), "")


# -- routes during a MusicBrainz outage (QA F-1/F-2), boundary follow-ups (F-3, sec F1) --

def _patch_all(target, **attrs):
    return mock.patch.multiple(target, **attrs)


class _RouteCase(_BoundaryCase):
    def setUp(self):
        super().setUp()
        from tests._app_family import patch_app_family
        for patcher in (patch_app_family("app", "_security_auth_disabled", return_value=True),
                        mock.patch.object(urllib.request, "urlopen", side_effect=http_error(503))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = app_module.app.test_client()

    def assertUnavailable(self, res):
        self.assertEqual(res.status_code, 503)
        body = res.get_json()
        self.assertEqual(body, {"ok": False, "error": "MusicBrainz is unavailable; try again later",
                                "unavailable": True})


class MusicBrainzOutageRouteTests(_RouteCase):
    def test_mb_candidate_routes_are_503_not_500(self):
        import routes_library
        lib = mock.Mock()
        lib.get_item.return_value = mock.Mock(title="Song", artist="Artist", albumartist="")
        lib.get_album.return_value = mock.Mock(album="Album", albumartist="Artist", mb_albumid="")
        with mock.patch.object(routes_library, "lib", lib):
            self.assertUnavailable(self.client.get("/api/items/1/mb-candidates"))
            self.assertUnavailable(self.client.get("/api/albums/1/mb-candidates"))

    def test_folder_ai_suggest_keeps_the_specific_reason(self):
        import backend.ai_service as ai_service
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        evidence = {"audio_files": [], "folder_track_count": 0, "nested_audio_count": 0,
                    "guessed_artist": "Artist", "guessed_album": "Album", "guessed_year": "",
                    "track_lines": [], "filenames": []}
        with _patch_all(ai_service, _ai_api_key=mock.Mock(return_value=""),
                        _build_folder_evidence=mock.Mock(return_value=evidence),
                        _resolve_import_review_source_path=mock.Mock(return_value=(Path(tmp.name), None)),
                        _acoustid_multi_file=mock.Mock(return_value={}),
                        _acoustid_lookup_cached=mock.Mock(return_value=[]),
                        _discogs_release_fallback_candidate=mock.Mock(return_value={})):
            res = self.client.post("/api/folders/ai-suggest", json={"path": tmp.name})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["musicbrainz_unavailable"])
        self.assertTrue(body["suggestion"]["reason"].startswith("MusicBrainz lookup failed"))

    def test_track_ai_suggest_keeps_acoustid_evidence(self):
        import routes_library
        lib = mock.Mock()
        lib.get_item.return_value = mock.Mock(
            title="Song", artist="Artist", album="Album", albumartist="Artist", year=2001, track=1,
            path=b"/music/a/song.flac", length=200.0, genre="", label="", mb_trackid="", mb_albumid="",
            mb_releasegroupid="")
        acoustid = [{"mb_trackid": REL, "title": "Song", "artist": "Artist", "album": "Album",
                     "year": "2001", "score": 97, "source": "acoustid", "country": ""}]
        with _patch_all(routes_library, lib=lib, _ai_api_key=mock.Mock(return_value=""),
                        _item_ai_abs_path=mock.Mock(return_value="/music/a/song.flac"),
                        _acoustid_lookup_cached=mock.Mock(return_value=acoustid),
                        _discogs_track_search=mock.Mock(return_value=[])):
            res = self.client.post("/api/items/1/ai-suggest", json={})
        body = res.get_json()
        self.assertTrue(body["ok"], body)
        self.assertTrue(body["musicbrainz_unavailable"])
        self.assertEqual(body["acoustid_candidates"], acoustid)
        self.assertIn(REL, [c.get("mb_trackid") for c in body["mb_candidates"]])

    def test_playlist_suggestions_keep_beets_suggestions(self):
        import routes_playlist
        track = {"artist": "Artist", "title": "Song"}
        index = {"by_title": {ps._norm("Song"): [{"id": 4, "artist": "Artist", "title": "Song", "album": "A"}]}}
        with _patch_all(routes_playlist, _playlist_saved_playlist_exists=mock.Mock(return_value=True),
                        _playlist_library_index=mock.Mock(return_value=index),
                        _playlist_detail_payload=mock.Mock(return_value={"missing": [track, dict(track)]})), \
                mock.patch.object(ps, "_match_track", return_value=None):
            res = self.client.get("/api/playlists/Mix/suggestions")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["musicbrainz_unavailable"])
        self.assertEqual([r["best"]["source"] for r in body["rows"]], ["beets-title", "beets-title"])
        # the outage is detected once; later tracks do not ask MusicBrainz again
        self.assertEqual(urllib.request.urlopen.call_count, pb.POLICIES["musicbrainz"].max_attempts)


class BoundaryFollowUpTests(_BoundaryCase):
    def test_overflowing_retry_after_date_keeps_the_http_error(self):  # security F1
        headers = {"Retry-After": "Wed, 21 Oct 99999999999999999999 07:28:00 GMT"}
        self.assertIsNone(pb._retry_after(headers))
        with mock.patch.object(urllib.request, "urlopen", side_effect=http_error(503, headers)):
            with self.assertRaises(urllib.error.HTTPError):
                with pb.opened("lidarr", "https://provider.test/x", timeout=5):
                    pass

    def test_a_bug_in_the_callers_block_is_not_a_provider_failure(self):  # QA F-3
        with mock.patch.object(urllib.request, "urlopen", side_effect=[_Resp(b"{}")]):
            with self.assertRaises(KeyError):
                with pb.opened("plex", "https://provider.test/x", timeout=5) as r:
                    json.loads(r.read())["missing"]
        row = pb.provider_health().get("plex") or {}
        self.assertEqual(row.get("failures", 0), 0)

    def test_a_malformed_body_is_still_a_provider_failure(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=[_Resp(b"<html>")]):
            with self.assertRaises(ValueError):
                with pb.opened("plex", "https://provider.test/x", timeout=5) as r:
                    json.loads(r.read())
        self.assertEqual(pb.provider_health()["plex"]["failures"], 1)


class LidarrRouteHelperTests(_BoundaryCase):  # QA F-5
    def _message(self, effects):
        import routes_lidarr
        with mock.patch.object(routes_lidarr, "LIDARR_KEY", "k"), \
                mock.patch.object(routes_lidarr, "LIDARR_URL", "http://lidarr.test:8686"), \
                mock.patch.object(urllib.request, "urlopen", side_effect=effects):
            try:
                routes_lidarr._lidarr_request_json("/api/v1/album")
            except Exception as exc:  # noqa: BLE001 -- the helper under test maps it
                return routes_lidarr._http_error_message(exc)
        self.fail("no error raised")

    def test_failure_modes(self):
        self.assertEqual(self._message([http_error(401)]), ("Lidarr rejected the API key", 502))
        self.assertEqual(self._message([http_error(429)] * 2), ("Lidarr returned HTTP 429", 502))
        self.assertEqual(self._message([socket.timeout("slow")] * 2), ("Could not reach Lidarr", 502))
        self.assertEqual(self._message([_Resp(b"<html>")]), ("Lidarr returned an unexpected response", 502))


# -- download root: BA-12-------------------------------------------------------------

class DownloadRootTests(unittest.TestCase):
    def test_plex_and_slskd_use_the_configured_download_root(self):
        # config_layers.downloads_root() (DOWNLOADS_ROOT / DOWNLOAD_PATH / /downloads)
        # is covered by tests/test_config_layers.py; here: these modules use it.
        from backend import config_layers
        expected = Path(config_layers.downloads_root())
        for module in (slskd, plex_service):
            with self.subTest(module=module.__name__):
                self.assertEqual(module.DOWNLOADS_ROOT, expected)
                source = Path(module.__file__).read_text(encoding="utf-8")
                self.assertNotIn("/data/torrents", source)
                self.assertNotIn("/data/downloads", source)
                self.assertNotIn("DOWNLOADS_ROOT.parent", source)  # "/" under the /downloads default

    def test_slskd_never_searches_shared_system_folders(self):
        # The completed-file search waits on slskd in a loop (#235 bounds it),
        # so the root list is checked at source level.
        source = Path(slskd.__file__).read_text(encoding="utf-8")
        for literal in ('"/tmp"', '"/download"', '"/downloads"'):
            self.assertNotIn(literal, source)
        self.assertIn("for raw in (expected, user_root, DOWNLOADS_ROOT, *TORRENT_SOURCE_ROOTS):", source)
        self.assertIn("if any(_path_is_under(cand, root) for root in roots):", source)


if __name__ == "__main__":
    unittest.main()
