"""Missing-track import into an existing album: per-track AcoustID recording
proof replaces the release-level AcoustID hits the preflight used to need,
plus the stable refusal reason code and the stale-review prune.
Synthetic MusicBrainz/AcoustID data only; no provider is called."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.library_service as ls
import backend.matching_service as ms
import backend.pending_review_store as prs
from backend.provider_boundary import ProviderOutcome, ProviderResult

REL, RG = "a0000000-0000-0000-0000-00000000000a", "a1000000-0000-0000-0000-00000000000a"
OTHER_REL = "b0000000-0000-0000-0000-00000000000b"
RECS = ["c0000000-0000-0000-0000-00000000000%d" % i for i in range(1, 6)]
OTHER_REC = "d0000000-0000-0000-0000-00000000000d"
TITLES = ["One", "Two", "Three", "Four", "Five"]
TRACKS = [{"mb_trackid": RECS[i], "title": TITLES[i], "track": i + 1, "disc": 1} for i in range(5)]
ALBUM_ID = 7


def _hit(rec, score=100):
    return {"mb_trackid": rec, "score": score, "mb_albumids": [REL], "mb_releasegroupid": RG}


def _wanted(*numbers):
    return [dict(TRACKS[n - 1]) for n in numbers]


class _Fixture(unittest.TestCase):
    """Album 7 (REL / RG) holds tracks 1 and 3; the staged folder has the
    downloads for the missing tracks 2, 4 and 5, each fingerprinting to its
    recording. AcoustID returns no release-level hits at all (the live case)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "_beets_missing_import" / "abc-Some Album"
        self.folder.mkdir(parents=True)
        self.paths = {}
        for n in (2, 4, 5):
            p = self.folder / f"0{n} {TITLES[n - 1]}.flac"
            p.write_bytes(b"synthetic")
            self.paths[n] = str(p)
        self.hits = {self.paths[n]: [_hit(RECS[n - 1])] for n in (2, 4, 5)}
        self.album = {"id": ALBUM_ID, "album": "Some Album", "albumartist": "Synthetic Artist",
                      "mb_albumid": REL, "mb_releasegroupid": RG}
        self.items = [{"id": 1, "title": "One", "track": 1, "disc": 1, "mb_trackid": RECS[0], "path": "/music/a/01.flac"},
                      {"id": 3, "title": "Three", "track": 3, "disc": 1, "mb_trackid": RECS[2], "path": "/music/a/03.flac"}]

    def tearDown(self):
        self.tmp.cleanup()

    def _fetch(self, rid, log=None):
        if rid not in (REL, OTHER_REL):
            return {"ok": False, "error": "not found", "tracks": []}
        return {"ok": True, "tracks": [dict(t) for t in TRACKS], "release_title": "Some Album",
                "release_artist": "Synthetic Artist", "release_group": RG}

    def _lookup(self, path):
        hits = self.hits.get(str(path))
        if isinstance(hits, ProviderResult):
            return hits
        if hits is None:
            return ProviderResult("acoustid", ProviderOutcome.UNAVAILABLE, data=[])
        return ProviderResult("acoustid", ProviderOutcome.CONFIRMED if hits else ProviderOutcome.NO_RESULT, data=hits)

    def _patches(self):
        cw = ms.composite_workflows
        return [
            mock.patch.object(ms, "DOWNLOADS_ROOT", Path(self.tmp.name)),
            mock.patch.object(ms, "_fetch_mb_release_tracklist", side_effect=self._fetch),
            mock.patch.object(ms, "_acoustid_multi_file", return_value={}),
            mock.patch.object(ms, "_acoustid_lookup_cached_outcome", side_effect=self._lookup),
            mock.patch.object(cw, "get_album", side_effect=lambda aid: dict(self.album)),
            mock.patch.object(cw, "find_all_items_by_album_id", side_effect=lambda aid: [dict(i) for i in self.items]),
        ]

    def _run(self, fn, *a, **k):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            return fn(*a, **k)
        finally:
            for p in reversed(patches):
                p.stop()

    def preflight(self, wanted=(2, 4, 5), rid=REL):
        return self._run(ms._folder_release_preflight, str(self.folder), rid,
                         existing_album_id=ALBUM_ID, log=None,
                         wanted_tracks=_wanted(*wanted) if wanted else None)

    def assertRefused(self, pre, code):
        self.assertFalse(pre["ok"], pre.get("missing_track_proof"))
        self.assertEqual(pre["missing_track_proof"]["reason_code"], code)
        self.assertFalse(pre["matching_decision"]["action_allowed"])


class MissingTrackRecordingProofTests(_Fixture):
    def test_without_wanted_tracks_behaviour_is_unchanged(self):
        pre = self.preflight(wanted=None)
        self.assertFalse(pre["ok"])
        self.assertNotIn("missing_track_proof", pre)
        # The count covers the album's two tracks plus the three files.
        self.assertEqual((pre["matches"], pre["expected"], pre["audio_count"]), (5, 5, 3))

    # (a)
    def test_every_file_recording_confirmed_is_accepted_without_release_hits(self):
        pre = self.preflight()
        self.assertTrue(pre["ok"], pre.get("missing_track_proof"))
        self.assertEqual(pre["acoustid_release_hits"], {})
        proof = pre["missing_track_proof"]
        self.assertTrue(proof["ok"])
        self.assertEqual(sorted(f["track"] for f in proof["files"]), [2, 4, 5])
        decision = pre["matching_decision"]
        self.assertTrue(decision["action_allowed"])
        self.assertEqual(decision["release_group_id"], RG)
        self.assertEqual(decision["conflicts"], [])
        # Proven fill is never swapped for a source-sized edition.
        self.assertFalse(ms._preflight_oversized_subset_complete(pre))

    # (b)
    def test_one_unverified_file_goes_to_review(self):
        for hits in ([], [_hit(RECS[1], 79)]):
            with self.subTest(hits=hits):
                self.hits[self.paths[2]] = hits
                self.assertRefused(self.preflight(), "file_unverified")

    def test_ambiguous_file_goes_to_review(self):
        self.hits[self.paths[2]] = [_hit(RECS[1], 95), _hit(OTHER_REC, 94)]
        self.assertRefused(self.preflight(), "file_unverified")

    def test_acoustid_unavailable_goes_to_review(self):
        self.hits[self.paths[4]] = None
        self.assertRefused(self.preflight(), "acoustid_unavailable")

    def test_provider_failures_are_acoustid_unavailable(self):
        # Not specific to this candidate: every peer's copy would be refused.
        for outcome in (ProviderOutcome.NOT_CONFIGURED, ProviderOutcome.AUTHENTICATION_ERROR,
                        ProviderOutcome.UNAVAILABLE, ProviderOutcome.RATE_LIMITED,
                        ProviderOutcome.TRANSIENT_ERROR):
            with self.subTest(outcome=outcome):
                self.hits[self.paths[4]] = ProviderResult("acoustid", outcome, data=[])
                self.assertRefused(self.preflight(), "acoustid_unavailable")
                self.assertFalse(ms.IMPORT_REFUSAL_REASONS["acoustid_unavailable"])

    def test_unfingerprintable_file_is_specific_to_the_candidate(self):
        # No fingerprint / too short (UNAVAILABLE) and an fpcalc timeout (TRANSIENT_ERROR).
        for outcome in (ProviderOutcome.UNAVAILABLE, ProviderOutcome.TRANSIENT_ERROR):
            with self.subTest(outcome=outcome):
                self.hits[self.paths[4]] = ProviderResult("acoustid", outcome, data=[],
                                                          evidence={"fingerprintable": False})
                self.assertRefused(self.preflight(), "file_unfingerprintable")
                self.assertTrue(ms.IMPORT_REFUSAL_REASONS["file_unfingerprintable"])

    def test_provider_failure_outranks_an_unfingerprintable_file(self):
        self.hits[self.paths[2]] = ProviderResult("acoustid", ProviderOutcome.UNAVAILABLE, data=[],
                                                  evidence={"fingerprintable": False})
        self.hits[self.paths[4]] = ProviderResult("acoustid", ProviderOutcome.RATE_LIMITED, data=[])
        self.assertRefused(self.preflight(), "acoustid_unavailable")

    # (c)
    def test_recording_not_on_target_release_goes_to_review(self):
        self.hits[self.paths[5]] = [_hit(OTHER_REC)]
        self.assertRefused(self.preflight(), "recording_not_on_release")

    # (d)
    def test_recording_mapping_to_unrequested_track_goes_to_review(self):
        self.assertRefused(self.preflight(wanted=(2, 4)), "track_not_requested")

    def test_duplicate_of_album_track_goes_to_review(self):
        self.hits[self.paths[5]] = [_hit(RECS[0])]  # track 1 is already in the album
        self.assertRefused(self.preflight(wanted=(1, 2, 4, 5)), "duplicate_track")

    def test_two_files_same_recording_goes_to_review(self):
        self.hits[self.paths[5]] = [_hit(RECS[3])]
        self.assertRefused(self.preflight(), "duplicate_track")

    def test_release_other_than_existing_albums_is_out_of_scope(self):
        self.assertRefused(self.preflight(rid=OTHER_REL), "missing_track_scope")
        self.album["mb_releasegroupid"] = "e1000000-0000-0000-0000-00000000000e"
        pre = self.preflight()
        self.assertFalse(pre["ok"])

    def test_resolver_accepts_proven_fill_inside_its_release_group(self):
        def resolve():
            with mock.patch.object(ls, "_resolve_mb_release_id", return_value=REL), \
                    mock.patch.object(ls, "_mb_release_has_tracks", return_value=True), \
                    mock.patch.object(ls, "_fetch_mb_release_tracklist", side_effect=self._fetch), \
                    mock.patch.object(ls, "_mb_release_group_candidates", return_value=[]):
                log = []
                return ls._resolve_album_release_for_import(
                    REL, "Synthetic Artist", "Some Album", "", 0, log,
                    source_folder=str(self.folder), existing_album_id=ALBUM_ID,
                    wanted_tracks=_wanted(2, 4, 5)), log
        self.assertEqual(self._run(resolve)[0], REL)
        self.hits[self.paths[2]] = []
        resolved, log = self._run(resolve)
        self.assertEqual(resolved, "")
        self.assertTrue(any("REFUSED" in line for line in log), log)


class RefusalReasonCodeTests(unittest.TestCase):
    def test_codes_parse_and_flag_candidate_specificity(self):
        msg = "import job failed: ERROR: Could not resolve a MusicBrainz release ID for import [reason_code=acoustid_unavailable]; queued"
        self.assertEqual(ms.import_refusal_reason_code(msg), "acoustid_unavailable")
        self.assertFalse(ms.IMPORT_REFUSAL_REASONS["acoustid_unavailable"])
        self.assertTrue(ms.IMPORT_REFUSAL_REASONS["file_unverified"])
        self.assertEqual(ms.import_refusal_reason_code("[reason_code=made_up]"), "")
        self.assertEqual(ms.import_refusal_reason_code("no code"), "")
        self.assertEqual(ms.import_refusal_reason_code("x [reason_code=file_unfingerprintable]"),
                         "file_unfingerprintable")


class FpcalcOutcomeTests(unittest.TestCase):
    """helpers_mb flags per-file fpcalc failures; no network, no real fpcalc."""

    def _fpcalc_lookup(self, run):
        import helpers_mb
        with mock.patch.dict("os.environ", {"ACOUSTID_API_KEY": "synthetic"}), \
                mock.patch.object(helpers_mb.shutil, "which", return_value=__file__), \
                mock.patch.object(helpers_mb.subprocess, "run", side_effect=run), \
                mock.patch.object(helpers_mb.provider_boundary, "opened", side_effect=AssertionError("network")):
            return helpers_mb.acoustid_lookup_outcome("/synthetic/a.flac")

    def test_timeout_and_short_audio_are_unfingerprintable(self):
        import subprocess

        def timeout(*a, **k):
            raise subprocess.TimeoutExpired("fpcalc", 30)

        def short(*a, **k):
            return subprocess.CompletedProcess(a, 0, stdout=json.dumps({"duration": 3, "fingerprint": "AQ"}), stderr="")

        def corrupt(*a, **k):
            return subprocess.CompletedProcess(a, 2, stdout="", stderr="ERROR: Error decoding audio frame")

        for run in (timeout, short, corrupt):
            with self.subTest(run=run.__name__):
                outcome = self._fpcalc_lookup(run)
                self.assertFalse(outcome.answered)
                self.assertIs(outcome.evidence.get("fingerprintable"), False)

    # QA #408: an AcoustID/fpcalc failure that would hit every file is never flagged per-file.
    def test_provider_and_fpcalc_environment_failures_are_not_flagged(self):
        import io
        import subprocess
        import urllib.error
        import helpers_mb

        def good(*a, **k):
            return subprocess.CompletedProcess(a, 0, stdout=json.dumps({"duration": 200, "fingerprint": "AQ"}), stderr="")

        def cannot_exec(*a, **k):
            raise PermissionError(13, "Permission denied")

        def missing_library(*a, **k):
            return subprocess.CompletedProcess(a, 127, stdout="", stderr="fpcalc: error while loading shared libraries")

        def auth_error(*a, **k):
            raise urllib.error.HTTPError("https://api.acoustid.org/v2/lookup", 400, "bad", {},
                                         io.BytesIO(b'{"status":"error","error":{"code":4}}'))

        def down(*a, **k):
            raise urllib.error.URLError("connection refused")

        cases = {"fpcalc_cannot_exec": (cannot_exec, None), "fpcalc_missing_library": (missing_library, None),
                 "auth": (good, auth_error),
                 "network": (good, down), "lookup_exception": (good, AssertionError("boom"))}
        for name, (run, opened) in cases.items():
            with self.subTest(case=name), \
                    mock.patch.dict("os.environ", {"ACOUSTID_API_KEY": "synthetic"}), \
                    mock.patch.object(helpers_mb, "_ACOUSTID_NEXT_LOOKUP_AT", 0.0), \
                    mock.patch.object(helpers_mb.shutil, "which", return_value=__file__), \
                    mock.patch.object(helpers_mb.subprocess, "run", side_effect=run), \
                    mock.patch.object(helpers_mb.provider_boundary, "opened",
                                      side_effect=opened or AssertionError("network")):
                outcome = helpers_mb.acoustid_lookup_outcome("/synthetic/a.flac")
                self.assertFalse(outcome.answered, name)
                self.assertNotEqual((outcome.evidence or {}).get("fingerprintable"), False, name)
        with mock.patch.dict("os.environ", {"ACOUSTID_API_KEY": "", "ACOUSTID_KEY": ""}):
            self.assertFalse((helpers_mb.acoustid_lookup_outcome("/synthetic/a.flac").evidence or {}))
        with mock.patch.dict("os.environ", {"ACOUSTID_API_KEY": "synthetic"}), \
                mock.patch.object(helpers_mb.shutil, "which", return_value=None), \
                mock.patch.object(helpers_mb, "Path", return_value=mock.Mock(exists=mock.Mock(return_value=False))):
            self.assertFalse((helpers_mb.acoustid_lookup_outcome("/synthetic/a.flac").evidence or {}))

    def test_fpcalc_timeout_is_never_cached(self):
        import backend.acoustid_service as aid
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        audio = Path(tmp.name) / "a.flac"
        audio.write_bytes(b"synthetic")
        timeout = ProviderResult("acoustid", ProviderOutcome.TRANSIENT_ERROR, data=[], message="fpcalc timed out",
                                 evidence={"fingerprintable": False})
        with mock.patch.object(aid, "_ACOUSTID_FILE_CACHE_DIR", Path(tmp.name) / "cache"), \
                mock.patch.object(aid, "acoustid_lookup_outcome", return_value=timeout) as lookup:
            for _ in range(2):
                self.assertIs(aid._acoustid_lookup_cached_outcome(str(audio)).evidence.get("fingerprintable"), False)
        self.assertEqual(lookup.call_count, 2)
        self.assertFalse((Path(tmp.name) / "cache").exists())


class UnfingerprintableSetIsPerCallTests(_Fixture):
    """QA #408: a path flagged by an earlier lookup must not keep the flag when
    the later lookup (the one the proof uses) failed at the provider."""

    def test_later_provider_failure_wins_over_earlier_timeout(self):
        timeout = ProviderResult("acoustid", ProviderOutcome.TRANSIENT_ERROR, data=[],
                                 evidence={"fingerprintable": False})
        throttled = ProviderResult("acoustid", ProviderOutcome.RATE_LIMITED, data=[])
        seq = {self.paths[4]: iter([timeout, throttled])}
        base = self._lookup
        self._lookup = lambda p: next(seq[str(p)]) if str(p) in seq else base(p)
        pre = self._run(ms._folder_release_preflight, str(self.folder), REL, existing_album_id=ALBUM_ID,
                        log=None, fingerprint_evidence=True, wanted_tracks=_wanted(2, 4, 5))
        self.assertRefused(pre, "acoustid_unavailable")


class PreflightCodeDrivesPeerLoopTests(_Fixture):
    """QA #408: the code the real preflight produces decides the slskd peer loop."""

    def test_stop_on_provider_failure_continue_on_unfingerprintable_file(self):
        from tests.test_slskd_noncandidate_refusal import NonCandidateRefusalStopsPeerLoop as Loop
        cases = [
            (ProviderResult("acoustid", ProviderOutcome.RATE_LIMITED, data=[]), "acoustid_unavailable", 1),
            (ProviderResult("acoustid", ProviderOutcome.TRANSIENT_ERROR, data=[],
                            evidence={"fingerprintable": False}), "file_unfingerprintable", 12),
        ]
        for outcome, code, peers in cases:
            with self.subTest(code=code):
                self.hits[self.paths[4]] = outcome
                proof_code = self.preflight()["missing_track_proof"]["reason_code"]
                self.assertEqual(proof_code, code)
                m, _log = Loop._run(self, f" [reason_code={proof_code}]")
                self.assertEqual(m["_slskd_search_and_queue"].call_count, peers)


# (e)
class StaleReviewItemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.downloads = self.root / "downloads"
        (self.downloads / "kept").mkdir(parents=True)
        self.store = self.root / "pending.json"
        self.resolved = []

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self, items, *, downloads=None):
        self.store.write_text(json.dumps(items))
        with mock.patch.object(prs, "_AI_PENDING_FILE", self.store), \
                mock.patch.object(prs, "DOWNLOADS_ROOT", downloads or self.downloads), \
                mock.patch.object(prs, "_review_origin_payload", return_value={"origin_type": "x"}), \
                mock.patch.object(prs, "_reconcile_pending_review_enqueue_item", side_effect=lambda i: (i, False)), \
                mock.patch.object(prs, "_record_ai_review_decision",
                                  side_effect=lambda action, path, *a, **k: self.resolved.append((action, path))):
            return [i["path"] for i in prs._load_pending_reviews(prune_resolved=False)]

    def _item(self, path, **extra):
        return {"path": str(path), "status": "Pending AI", "origin_type": "x",
                "suggestion": {"origin_type": "x"}, **extra}

    def test_item_whose_download_folder_is_gone_is_auto_resolved(self):
        gone = self.downloads / "_beets_missing_import" / "f49e0dd276-Some Album"
        kept = self.downloads / "kept"
        outside = self.root / "elsewhere" / "missing"
        refused = self.downloads / "linked-gone"
        items = [self._item(gone), self._item(kept), self._item(outside),
                 self._item(refused, evidence={"conflict": "symlinked_audio"})]
        self.assertEqual(self._load(items), [str(kept), str(outside)])
        self.assertEqual(self.resolved, [("auto_resolved", str(gone)), ("auto_resolved", str(refused))])
        self.assertEqual([i["path"] for i in json.loads(self.store.read_text())], [str(kept), str(outside)])

    def test_active_import_and_missing_mount_are_kept(self):
        gone = self.downloads / "gone"
        self.assertEqual(self._load([self._item(gone, status="import_queued")]), [str(gone)])
        self.assertEqual(self._load([self._item(gone)], downloads=self.root / "no-mount"), [str(gone)])
        self.assertEqual(self.resolved, [])


if __name__ == "__main__":
    unittest.main()
