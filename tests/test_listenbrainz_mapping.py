"""ListenBrainz MBID mapping: a review-only, MusicBrainz-verified candidate
source. Synthetic provider data only; no live calls, no real token."""

import io
import json
import socket
import unittest
import urllib.parse
import urllib.request
from unittest import mock

import backend.provider_boundary as pb
from backend import listenbrainz_mapping as lbm
from backend.matching import AcoustIDRecordingEvidence, evaluate_recording_candidate
from backend.matching.models import AcoustIDStatus
from backend.recording_review import resolve_recording_identity
from backend.security import OutboundPolicyError

TOKEN = "synthetic-test-token-0000"
RID = "11111111-2222-3333-4444-555555555555"
REL = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RG = "99999999-8888-7777-6666-555555555555"


class _Response(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}


def _json(body):
    return _Response(json.dumps(body).encode())


def _mapping_body(**overrides):
    body = {"recording_mbid": RID, "release_mbid": REL, "recording_name": "Here Comes the Sun",
            "artist_credit_name": "The Beatles"}
    body.update(overrides)
    return body


def _mb_details(**overrides):
    details = {"recording_id": RID, "recording_title": "Here Comes the Sun", "recording_artist": "The Beatles",
               "recording_length_ms": 185000, "album": "Abbey Road", "mb_albumid": REL, "year": "1969",
               "linked_releases": [{"mb_albumid": REL, "mb_releasegroupid": RG}],
               "selected_release": {"mb_albumid": REL, "mb_releasegroupid": RG}}
    details.update(overrides)
    return details


def _query(request):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)


class _Base(unittest.TestCase):
    def setUp(self):
        pb.reset_provider_health()
        self.addCleanup(pb.reset_provider_health)
        lbm.clear_cache()
        self.addCleanup(lbm.clear_cache)
        for patcher in (
            mock.patch.dict("os.environ", {"LISTENBRAINZ_TOKEN": TOKEN, "PROVIDER_MAX_ATTEMPTS": ""}),
            mock.patch.dict(pb.POLICIES, {"listenbrainz": pb.ProviderPolicy(2, 0.0)}),
            mock.patch("time.sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class ProviderRoutingTests(_Base):
    def test_lookup_goes_through_the_listenbrainz_provider_with_the_token_header(self):
        self.assertIn("listenbrainz", pb.POLICIES)
        with mock.patch.object(urllib.request, "urlopen", return_value=_json(_mapping_body())) as urlopen:
            result = lbm.lookup_recording("Here Comes the Sun", "The Beatles")
        self.assertEqual(result.outcome, pb.ProviderOutcome.CONFIRMED)
        self.assertEqual(result.data["recording_mbid"], RID)
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.startswith("https://api.listenbrainz.org/1/metadata/lookup/?"))
        self.assertEqual(request.get_header("Authorization"), f"Token {TOKEN}")
        self.assertEqual(_query(request)["recording_name"], ["Here Comes the Sun"])
        row = pb.provider_health()["listenbrainz"]
        self.assertEqual((row["calls"], row["last_outcome"]), (1, "confirmed"))

    def test_without_a_token_nothing_is_sent(self):
        with mock.patch.dict("os.environ", {"LISTENBRAINZ_TOKEN": ""}), \
                mock.patch.object(urllib.request, "urlopen") as urlopen:
            out = lbm.listenbrainz_recording_candidates("Song", "Artist", 200)
        urlopen.assert_not_called()
        self.assertEqual((out["status"], out["candidates"]), ("not_configured", []))


class CleanedRetryTests(_Base):
    def test_a_miss_is_retried_once_with_the_cleaned_query(self):
        responses = [_json({}), _json(_mapping_body())]
        with mock.patch.object(urllib.request, "urlopen", side_effect=responses) as urlopen:
            result = lbm.lookup_recording("Here Comes the Sun (2019 Remaster)", "The Beatles feat. Nobody")
        self.assertEqual(result.outcome, pb.ProviderOutcome.CONFIRMED)
        first, second = (_query(c.args[0]) for c in urlopen.call_args_list)
        self.assertEqual(first["recording_name"], ["Here Comes the Sun (2019 Remaster)"])
        self.assertEqual(second["recording_name"], ["here comes the sun"])
        self.assertEqual(second["artist_name"], ["the beatles"])

    def test_two_misses_stop_after_the_one_retry(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=[_json({}), _json({})]) as urlopen:
            result = lbm.lookup_recording("Song - Remastered 2009", "Artist")
        self.assertEqual((result.outcome, urlopen.call_count), (pb.ProviderOutcome.NO_RESULT, 2))

    def test_no_retry_when_cleaning_changes_nothing(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=[_json({})]) as urlopen:
            lbm.lookup_recording("song", "artist")
        self.assertEqual(urlopen.call_count, 1)

    def test_a_failure_is_not_retried_as_a_miss(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=OutboundPolicyError("blocked")) as urlopen:
            result = lbm.lookup_recording("Song (Live)", "Artist")
        self.assertEqual((result.outcome, urlopen.call_count), (pb.ProviderOutcome.REJECTED, 1))


class VerificationTests(unittest.TestCase):
    def _verify(self, details, duration=186, mapping=None):
        return lbm.verify_mapping(mapping or _mapping_body(), title="Here Comes the Sun", artist="The Beatles",
                                  duration_seconds=duration, fetch_recording=lambda rid, rel: details)

    def test_agreeing_musicbrainz_lookup_yields_a_review_only_candidate(self):
        cand, reason = self._verify(_mb_details())
        self.assertEqual(reason, "")
        self.assertEqual((cand["mb_trackid"], cand["mb_releasegroupid"], cand["source"]), (RID, RG, "listenbrainz"))
        self.assertTrue(cand["review_required"])

    def test_musicbrainz_disagreement_rejects_the_candidate(self):
        cases = {
            "musicbrainz_lookup_failed": {},
            "title_disagrees": _mb_details(recording_title="Something"),
            "artist_disagrees": _mb_details(recording_artist="Nina Simone"),
            "tracklist_disagrees": _mb_details(linked_releases=[{"mb_albumid": "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"}]),
            "duration_disagrees": _mb_details(recording_length_ms=240000),
            "duration_unverifiable": _mb_details(recording_length_ms=None),
        }
        for expected, details in cases.items():
            with self.subTest(expected):
                self.assertEqual(self._verify(details), (None, expected))

    def test_unknown_local_duration_cannot_verify(self):
        self.assertEqual(self._verify(_mb_details(), duration=0), (None, "duration_unverifiable"))


class CandidateAloneGoesToReviewTests(unittest.TestCase):
    LOCAL = {"title": "Here Comes the Sun", "artist": "The Beatles", "duration_seconds": 186, "filename": "x.flac"}

    def _candidate(self):
        cand, _ = lbm.verify_mapping(_mapping_body(), title="Here Comes the Sun", artist="The Beatles",
                                     duration_seconds=186, fetch_recording=lambda rid, rel: _mb_details())
        return cand

    def test_evaluator_never_auto_attaches_a_listenbrainz_candidate(self):
        cand = self._candidate()
        result = evaluate_recording_candidate(
            self.LOCAL,
            {"recording_id": cand["mb_trackid"], "title": cand["title"], "artist": cand["artist"],
             "release_group_id": cand["mb_releasegroupid"], "duration_seconds": 185},
            acoustid=AcoustIDRecordingEvidence(status=AcoustIDStatus.NO_RESULT))
        self.assertFalse(result.can_auto_attach())
        self.assertFalse(result.identity_established())
        self.assertEqual(result.safety_key, "review")
        self.assertIn("no_deterministic_recording_proof", result.review_reasons)

    def test_resolver_does_not_establish_identity_from_it(self):
        out = resolve_recording_identity(self.LOCAL, acoustid_hits=[], text_candidates=[self._candidate()])
        self.assertFalse(out["ok"])
        self.assertEqual(out["recording_id"], "")


class FailureHandlingTests(_Base):
    def _assert_no_leak(self, out):
        text = json.dumps(out) + json.dumps(pb.provider_health())
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("api.listenbrainz.org", text)

    def test_outbound_blocked_yields_no_candidates_and_a_fixed_message(self):
        blocked = OutboundPolicyError("blocked https://api.listenbrainz.org/1/metadata/lookup/?x", entry="")
        fetch = mock.Mock()
        with mock.patch.object(urllib.request, "urlopen", side_effect=blocked):
            out = lbm.listenbrainz_recording_candidates("Song", "Artist", 200, fetch_recording=fetch)
            result = lbm.lookup_recording("Song", "Artist")
        self.assertEqual((out["status"], out["candidates"]), ("rejected", []))
        self.assertEqual(result.message, "ListenBrainz lookup failed")
        fetch.assert_not_called()
        self._assert_no_leak(out)

    def test_timeout_is_bounded_by_the_policy_and_yields_no_candidates(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=socket.timeout("slow")) as urlopen:
            out = lbm.listenbrainz_recording_candidates("Song", "Artist", 200)
        self.assertEqual(urlopen.call_count, 2)  # the listenbrainz policy, no extra miss-retry
        self.assertEqual((out["status"], out["candidates"]), ("transient_error", []))
        self.assertEqual(pb.provider_health()["listenbrainz"]["last_outcome"], "transient_error")
        self._assert_no_leak(out)

    def test_malformed_body_is_a_failure_not_a_miss(self):
        with mock.patch.object(urllib.request, "urlopen", return_value=_Response(b"<html>")):
            out = lbm.listenbrainz_recording_candidates("Song", "Artist", 200)
        self.assertEqual((out["status"], out["candidates"]), ("transient_error", []))


class PlaylistSuggestionTests(unittest.TestCase):
    """Playlist/Find-a-track text resolution: the candidate is offered for
    review, never as a safe suggestion, and only when duration can verify it."""

    def _suggest(self, track, lb_result):
        from backend import playlist_service as ps
        lb = mock.Mock(return_value=lb_result)
        with mock.patch.object(ps, "_match_track", return_value=None),                 mock.patch.object(ps, "_playlist_recording_search_candidates", return_value=[]),                 mock.patch.object(ps, "listenbrainz_recording_candidates", lb):
            return ps._playlist_suggestions_for_track(track, {}, include_musicbrainz=True,
                                                      listenbrainz_state={}), lb

    def test_listenbrainz_suggestion_is_never_safe(self):
        cand, _ = lbm.verify_mapping(_mapping_body(), title="Here Comes the Sun", artist="The Beatles",
                                     duration_seconds=186, fetch_recording=lambda rid, rel: _mb_details())
        rows, lb = self._suggest({"artist": "The Beatles", "title": "Here Comes the Sun", "duration": "3:06"},
                                 {"status": "confirmed", "candidates": [cand], "reason": ""})
        lb.assert_called_once_with("Here Comes the Sun", "The Beatles", 186.0)
        row = next(r for r in rows if r["source"] == "listenbrainz")
        self.assertFalse(row["safe"])
        self.assertTrue(row["review_required"])
        self.assertEqual(row["mb_trackid"], RID)

    def test_not_asked_without_a_duration(self):
        rows, lb = self._suggest({"artist": "The Beatles", "title": "Here Comes the Sun"},
                                 {"status": "confirmed", "candidates": [], "reason": ""})
        lb.assert_not_called()


class CacheTests(_Base):
    def test_a_repeat_lookup_is_served_from_the_cache(self):
        with mock.patch.object(urllib.request, "urlopen", return_value=_json(_mapping_body())) as urlopen:
            first = lbm.lookup_recording("Here Comes the Sun", "The Beatles")
            again = lbm.lookup_recording("here comes the sun (Remastered)", "THE BEATLES")
        self.assertEqual(urlopen.call_count, 1)
        self.assertTrue(again.from_cache)
        self.assertEqual(again.data, first.data)

    def test_a_clean_miss_is_cached(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=[_json({})]) as urlopen:
            lbm.lookup_recording("song", "artist")
            self.assertEqual(lbm.lookup_recording("song", "artist").outcome, pb.ProviderOutcome.NO_RESULT)
        self.assertEqual(urlopen.call_count, 1)

    def test_failures_are_not_cached(self):
        effects = [_http_429(), _http_429(), _json(_mapping_body())]
        with mock.patch.object(urllib.request, "urlopen", side_effect=effects) as urlopen:
            self.assertEqual(lbm.lookup_recording("Song", "Artist").outcome, pb.ProviderOutcome.RATE_LIMITED)
            self.assertEqual(lbm.lookup_recording("Song", "Artist").outcome, pb.ProviderOutcome.CONFIRMED)
        self.assertEqual(urlopen.call_count, 3)

    def test_the_cache_is_bounded(self):
        with mock.patch.object(lbm, "CACHE_MAX_ENTRIES", 2),                 mock.patch.object(urllib.request, "urlopen", side_effect=lambda *a, **k: _json({})):
            for name in ("one", "two", "three"):
                lbm.lookup_recording(name, "artist")
        self.assertEqual(len(lbm._CACHE), 2)


def _http_429():
    import urllib.error
    return urllib.error.HTTPError("https://api.listenbrainz.org/x", 429, "slow", {}, None)


class AmplificationTests(_Base):
    TRACKS = [{"artist": "A", "title": f"Song {n}", "duration": "3:00"} for n in range(3)]

    def _patched(self):
        from backend import playlist_service as ps
        return mock.patch.multiple(ps, _match_track=mock.Mock(return_value=None),
                                   _playlist_recording_search_candidates=mock.Mock(return_value=[]))

    def test_one_429_stops_listenbrainz_for_the_rest_of_the_loop(self):
        from backend import playlist_service as ps
        state = {}
        with self._patched(), mock.patch.object(urllib.request, "urlopen", side_effect=_http_429()) as urlopen:
            for track in self.TRACKS:
                ps._playlist_suggestions_for_track(track, {}, listenbrainz_state=state)
        self.assertEqual(urlopen.call_count, 2)  # one track, the policy's 2 attempts, no cleaned retry
        self.assertTrue(state["listenbrainz_unavailable"])

    def test_a_plain_miss_does_not_stop_the_loop(self):
        from backend import playlist_service as ps
        state = {}
        with self._patched(), mock.patch.object(urllib.request, "urlopen",
                                                side_effect=lambda *a, **k: _json({})) as urlopen:
            for track in self.TRACKS:
                ps._playlist_suggestions_for_track(track, {}, listenbrainz_state=state)
        self.assertEqual(urlopen.call_count, 3)  # every track asked; cleaning changes nothing here
        self.assertFalse(state.get("listenbrainz_unavailable"))

    def test_apply_safe_suggestions_route_never_asks_listenbrainz(self):
        import app as app_module
        import routes_playlist
        from backend import playlist_service as ps
        from tests._app_family import patch_app_family
        lb = mock.Mock(side_effect=AssertionError("ListenBrainz asked"))
        with patch_app_family("app", "_security_auth_disabled", return_value=True), self._patched(),                 mock.patch.object(ps, "listenbrainz_recording_candidates", lb),                 mock.patch.multiple(routes_playlist,
                                    _playlist_saved_playlist_exists=mock.Mock(return_value=True),
                                    _playlist_library_index=mock.Mock(return_value={}),
                                    _playlist_detail_payload=mock.Mock(return_value={"missing": self.TRACKS})):
            client = app_module.app.test_client()
            res = client.post("/api/playlists/Mix/apply-safe-suggestions", json={})
            self.assertEqual(res.status_code, 200)
            lb.assert_not_called()
            lb.side_effect, lb.return_value = None, {"status": "no_result", "candidates": [], "reason": ""}
            self.assertEqual(client.get("/api/playlists/Mix/suggestions").status_code, 200)
        self.assertEqual(lb.call_count, len(self.TRACKS))  # the review list does ask

    def test_missing_ids_batch_asks_for_candidates_without_listenbrainz(self):
        from backend import missing_mb_batch
        item = mock.Mock(album_id=0, mb_trackid="", mb_albumid="", mb_releasegroupid="", path="/x/a.mp3")
        recon = mock.Mock(return_value=({}, [], "", "a.mp3"))
        with mock.patch.object(missing_mb_batch, "lib") as lib,                 mock.patch.object(missing_mb_batch, "_reconstruct_track_recording_candidates", recon):
            lib.get_item.return_value = item
            missing_mb_batch.prove_item(5)
        recon.assert_called_once_with(item, 5, listenbrainz=False)

    def test_reconstruct_without_listenbrainz_makes_no_call(self):
        from backend import library_service as ls
        item = mock.Mock(title="Song", artist="Artist", albumartist="", album="", year="", track=1, genre="",
                         label="", mb_trackid="", mb_albumid="", mb_releasegroupid="", length=200.0, path=b"")
        lb = mock.Mock(side_effect=AssertionError("ListenBrainz asked"))
        with mock.patch.object(ls, "listenbrainz_recording_candidates", lb),                 mock.patch.object(ls, "_mb_recording_search", return_value=[]),                 mock.patch.object(ls, "_item_ai_abs_path", return_value="", create=True):
            ls._reconstruct_track_recording_candidates(item, 5, listenbrainz=False)
        lb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
