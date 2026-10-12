"""Reimport/import resolver identity follow-ups (#264, #261 F-6).

All MusicBrainz data is synthetic; nothing here calls a provider.
"""
import inspect
import unittest
from unittest.mock import MagicMock, patch

import backend.import_service as imp
import backend.library_service as ls

RG = "11111111-1111-1111-1111-111111111111"
REL_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
FOLDER = "/downloads/Artist - Album"


def _resolve(mb_input, *, source_folder=FOLDER, group=(), fitting=(), rg_first=REL_A,
             has_tracks=lambda r: r != RG):
    log, preflights = [], []
    search = MagicMock(return_value=[{"mb_albumid": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                                      "artist": "Artist", "album": "Album", "tracks": 10}])

    def _pre(_f, rel, **_k):
        preflights.append(rel)
        return {"ok": rel in fitting, "matches": 10, "expected": 10, "audio_count": 10}

    with patch.object(ls, "_folder_import_track_count", return_value=10), \
         patch.object(ls, "_resolve_release_group_to_release", return_value=rg_first), \
         patch.object(ls, "_mb_release_group_candidates",
                      return_value=[{"mb_albumid": r, "track_count": 10} for r in group]), \
         patch.object(ls, "_folder_release_preflight", side_effect=_pre), \
         patch.object(ls, "_mb_release_search", search), \
         patch.object(ls, "_mb_release_search_by_folder_tracks", search), \
         patch.object(ls, "_resolve_mb_release_id", side_effect=lambda s, _l: s.lower()), \
         patch.object(ls, "_mb_release_has_tracks", side_effect=has_tracks):
        got = ls._resolve_album_release_for_import(mb_input, "Artist", "Album", "", 10, log,
                                                   source_folder=source_folder)
    return got, log, preflights, search


class TextOnlyIdentityTests(unittest.TestCase):
    """#261 F-6: identity inferred from a text search alone goes to review."""

    def test_no_folder_free_search_refuses_and_does_not_search(self):
        got, log, _, search = _resolve("", source_folder="")
        self.assertEqual(got, "")
        search.assert_not_called()
        self.assertTrue(any("text match alone never sets album identity" in line for line in log))

    def test_folder_backed_search_still_needs_a_passing_preflight(self):
        got, _, _, _ = _resolve("", fitting=())
        self.assertEqual(got, "")
        got, _, _, _ = _resolve("", fitting={"cccccccc-cccc-cccc-cccc-cccccccccccc"})
        self.assertEqual(got, "cccccccc-cccc-cccc-cccc-cccccccccccc")


class ReleaseGroupResolverTests(unittest.TestCase):
    """#264."""

    def test_bare_rg_uuid_lookup_failure_fails_closed(self):
        # MusicBrainz 503 / empty group: never the free search.
        got, log, _, search = _resolve(RG, rg_first="")
        self.assertEqual(got, "")
        search.assert_not_called()
        self.assertTrue(any("REFUSED" in line for line in log))

    def test_candidate_loop_is_capped(self):
        group = [f"{i:08x}-0000-0000-0000-000000000000" for i in range(1, 60)]
        got, log, preflights, _ = _resolve(f"https://musicbrainz.org/release-group/{RG}", group=group)
        self.assertEqual(got, "")
        # The ranked release plus at most the cap of alternatives.
        self.assertLessEqual(len(preflights), 1 + ls._RG_ALTERNATIVE_RELEASE_CAP)
        self.assertTrue(any("closest by track count" in line for line in log))

    def test_import_with_id_passes_the_release_group_url(self):
        src = inspect.getsource(imp.start_folder_import_with_id)
        self.assertIn('f"https://musicbrainz.org/release-group/{selected_releasegroupid}"', src)

    def test_reimport_disk_accepts_release_group_only(self):
        started = {}

        def _start(_fn, label="", metadata=None):
            started.update(metadata or {})
            return MagicMock(job_id="j1")

        with patch.object(imp.composite_workflows, "inspect_import_source",
                          return_value={"ok": True, "path": FOLDER, "audio_file_count": 3}), \
             patch.object(imp, "_library_album_ids_for_folder", return_value=[]), \
             patch.object(imp.jobs, "start_python", side_effect=_start):
            body, status = imp.start_reimport_disk({"aldir": FOLDER, "mb_releasegroupid": RG})
        self.assertEqual(status, 200, body)
        self.assertEqual(started["mb_albumid"], f"https://musicbrainz.org/release-group/{RG}")

    def test_reimport_disk_still_requires_an_id(self):
        body, status = imp.start_reimport_disk({"aldir": FOLDER})
        self.assertEqual(status, 400)
        self.assertIn("mb_albumid required", body["error"])


class ProviderOutageReportingTests(unittest.TestCase):
    """#252 NF-1, #297 N4: an outage is reported as not checked, never as
    "no candidates", and evidence already gathered is kept."""

    def setUp(self):
        import tempfile, os
        fd, self.audio = tempfile.mkstemp(suffix=".flac")
        os.close(fd)
        self.addCleanup(os.remove, self.audio)

    def _album_suggest(self, aid_result, mb_side_effect):
        import types
        import backend.ai_service as ai
        track = types.SimpleNamespace(path=self.audio, title="Song", track=1)
        album = types.SimpleNamespace(id=7, album="Album", albumartist="Artist", year=2001, items=lambda: [track])
        with patch.object(ai, "_ai_api_key", return_value=""), \
             patch.object(ai, "_album_preflight_folder", return_value=""), \
             patch.object(ai, "_acoustid_multi_file", return_value={}), \
             patch.object(ai, "_acoustid_lookup_cached_outcome", return_value=aid_result), \
             patch.object(ai, "_mb_release_search", side_effect=mb_side_effect), \
             patch.object(ai, "_ai_match_evidence_packet", return_value={}):
            return ai._ai_suggest_album_internal(album, [])

    def test_album_suggest_mb_outage_keeps_acoustid_evidence(self):
        from backend.provider_boundary import ProviderError, ProviderOutcome, ProviderResult
        hit = {"mb_trackid": "r1", "score": 95}
        out = self._album_suggest(ProviderResult("acoustid", ProviderOutcome.CONFIRMED, data=[hit]),
                                  ProviderError(ProviderOutcome.UNAVAILABLE, "503"))
        self.assertTrue(out["ok"])
        self.assertTrue(out["musicbrainz_unavailable"])
        self.assertEqual(out["acoustid_candidates"], [hit])
        self.assertEqual(out["acoustid_status"], "")
        self.assertIn("could not be reached", out["suggestion"]["reason"])

    def test_album_suggest_reports_acoustid_not_checked(self):
        from backend.provider_boundary import ProviderOutcome, ProviderResult
        for outcome, status in ((ProviderOutcome.NOT_CONFIGURED, "not_configured"),
                                (ProviderOutcome.AUTHENTICATION_ERROR, "auth_failed"),
                                (ProviderOutcome.UNAVAILABLE, "lookup_failed")):
            with self.subTest(status):
                out = self._album_suggest(ProviderResult("acoustid", outcome), lambda *a, **k: [])
                self.assertEqual(out["acoustid_status"], status)
                self.assertTrue(out["acoustid_unavailable"])
                self.assertFalse(out["musicbrainz_unavailable"])
                self.assertIn("No MusicBrainz release candidates", out["suggestion"]["reason"])

    def _track_candidates(self, aid_result):
        import types
        item = types.SimpleNamespace(path=self.audio, title="Song", artist="Artist", album="Album",
                                     albumartist="Artist", year=2001, track=1, length=200.0)
        captured = {}
        with patch.object(ls, "_item_ai_abs_path", return_value=self.audio), \
             patch.object(ls, "_acoustid_lookup_cached_outcome", return_value=aid_result), \
             patch.object(ls, "_mb_recording_search", return_value=[]), \
             patch.object(ls.recording_review, "enrich_and_index",
                          side_effect=lambda c, f, hits: captured.setdefault("hits", hits)):
            ls._reconstruct_track_recording_candidates(item, 1)
        return captured["hits"]

    def test_track_candidates_pass_none_when_acoustid_did_not_answer(self):
        from backend.provider_boundary import ProviderOutcome, ProviderResult
        self.assertIsNone(self._track_candidates(ProviderResult("acoustid", ProviderOutcome.UNAVAILABLE)))
        self.assertIsNone(self._track_candidates(ProviderResult("acoustid", ProviderOutcome.NOT_CONFIGURED)))
        # A real "no result" answer is still an answer.
        self.assertIsNotNone(self._track_candidates(ProviderResult("acoustid", ProviderOutcome.NO_RESULT, data=[])))


class EvidenceFingerprintStatusTests(unittest.TestCase):
    """QA D11: AcoustID not checked is "unavailable" in the evidence packet,
    never "no_result"; a real empty answer stays "no_result"."""

    def test_album_and_folder_packet(self):
        import backend.ai_service as ai
        fp = ai._ai_match_evidence_packet("fresh_import", acoustid_status="not_configured")["fingerprint"]
        self.assertEqual((fp["status"], fp["unavailable_reason"]), ("unavailable", "not_configured"))
        self.assertEqual(ai._ai_match_evidence_packet("fresh_import")["fingerprint"]["status"], "no_result")

    def test_track_packet(self):
        import backend.ai_service as ai

        def packet(status):
            return ai._track_ai_evidence_packet(
                1, filename="a.mp3", current={}, search_title="t", search_artist="a", suggestions={},
                selected_candidate=None, candidates=[], acoustid_candidates=[], discogs_candidates=[],
                acoustid_status=status)
        ev = packet("auth_failed")
        self.assertEqual(ev["fingerprint"]["status"], "unavailable")
        self.assertEqual(ev["fingerprint"]["acoustid_status"], "unavailable")
        self.assertEqual(ev["validation"]["fingerprint_status"], "unavailable")
        self.assertEqual(packet("")["fingerprint"]["status"], "no_result")

    def test_suggest_routes_pass_the_status(self):
        import inspect
        import backend.ai_service as ai
        import routes_library
        for fn in (ai._ai_suggest_album_internal, ai._ai_suggest_folder_internal):
            src = inspect.getsource(fn)
            self.assertEqual(src.count("_ai_match_evidence_packet("), src.count("acoustid_status=acoustid_status,"),
                             fn.__name__)
        self.assertIn("acoustid_status=acoustid_status,", inspect.getsource(routes_library))


class AcoustIDUnavailableReviewReasonTests(unittest.TestCase):
    """#303: review stays the outcome without fingerprint evidence, but the
    reason says how to resolve it."""

    def _decide(self, items):
        from backend.matching_contract import build_album_matching_decision
        tracks = [{"title": "Song 1", "track": 1, "length": 200}, {"title": "Song 2", "track": 2, "length": 210}]
        cand = {"mb_releasegroupid": RG, "mb_albumid": REL_A, "album": "Album", "artist": "Artist"}
        return build_album_matching_decision(current={"album": "Album", "artist": "Artist"}, candidate=cand,
                                             items=items, mb_tracks=tracks).decision

    def test_review_reason_names_the_acoustid_fix(self):
        d = self._decide([{"title": "Song 1", "track": 1, "length": 200},
                          {"title": "Song 2", "track": 2, "length": 210}])
        self.assertFalse(d["action_allowed"])
        self.assertIn("acoustid_unavailable", d["warnings"])
        self.assertIn("ACOUSTID_API_KEY", d["eligibility_reason"])

    def test_answered_lookup_with_no_result_carries_no_key_hint(self):
        # AcoustID answered (no match): still review, but setting a key is not the fix.
        d = self._decide([{"title": "Song 1", "track": 1, "length": 200, "acoustid_hits": []},
                          {"title": "Song 2", "track": 2, "length": 210, "acoustid_hits": []}])
        self.assertFalse(d["action_allowed"])
        self.assertNotIn("acoustid_unavailable", d["warnings"])
        self.assertNotIn("ACOUSTID_API_KEY", d["eligibility_reason"])


if __name__ == "__main__":
    unittest.main()
