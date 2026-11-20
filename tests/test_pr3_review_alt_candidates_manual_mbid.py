"""PR3 / Lane C regression tests.

Covers the three backend contracts this lane owns:

* ``serialize_review_alt_candidates`` -- the clean ranked alt-candidate + per-track
  projection of the already-stored deterministic evidence (no re-ranking, no MB).
* ``_folder_import_review_payload`` -- a provided ID that could not be imported is
  stamped with the authoritative slice-1 refusal verdict, so ``mb_valid`` (which
  stays syntactic-only) can no longer masquerade as "ready".
* ``resolve_release_group_to_compatible_release`` -- the shared resolver exposed
  for PR5: it preserves Release-Group identity (never reassigns to another group
  on partial/superset overlap) and fails closed to Review.

All synthetic; every MusicBrainz / preflight call is mocked at the boundary.
"""
import types
import unittest
from unittest import mock

import backend.import_service as isvc
import backend.library_service as lsvc
from backend.import_review_decision import item_match_bucket
from backend.import_review_service import serialize_review_alt_candidates

RG1 = "11111111-1111-1111-1111-111111111111"
REL1 = "22222222-2222-2222-2222-222222222222"
RG2 = "33333333-3333-3333-3333-333333333333"
REL2 = "44444444-4444-4444-4444-444444444444"
REC1 = "55555555-5555-5555-5555-555555555555"
REC2 = "66666666-6666-6666-6666-666666666666"
SAME_RG_REL = "77777777-7777-7777-7777-777777777777"


def _candidate(rgid, rel, rank, score, *, action_allowed, verified, total,
               review_reasons=(), with_alignment=True):
    canonical = {"score": score}
    if with_alignment:
        canonical["track_alignment"] = {
            "assignments": [{
                "target_index": 0, "local_title": "Local A", "target_title": "Track A",
                "target_recording_id": REC1, "status": "matched",
                "acoustid_status": "confirmed", "is_deterministic": True,
                "conflicts": [], "warnings": [],
            }],
            "unmatched_local": [{"local_index": 1, "local_title": "Stray", "reason": "no release track matched"}],
            "missing_tracks": [{"track": 2, "title": "Track B", "mb_trackid": REC2}],
        }
    return {
        "release_group_id": rgid,
        "representative_release_id": rel,
        "deterministic_rank": rank,
        "ai_rank": None,
        "matching_decision": {"evidence": {"canonical_match": canonical}},
        "evidence": {
            "action_allowed": action_allowed,
            "review_reasons": list(review_reasons),
            "conflicts": [],
            "local_tracks_verified": verified,
            "local_tracks_total": total,
            "sources": ["acoustid"],
            "edition_match": [],
        },
    }


class AltCandidateSerializerTests(unittest.TestCase):
    def _evidence(self):
        return {"deterministic_decision": {
            "auto_import": {"eligible": False, "reasons": ["no_release_group_verified"]},
            "candidates": [
                _candidate(RG1, REL1, 1, 0.91, action_allowed=True, verified=1, total=2),
                _candidate(RG2, REL2, 2, 0.40, action_allowed=False, verified=0, total=2,
                           review_reasons=["close_runner_up"], with_alignment=False),
            ],
        }}

    def test_ranked_candidates_and_best_verified(self):
        out = serialize_review_alt_candidates(self._evidence())
        self.assertEqual([c["release_group_id"] for c in out["candidates"]], [RG1, RG2])
        self.assertEqual([c["deterministic_rank"] for c in out["candidates"]], [1, 2])
        self.assertEqual(out["best_verified_release_group_id"], RG1)
        self.assertTrue(out["candidates"][0]["is_best_verified"])
        self.assertFalse(out["candidates"][1]["is_best_verified"])

    def test_scores_coverage_urls_and_reasons(self):
        top = serialize_review_alt_candidates(self._evidence())["candidates"][0]
        self.assertEqual(top["score"], 0.91)
        self.assertTrue(top["action_allowed"])
        self.assertEqual(top["coverage"], {"local_tracks_verified": 1, "local_tracks_total": 2})
        self.assertEqual(top["mb_release_url"], f"https://musicbrainz.org/release/{REL1}")
        self.assertEqual(top["mb_release_group_url"], f"https://musicbrainz.org/release-group/{RG1}")
        runner = serialize_review_alt_candidates(self._evidence())["candidates"][1]
        self.assertEqual(runner["review_reasons"], ["close_runner_up"])

    def test_verdict_is_surfaced_as_the_why(self):
        out = serialize_review_alt_candidates(self._evidence())
        self.assertEqual(out["auto_import"], {"eligible": False, "reasons": ["no_release_group_verified"]})

    def test_per_track_from_stored_alignment(self):
        out = serialize_review_alt_candidates(self._evidence(), include_per_track=True)
        rows = out["candidates"][0]["per_track"]
        by_status = {r["status"] for r in rows}
        self.assertEqual(by_status, {"matched", "unmatched_local", "missing"})
        matched = next(r for r in rows if r["status"] == "matched")
        self.assertEqual(matched["position"], 1)
        self.assertEqual(matched["mb_title"], "Track A")
        self.assertEqual(matched["recording_id"], REC1)
        self.assertEqual(matched["acoustid_status"], "confirmed")
        self.assertTrue(matched["is_deterministic"])
        missing = next(r for r in rows if r["status"] == "missing")
        self.assertEqual(missing["mb_title"], "Track B")
        # Compact row omitted unless explicitly requested.
        self.assertNotIn("per_track", serialize_review_alt_candidates(self._evidence())["candidates"][0])

    def test_absent_or_malformed_evidence_is_empty(self):
        for ev in (None, {}, {"deterministic_decision": {}}, {"deterministic_decision": {"candidates": "x"}}):
            out = serialize_review_alt_candidates(ev)
            self.assertEqual(out["candidates"], [])
            self.assertEqual(out["best_verified_release_group_id"], "")
            self.assertIsNone(out["auto_import"]["eligible"])

    def test_nonnumeric_fields_never_raise_and_do_not_drop_later_candidates(self):
        # QA regression (F3): the serializer runs inside the review-queue loop
        # whose only error handling is a single outer try/except; a raise here
        # would silently drop this item AND every subsequent pending row. A
        # malformed numeric field must degrade, not raise, and must not hide a
        # following well-formed candidate.
        bad = _candidate(RG1, REL1, "notanint", 0.9, action_allowed=False,
                         verified="x", total="y", with_alignment=True)
        bad["matching_decision"]["evidence"]["canonical_match"]["track_alignment"]["assignments"][0]["target_index"] = "z"
        good = _candidate(RG2, REL2, 2, 0.5, action_allowed=True, verified=2, total=2,
                          with_alignment=False)
        ev = {"deterministic_decision": {"auto_import": {"eligible": False, "reasons": []},
                                         "candidates": [bad, good]}}
        out = serialize_review_alt_candidates(ev, include_per_track=True)
        self.assertEqual([c["release_group_id"] for c in out["candidates"]], [RG1, RG2])
        self.assertEqual(out["candidates"][0]["deterministic_rank"], 0)
        self.assertEqual(out["candidates"][0]["coverage"],
                         {"local_tracks_verified": 0, "local_tracks_total": 0})
        # the later well-formed candidate survives and is the verified best
        self.assertTrue(out["candidates"][1]["is_best_verified"])
        self.assertEqual(out["best_verified_release_group_id"], RG2)

    def test_malformed_container_and_nonfinite_fields_never_raise(self):
        # QA re-review of F3: the F3 fix only guarded numeric *strings*. These
        # three corrupted-evidence shapes still reached unguarded `.get`/`int`/
        # comprehension sites and raised (AttributeError / OverflowError /
        # TypeError), which the queue's single outer try/except turns into a
        # silent drop of this row AND every later pending row. Each must degrade.
        shapes = {
            "matching_decision_is_list": {
                "release_group_id": RG1, "deterministic_rank": 1,
                "matching_decision": ["corrupt"],  # truthy non-dict -> .get raises
                "evidence": {"action_allowed": False},
            },
            "rank_is_infinity": {
                "release_group_id": RG1, "deterministic_rank": float("inf"),  # int(inf) OverflowError
                "evidence": {"action_allowed": False},
            },
            "review_reasons_not_a_list": {
                "release_group_id": RG1, "deterministic_rank": 1,
                "evidence": {"action_allowed": False, "review_reasons": 5},  # non-iterable
            },
            "track_alignment_is_list": {
                "release_group_id": RG1, "deterministic_rank": 1,
                "matching_decision": {"evidence": {"canonical_match": {"track_alignment": ["x"]}}},
                "evidence": {"action_allowed": False},
            },
        }
        good = _candidate(RG2, REL2, 2, 0.5, action_allowed=True, verified=2, total=2,
                          with_alignment=False)
        for name, bad in shapes.items():
            ev = {"deterministic_decision": {"auto_import": {"eligible": False, "reasons": []},
                                             "candidates": [bad, good]}}
            out = serialize_review_alt_candidates(ev, include_per_track=True)
            # never raises; the malformed row is kept (degraded) and the later
            # well-formed candidate is NOT dropped and is still the verified best.
            self.assertEqual([c["release_group_id"] for c in out["candidates"]], [RG1, RG2], name)
            self.assertEqual(out["candidates"][0]["deterministic_rank"], 0 if name == "rank_is_infinity" else 1, name)
            self.assertEqual(out["candidates"][0]["score"], 0.0, name)
            self.assertEqual(out["candidates"][0]["review_reasons"], [], name)
            self.assertTrue(out["candidates"][1]["is_best_verified"], name)
            self.assertEqual(out["best_verified_release_group_id"], RG2, name)


class ReviewPayloadRefusalVerdictTests(unittest.TestCase):
    """A provided ID queued to Review because it could not import must read as
    refused, not "ready", even though its MBID is a well-formed UUID."""

    def _payload(self, evidence_packet):
        ctx = types.SimpleNamespace(
            log=[], folder_path="/downloads/Artist/Album (2001)",
            ai_suggestion={"albumartist": "Artist", "album": "Album"}, wanted_tracks=[])
        with mock.patch.object(isvc, "_folder_release_preflight", return_value={"ok": False}), \
                mock.patch.object(isvc, "_build_folder_evidence", return_value={}), \
                mock.patch.object(isvc, "_ai_match_evidence_packet", return_value=evidence_packet):
            return isvc._folder_import_review_payload(ctx, REL1, RG1, 0, "Could not import this release.")

    def test_refusal_verdict_is_stamped_and_mb_valid_stays_syntactic(self):
        sug, evidence = self._payload({})
        self.assertTrue(sug["mb_valid"])  # REL1 is a well-formed UUID
        self.assertEqual(evidence["deterministic_decision"]["auto_import"]["eligible"], False)
        # The slice-1 verdict-driven bucket now demotes it out of "ready".
        item = {"type": "pending_ai", "mb_albumid": REL1, "mb_valid": sug["mb_valid"], "evidence": evidence}
        self.assertEqual(item_match_bucket(item), "no_candidate")

    def test_existing_deterministic_decision_is_not_clobbered(self):
        existing = {"deterministic_decision": {"auto_import": {"eligible": True, "reasons": []}}}
        _sug, evidence = self._payload(existing)
        self.assertIs(evidence["deterministic_decision"], existing["deterministic_decision"])


class SharedResolveHelperTests(unittest.TestCase):
    """resolve_release_group_to_compatible_release: PR5's shared entrypoint."""

    FOLDER = "/downloads/Artist/Album"

    def test_delegates_to_resolve_album_release_for_import(self):
        with mock.patch.object(lsvc, "_resolve_album_release_for_import", return_value=REL1) as inner:
            got = lsvc.resolve_release_group_to_compatible_release(
                f"https://musicbrainz.org/release-group/{RG1}", "Artist", "Album", "", 2, [],
                source_folder=self.FOLDER, existing_album_id=0)
        self.assertEqual(got, REL1)
        self.assertEqual(inner.call_count, 1)
        self.assertEqual(inner.call_args.args[0], f"https://musicbrainz.org/release-group/{RG1}")
        self.assertEqual(inner.call_args.kwargs["source_folder"], self.FOLDER)

    def _resolve(self, *, passing):
        free = mock.MagicMock(return_value=[{"mb_albumid": REL2, "tracks": 2, "artist": "Artist", "album": "Album"}])

        def preflight(folder, rid, **k):
            ok = rid in passing
            return {"ok": ok, "matches": 2 if ok else 0, "expected": 2, "audio_count": 2,
                    "release_group": RG2 if rid == REL2 else RG1}

        log = []
        with mock.patch.object(lsvc, "_resolve_release_group_to_release", return_value=REL1), \
                mock.patch.object(lsvc, "_folder_release_preflight", side_effect=preflight), \
                mock.patch.object(lsvc, "_preflight_oversized_subset_complete", return_value=False), \
                mock.patch.object(lsvc, "_folder_import_track_count", return_value=2), \
                mock.patch.object(lsvc, "_mb_release_group_candidates",
                                  return_value=[{"mb_albumid": SAME_RG_REL, "track_count": 2}]), \
                mock.patch.object(lsvc, "_mb_release_search", free), \
                mock.patch.object(lsvc, "_mb_release_search_by_folder_tracks", free):
            got = lsvc.resolve_release_group_to_compatible_release(
                f"https://musicbrainz.org/release-group/{RG1}", "Artist", "Album", "", 2, log,
                source_folder=self.FOLDER, existing_album_id=0)
        return got, log, free

    def test_resolves_a_compatible_release_inside_the_group(self):
        got, _log, free = self._resolve(passing={REL1})
        self.assertEqual(got, REL1)
        free.assert_not_called()

    def test_no_release_in_group_fits_fails_closed_without_cross_rg_reassign(self):
        # Only another Release Group's release (REL2) would pass; the free search
        # that could surface it must never run, and nothing resolves -> Review.
        got, log, free = self._resolve(passing={REL2})
        self.assertEqual(got, "")
        free.assert_not_called()
        self.assertTrue(any("REFUSED" in line for line in log), log)


if __name__ == "__main__":
    unittest.main()
