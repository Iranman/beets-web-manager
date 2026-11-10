"""#584: an unresolved AI artist identity (``identity_validated is None``) is
not proof. Import All refuses it, revalidation keeps it unresolved, and the
auto-enqueue preview does not upgrade it to True.

#574: the reimport job's preflight gets the same ``wanted_tracks`` evidence
Import All discovery used, so a proven missing-track fill inside the music
library passes both instead of failing the job.

Synthetic MusicBrainz/AcoustID data only; no provider is called.
"""
import contextlib
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import backend.import_review_service as irs
import backend.import_service as isvc
import backend.matching_service as ms
import routes_library
import tests.test_missing_track_recording_proof as mtp

REL = "083b9009-2222-2222-2222-222222222222"
RG = "37cb4a72-1111-1111-1111-111111111111"


def _accepted(**sug):
    base = {"mb_albumid": REL, "mb_valid": True, "confidence": "high", "preflight": {"ok": True}}
    return routes_library._import_all_discovery_accepted({"ok": True, "suggestion": {**base, **sug}})


class ImportAllRequiresProvenIdentity(unittest.TestCase):
    def test_unresolved_identity_is_refused(self):
        self.assertFalse(_accepted(identity_validated=None))
        self.assertFalse(_accepted())  # absent = unresolved

    def test_proven_identity_is_accepted(self):
        self.assertTrue(_accepted(identity_validated=True))

    def test_route_queues_unresolved_identity_for_review(self):
        import app as app_module
        jobs = []
        sug = {"mb_albumid": REL, "mb_valid": True, "confidence": "high", "album": "X",
               "albumartist": "A", "identity_validated": None, "candidate_identity_error": "",
               "preflight": {"ok": True}, "reason": "AI picked this release."}

        def start_job(fn, **_kw):
            jobs.append(fn)
            return {"ok": True}, 200

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
                    _ai_suggest_folder_internal=mock.Mock(return_value={"ok": True, "suggestion": sug}),
                ):
            app_module.app.test_client().post(
                "/api/library/import-all", headers={"X-Requested-With": "XMLHttpRequest"},
                json={"albums": [{"aldir": "/downloads/x", "artist": "A"}]})
            jobs[0]([])
        reimport.assert_not_called()
        self.assertIn("Artist identity unresolved", queue.call_args.args[2])


class RevalidationKeepsUnresolvedIdentity(unittest.TestCase):
    COMPARISON = {"identity_validated": True, "selected_release_group_id": RG,
                  "representative_release_id": REL, "release_artist": "Synthetic Artist",
                  "comparison": [{"status": "matched"}], "mb_track_count": 1,
                  "local_track_count": 1, "matched_count": 1}

    def _match(self, suggestion, guessed_artist=""):
        item = {"suggestion": suggestion,
                "evidence": {"folder": {"guessed_artist": guessed_artist}}}
        return irs._import_review_build_revalidated_match(item, dict(self.COMPARISON), {})

    def test_unresolved_stays_none_and_is_not_a_rejection(self):
        m = self._match({"identity_validated": None})
        self.assertIsNone(m["identity_validated"])
        self.assertTrue(m["is_importable"])  # a human may still import it
        self.assertEqual(m["preflight_status"], "passed")

    def test_artists_compared_now_resolve_it(self):
        self.assertIs(self._match({"identity_validated": None}, "Synthetic Artist")["identity_validated"], True)
        self.assertIs(self._match({"identity_validated": None}, "Somebody Else")["identity_validated"], False)

    def test_proven_identity_stays_true(self):
        self.assertIs(self._match({"identity_validated": True})["identity_validated"], True)


class EligibilityPreviewKeepsUnresolvedIdentity(unittest.TestCase):
    def _preview_identity(self, value):
        seen = {}

        def preview(payload):
            seen.update(payload)
            return {"safe": True, "tracks_to_import_count": 0}

        with mock.patch.object(isvc, "_cached_import_target_preview", side_effect=preview):
            isvc.evaluate_import_eligibility({"path": "", "selected_match": {"identity_validated": value}})
        return seen["identity_validated"]

    def test_none_is_not_upgraded(self):
        self.assertIsNone(self._preview_identity(None))
        self.assertIs(self._preview_identity(True), True)
        self.assertIs(self._preview_identity(False), False)


class JobPreflightUsesWantedTracks(mtp._Fixture):
    """#574: Album 7 holds tracks 1 and 3; its library folder gains 2, 4 and 5,
    each fingerprinted to its recording. Import All discovery passes this
    fill (test_issue520 RealPreflightForMissingTrackFill); the child job's
    real preflight must pass it too instead of failing the job."""

    def setUp(self):
        super().setUp()
        self.music = Path(self.tmp.name) / "music"
        album_dir = self.music / "Synthetic Artist" / "Some Album"
        album_dir.parent.mkdir(parents=True)
        self.folder.rename(album_dir)
        self.folder = album_dir
        self.paths = {n: str(album_dir / Path(p).name) for n, p in self.paths.items()}
        self.hits = {self.paths[n]: [mtp._hit(mtp.RECS[n - 1])] for n in (2, 4, 5)}

    def _run_job(self):
        result, preflights = {}, []
        real_preflight = ms._folder_release_preflight

        def run_now(fn, label="", metadata=None):
            log = []
            try:
                result.update(status="completed", result=fn(log), log=log)
            except Exception as ex:
                result.update(status="failed", error=str(ex), log=log)
            return types.SimpleNamespace(job_id="job-1")

        def spy_preflight(*a, **k):
            preflights.append(real_preflight(*a, **k))
            return preflights[-1]

        cw = isvc.composite_workflows
        patches = [
            mock.patch.object(ms, "MUSIC_ROOT", self.music),
            mock.patch.object(isvc, "MUSIC_ROOT", self.music),
            mock.patch.object(isvc.jobs, "start_python", side_effect=run_now),
            mock.patch.object(isvc.job_contract, "held", side_effect=lambda *a, **k: contextlib.nullcontext()),
            mock.patch.object(cw, "inspect_import_source", return_value={
                "ok": True, "path": str(self.folder), "audio_file_count": 3}),
            mock.patch.object(isvc, "_pending_review_is_quality_upgrade", return_value=False),
            mock.patch.object(isvc, "_resolve_album_release_for_import", return_value=mtp.REL),
            mock.patch.object(isvc, "_folder_release_preflight", side_effect=spy_preflight),
            # Stop right after the job's preflight: nothing below it may run here.
            mock.patch.object(isvc, "_reimport_log_preflight", side_effect=RuntimeError("stop after preflight")),
        ]
        body = {"aldir": str(self.folder), "mb_albumid": mtp.REL, "existing_album_id": mtp.ALBUM_ID,
                "wanted_tracks": mtp._wanted(2, 4, 5), "skip_import_lock": True,
                "strict_edition_guard": True}
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            resp, code = self._run(isvc.start_reimport_disk, body)
        self.assertEqual(code, 200, resp)
        self.assertEqual(result.get("error"), "stop after preflight", result)
        self.assertEqual(len(preflights), 1, result)
        return preflights[0]

    def test_proven_in_library_fill_passes_the_job_preflight(self):
        pre = self._run_job()
        self.assertTrue(pre["ok"], pre.get("missing_track_proof"))
        self.assertTrue(pre["missing_track_proof"]["ok"])
        # Same verdict Import All discovery reached for this folder.
        discovery = self._run(ms._folder_release_preflight, str(self.folder), mtp.REL,
                              existing_album_id=mtp.ALBUM_ID, wanted_tracks=mtp._wanted(2, 4, 5))
        self.assertEqual(pre["ok"], discovery["ok"])

    def test_unproven_in_library_fill_still_fails_closed(self):
        self.hits[self.paths[2]] = []
        pre = self._run_job()
        self.assertFalse(pre["ok"])
        self.assertEqual(pre["missing_track_proof"]["reason_code"], "file_unverified")


if __name__ == "__main__":
    unittest.main()
