"""Golden contract of POST /api/items/<iid>/ai-suggest.

Pins the whole JSON response and the AI request body for every branch of the
handler, so moving its orchestration cannot change what the review UI receives
or what the (untrusted) AI is asked. Regenerate only for an intended contract
change: UPDATE_ITEM_AI_SUGGEST_GOLDEN=1 python -m unittest tests.test_item_ai_suggest_contract
"""
import io
import json
import os
import sys
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import app as app_module
import routes_library
from backend import ai_evidence_service, ai_service, item_ai_suggest_service, library_service, recovery_carrier
from backend import provider_boundary as pb

_TARGET = item_ai_suggest_service
_GOLDEN = Path(__file__).parent / "fixtures" / "item_ai_suggest_golden.json"
_CSRF = {"X-Requested-With": "XMLHttpRequest"}
_REC = "11111111-1111-1111-1111-111111111111"
_REC2 = "22222222-2222-2222-2222-222222222222"
_INVENTED = "99999999-9999-9999-9999-999999999999"
_REL = "33333333-3333-3333-3333-333333333333"
_RG = "44444444-4444-4444-4444-444444444444"
_ARTIST = "55555555-5555-5555-5555-555555555555"


def _item(**over):
    base = dict(title="Some Song", artist="Some Artist", album="Some Album", albumartist="Some Artist",
                year="20120923", genre="", track=3, label="", mb_trackid="", mb_albumid="",
                mb_releasegroupid="", path=b"/music/a/03 - Some Song.flac", length=200.0)
    base.update(over)
    return types.SimpleNamespace(**base)


def _mb_cand(rec=_REC, score=95):
    return {"mb_trackid": rec, "title": "Some Song", "artist": "Some Artist", "album": "Some Album",
            "mb_albumid": _REL, "year": "2012", "score": score, "source": "mb", "country": "US"}


def _details():
    release = {"mb_albumid": _REL, "mb_releasegroupid": _RG, "album": "Some Album", "artist": "Some Artist",
               "year": "2012", "country": "US", "track": "3", "tracktotal": "10", "disc": "1",
               "medium_position": 1, "track_position": 3, "track_count": 10}
    return {"recording_id": _REC, "recording_title": "Some Song", "artist": "Some Artist",
            "mb_artistid": _ARTIST, "genre": "rock", "linked_releases": [release], "selected_release": release,
            "same_recording_release_count": 1, "mb_albumid": _REL, "mb_releasegroupid": _RG, "album": "Some Album",
            "albumartist": "Some Artist", "year": "2012", "track": "3", "tracktotal": "10", "disc": "1",
            "disctotal": "1"}


def _ai_reply(obj):
    raw = obj if isinstance(obj, str) else json.dumps(obj)
    return io.BytesIO(json.dumps({"choices": [{"message": {"content": raw}}]}).encode())


# name -> {item, key, mb (list or Exception), ai (dict/str/Exception), details, evidence_error}
_SCENARIOS = {
    "no_key_no_candidates": {"key": "", "mb": []},
    "no_key_falls_back_to_top_candidate": {"key": "", "mb": [_mb_cand()], "details": _details()},
    "no_key_low_score_candidate": {"key": "", "mb": [_mb_cand(score=10)], "details": _details()},
    "ai_selects_listed_candidate": {
        "mb": [_mb_cand(), _mb_cand(_REC2, 60)], "details": _details(),
        "ai": {"title": "AI Title", "artist": "AI Artist", "album": "AI Album", "year": 20129999, "track": 3,
               "mb_trackid": _REC, "mb_albumid": _REL, "confidence": "high", "reason": "fingerprint"}},
    "ai_invents_id_with_candidates_is_downgraded": {
        "mb": [_mb_cand()], "ai": {"title": "T", "mb_trackid": _INVENTED, "confidence": "high", "reason": "guess"}},
    "ai_invents_id_without_candidates": {
        "mb": [], "ai": {"title": "T", "mb_trackid": _INVENTED, "confidence": "high", "reason": "guess"}},
    "ai_returns_malformed_id": {
        "mb": [_mb_cand()], "ai": {"title": "T", "mb_trackid": "not-a-uuid", "confidence": "high", "reason": "r"}},
    "ai_returns_malformed_id_without_candidates": {
        "mb": [], "ai": {"title": "T", "mb_trackid": "not-a-uuid", "confidence": "high", "reason": "r"}},
    "ai_returns_no_id_or_confidence": {"mb": [], "ai": {"title": "T"}},
    "ai_rejected_key": {
        "mb": [_mb_cand()], "details": _details(),
        "ai": urllib.error.HTTPError("https://ai.test", 401, "Unauthorized", {}, None)},
    "ai_outage": {"mb": [], "ai": urllib.error.URLError("down")},
    "ai_unparseable_reply": {"mb": [_mb_cand()], "details": _details(), "ai": "not json"},
    "musicbrainz_outage": {"key": "", "mb": pb.ProviderError("musicbrainz", pb.ProviderOutcome.UNAVAILABLE)},
    "details_fetch_fails_keeps_suggestion": {
        "mb": [_mb_cand()], "details": RuntimeError("boom"),
        "ai": {"mb_trackid": _REC, "confidence": "medium", "reason": "r"}},
    "acoustid_confirmed_hit_reaches_enrichment": {
        "mb": [], "acoustid": [{**_mb_cand(), "source": "acoustid"}], "details": _details(),
        "ai": {"mb_trackid": _REC, "confidence": "high", "reason": "fingerprint"}},
    "evidence_packet_failure_is_generic_error": {"key": "", "mb": [], "evidence_error": True},
    "serialization_failure_is_generic_error": {"key": "", "mb": [], "unserializable": True},
    "ai_non_string_artist_id_falls_back": {
        "mb": [_mb_cand()], "details": _details(),
        "ai": {"mb_trackid": _REC, "mb_artistid": 123, "confidence": "high", "reason": "r"}},
    "ai_non_string_album_id_falls_back": {
        "mb": [_mb_cand()], "details": _details(),
        "ai": {"mb_trackid": _REC, "mb_albumid": 123, "confidence": "high", "reason": "r"}},
    "title_split_replaces_noise_artist": {
        "key": "", "mb": [], "item": {"title": "Real Artist - Real Song", "artist": "Some Radio Station"}},
    "title_split_keeps_good_artist": {
        "key": "", "mb": [], "item": {"title": "Part 1 - Part 2", "artist": "Good Artist"}},
    "untagged_uses_filename": {
        "key": "", "mb": [], "item": {"title": "", "artist": "", "albumartist": "", "album": "", "year": ""}},
}


def _fake_enrich(current, candidate, details=None, *, item_id=None, ai_state=None, acoustid_hits=None):
    """Enrichment has its own suites; here only its inputs and the fields the handler reads matter."""
    candidate["_match_score"] = {"total": float(candidate.get("score") or 0) / 100}
    candidate["enrich_calls"] = candidate.get("enrich_calls", []) + [{
        "item_id": item_id, "has_details": bool(details), "acoustid_hits": acoustid_hits,
        "ai_state": vars(ai_state) if ai_state is not None else None}]
    candidate["recommended_action"] = "review"
    candidate["requires_confirmation"] = True
    candidate["safety_result"] = "needs_review"
    candidate["confidence_score"] = candidate["_match_score"]["total"]
    return candidate


def _fake_evidence(iid, **kw):
    selected = kw.get("selected_candidate") or {}
    return {"iid": iid, "filename": kw["filename"], "search_title": kw["search_title"],
            "search_artist": kw["search_artist"], "current": kw["current"],
            "selected": selected.get("mb_trackid", ""), "candidates": len(kw["candidates"]),
            "acoustid": len(kw["acoustid_candidates"]), "discogs": len(kw["discogs_candidates"]),
            "acoustid_status": kw["acoustid_status"], "confidence": kw["suggestions"].get("confidence")}


def _unserializable_evidence(iid, **kw):
    return {"not_json": {1, 2}}


def _traced(events, label, fn):
    def call(*args, **kwargs):
        events.append(label)
        return fn(*args, **kwargs)
    return call


def _run(spec, events=None):
    item = _item(**spec.get("item", {}))
    mb = spec.get("mb", [])
    mb_search = mock.Mock(side_effect=mb) if isinstance(mb, Exception) else mock.Mock(return_value=mb)
    details = spec.get("details", {})
    details_fn = (mock.Mock(side_effect=details) if isinstance(details, Exception)
                  else mock.Mock(return_value=details))
    ai = spec.get("ai", {"confidence": "low", "reason": "unused"})
    sent = []
    events = [] if events is None else events

    def opened(provider, req, **_kw):
        events.append("ai")
        sent.append({"provider": provider, "url": req.full_url, "body": json.loads(req.data)})
        if isinstance(ai, Exception):
            raise ai
        return _ai_reply(ai)

    patches = dict(
        _ai_api_key=mock.Mock(return_value=spec.get("key", "test-placeholder")),
        _ai_model_and_endpoint=mock.Mock(return_value=("model", spec.get("endpoint", "https://ai.test/v1"))),
        _item_ai_abs_path=mock.Mock(return_value="/music/a/03 - Some Song.flac"),
        _acoustid_lookup_cached_outcome=_traced(events, "acoustid", mock.Mock(return_value=(
            pb.ProviderResult("acoustid", pb.ProviderOutcome.CONFIRMED, data=spec["acoustid"]) if "acoustid" in spec
            else pb.ProviderResult("acoustid", pb.ProviderOutcome.NOT_CONFIGURED)))),
        _mb_recording_search=_traced(events, "musicbrainz", mb_search),
        _discogs_track_search=_traced(events, "discogs", mock.Mock(return_value=[])),
        _fetch_mb_recording_details=details_fn,
        _enrich_track_ai_candidate=_fake_enrich,
        _track_ai_evidence_packet=(mock.Mock(side_effect=RuntimeError("boom")) if spec.get("evidence_error")
                                   else _unserializable_evidence if spec.get("unserializable")
                                   else ai_service._track_ai_evidence_packet if spec.get("real_packet")
                                   else _fake_evidence),
        _compact_track_ai_candidate=lambda c: {k: v for k, v in (c or {}).items() if k != "enrich_calls"},
    )
    with mock.patch.object(app_module, "_security_auth_disabled", return_value=True), \
            mock.patch.object(routes_library, "lib", mock.Mock(get_item=mock.Mock(return_value=item))), \
            mock.patch.multiple(_TARGET, **patches), \
            mock.patch.object(pb, "opened", side_effect=opened), \
            mock.patch.object(pb, "provider_health", return_value={"ai": {"last_outcome": "x", "last_status_code": 1}}):
        resp = app_module.app.test_client().post("/api/items/7/ai-suggest", headers=_CSRF)
    return {"status": resp.status_code, "body": resp.get_json(), "ai_request": sent,
            "mb_search_calls": [list(c.args) + [c.kwargs] for c in mb_search.call_args_list],
            "details_calls": [list(c.args) for c in details_fn.call_args_list]}


class ItemAiSuggestGoldenTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.actual = {name: json.loads(json.dumps(_run(spec))) for name, spec in _SCENARIOS.items()}
        if os.environ.get("UPDATE_ITEM_AI_SUGGEST_GOLDEN"):
            _GOLDEN.write_text(json.dumps(cls.actual, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        cls.golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))

    def test_every_scenario_matches_golden(self):
        self.assertEqual(sorted(self.golden), sorted(_SCENARIOS))
        for name in _SCENARIOS:
            with self.subTest(name):
                self.assertEqual(self.actual[name], self.golden[name])

    def test_not_found_contract(self):
        with mock.patch.object(app_module, "_security_auth_disabled", return_value=True), \
                mock.patch.object(routes_library, "lib", mock.Mock(get_item=mock.Mock(return_value=None))):
            resp = app_module.app.test_client().post("/api/items/7/ai-suggest", headers=_CSRF)
        self.assertEqual(resp.get_json(), {"ok": False, "error": "Not found"})

    def test_confirmed_acoustid_hits_are_forwarded_to_every_enrichment(self):
        body = self.golden["acoustid_confirmed_hit_reaches_enrichment"]["body"]
        self.assertEqual(body["acoustid_status"], "")
        calls = body["mb_candidates"][0]["enrich_calls"]
        self.assertEqual([c["has_details"] for c in calls], [False])
        for call in calls:
            self.assertEqual([h["mb_trackid"] for h in call["acoustid_hits"]], [_REC])

    def test_failures_are_the_generic_error_not_a_500(self):
        for name in ("evidence_packet_failure_is_generic_error", "serialization_failure_is_generic_error"):
            with self.subTest(name):
                self.assertEqual(self.golden[name]["status"], 200)
                self.assertEqual(self.golden[name]["body"], {"ok": False, "error": "Could not generate suggestions."})

    def test_evidence_is_gathered_first_with_or_without_ai(self):
        for key, expected in (("", ["acoustid", "musicbrainz", "discogs"]),
                              ("test-placeholder", ["acoustid", "musicbrainz", "discogs", "ai"])):
            with self.subTest(key=key):
                events = []
                _run({"key": key, "mb": []}, events=events)
                # MusicBrainz is searched twice when an artist search finds nothing.
                self.assertEqual(list(dict.fromkeys(events)), expected)

    def test_unusable_ai_endpoint_falls_back_to_the_evidence(self):
        """Declared difference from the pre-extraction handler, which returned 500 here."""
        out = _run({"mb": [_mb_cand()], "details": _details(), "endpoint": "not-a-url"})
        self.assertEqual(out["status"], 200)
        self.assertEqual(out["ai_request"], [])
        s = out["body"]["suggestions"]
        self.assertEqual(s["mb_trackid"], _REC)
        self.assertEqual(s["confidence_source"], "musicbrainz")
        self.assertFalse(s["ai_status"]["contributed"])
        self.assertIn("AI unavailable: the AI provider request failed unexpectedly (ValueError)", s["reason"])

    def test_ai_cannot_supply_the_release_group_of_a_trusted_recording(self):
        s = _run({"mb": [_mb_cand()], "details": _details(),
                  "ai": {"mb_trackid": _REC, "mb_releasegroupid": _INVENTED, "confidence": "high", "reason": "r"}}
                 )["body"]["suggestions"]
        self.assertTrue(s["mb_trusted"])
        self.assertEqual(s["mb_releasegroupid"], _RG)
        # Without MusicBrainz details nothing verified names a release group, so there is none.
        s = _run({"mb": [_mb_cand()], "details": {},
                  "ai": {"mb_trackid": _REC, "mb_releasegroupid": _INVENTED, "confidence": "high", "reason": "r"}}
                 )["body"]["suggestions"]
        self.assertNotIn("mb_releasegroupid", s)

    def test_ai_release_and_artist_ids_are_never_used(self):
        ai = {"mb_trackid": _REC, "mb_albumid": _INVENTED, "mb_artistid": _INVENTED, "confidence": "high", "reason": "r"}
        s = _run({"mb": [_mb_cand()], "details": {}, "ai": ai})["body"]["suggestions"]
        self.assertTrue(s["mb_trusted"])
        self.assertNotIn("mb_albumid", s)
        self.assertNotIn("mb_artistid", s)
        self.assertNotIn("mb_album_url", s)
        s = _run({"mb": [], "ai": {**ai, "mb_trackid": _INVENTED}})["body"]["suggestions"]
        self.assertNotIn("mb_albumid", s)
        self.assertNotIn("mb_artistid", s)
        out = _run({"mb": [_mb_cand()], "details": _details(), "ai": ai})
        self.assertEqual(out["details_calls"], [[_REC, _REL]])
        self.assertEqual(out["body"]["suggestions"]["mb_albumid"], _REL)
        self.assertEqual(out["body"]["suggestions"]["mb_artistid"], _ARTIST)

    def test_shown_decision_is_the_deterministic_one_with_real_enrichment(self):
        """Same decision and version with or without AI, equal to one enrichment pass (what attach rebuilds)."""
        other = "66666666-6666-6666-6666-666666666666"
        details = _details()
        fields = ("decision", "decision_version", "safety_result", "requires_confirmation", "recommended_action")

        def enrich(current, candidate, det=None, **kw):
            return ai_evidence_service._enrich_track_ai_candidate(current, candidate, det, **kw)

        with mock.patch.object(sys.modules[__name__], "_fake_enrich", enrich), \
                mock.patch.object(ai_evidence_service, "_fetch_mb_recording_details", return_value=details):
            base = _run({"key": "", "mb": [_mb_cand()], "details": details})["body"]
            with_ai = _run({"mb": [_mb_cand()], "details": details,
                            "ai": {"mb_trackid": _REC, "mb_albumid": other, "confidence": "high", "reason": "r"}})["body"]
            with mock.patch.object(library_service, "_item_ai_abs_path", return_value="/music/a/03 - Some Song.flac"),                     mock.patch.object(library_service, "_acoustid_lookup_cached_outcome",
                                      return_value=pb.ProviderResult("acoustid", pb.ProviderOutcome.NOT_CONFIGURED)),                     mock.patch.object(library_service, "_mb_recording_search", return_value=[_mb_cand()]):
                enforced = library_service._reconstruct_track_recording_candidates(_item(), 7)[1][0]
        for field in fields:
            self.assertEqual(with_ai["mb_candidates"][0][field], base["mb_candidates"][0][field], field)
        self.assertEqual(base["mb_candidates"][0]["decision_version"], enforced["decision_version"])
        self.assertTrue(with_ai["mb_candidates"][0]["decision_version"])
        self.assertTrue(with_ai["mb_candidates"][0]["matching_contract"]["ai"]["available"])

    def test_failed_enrichment_needs_confirmation_and_is_not_trusted(self):
        def boom(*_a, **_k):
            raise RuntimeError("boom")
        with mock.patch.object(sys.modules[__name__], "_fake_enrich", boom):
            body = _run({"mb": [_mb_cand()], "details": _details(),
                         "ai": {"mb_trackid": _REC, "confidence": "high", "reason": "r"}})["body"]
        s = body["suggestions"]
        self.assertTrue(body["ok"])
        self.assertTrue(s["requires_confirmation"])
        self.assertFalse(s["mb_trusted"])
        self.assertIn("mb_apply_warning", s)
        self.assertTrue(ai_service._compact_track_ai_candidate(_mb_cand())["requires_confirmation"])

    def test_evidence_packet_trusts_for_apply_only_an_evaluated_candidate(self):
        def spec():  # enrichment changes the candidate in place, so each run needs its own
            return {"mb": [_mb_cand()], "details": _details(), "real_packet": True,
                    "ai": {"mb_trackid": _REC, "confidence": "high", "reason": "r"}}
        body = _run(spec())["body"]
        self.assertTrue(body["evidence"]["validation"]["trusted_for_apply"])

        def boom(*_a, **_k):
            raise RuntimeError("boom")
        with mock.patch.object(sys.modules[__name__], "_fake_enrich", boom):
            body = _run(spec())["body"]
        self.assertTrue(body["evidence"]["mb_candidate_valid"])
        self.assertFalse(body["evidence"]["validation"]["trusted_for_apply"])

    def test_binder_ignores_forged_fields_and_bad_indexes(self):
        cands = [{"mb_albumid": _REL}, {"mb_albumid": _REC}]
        forged = {"candidate_index": 0, "mb_artistid": _INVENTED, "requires_confirmation": False,
                  "safety_result": "safe", "selected_release": {"x": 1}}
        with mock.patch.object(ai_service, "_fetch_mb_release_tracklist", return_value={}):
            self.assertEqual(ai_service._ai_bind_selected_release(forged, cands)[1], _REL)
            for field in ("mb_artistid", "requires_confirmation", "safety_result", "selected_release"):
                self.assertNotIn(field, forged)
            for bad in ("x", 1.9, True, None, [0], "0"):
                self.assertEqual(ai_service._ai_bind_selected_release({"candidate_index": bad}, cands), (None, ""), bad)
        for reply in ([], "x", 5, None):
            self.assertEqual(ai_service._ai_bind_selected_release(reply, cands), (None, ""))
        for raw in ("[]", '"x"', "5", "null"):
            with self.assertRaises(ValueError):
                ai_service._ai_reply_object(raw)

    def test_ai_cannot_supply_safety_or_evidence_fields(self):
        forged = {"selected_recording_candidate": {"mb_trackid": _INVENTED}, "candidate_evidence": {"x": 1},
                  "review_evidence": {"x": 1}, "requires_confirmation": False, "safety_result": "safe",
                  "recommended_action": "attach", "mb_trusted": True, "mb_candidate_valid": True}
        s = _run({"mb": [], "ai": {"mb_trackid": _INVENTED, "confidence": "high", "reason": "r", **forged}}
                 )["body"]["suggestions"]
        for field in ("selected_recording_candidate", "candidate_evidence", "review_evidence",
                      "requires_confirmation", "safety_result", "recommended_action"):
            self.assertNotIn(field, s)
        self.assertFalse(s["mb_trusted"])
        self.assertFalse(s["mb_candidate_valid"])
        self.assertEqual(s["confidence"], "low")
        self.assertIn("mb_apply_warning", s)
        # With a selected candidate those fields are the candidate's, not the AI's.
        s = _run({"mb": [_mb_cand()], "details": _details(),
                  "ai": {"mb_trackid": _REC, "confidence": "high", "reason": "r", **forged}})["body"]["suggestions"]
        self.assertEqual(s["selected_recording_candidate"]["mb_trackid"], _REC)
        self.assertTrue(s["requires_confirmation"])
        self.assertEqual(s["safety_result"], "needs_review")

    def test_schema_invalid_ai_reply_falls_back_to_the_evidence(self):
        good = {"mb_trackid": _REC, "confidence": "high", "reason": "r"}
        replies = ["[]", '"x"', "5", "null", "{}", {"foo": 1},
                   {"mb_releasegroupid": _INVENTED, "safety_result": "safe"},
                   {"mb_albumid": _INVENTED}, {"mb_artistid": _INVENTED},
                   {"mb_albumid": _INVENTED, "mb_artistid": _INVENTED},
                   '{"title": NaN, "mb_trackid": "%s", "reason": "r"}' % _REC,
                   '{"year": 1e999, "mb_trackid": "%s"}' % _REC,
                   {**good, "confidence": "certain"}, {**good, "confidence": 5}, {**good, "confidence": ["high"]},
                   {**good, "title": "x" * 1001}, {**good, "year": 10 ** 12}, {**good, "track": True}]
        for field in ("mb_trackid", "mb_albumid", "mb_artistid"):
            replies += [{**good, field: bad} for bad in (5, True, ["x"], {"a": 1}, 1.5)]
        for field in ("title", "artist", "album", "reason", "label", "genre", "year", "track"):
            replies += [{**good, field: bad} for bad in (["x"], {"a": 1}, 1.5)]
        for reply in replies:
            with self.subTest(reply=reply):
                resp = _run({"mb": [_mb_cand()], "details": _details(), "ai": reply})
                self.assertEqual(resp["status"], 200)
                self.assertTrue(resp["body"]["ok"])
                self.assertNotIn("NaN", json.dumps(resp["body"], allow_nan=True))
                s = resp["body"]["suggestions"]
                self.assertEqual(s["mb_trackid"], _REC)
                self.assertEqual(s["confidence_source"], "musicbrainz")
                self.assertFalse(s["ai_status"]["contributed"])
                self.assertIn("(ValueError)", s["ai_unavailable_reason"])

    def test_confidence_variants_and_out_of_candidate_ids(self):
        for spelled in ("HIGH", "high ", "High"):
            s = _run({"mb": [_mb_cand()], "details": _details(),
                      "ai": {"mb_trackid": _REC, "confidence": spelled, "reason": "r"}})["body"]["suggestions"]
            self.assertEqual(s["confidence"], "high")
        for mb in ([_mb_cand()], []):
            for conf in ("high", "HIGH", "high ", "medium", "low"):
                s = _run({"mb": mb, "ai": {"mb_trackid": _INVENTED, "confidence": conf, "reason": "r"}}
                         )["body"]["suggestions"]
                self.assertEqual(s["confidence"], "low", (bool(mb), conf))
                self.assertFalse(s["mb_trusted"])
                self.assertIn("mb_apply_warning", s)

    def test_identity_rules_hold_in_golden(self):
        """AI may rank, never establish identity: only a listed candidate is trusted."""
        s = self.golden["ai_invents_id_with_candidates_is_downgraded"]["body"]["suggestions"]
        self.assertFalse(s["mb_trusted"])
        self.assertFalse(s["mb_candidate_valid"])
        self.assertEqual(s["confidence"], "low")
        s = self.golden["ai_invents_id_without_candidates"]["body"]["suggestions"]
        self.assertFalse(s["mb_trusted"])
        self.assertIn("not in the AcoustID/MusicBrainz candidate list", s["mb_apply_warning"])
        s = self.golden["ai_returns_malformed_id"]["body"]["suggestions"]
        self.assertFalse(s["mb_trusted"])
        self.assertEqual(s["confidence"], "low")
        self.assertEqual(s["confidence"], "low")
        s = self.golden["ai_returns_malformed_id_without_candidates"]["body"]["suggestions"]
        self.assertFalse(s["mb_trusted"])
        self.assertEqual(s["confidence"], "low")
        s = self.golden["ai_selects_listed_candidate"]["body"]["suggestions"]
        self.assertTrue(s["mb_trusted"])
        self.assertEqual(s["mb_albumid"], _REL)
        self.assertEqual(s["mb_releasegroupid"], _RG)
        self.assertEqual(s["year"], "2012")
        self.assertEqual(s["mb_url"], f"https://musicbrainz.org/recording/{_REC}")
        self.assertEqual(s["mb_album_url"], f"https://musicbrainz.org/release/{_REL}")
        self.assertEqual(s["confidence_source"], "ai")
        s = self.golden["no_key_falls_back_to_top_candidate"]["body"]["suggestions"]
        self.assertEqual(s["confidence_source"], "musicbrainz")
        self.assertFalse(s["ai_status"]["contributed"])


class AlbumAndFolderAiFallbackTests(unittest.TestCase):
    """AI failures on these paths must leave the MusicBrainz/AcoustID match standing."""

    def _suggest(self, path, reply=None, env=None, artist="A"):
        def release(*_a, **_k):
            return [{"mb_albumid": _REL, "mb_releasegroupid": _RG, "artist": artist, "album": "B", "year": "2012",
                     "country": "US", "score": 90, "tracks": 1, "title": "B", "label": "", "format_summary": "",
                     "mb_url": ""}]
        album = types.SimpleNamespace(id=7, year=2012, album="B", albumartist="A", mb_releasegroupid="",
                                      mb_albumartistid="",
                                      items=lambda: [types.SimpleNamespace(track=1, title="t", path="")])
        folder_evidence = {"audio_files": [], "folder_track_count": 1, "nested_audio_count": 0, "guessed_artist": "A",
                           "guessed_artist_mbid": "", "guessed_album": "B", "guessed_year": "2012", "track_lines": [],
                           "filenames": []}
        source = mock.MagicMock()
        source.is_dir.return_value = True
        source.iterdir.return_value = []
        opened = mock.Mock(side_effect=lambda *_a, **_k: _ai_reply(reply))
        with mock.patch.dict(os.environ, env or {}), \
                mock.patch.multiple(
                    ai_service,
                    _ai_api_key=mock.Mock(return_value="test-placeholder"),
                    _album_preflight_folder=mock.Mock(return_value="/downloads/x"),
                    _mb_release_search=release,
                    _run_ai_release_preflight=mock.Mock(return_value={"ok": True, "matches": 1, "expected": 1}),
                    _build_folder_evidence=mock.Mock(return_value=folder_evidence),
                    _resolve_import_review_source_path=mock.Mock(return_value=(source, None)),
                    _import_review_folder_signature=mock.Mock(return_value=""),
                    _acoustid_multi_file=mock.Mock(return_value={}),
                    _acoustid_lookup_cached_outcome=mock.Mock(
                        return_value=pb.ProviderResult("acoustid", pb.ProviderOutcome.NO_RESULT, data=[])),
                    _mb_release_search_by_folder_tracks=mock.Mock(return_value=[]),
                    _discogs_release_fallback_candidate=mock.Mock(return_value={}),
                ), \
                mock.patch.object(recovery_carrier, "load_for_folder", return_value=None), \
                mock.patch.object(pb, "opened", opened):
            if callable(path):
                return path(), opened
            if path == "album":
                return ai_service._ai_suggest_album_internal(album, [], existing_album_id=7), opened
            return ai_service._ai_suggest_folder_internal("/downloads/x"), opened

    def _assert_evidence_fallback(self, out):
        self.assertTrue(out["ok"])
        s = out["suggestion"]
        self.assertFalse(s["ai_available"])
        self.assertIn("(ValueError)", s["ai_unavailable_reason"])
        self.assertIn("Matched using MusicBrainz and AcoustID", s["reason"])
        self.assertNotEqual(s["confidence"], "high")
        self.assertEqual(s["mb_albumid"], _REL)
        self.assertEqual(s["mb_releasegroupid"], _RG)

    def test_usable_ai_reply_is_used(self):
        for path in ("album", "folder"):
            with self.subTest(path):
                out, _ = self._suggest(path, {"candidate_index": 0, "albumartist": "A", "confidence": "high",
                                              "reason": "ai reason"})
                self.assertTrue(out["suggestion"]["ai_available"])
                self.assertEqual(out["suggestion"]["reason"], "ai reason")

    def test_unusable_ai_endpoint_falls_back_to_the_evidence(self):
        for path in ("album", "folder"):
            with self.subTest(path):
                out, opened = self._suggest(path, env={"AI_BASE_URL": "not-a-url"})
                self._assert_evidence_fallback(out)
                opened.assert_not_called()

    def test_non_string_albumartist_falls_back_to_the_evidence(self):
        for path in ("album", "folder"):
            for bad in (123, ["A"], {"a": 1}, True, 1.5):
                with self.subTest(path=path, albumartist=bad):
                    out, opened = self._suggest(path, {"candidate_index": 0, "albumartist": bad, "confidence": "high",
                                                       "reason": "ai reason"})
                    self._assert_evidence_fallback(out)
                    opened.assert_called_once()


class ImportAllDiscoveryIdentityGateTests(unittest.TestCase):
    """Import All must send a candidate that failed identity validation to Review, not to the reimport."""

    # One AI reply path; the rest reach the evidence fallback.
    PATHS = {
        "ai_reply": {"reply": {"candidate_index": 0, "albumartist": "A", "confidence": "high", "reason": "ai"}},
        "unusable_endpoint": {"env": {"AI_BASE_URL": "not-a-url"}},
        "non_object_reply": {"reply": "[1]"},
        "non_string_albumartist": {"reply": {"candidate_index": 0, "albumartist": 123, "confidence": "high"}},
    }

    def _import_all(self, artist, **suggest_kwargs):
        jobs = []

        def start_job(fn, **_kw):
            jobs.append(fn)
            return {"ok": True}, 200

        def post_and_run():
            client = app_module.app.test_client()
            client.post("/api/library/import-all", headers=_CSRF,
                        json={"albums": [{"aldir": "/downloads/x", "artist": "A"}]})
            log = []
            jobs[0](log)
            return log

        queue, reimport = mock.Mock(return_value=True), mock.Mock(return_value="child")
        with mock.patch.object(app_module, "_security_auth_disabled", return_value=True), \
                mock.patch.multiple(
                    routes_library,
                    _start_job=start_job,
                    _queue_folder_for_manual_review=queue,
                    _start_reimport_disk_job_internal=reimport,
                    _wait_for_child_job=mock.Mock(return_value={"status": "ok"}),
                    _invalidate_lib_cache=mock.Mock(),
                    _library_import_all_write_last=mock.Mock(),
                ), \
                mock.patch.object(ai_service, "_score_mb_release_candidate",
                                  return_value={"total": 0.9}):  # the fallback's score for medium confidence
            AlbumAndFolderAiFallbackTests._suggest(self, post_and_run, artist=artist, **suggest_kwargs)
        return queue, reimport

    def test_candidate_failing_identity_validation_goes_to_review(self):
        for name, kwargs in self.PATHS.items():
            with self.subTest(name):
                queue, reimport = self._import_all("Vitalic", **kwargs)
                reimport.assert_not_called()
                queue.assert_called_once()
                suggestion, reason = queue.call_args.args[1:3]
                self.assertIs(suggestion["identity_validated"], False)
                self.assertIn("artist mismatch", reason)

    def test_validated_candidate_still_proceeds(self):
        for name, kwargs in self.PATHS.items():
            with self.subTest(name):
                queue, reimport = self._import_all("A", **kwargs)
                queue.assert_not_called()
                reimport.assert_called_once()
                self.assertEqual(reimport.call_args.args[:2], ("/downloads/x", _REL))


if __name__ == "__main__":
    unittest.main()
