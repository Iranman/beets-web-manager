"""Regression tests for the AcoustID evidence floors (MI-5, MI-6, MI-7, MI-10,
MI-11, MI-13, MI-16, MI-17).

Every identity decision here must use the canonical rule: a hit counts only
at or above ACOUSTID_MIN_SCORE (80), and a recording is CONFIRMED only when
no other recording sits within the 3-point ambiguity window. All provider
data is synthetic.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.acoustid_service as acs
import backend.import_service as imp
from backend.matching import AcoustIDStatus, align_tracks_global
from backend.matching.evidence import _release_id
from helpers_mb import _acoustid_parse_candidates

REC_A = "aaaaaaaa-0000-0000-0000-000000000001"
REC_B = "bbbbbbbb-0000-0000-0000-000000000002"
REC_C = "cccccccc-0000-0000-0000-000000000003"
RELEASE = "dddddddd-0000-0000-0000-000000000004"


def hit(rid, score, title="Song", artist="Artist", acoustid_id="aid-1", **extra):
    return {"mb_trackid": rid, "score": score, "title": title, "artist": artist,
            "acoustid_id": acoustid_id, **extra}


def lookup(mapping):
    return mock.patch.object(acs, "_acoustid_lookup_cached", side_effect=lambda p: list(mapping.get(p, [])))


class FingerprintMatchFloorTests(unittest.TestCase):
    """MI-5: same-recording proof for duplicate/replacement callers."""

    def test_weak_overlap_is_not_shared_and_not_a_verdict(self):
        with lookup({"src": [hit(REC_A, 40)], "lib": [hit(REC_A, 35)]}):
            self.assertEqual(acs._acoustid_fingerprint_match("src", "lib"), ("", [], []))

    def test_overlap_inside_an_ambiguous_tier_is_not_shared(self):
        with lookup({"src": [hit(REC_A, 95), hit(REC_B, 94, title="Other", acoustid_id="aid-2")],
                     "lib": [hit(REC_A, 96)]}):
            shared, src_ids, _ = acs._acoustid_fingerprint_match("src", "lib")
        self.assertEqual(shared, "")
        self.assertEqual(src_ids, [], "an ambiguous file proves nothing, not 'different recording'")

    def test_low_rank_overlap_is_not_shared(self):
        # REC_B overlaps but is not either file's confirmed recording.
        with lookup({"src": [hit(REC_A, 95), hit(REC_B, 60)], "lib": [hit(REC_C, 95), hit(REC_B, 60)]}):
            shared, src_ids, lib_ids = acs._acoustid_fingerprint_match("src", "lib")
        self.assertEqual((shared, src_ids, lib_ids), ("", [REC_A], [REC_C]))

    def test_both_files_confirming_one_recording_is_shared(self):
        with lookup({"src": [hit(REC_A, 95), hit(REC_B, 50)], "lib": [hit(REC_A, 90)]}):
            self.assertEqual(acs._acoustid_fingerprint_match("src", "lib")[0], REC_A)

    def test_cached_ids_use_the_same_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.flac"
            audio.write_bytes(b"x")
            cache_dir = Path(tmp) / "cache"
            _, key = acs._audio_cache_file_identity(str(audio))
            (cache_dir / key[:2]).mkdir(parents=True)
            (cache_dir / key[:2] / f"{key}.json").write_text(json.dumps([hit(REC_A, 40)]), encoding="utf-8")
            with mock.patch.object(acs, "_ACOUSTID_FILE_CACHE_DIR", cache_dir):
                self.assertEqual(acs._acoustid_cached_fingerprint_ids(str(audio)), [])


class AlbumTrackFingerprintCheckTests(unittest.TestCase):
    """MI-6."""

    TRACKS = [{"mb_trackid": REC_A, "title": "Crossfire", "title_norm": "crossfire"}]

    def _check(self, cands):
        from backend.provider_boundary import ProviderOutcome, ProviderResult
        answer = ProviderResult("acoustid", ProviderOutcome.CONFIRMED if cands else ProviderOutcome.NO_RESULT, data=cands)
        with tempfile.NamedTemporaryFile(suffix=".flac") as tf, \
                mock.patch.object(acs, "_acoustid_lookup_cached_outcome", return_value=answer):
            return acs._album_track_fingerprint_check({"path": tf.name}, self.TRACKS)["status"]

    def test_weak_member_hit_is_not_confirmed(self):
        self.assertEqual(self._check([hit(REC_A, 40, title="Crossfire")]), AcoustIDStatus.AMBIGUOUS.value)

    def test_tied_member_hit_is_not_confirmed(self):
        self.assertEqual(self._check([hit(REC_B, 95, title="Unrelated"), hit(REC_A, 94, title="Crossfire")]),
                         AcoustIDStatus.AMBIGUOUS.value)

    def test_conflict_needs_a_confirmed_recording(self):
        self.assertEqual(self._check([hit(REC_B, 75, title="Unrelated")]), AcoustIDStatus.AMBIGUOUS.value)
        self.assertEqual(self._check([hit(REC_B, 95, title="Unrelated")]), AcoustIDStatus.CONFLICT.value)

    def test_confirmed_member_is_confirmed(self):
        self.assertEqual(self._check([hit(REC_A, 95, title="Crossfire")]), AcoustIDStatus.CONFIRMED.value)


class WantedAlbumValidationTests(unittest.TestCase):
    """MI-7: release membership and weak hits are not confirmation."""

    def _validate(self, cands, *, outcome=None, mb=None, rows=None):
        from backend.provider_boundary import ProviderOutcome, ProviderResult
        mb = mb or {"ok": True, "tracks": [{"disc": 1, "track": 1, "mb_trackid": REC_A, "title": "Crossfire"}]}
        rows = rows if rows is not None else [{"id": 7, "title": "Crossfire", "path": "/music/a.flac", "disc": 1,
                                               "track": 1, "mb_trackid": REC_A, "length": 200}]
        outcome = outcome or (ProviderOutcome.CONFIRMED if cands else ProviderOutcome.NO_RESULT)
        answer = ProviderResult("acoustid", outcome, data=cands)
        with mock.patch.object(imp, "_fetch_mb_release_tracklist", return_value=mb), \
                mock.patch.object(imp.composite_workflows, "find_all_items_by_album_id", return_value=rows), \
                mock.patch.object(imp, "_album_item_abs_path", side_effect=lambda p: p), \
                mock.patch.object(imp, "_acoustid_lookup_cached_outcome", return_value=answer):
            return imp._validate_wanted_album_items_with_acoustid(
                1, RELEASE, [{"disc": 1, "track": 1, "mb_trackid": REC_A, "title": "Crossfire"}], [])

    def test_wrong_track_on_the_target_release_is_rejected(self):
        res = self._validate([hit(REC_B, 95, title="Space and Time", mb_albumids=[RELEASE])])
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["mismatches"]), 1)

    def test_weak_target_hit_is_unverified_and_goes_to_review(self):
        # #244 F1: unverified is not merged; it goes to review (never deleted).
        res = self._validate([hit(REC_A, 30, title="Crossfire")])
        self.assertFalse(res["ok"])
        self.assertEqual(res["confirmed"], 0)
        self.assertEqual(len(res["unverified"]), 1)
        self.assertEqual(res["mismatches"], [])
        self.assertIn("did not confirm", res["reason"])

    def test_confirmed_target_passes(self):
        res = self._validate([hit(REC_A, 95, title="Crossfire")])
        self.assertTrue(res["ok"])
        self.assertEqual(res["confirmed"], 1)
        self.assertEqual(res["reason"], "")

    def test_not_checked_is_not_no_result_and_blocks_the_merge(self):
        # #297 N2: no key / rejected key / outage is "not checked", fail closed.
        from backend.provider_boundary import ProviderOutcome
        for outcome, status in ((ProviderOutcome.NOT_CONFIGURED, "not_configured"),
                                (ProviderOutcome.AUTHENTICATION_ERROR, "auth_failed"),
                                (ProviderOutcome.UNAVAILABLE, "lookup_failed")):
            with self.subTest(status=status):
                res = self._validate([], outcome=outcome)
                self.assertFalse(res["ok"])
                self.assertEqual(res["no_result"], 0)
                self.assertEqual([r["status"] for r in res["not_checked"]], [status])
                self.assertIn("not checked", res["reason"])

    def test_no_acoustid_result_blocks_the_merge(self):
        res = self._validate([])
        self.assertFalse(res["ok"])
        self.assertEqual(res["no_result"], 1)

    def test_musicbrainz_failure_fails_closed(self):
        res = self._validate([hit(REC_A, 95, title="Crossfire")], mb={"ok": False, "error": "503"})
        self.assertFalse(res["ok"])
        self.assertIn("could not be loaded", res["reason"])

    def test_file_matching_no_requested_track_is_unverified(self):
        rows = [{"id": 8, "title": "Bonus Jam", "path": "/music/b.flac", "disc": 1, "track": 9,
                 "mb_trackid": "", "length": 30}]
        res = self._validate([hit(REC_A, 95, title="Crossfire")], rows=rows)
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["unverified"]), 1)

    def test_mixed_top_tier_is_review_not_mismatch(self):
        # #244 F2: expected recording absent, one tied hit is the same song and
        # one is a different song -> ambiguous, never a reject.
        res = self._validate([hit(REC_B, 95, title="Crossfire"),
                              hit(REC_C, 95, title="Space and Time", acoustid_id="aid-2")])
        self.assertFalse(res["ok"])
        self.assertEqual(res["mismatches"], [])
        self.assertEqual(len(res["unverified"]), 1)


class FreshImportEmbeddedConflictTests(unittest.TestCase):
    """MI-10: embedded Recording ID vs target/fingerprint on fresh imports."""

    def test_embedded_id_contradicting_fingerprint_confirmed_target_is_a_conflict(self):
        local = [{"title": "Crossfire", "track": 1, "recording_id": REC_B,
                  "acoustid_hits": [{"recording_id": REC_A, "score": 95}]}]
        target = [{"title": "Crossfire", "track": 1, "recording_id": REC_A}]
        res = align_tracks_global(local, target, trust_model="fresh_reviewed_import")
        self.assertIn("fingerprint_recording_id_conflict", res.conflicts)
        self.assertFalse([a for a in res.assignments if a.status == "matched"])

    def test_acoustid_conflict_is_recorded_once(self):
        # #244 F7.
        from backend.matching.track_alignment import _edge_for
        local = {"title": "Crossfire", "track": 1, "acoustid_hits": [{"recording_id": REC_B, "score": 95}]}
        target = {"title": "Crossfire", "track": 1, "recording_id": REC_A}
        edge = _edge_for(0, 0, local, target, trust_model="fresh_reviewed_import")
        self.assertEqual(edge.conflicts.count("acoustid_conflict"), 1)

    def test_embedded_id_contradicting_target_is_a_conflict(self):
        local = [{"title": "Crossfire", "track": 1, "recording_id": REC_B}]
        target = [{"title": "Crossfire", "track": 1, "recording_id": REC_A}]
        res = align_tracks_global(local, target, trust_model="fresh_reviewed_import")
        self.assertIn("recording_id_conflict", res.conflicts)


class ArtistFolderFingerprintTests(unittest.TestCase):
    """MI-11: artist-folder merge confirmation."""

    def _run(self, per_file):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i in range(len(per_file)):
                p = Path(tmp) / f"{i}.flac"
                p.write_bytes(b"x")
                paths.append(str(p))
            mapping = {path: per_file[i] for i, path in enumerate(paths)}
            with lookup(mapping):
                return acs._artist_folder_fingerprint_confirms(Path(tmp), "Real Artist")

    def test_weak_artist_hit_is_not_confirmation(self):
        self.assertIsNone(self._run([[hit(REC_A, 30, artist="Real Artist")]]))

    def test_low_ranked_artist_hit_does_not_override_confirmed_other_artist(self):
        self.assertFalse(self._run([[hit(REC_A, 95, artist="Somebody Else"), hit(REC_B, 50, artist="Real Artist")]]))

    def test_any_confirmed_disagreement_blocks(self):
        self.assertFalse(self._run([[hit(REC_A, 95, artist="Real Artist")],
                                    [hit(REC_B, 95, artist="Somebody Else")]]))

    def test_confirmed_agreement_confirms(self):
        self.assertTrue(self._run([[hit(REC_A, 95, artist="Real Artist")]]))


class VerifyMatchTests(unittest.TestCase):
    """MI-13: playlist status never reports confirmed on weak evidence."""

    def _verify(self, cands):
        with mock.patch.object(acs, "_acoustid_lookup_cached", return_value=cands):
            return acs._acoustid_verify_match("/x.flac", "Real Artist", "Crossfire")

    def test_weak_text_match_is_unverified(self):
        self.assertEqual(self._verify([hit(REC_A, 30, title="Crossfire", artist="Real Artist")]), "unverified")

    def test_low_ranked_text_match_is_mismatch(self):
        self.assertEqual(self._verify([hit(REC_B, 95, title="Other Song", artist="Other"),
                                       hit(REC_A, 50, title="Crossfire", artist="Real Artist")]), "mismatch")

    def test_tier_split_between_songs_is_unverified(self):
        self.assertEqual(self._verify([hit(REC_B, 95, title="Other Song", artist="Other"),
                                       hit(REC_A, 95, title="Crossfire", artist="Real Artist")]), "unverified")

    def test_confident_match_is_confirmed(self):
        self.assertEqual(self._verify([hit(REC_A, 95, title="Crossfire", artist="Real Artist")]), "confirmed")


class ParseCandidatesTests(unittest.TestCase):
    """MI-16."""

    def test_keeps_every_recording_of_a_result(self):
        recs = [{"id": f"rec-{i}", "title": "Song"} for i in range(6)]
        out = _acoustid_parse_candidates({"results": [{"id": "aid", "score": 0.95, "recordings": recs}]})
        self.assertEqual([c["mb_trackid"] for c in out], [f"rec-{i}" for i in range(6)])

    def test_release_group_only_when_unambiguous(self):
        data = {"results": [{"id": "aid", "score": 0.95, "recordings": [
            {"id": "r1", "releasegroups": [{"id": "RG-1", "title": "Album"}, {"id": "rg-2", "title": "Single"}]},
            {"id": "r2", "releasegroups": [{"id": "rg-3", "title": "Album"}]},
        ]}]}
        r1, r2 = _acoustid_parse_candidates(data)
        self.assertEqual(r1["mb_releasegroupid"], "")
        self.assertTrue(r1["release_group_ambiguous"])
        self.assertEqual(r1["mb_releasegroupids"], ["rg-1", "rg-2"])
        self.assertEqual(r2["mb_releasegroupid"], "rg-3")
        self.assertFalse(r2["release_group_ambiguous"])


class ReleaseIdTests(unittest.TestCase):
    """MI-17: a Beets row id is never a Release ID."""

    def test_album_id_is_not_a_release_id(self):
        self.assertEqual(_release_id({"album_id": 42}), "")
        self.assertEqual(_release_id({"mb_albumid": RELEASE.upper()}), RELEASE)


class HitScoreScaleTests(unittest.TestCase):
    """Integer scores are percents; only a float in 0..1 is a fraction."""

    def test_scale(self):
        from backend.matching.track_alignment import acoustid_score_percent as pct
        self.assertEqual(pct(1), 1.0)
        self.assertEqual(pct(0), 0.0)
        self.assertEqual(pct(95), 95.0)
        self.assertEqual(pct(1.0), 100.0)
        self.assertEqual(pct(0.95), 95.0)
        self.assertEqual(pct(95.5), 95.5)
        self.assertEqual(pct("1"), 1.0)
        self.assertEqual(pct("0.95"), 95.0)
        self.assertEqual(pct(None), 0.0)
        self.assertEqual(pct(True), 0.0)
        self.assertEqual(pct("junk"), 0.0)

    def test_one_percent_hit_is_not_confirmed(self):
        from backend.matching import acoustid_evidence_from_hits
        ev = acoustid_evidence_from_hits([{"mb_trackid": REC_A, "score": 1}], REC_A)
        self.assertNotEqual(ev.status, AcoustIDStatus.CONFIRMED)
        with lookup({"src": [hit(REC_A, 1)], "lib": [hit(REC_A, 1)]}):
            self.assertEqual(acs._acoustid_fingerprint_match("src", "lib"), ("", [], []))
        self.assertEqual(acs._audio_identity_score({"score": 1}), 0.01)
        self.assertEqual(acs._audio_identity_score({"score": 0.95}), 0.95)


class DeadAcoustidPromotionRemovedTests(unittest.TestCase):
    """#261 N-7: the fresh-import AcoustID promotion had no production caller
    after #249 and mbsync never assigns Recording IDs from alignment (MI-18),
    so neither the helper nor a no-op ``acoustid_verify`` request survives."""

    def test_track_align_is_gone(self):
        import importlib.util
        self.assertIsNone(importlib.util.find_spec("backend.track_align"))

    def test_mbsync_request_does_not_claim_an_acoustid_check(self):
        import backend.matching_service as ms
        with mock.patch.object(ms.composite_workflows, "plan_album_mb_track_repair",
                               return_value={"ok": False, "code": "repair_no_release", "error": "x"}) as plan:
            ms._match_tracks_from_mb_shared("", 1, [])
        self.assertNotIn("acoustid_verify", plan.call_args.args[0])

if __name__ == "__main__":
    unittest.main()
