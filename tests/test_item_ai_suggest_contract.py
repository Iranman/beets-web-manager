"""Golden contract of POST /api/items/<iid>/ai-suggest.

Pins the whole JSON response and the AI request body for every branch of the
handler, so moving its orchestration cannot change what the review UI receives
or what the (untrusted) AI is asked. Regenerate only for an intended contract
change: UPDATE_ITEM_AI_SUGGEST_GOLDEN=1 python -m unittest tests.test_item_ai_suggest_contract
"""
import io
import json
import os
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import app as app_module
import routes_library
from backend import item_ai_suggest_service, provider_boundary as pb

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
    "serialization_failure_is_generic_error": {"key": "", "mb": [], "evidence_error": True},
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


def _run(spec):
    item = _item(**spec.get("item", {}))
    mb = spec.get("mb", [])
    mb_search = mock.Mock(side_effect=mb) if isinstance(mb, Exception) else mock.Mock(return_value=mb)
    details = spec.get("details", {})
    details_fn = (mock.Mock(side_effect=details) if isinstance(details, Exception)
                  else mock.Mock(return_value=details))
    ai = spec.get("ai", {"confidence": "low", "reason": "unused"})
    sent = []

    def opened(provider, req, **_kw):
        sent.append({"provider": provider, "url": req.full_url, "body": json.loads(req.data)})
        if isinstance(ai, Exception):
            raise ai
        return _ai_reply(ai)

    patches = dict(
        _ai_api_key=mock.Mock(return_value=spec.get("key", "test-placeholder")),
        _ai_model_and_endpoint=mock.Mock(return_value=("model", "https://ai.test/v1")),
        _item_ai_abs_path=mock.Mock(return_value="/music/a/03 - Some Song.flac"),
        _acoustid_lookup_cached_outcome=mock.Mock(
            return_value=pb.ProviderResult("acoustid", pb.ProviderOutcome.NOT_CONFIGURED)),
        _mb_recording_search=mb_search,
        _discogs_track_search=mock.Mock(return_value=[]),
        _fetch_mb_recording_details=details_fn,
        _enrich_track_ai_candidate=_fake_enrich,
        _track_ai_evidence_packet=(mock.Mock(side_effect=RuntimeError("boom"))
                                   if spec.get("evidence_error") else _fake_evidence),
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

    def test_identity_rules_hold_in_golden(self):
        """AI may rank, never establish identity: only a listed candidate is trusted."""
        s = self.golden["ai_invents_id_with_candidates_is_downgraded"]["body"]["suggestions"]
        self.assertFalse(s["mb_trusted"])
        self.assertFalse(s["mb_candidate_valid"])
        self.assertEqual(s["confidence"], "low")
        s = self.golden["ai_invents_id_without_candidates"]["body"]["suggestions"]
        self.assertFalse(s["mb_trusted"])
        s = self.golden["ai_returns_malformed_id"]["body"]["suggestions"]
        self.assertFalse(s["mb_trusted"])
        self.assertEqual(s["confidence"], "low")
        s = self.golden["ai_returns_malformed_id_without_candidates"]["body"]["suggestions"]
        self.assertFalse(s["mb_trusted"])
        self.assertEqual(s["confidence"], "medium")
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


if __name__ == "__main__":
    unittest.main()
