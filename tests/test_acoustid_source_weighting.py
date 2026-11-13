"""AcoustID source weighting and the compressed lookup request.

A recording linked by fewer than 25% of the best-sourced recording's AcoustID
submissions is weak: on its own it can neither confirm a recording nor
contradict one. Synthetic AcoustID payloads only; no provider is called and
no real key is used."""
import gzip
import io
import json
import sys
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import helpers_mb
from backend.matching import AcoustIDStatus, acoustid_evidence_from_hits, verify_audio_against_request
from backend.matching.folder_identity import confirmed_recording
from backend.matching.track_alignment import _acoustid_status_for_target
from backend.provider_boundary import ProviderOutcome

REC_A = "a0000000-0000-0000-0000-00000000000a"
REC_B = "b0000000-0000-0000-0000-00000000000b"
FAKE_KEY = "synthetic-test-key"


def _payload(*results):
    return {"status": "ok", "results": [
        {"id": f"aid-{i}", "score": score, "recordings": recs} for i, (score, recs) in enumerate(results)]}


def _rec(rid, title, sources=None):
    rec = {"id": rid, "title": title, "artists": [{"name": "Synthetic Artist"}]}
    if sources is not None:
        rec["sources"] = sources
    return rec


def _hit(rid, score=95, weak=False, title="Song"):
    return {"mb_trackid": rid, "score": score, "weak_sources": weak, "title": title, "artist": "Synthetic Artist"}


class ParseTests(unittest.TestCase):
    def test_sources_kept_and_weak_flagged(self):
        out = helpers_mb._acoustid_parse_candidates(
            _payload((0.97, [_rec(REC_A, "Song", 40), _rec(REC_B, "Other", 9)])))
        by_id = {row["mb_trackid"]: row for row in out}
        self.assertEqual((by_id[REC_A]["sources"], by_id[REC_A]["weak_sources"]), (40, False))
        self.assertEqual((by_id[REC_B]["sources"], by_id[REC_B]["weak_sources"]), (9, True))

    def test_exactly_a_quarter_is_not_weak(self):
        out = helpers_mb._acoustid_parse_candidates(
            _payload((0.97, [_rec(REC_A, "Song", 40), _rec(REC_B, "Other", 10)])))
        self.assertFalse(any(row["weak_sources"] for row in out))

    def test_strong_single_recording_unchanged(self):
        out = helpers_mb._acoustid_parse_candidates(_payload((0.97, [_rec(REC_A, "Song", 3)])))
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["weak_sources"])
        self.assertEqual(out[0]["score"], 97)

    def test_missing_sources_field(self):
        out = helpers_mb._acoustid_parse_candidates(_payload((0.97, [_rec(REC_A, "Song"), _rec(REC_B, "Other")])))
        self.assertEqual([(r["sources"], r["weak_sources"]) for r in out], [(None, False), (None, False)])
        # An unreadable count is unknown: never weak, and never sets the bar.
        out = helpers_mb._acoustid_parse_candidates(
            _payload((0.97, [_rec(REC_A, "Song", "junk"), _rec(REC_B, "Other", 1)])))
        self.assertEqual([(r["sources"], r["weak_sources"]) for r in out], [(None, False), (1, False)])


    def test_non_finite_sources_is_unknown(self):
        for raw in ("Infinity", "1e400", "-Infinity", "NaN"):
            with self.subTest(raw=raw):
                data = json.loads('{"status": "ok", "results": [{"id": "aid", "score": 0.97, "recordings": ['
                                  f'{{"id": "{REC_A}", "sources": {raw}}}, {{"id": "{REC_B}", "sources": 2}}]}}]}}')
                out = helpers_mb._acoustid_parse_candidates(data)
                self.assertEqual([(r["sources"], r["weak_sources"]) for r in out], [(None, False), (2, False)])

    def test_non_finite_score_is_zero(self):
        for raw in ("Infinity", "1e400", "-Infinity", "NaN"):
            with self.subTest(raw=raw):
                data = json.loads('{"status": "ok", "results": [{"id": "aid", "score": ' + raw
                                  + f', "recordings": [{{"id": "{REC_A}", "sources": 3}}]}}]}}')
                out = helpers_mb._acoustid_parse_candidates(data)
                self.assertEqual([r["score"] for r in out], [0])


class CanonicalEvidenceTests(unittest.TestCase):
    def test_weak_target_alone_is_not_a_verified_mapping(self):
        hits = [_hit(REC_A, weak=True), _hit(REC_B, score=50)]
        self.assertEqual(acoustid_evidence_from_hits(hits, REC_A).status, AcoustIDStatus.AMBIGUOUS)
        self.assertEqual(confirmed_recording(hits), "")

    def test_weak_other_recording_cannot_create_a_conflict(self):
        hits = [_hit(REC_B, weak=True), _hit(REC_A, score=50)]
        self.assertEqual(acoustid_evidence_from_hits(hits, REC_A).status, AcoustIDStatus.AMBIGUOUS)
        status, _positives, conflicts = _acoustid_status_for_target({"acoustid_hits": hits}, REC_A)
        self.assertEqual((status, conflicts), (AcoustIDStatus.AMBIGUOUS, []))

    def test_weak_neighbour_blocks_confirmation_without_a_conflict(self):
        status, positives, conflicts = _acoustid_status_for_target(
            {"acoustid_hits": [_hit(REC_A), _hit(REC_B, weak=True)]}, REC_A)
        self.assertEqual((status, positives, conflicts), (AcoustIDStatus.AMBIGUOUS, [], []))
        self.assertEqual(confirmed_recording([_hit(REC_A), _hit(REC_B, weak=True)]), "")

    def test_strong_recording_unchanged(self):
        hits = [_hit(REC_A), _hit(REC_B, score=60, weak=True)]
        self.assertEqual(acoustid_evidence_from_hits(hits, REC_A).status, AcoustIDStatus.CONFIRMED)
        self.assertEqual(confirmed_recording(hits), REC_A)
        self.assertEqual(acoustid_evidence_from_hits([_hit(REC_A)], REC_A).status, AcoustIDStatus.CONFIRMED)
        self.assertEqual(acoustid_evidence_from_hits([_hit(REC_B)], REC_A).status, AcoustIDStatus.CONFLICT)

    def test_request_check_weak_link_neither_accepts_nor_rejects(self):
        accept = verify_audio_against_request([_hit(REC_A, weak=True)], expected_recording_id=REC_A,
                                              expected_title="Song")
        self.assertEqual(accept["decision"], "review")
        reject = verify_audio_against_request([_hit(REC_B, weak=True, title="Different")],
                                              expected_recording_id=REC_A, expected_title="Song")
        self.assertEqual((reject["decision"], reject["conflicts"]), ("review", []))
        strong = verify_audio_against_request([_hit(REC_B, title="Different")], expected_recording_id=REC_A,
                                              expected_title="Song")
        self.assertEqual(strong["decision"], "reject")


class LookupRequestTests(unittest.TestCase):
    def _lookup(self, opened):
        fpcalc = mock.Mock(returncode=0, stdout=json.dumps({"duration": 200, "fingerprint": "AQADtest"}), stderr="")
        with mock.patch.object(helpers_mb, "acoustid_api_key", return_value=FAKE_KEY), \
             mock.patch.object(helpers_mb.shutil, "which", return_value=sys.executable), \
             mock.patch.object(helpers_mb.subprocess, "run", return_value=fpcalc), \
             mock.patch.object(helpers_mb, "_ACOUSTID_MIN_INTERVAL_SECONDS", 0), \
             mock.patch("backend.provider_boundary.time.sleep"), \
             mock.patch.object(helpers_mb.provider_boundary, "opened", opened):
            return helpers_mb.acoustid_lookup_outcome("/music/x.flac")

    def test_compressed_post_through_provider_boundary(self):
        body = json.dumps(_payload((0.97, [_rec(REC_A, "Song", 12)]))).encode()
        opened = mock.MagicMock()
        opened.return_value.__enter__.return_value = io.BytesIO(body)
        result = self._lookup(opened)
        self.assertEqual(result.outcome, ProviderOutcome.CONFIRMED)
        self.assertEqual(result.data[0]["sources"], 12)
        provider, req = opened.call_args.args[:2]
        self.assertEqual(provider, "acoustid")
        self.assertEqual((req.get_method(), req.full_url), ("POST", "https://api.acoustid.org/v2/lookup"))
        self.assertEqual(req.get_header("Content-encoding"), "gzip")
        form = urllib.parse.parse_qs(gzip.decompress(req.data).decode())
        self.assertEqual(form["meta"], ["recordings releases releasegroups sources"])
        self.assertEqual((form["fingerprint"], form["duration"]), (["AQADtest"], ["200"]))

    def test_error_strings_are_fixed(self):
        def rejected(*_a, **_k):
            raise urllib.error.HTTPError("https://api.acoustid.org/v2/lookup", 400, "Bad Request", {},
                                         io.BytesIO(b'{"status": "error", "error": {"code": 4, "message": "x"}}'))
        result = self._lookup(mock.Mock(side_effect=rejected))
        self.assertEqual((result.outcome, result.message),
                         (ProviderOutcome.AUTHENTICATION_ERROR, "AcoustID rejected the API key"))

        def outage(*_a, **_k):
            raise urllib.error.HTTPError("https://api.acoustid.org/v2/lookup", 503, "Unavailable", {},
                                         io.BytesIO(b""))
        result = self._lookup(mock.Mock(side_effect=outage))
        self.assertEqual(result.message, "AcoustID HTTP 503")
        self.assertNotIn(FAKE_KEY, result.message)


class WeakHitNeverClearsAStrongConflictTests(unittest.TestCase):
    """QA #702: a file embedded with its own Recording ID, a strong
    fingerprint naming a different recording, and a weak hit for its own
    recording must stay a hard conflict (it used to become auto-acceptable)."""

    def test_embedded_id_with_strong_other_and_weak_own_stays_blocked(self):
        from backend.matching import DEFAULT_MATCH_POLICY
        from tests.test_mtr_deterministic_slot import SUBSET_POLICY, _evaluate, _file
        strong_other = {"recording_id": "rec-112-3", "score": 0.97, "weak_sources": False}
        for own_score in (0.96, 0.85):
            with self.subTest(own_score=own_score):
                weak_own = {"recording_id": "rec-112-2", "score": own_score, "weak_sources": True}
                result = _evaluate([_file("Only You", 2, 255, acoustid_hits=[strong_other, weak_own],
                                          recording_id="rec-112-2")])
                self.assertIn("acoustid_conflict", result.conflicts)
                self.assertFalse(result.can_auto_accept(SUBSET_POLICY))
                self.assertFalse(result.can_auto_accept(DEFAULT_MATCH_POLICY))

    def test_strong_conflict_kept_by_every_weak_aware_check(self):
        hits = [_hit(REC_B, score=97), _hit(REC_A, score=96, weak=True)]
        status, _positives, conflicts = _acoustid_status_for_target({"acoustid_hits": hits}, REC_A)
        self.assertEqual((status, conflicts), (AcoustIDStatus.CONFLICT, ["acoustid_conflict"]))
        self.assertEqual(acoustid_evidence_from_hits(hits, REC_A).status, AcoustIDStatus.CONFLICT)
        verdict = verify_audio_against_request(
            [_hit(REC_B, score=97, title="Different"), _hit(REC_A, score=96, weak=True)],
            expected_recording_id=REC_A, expected_title="Song")
        self.assertEqual(verdict["decision"], "reject")

    def test_strong_ambiguity_conflict_kept(self):
        hits = [_hit(REC_A, score=97), _hit(REC_B, score=96), _hit("c0000000-0000-0000-0000-00000000000c",
                                                                    score=99, weak=True)]
        status, _positives, conflicts = _acoustid_status_for_target({"acoustid_hits": hits}, REC_A)
        self.assertEqual((status, conflicts), (AcoustIDStatus.AMBIGUOUS, ["acoustid_ambiguous"]))


if __name__ == "__main__":
    unittest.main()
