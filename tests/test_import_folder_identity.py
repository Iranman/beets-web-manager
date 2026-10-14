"""Stage 2 folder identification and the Stage 2b auto-import decision, plus
I-8 (per-file AcoustID hits reach the album preflight). Synthetic
MusicBrainz/AcoustID data only; no provider is called."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.import_identity as ii
import backend.matching_service as ms
from backend.provider_boundary import ProviderOutcome, ProviderResult

REL_A, RG_A = "a0000000-0000-0000-0000-00000000000a", "a1000000-0000-0000-0000-00000000000a"
REL_B, RG_B = "b0000000-0000-0000-0000-00000000000b", "b1000000-0000-0000-0000-00000000000b"
RECS = ["c0000000-0000-0000-0000-00000000000%d" % i for i in range(1, 6)]
OTHER_REC = "d0000000-0000-0000-0000-00000000000d"
TITLES = ["One", "Two", "Three", "Four", "Five"]


def _tracks(n, recs=None):
    recs = recs or RECS
    return [{"mb_trackid": recs[i], "title": TITLES[i], "track": i + 1, "disc": 1} for i in range(n)]


def _hit(rec, score=95, releases=(REL_A,), rg=RG_A):
    return {"mb_trackid": rec, "score": score, "mb_albumids": list(releases),
            "mb_releasegroupid": rg, "mb_releasegroupids": [rg]}


class _FolderFixture(unittest.TestCase):
    """Default fixture: a 3-file folder whose files fingerprint to the three
    recordings of REL_A (Release Group RG_A)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "Some Album"
        self.folder.mkdir()
        self.paths = []
        for i in range(3):
            p = self.folder / f"0{i + 1} {TITLES[i]}.flac"
            p.write_bytes(b"synthetic")
            self.paths.append(str(p))
        self.releases = {REL_A: (RG_A, _tracks(3)), REL_B: (RG_B, _tracks(3, [OTHER_REC] * 3))}
        self.file_hits = {p: [_hit(RECS[i])] for i, p in enumerate(self.paths)}
        self.outcome = None  # override: one ProviderResult for every file
        self.embedded = {}
        self.search = [REL_A]
        self.rg_to_release = {RG_A: REL_A, RG_B: REL_B}

    def tearDown(self):
        self.tmp.cleanup()

    def _fetch(self, rid, log=None):
        if rid not in self.releases:
            return {"ok": False, "error": "not found"}
        rg, tracks = self.releases[rid]
        return {"ok": True, "tracks": tracks, "release_title": "Some Album",
                "release_artist": "Synthetic Artist", "release_group": rg}

    def _lookup(self, path):
        if self.outcome is not None:
            return self.outcome
        hits = self.file_hits.get(str(path), [])
        return ProviderResult("acoustid", ProviderOutcome.CONFIRMED if hits else ProviderOutcome.NO_RESULT, data=hits)

    def _evidence(self, folder):
        return {"audio_files": list(self.paths), "folder_track_count": len(self.paths),
                "guessed_artist": "Synthetic Artist", "guessed_album": "Some Album", "guessed_year": ""}

    def _patches(self):
        return [
            mock.patch.object(ms, "DOWNLOADS_ROOT", Path(self.tmp.name)),
            mock.patch.object(ms, "_fetch_mb_release_tracklist", side_effect=self._fetch),
            mock.patch.object(ms, "_acoustid_multi_file", return_value={}),
            mock.patch.object(ms, "_acoustid_lookup_cached_outcome", side_effect=self._lookup),
            mock.patch.object(ii, "_acoustid_lookup_cached_outcome", side_effect=self._lookup),
            mock.patch.object(ii, "_build_folder_evidence", side_effect=self._evidence),
            mock.patch.object(ii, "_embedded_mb_ids", side_effect=lambda p: {
                "mb_trackid": "", "mb_albumid": "", "mb_releasegroupid": "", **self.embedded.get(p, {})}),
            mock.patch.object(ii, "_mb_release_search", side_effect=lambda *a, **k: [{"mb_albumid": r} for r in self.search]),
            mock.patch.object(ii, "_mb_release_search_by_folder_tracks", return_value=[]),
            mock.patch.object(ii, "_resolve_release_group_to_release",
                              side_effect=lambda rg, log, **k: self.rg_to_release.get(rg, "")),
        ]

    def identify(self, skip_reason="no_strong_match"):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            return ii.identify_import_folder(str(self.folder), skip_reason)
        finally:
            for p in reversed(patches):
                p.stop()

    def assertNotEligible(self, result, reason_prefix):
        auto = result["auto_import"]
        self.assertFalse(auto["eligible"], auto)
        self.assertEqual((auto["release_group_id"], auto["release_id"]), ("", ""))
        self.assertTrue(any(r.startswith(reason_prefix) for r in auto["reasons"]), auto["reasons"])


class FolderIdentityTests(_FolderFixture):
    # ── evidence tiers ────────────────────────────────────────────────────

    def test_fingerprint_proof_of_every_file_is_eligible(self):
        result = self.identify()
        auto = result["auto_import"]
        self.assertTrue(auto["eligible"], auto)
        self.assertEqual((auto["release_group_id"], auto["release_id"]), (RG_A, REL_A))
        self.assertIn("beets_strong_match", auto["post_conditions"])
        top = result["candidates"][0]
        self.assertEqual((top["deterministic_rank"], top["ai_rank"]), (1, None))
        self.assertEqual(top["evidence"]["local_tracks_verified"], 3)
        self.assertIn("acoustid_release_group", top["evidence"]["sources"])
        self.assertEqual(result["ai_state"], {"status": "not_run"})
        self.assertEqual(result["evidence_capabilities"]["acoustid"], "available")
        self.assertEqual(result["conflicts"], [])

    def test_embedded_release_id_seeds_a_candidate(self):
        self.file_hits = {}
        self.search = []
        self.embedded = {p: {"mb_albumid": REL_A} for p in self.paths}
        result = self.identify()
        self.assertEqual(result["candidates"][0]["release_group_id"], RG_A)
        self.assertIn("embedded_release_id", result["candidates"][0]["evidence"]["sources"])
        # Release ID alone, with no per-file proof, never auto-imports.
        self.assertNotEligible(result, "no_release_group_verified")

    def test_text_search_alone_never_auto_imports(self):
        self.file_hits = {}
        result = self.identify()
        self.assertEqual(result["candidates"][0]["release_group_id"], RG_A)
        self.assertEqual(result["candidates"][0]["evidence"]["sources"], ["musicbrainz_search"])
        self.assertNotEligible(result, "no_release_group_verified")

    # ── conflicts and ambiguity ───────────────────────────────────────────

    def test_embedded_recording_id_contradicted_by_fingerprint(self):
        self.embedded = {self.paths[0]: {"mb_trackid": OTHER_REC}}
        result = self.identify()
        self.assertIn("fingerprint_recording_id_conflict", result["conflicts"])
        self.assertNotEligible(result, "folder_conflict:")

    def test_embedded_release_group_contradicted_by_candidate(self):
        self.embedded = {p: {"mb_releasegroupid": RG_B} for p in self.paths}
        self.rg_to_release = {RG_B: ""}  # the tagged group cannot be resolved
        result = self.identify()
        top = next(c for c in result["candidates"] if c["release_group_id"] == RG_A)
        self.assertIn("embedded_release_group_conflict", top["evidence"]["conflicts"])
        self.assertNotEligible(result, "no_release_group_verified")

    def test_acoustid_ambiguous_blocks(self):
        for i, p in enumerate(self.paths):
            self.file_hits[p] = [_hit(RECS[i], 95), _hit(OTHER_REC, 94, releases=(REL_B,), rg=RG_B)]
        result = self.identify()
        self.assertNotEligible(result, "no_release_group_verified")

    def test_acoustid_unavailable_or_not_configured_goes_to_review(self):
        for outcome, capability in (
            (ProviderResult("acoustid", ProviderOutcome.NOT_CONFIGURED, data=[]), "not_configured"),
            (ProviderResult("acoustid", ProviderOutcome.UNAVAILABLE, data=[], message="fpcalc is not installed"), "lookup_failed"),
        ):
            with self.subTest(capability):
                self.outcome = outcome
                result = self.identify()
                self.assertEqual(result["evidence_capabilities"]["acoustid"], capability)
                self.assertIn(f"fingerprint_{capability}", result["warnings"])
                self.assertEqual(result["candidates"][0]["release_group_id"], RG_A)  # still from text search
                self.assertNotEligible(result, "no_release_group_verified")

    def test_one_file_without_fingerprint_answer_blocks(self):
        self.file_hits[self.paths[2]] = []
        result = self.identify()
        self.assertNotEligible(result, "no_release_group_verified")

    def test_duplicate_skip_reason_never_auto_imports(self):
        result = self.identify("duplicate")
        self.assertTrue(result["candidates"])  # evidence is still built for review
        self.assertNotEligible(result, "skip_reason_duplicate")

    # ── several Release Groups and partial albums ─────────────────────────

    def test_same_recordings_in_two_release_groups_go_to_review(self):
        # Album and compilation share the recordings: both groups are proven.
        self.releases[REL_B] = (RG_B, _tracks(3))
        self.file_hits = {p: [_hit(RECS[i], releases=(REL_A, REL_B), rg=RG_A)] for i, p in enumerate(self.paths)}
        self.search = [REL_A, REL_B]
        result = self.identify()
        self.assertEqual({c["release_group_id"] for c in result["candidates"]}, {RG_A, RG_B})
        self.assertNotEligible(result, "multiple_release_groups_verified")

    def test_unproven_text_candidate_in_another_group_does_not_block(self):
        self.search = [REL_B, REL_A]
        result = self.identify()
        self.assertEqual([c["release_group_id"] for c in result["candidates"]][0], RG_A)
        self.assertTrue(result["auto_import"]["eligible"], result["auto_import"])

    def test_partial_album_passes_identity_and_leaves_completeness_to_beets(self):
        self.releases[REL_A] = (RG_A, _tracks(5))
        result = self.identify()
        auto = result["auto_import"]
        self.assertTrue(auto["eligible"], auto)
        self.assertIn("beets_strong_match", auto["post_conditions"])
        self.assertEqual(result["candidates"][0]["evidence"]["preflight"]["expected"], 5)

    def test_representative_release_prefers_same_track_count(self):
        rel_a2 = "a2000000-0000-0000-0000-00000000000a"
        self.releases[REL_A] = (RG_A, _tracks(5))
        self.releases[rel_a2] = (RG_A, _tracks(3))
        self.search = [REL_A, rel_a2]
        result = self.identify()
        self.assertEqual(result["candidates"][0]["representative_release_id"], rel_a2)
        self.assertEqual(result["auto_import"]["release_id"], rel_a2)

    # ── competing proof threshold ─────────────────────────────────────────

    def test_album_and_single_sharing_one_track_is_eligible(self):
        rel_s, rg_s = "f0000000-0000-0000-0000-00000000000f", "f1000000-0000-0000-0000-00000000000f"
        self.releases[rel_s] = (rg_s, _tracks(1))
        self.search = [REL_A, rel_s]  # the single is evaluated and proves one file
        self.file_hits[self.paths[0]] = [_hit(RECS[0]) | {"mb_releasegroupids": [RG_A, rg_s]}]
        result = self.identify()
        self.assertIn(rg_s, {c["release_group_id"] for c in result["candidates"]})
        self.assertTrue(result["auto_import"]["eligible"], result["auto_import"])

    def test_unevaluated_single_group_from_fingerprint_does_not_block(self):
        self.file_hits[self.paths[0]] = [_hit(RECS[0]) | {"mb_releasegroupids": [RG_A, "f1000000-0000-0000-0000-00000000000f"]}]
        auto = self.identify()["auto_import"]
        self.assertTrue(auto["eligible"], auto)

    def test_deluxe_with_near_equal_coverage_blocks(self):
        self.releases[REL_B] = (RG_B, _tracks(5))
        self.search = [REL_A, REL_B]
        self.assertFalse(self.identify()["auto_import"]["eligible"])

    def test_compilation_with_majority_coverage_blocks(self):
        self.releases[REL_B] = (RG_B, _tracks(3, [RECS[0], RECS[1], OTHER_REC]))
        self.search = [REL_A, REL_B]
        self.assertNotEligible(self.identify(), "other_release_group_has_track_proof")

    def test_embedded_release_past_seed_cap_blocks(self):
        extra = ["a%d000000-0000-0000-0000-00000000000a" % i for i in (5, 6)]
        for rid in extra:
            self.releases[rid] = (RG_A, _tracks(3))
        self.embedded = {self.paths[0]: {"mb_albumid": REL_A}, self.paths[1]: {"mb_albumid": extra[0]},
                         self.paths[2]: {"mb_albumid": extra[1]}}
        with mock.patch.object(ii, "MAX_RELEASES", 2):
            self.assertNotEligible(self.identify(), "embedded_release_not_evaluated")

    # ── symlinks ──────────────────────────────────────────────────────────

    def test_symlinked_audio_is_not_evidence_and_blocks(self):
        outside = Path(self.tmp.name) / "elsewhere.flac"
        outside.write_bytes(b"synthetic")
        link = self.folder / "04 Linked.flac"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported here")
        files, linked = ii._recursive_audio(str(self.folder))
        self.assertTrue(linked)
        self.assertEqual(len(files), 3)
        self.assertNotIn(str(link), files)
        self.assertNotEligible(self.identify(), "folder_conflict:symlinked_audio_file")

    def test_symlink_loop_terminates_and_blocks(self):
        try:
            (self.folder / "loop").symlink_to(self.folder, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported here")
        files, linked = ii._recursive_audio(str(self.folder))
        self.assertEqual((len(files), linked), (3, True))

    def test_no_audio_files(self):
        self.paths = []
        result = self.identify()
        self.assertEqual(result["candidates"], [])
        self.assertIn("no_audio_files", result["warnings"])
        self.assertNotEligible(result, "no_candidates")


class PreflightFingerprintEvidenceTests(_FolderFixture):
    """I-8: _folder_release_preflight hands each local file's AcoustID hits
    to the canonical evaluator."""

    def _preflight(self):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            return ms._folder_release_preflight(str(self.folder), REL_A, fingerprint_evidence=True)
        finally:
            for p in reversed(patches):
                p.stop()

    def test_hits_prove_every_local_track(self):
        canonical = self._preflight()["matching_decision"]["evidence"]["canonical_match"]
        self.assertEqual((canonical["local_tracks_verified"], canonical["local_tracks_total"]), (3, 3))

    def test_unanswered_lookup_is_unavailable_not_no_result(self):
        self.outcome = ProviderResult("acoustid", ProviderOutcome.TRANSIENT_ERROR, data=[])
        pre = self._preflight()
        canonical = pre["matching_decision"]["evidence"]["canonical_match"]
        self.assertEqual(canonical["local_tracks_verified"], 0)
        statuses = {a["acoustid_status"] for a in canonical["track_alignment"]["assignments"]}
        self.assertEqual(statuses, {"unavailable"})


if __name__ == "__main__":
    unittest.main()
