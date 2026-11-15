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
        self.edition = {}  # path -> {"barcode"/"catalog_number"/"label"/"isrc": ...}
        self.edition_map = {}  # {release_id: ["barcode"/"catalog_number"/"isrc", ...]}
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
            mock.patch.object(ii, "_embedded_edition_keys", side_effect=lambda p: {
                "barcode": "", "catalog_number": "", "label": "", "isrc": "", **self.edition.get(p, {})}),
            mock.patch.object(ii, "_mb_releases_for_edition_keys",
                              side_effect=lambda **k: {r: list(v) for r, v in self.edition_map.items()}),
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


REL_A2 = "a2000000-0000-0000-0000-00000000000a"  # a second edition of RG_A


class EditionEvidenceTests(_FolderFixture):
    """Barcode / catalogue-number+label / ISRC edition evidence. SUPPORTING
    only: it picks the right EDITION within an already-decided Release Group
    and is surfaced as evidence, but can never change the Release Group
    (canonical album identity). Synthetic data; no provider is called."""

    def _two_editions_same_tracks(self):
        # REL_A and REL_A2 are two editions of RG_A with identical tracks, both
        # proven by fingerprint -> they tie on every deterministic key, so only
        # the edition tiebreak can decide which represents RG_A. Default
        # (release-id) order prefers REL_A over REL_A2.
        self.releases[REL_A2] = (RG_A, _tracks(3))
        self.search = [REL_A, REL_A2]

    def test_barcode_selects_right_edition_within_release_group(self):
        self._two_editions_same_tracks()
        self.edition = {p: {"barcode": "0123456789012"} for p in self.paths}
        self.edition_map = {REL_A2: ["barcode"]}
        result = self.identify()
        top = result["candidates"][0]
        self.assertEqual(top["release_group_id"], RG_A)  # identity unchanged
        self.assertEqual(top["representative_release_id"], REL_A2)  # edition picked
        self.assertEqual(result["auto_import"]["release_group_id"], RG_A)
        self.assertEqual(result["auto_import"]["release_id"], REL_A2)
        self.assertIn("edition_barcode", top["evidence"]["sources"])
        self.assertIn(REL_A2, top["evidence"]["edition_match"])

    def test_catno_label_selects_right_edition_within_release_group(self):
        self._two_editions_same_tracks()
        self.edition = {p: {"catalog_number": "ABC-123", "label": "Some Label"} for p in self.paths}
        self.edition_map = {REL_A2: ["catalog_number"]}
        result = self.identify()
        top = result["candidates"][0]
        self.assertEqual(top["release_group_id"], RG_A)
        self.assertEqual(top["representative_release_id"], REL_A2)
        self.assertIn("edition_catalog_number", top["evidence"]["sources"])

    def test_isrc_contributes_supporting_evidence(self):
        # ISRC points at the proven edition REL_A: it is surfaced as supporting
        # evidence without changing the already-correct identity.
        self.edition = {p: {"isrc": "USABC%07d" % i} for i, p in enumerate(self.paths)}
        self.edition_map = {REL_A: ["isrc"]}
        result = self.identify()
        top = result["candidates"][0]
        self.assertEqual(top["release_group_id"], RG_A)
        self.assertIn("edition_isrc", top["evidence"]["sources"])
        self.assertIn(REL_A, top["evidence"]["edition_match"])
        self.assertTrue(result["auto_import"]["eligible"], result["auto_import"])

    # ── identity-safety guarantees ────────────────────────────────────────

    def test_edition_hit_cannot_change_release_group(self):
        # Fingerprints prove RG_A. A barcode points at REL_B (a DIFFERENT
        # Release Group) whose recordings do not match the folder: it must not
        # become the identity, and RG_A stays the auto-import target.
        self.edition = {p: {"barcode": "9999999999999"} for p in self.paths}
        self.edition_map = {REL_B: ["barcode"]}
        result = self.identify()
        auto = result["auto_import"]
        self.assertTrue(auto["eligible"], auto)
        self.assertEqual(auto["release_group_id"], RG_A)  # NEVER RG_B
        self.assertNotEqual(auto["release_group_id"], RG_B)
        rg_b = next((c for c in result["candidates"] if c["release_group_id"] == RG_B), None)
        if rg_b is not None:  # surfaced, but never accepted on edition evidence
            self.assertFalse(rg_b["evidence"]["action_allowed"], rg_b)

    def test_edition_hit_in_other_group_cannot_force_identity_and_goes_to_review(self):
        # A barcode names REL_B whose recordings DO match (same tracks). Edition
        # evidence cannot pick a Release Group: now two groups are proven, so
        # the folder goes to review rather than silently importing to RG_B.
        self.releases[REL_B] = (RG_B, _tracks(3))
        self.edition = {p: {"barcode": "9999999999999"} for p in self.paths}
        self.edition_map = {REL_B: ["barcode"]}
        result = self.identify()
        self.assertEqual({c["release_group_id"] for c in result["candidates"]}, {RG_A, RG_B})
        self.assertNotEligible(result, "multiple_release_groups_verified")

    def test_edition_cannot_override_stronger_edition_signal_in_winning_group(self):
        # RG_A has a 5-track and a 3-track edition; the folder has 3 files, so
        # the 3-track edition is the right representative (track-count signal).
        # Edition evidence pointing at the WRONG (5-track) edition must NOT
        # override that -- the outcome stays identical to the no-edition
        # baseline. Guards the position of the edition tiebreak in _release_key
        # (it must rank BELOW track-count, not couple into the score).
        rel_a2 = "a2000000-0000-0000-0000-00000000000a"
        self.releases[REL_A] = (RG_A, _tracks(5))
        self.releases[rel_a2] = (RG_A, _tracks(3))
        self.search = [REL_A, rel_a2]
        baseline = self.identify()
        self.edition = {p: {"barcode": "0123456789012"} for p in self.paths}
        self.edition_map = {REL_A: ["barcode"]}  # the wrong (5-track) edition
        with_edition = self.identify()
        self.assertEqual(with_edition["auto_import"]["release_id"], rel_a2)
        for key in ("eligible", "release_group_id", "release_id"):
            self.assertEqual(with_edition["auto_import"][key], baseline["auto_import"][key], key)
        self.assertEqual(with_edition["candidates"][0]["representative_release_id"],
                         baseline["candidates"][0]["representative_release_id"])

    def test_empty_edition_lookup_is_a_clean_no_op(self):
        # Tags present but MusicBrainz returns nothing: identical to no edition
        # evidence at all (still eligible, representative unchanged).
        self.edition = {p: {"barcode": "0123456789012"} for p in self.paths}
        self.edition_map = {}
        result = self.identify()
        top = result["candidates"][0]
        self.assertTrue(result["auto_import"]["eligible"], result["auto_import"])
        self.assertEqual(top["release_group_id"], RG_A)
        self.assertEqual(top["evidence"]["edition_match"], [])
        self.assertFalse(any(s.startswith("edition_") for s in top["evidence"]["sources"]))

    def test_absent_edition_tags_do_not_query_musicbrainz(self):
        # No edition tags on any file -> the edition lookup is never called.
        import helpers_mb
        with mock.patch.object(
            ii, "_mb_releases_for_edition_keys",
            side_effect=AssertionError("edition lookup must not run without tags"),
        ):
            result = self.identify()
        self.assertTrue(result["auto_import"]["eligible"], result["auto_import"])


class EditionGroupAcceptanceGuardTests(unittest.TestCase):
    """Guard (pure ranking): edition evidence must never reach GROUP-level
    acceptance or the Release Group sort. A DIFFERENT-group release that is
    edition-seeded, ``action_allowed=False`` and WITHOUT any independent
    conflict must still never be accepted and must not change the chosen
    Release Group. This FAILS if a future change ORs edition evidence into
    group ``action_allowed`` or lets it reorder groups -- unlike the
    integration case, the other-group release here carries NO conflict, so a
    leak cannot be masked by ``and not conflicts``."""

    def _release(self, rid, rgid, score, *, allowed, verified, hard=None, total=3):
        # `hard` decoupled from `allowed`: the other-group release below is
        # hard_proven so the close_runner_up net can never mask an acceptance
        # leak by re-demoting it (while verified=0 keeps it a non-competitor).
        hard = allowed if hard is None else hard
        assignments = [{"status": "matched", "is_deterministic": hard} for _ in range(total)]
        canonical = {"score": score, "local_tracks_verified": verified, "local_tracks_total": total,
                     "track_alignment": {"assignments": assignments}}
        return {"release_id": rid, "release_group_id": rgid, "track_count": total,
                "sources": ["musicbrainz_search"], "preflight": {}, "error": "",
                "matching_decision": {"action_allowed": allowed, "conflicts": [],
                                      "evidence": {"canonical_match": canonical}}}

    def test_edition_seeded_other_group_without_conflict_is_never_accepted(self):
        from backend.matching.folder_identity import decide_auto_import, rank_folder_candidates
        winner = self._release(REL_A, RG_A, 0.90, allowed=True, verified=3)
        # Different group, seeded ONLY by edition evidence: not accepted, no
        # conflict, no competing track proof (verified=0), but hard_proven so a
        # leak into group acceptance surfaces instead of being demoted away.
        other = self._release(REL_B, RG_B, 0.90, allowed=False, verified=0, hard=True)
        other["sources"] = ["edition_barcode"]
        candidates = rank_folder_candidates([winner, other], 3, (), {REL_B})
        rg_b = next(c for c in candidates if c["release_group_id"] == RG_B)
        # Precondition: the guard is meaningful only because there is no conflict.
        self.assertEqual(rg_b["evidence"]["conflicts"], [])
        self.assertIn(REL_B, rg_b["evidence"]["edition_match"])
        # Edition evidence must NOT make the different group acceptable...
        self.assertFalse(rg_b["evidence"]["action_allowed"], rg_b)
        # ...nor sort it ahead of the real winner...
        self.assertEqual(candidates[0]["release_group_id"], RG_A)
        # ...and the chosen Release Group stays RG_A (identity unchanged).
        auto = decide_auto_import(candidates, skip_reason="no_strong_match",
                                  audio_count=3, unconfirmed_files=0)
        self.assertTrue(auto["eligible"], auto)
        self.assertEqual(auto["release_group_id"], RG_A)


class EditionHelperUnitTests(unittest.TestCase):
    """Pure helpers: aggregation agreement rule and the no-request guard."""

    def test_aggregate_requires_single_agreed_value(self):
        rows = [{"barcode": "111"}, {"barcode": "222"}]  # disagreement -> dropped
        self.assertEqual(ii._aggregate_edition_keys(rows)["barcode"], "")
        rows = [{"barcode": "111"}, {"barcode": "111"}, {}]  # agree -> used
        self.assertEqual(ii._aggregate_edition_keys(rows)["barcode"], "111")

    def test_aggregate_collects_distinct_isrcs(self):
        rows = [{"isrc": "A"}, {"isrc": "B"}, {"isrc": "A"}]
        self.assertEqual(ii._aggregate_edition_keys(rows)["isrcs"], ["A", "B"])

    def test_edition_lookup_without_keys_makes_no_request(self):
        import helpers_mb
        with mock.patch.object(helpers_mb, "_mb_edition_search_release_ids",
                               side_effect=AssertionError("no query expected")), \
             mock.patch.object(helpers_mb, "_mb_isrc_release_ids",
                               side_effect=AssertionError("no query expected")):
            self.assertEqual(helpers_mb._mb_releases_for_edition_keys(), {})
            self.assertEqual(helpers_mb._mb_releases_for_edition_keys(barcode="not-digits"), {})

    def test_edition_lookup_merges_kinds_per_release(self):
        import helpers_mb
        with mock.patch.object(helpers_mb, "_mb_edition_search_release_ids", return_value=[REL_A]), \
             mock.patch.object(helpers_mb, "_mb_isrc_release_ids", return_value=[REL_A]):
            out = helpers_mb._mb_releases_for_edition_keys(
                barcode="0123456789012", isrcs=["USABC0000001"])
        self.assertEqual(out, {REL_A: ["barcode", "isrc"]})


if __name__ == "__main__":
    unittest.main()
