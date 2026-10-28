"""ARCH-030 C1: the download-time identity gate must DEFER a genuine AcoustID
NO_RESULT (and an UNAVAILABLE lookup) to the downstream DETERMINISTIC_METADATA_
SLOT advancer gate instead of DELETING it before it can ever reach that gate.

The defect: for a NO_RESULT recovery file (the 112 "Forever" intro/outro case)
``_validate_wanted_download_identity_before_import`` set ``match["ok"]=False``
-> ``best_idx<0`` -> ``delete_file`` removed the file, the staged folder became
empty, and the slot tier was unreachable in the real flow -- the feature could
not complete end to end for its own anchor case.

These tests drive the REAL validator (and the real download job for the
integration case), not the engine directly and not a pre-staged folder, which
is the test gap QA named. A CONFLICT / wrong-audio file must still be deleted.

Synthetic AcoustID identity only; no network, no Beets.
"""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

acq = recovery_carrier = recovery_service = None

RGID = "17a64abf-6d31-473f-a740-38ffa548f225"
RELEASE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def setUpModule():
    # Deferred import: match the acquisition test family's app-load ordering.
    global acq, recovery_carrier, recovery_service
    import app  # noqa: F401  -- loads the module family in order
    import backend.acquisition_service as acq
    import backend.recovery_carrier as recovery_carrier
    import backend.recovery_service as recovery_service


def _identity_match(acoustid_status, *, ok=False, final_action="review", score=0.0,
                    title_ok=True):
    """A _playlist_download_match result carrying a specific acoustid_status."""
    def match(path_value, _artist, title, _mbid=""):
        return {
            "ok": ok,
            "title_score": 1.0 if title_ok else 0.0,
            "artist_score": 1.0,
            "identity": {
                "final_action": "accept" if ok else final_action,
                "acoustid_status": acoustid_status,
                "acoustid_match_score": score,
            },
        }
    return match


class ValidatorClassification(unittest.TestCase):
    """Unit: the per-file classify-and-defer logic in the real validator."""

    TRACK = {"title": "Last Song", "track": 6, "mb_trackid": "rec-6"}

    def _patch(self, matcher, delete_fn):
        patches = [
            mock.patch.object(acq, "_playlist_download_match", side_effect=matcher),
            mock.patch.object(acq, "_playlist_download_audio_allowed", return_value=True),
            mock.patch.object(acq, "_playlist_stamp_download_tags"),
            mock.patch.object(acq, "_playlist_identity_log", side_effect=lambda m: str(m)),
            mock.patch.object(acq.composite_workflows, "delete_file", side_effect=delete_fn),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])

    def _one_file(self, name="Last Song.flac"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        f = Path(tmp.name) / name
        f.write_bytes(b"x")
        return tmp.name, f

    def test_no_result_file_is_deferred_not_deleted(self):
        deleted = []
        self._patch(_identity_match("no_result"), deleted.append)
        tmp, f = self._one_file()
        verified, undelivered, _deferred = acq._validate_wanted_download_identity_before_import(
            tmp, [self.TRACK], [])
        self.assertEqual(verified, [])
        self.assertEqual([t["title"] for t in undelivered], ["Last Song"])
        self.assertTrue(f.exists(), "NO_RESULT file must survive staging for the slot gate")
        self.assertEqual(deleted, [], "NO_RESULT file must never be deleted at the download gate")

    def test_unavailable_lookup_file_is_retained_not_deleted(self):
        deleted = []
        self._patch(_identity_match("failed"), deleted.append)
        tmp, f = self._one_file()
        verified, undelivered, _deferred = acq._validate_wanted_download_identity_before_import(
            tmp, [self.TRACK], [])
        self.assertEqual(verified, [])
        self.assertTrue(f.exists(), "UNAVAILABLE file must be retained (never auto-import, never delete)")
        self.assertEqual(deleted, [])

    def test_conflict_file_is_deleted_and_fails_closed(self):
        deleted = []

        def _del(p):
            deleted.append(p)
            os.remove(p)

        self._patch(_identity_match("mismatch", score=0.95), _del)
        tmp, f = self._one_file("Wrong Song.flac")
        with self.assertRaisesRegex(RuntimeError, "did not fingerprint-verify"):
            acq._validate_wanted_download_identity_before_import(tmp, [self.TRACK], [])
        self.assertEqual(len(deleted), 1, "a contradicted (wrong-audio) file must still be deleted")
        self.assertFalse(f.exists())

    def test_mixed_batch_imports_accepted_and_defers_no_result(self):
        deleted = []

        def matcher(path_value, _artist, title, _mbid=""):
            stem = Path(path_value).stem
            if stem == "Alpha" and title == "Alpha":
                return {"ok": True, "title_score": 1.0, "artist_score": 1.0,
                        "identity": {"final_action": "accept", "acoustid_status": "confirmed",
                                     "acoustid_match_score": 0.95}}
            return {"ok": False, "title_score": 1.0 if stem == title else 0.0, "artist_score": 1.0,
                    "identity": {"final_action": "review", "acoustid_status": "no_result",
                                 "acoustid_match_score": 0.0}}

        def _del(p):
            deleted.append(p)
            os.remove(p)

        self._patch(matcher, _del)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        alpha = Path(tmp.name) / "Alpha.flac"
        last = Path(tmp.name) / "Last Song.flac"
        alpha.write_bytes(b"x")
        last.write_bytes(b"x")
        verified, undelivered, _deferred = acq._validate_wanted_download_identity_before_import(
            tmp.name,
            [{"title": "Alpha", "track": 1, "mb_trackid": "rec-1"}, self.TRACK],
            [],
        )
        self.assertEqual([t["title"] for t in verified], ["Alpha"])
        self.assertEqual([t["title"] for t in undelivered], ["Last Song"])
        self.assertTrue(last.exists(), "NO_RESULT file in a mixed batch must survive for the slot gate")
        self.assertEqual(deleted, [], "no delete in a verified+NO_RESULT mixed batch")


class NoResultReachesSlotTierViaDownloadJob(unittest.TestCase):
    """Integration: the real download -> _validate -> stage -> advance path.

    A NO_RESULT recovery file must SURVIVE staging, the inline Beets import must
    NOT run on it, the carrier must land in an advancer state with the file
    still on disk, and the slot-tier entrypoint (_first_staged_folder_with_audio
    -- the exact function the defect reduced to "") must now see the folder.
    """

    PAYLOAD = {
        "artist": "Artist", "album": "Album", "year": "2024", "method": "ytdlp",
        "auto_import": True, "existing_album_id": 42, "mb_releasegroupid": RGID,
        "missing_tracks": [{"track": 6, "title": "Last Song", "recording_id": "rec-6"}],
    }

    def _run(self, matcher, *, forbid_delete):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        downloads = Path(tmp.name, "downloads").resolve()
        downloads.mkdir(parents=True, exist_ok=True)
        state_dir = Path(tmp.name, "carrier_state")

        def _fake_ytdlp(artist, album, year, yt_dir, log, tracks=None, *a, **k):
            Path(yt_dir).mkdir(parents=True, exist_ok=True)
            (Path(yt_dir) / "Last Song.flac").write_bytes(b"x")
            return 1

        deleted = []

        def _del(p):
            if forbid_delete:
                raise AssertionError(f"NO_RESULT file must not be deleted: {p}")
            deleted.append(p)
            try:
                os.remove(p)
            except OSError:
                pass

        env = dict(os.environ)
        env["RECOVERY_CARRIER_STATE_DIR"] = str(state_dir)

        reimport = mock.Mock(return_value="imp-1")
        patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(acq, "jobs"),
            mock.patch.object(acq, "validated_downloads_root", return_value=downloads),
            mock.patch.object(acq, "slskd_configured", return_value=False),
            mock.patch.object(acq, "_resolve_album_release_for_import", return_value=RELEASE),
            mock.patch.object(acq, "_fetch_mb_release_tracklist",
                              return_value={"ok": True, "tracks": [1, 2, 3, 4, 5, 6],
                                            "release_group": RGID}),
            mock.patch.object(acq, "_wanted_tracks_not_in_album",
                              return_value=[{"disc": 1, "track": 6, "title": "Last Song",
                                             "mb_trackid": "rec-6", "duration_ms": 190000}]),
            mock.patch.object(acq, "_album_folder_for_album_id", return_value="/music/Artist/Album"),
            mock.patch.object(acq, "_ytdlp_missing_tracks_download", side_effect=_fake_ytdlp),
            mock.patch.object(acq, "_ytdlp_album_download", side_effect=_fake_ytdlp),
            mock.patch.object(acq, "_playlist_download_match", side_effect=matcher),
            mock.patch.object(acq, "_playlist_download_audio_allowed", return_value=True),
            mock.patch.object(acq, "_playlist_stamp_download_tags"),
            mock.patch.object(acq, "_playlist_identity_log", side_effect=lambda m: str(m)),
            mock.patch.object(acq, "_validate_import_source_audio"),
            mock.patch.object(acq, "_start_reimport_disk_job_internal", reimport),
            mock.patch.object(acq, "_wait_for_child_job", return_value={"ok": True}),
            mock.patch.object(acq, "acquisition_queue_mark_done"),
            mock.patch.object(acq.composite_workflows, "delete_file", side_effect=_del),
        ]
        started = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        jobs = started[1]
        jobs.start_python.return_value.job_id = "job-xyz"

        body, status = acq.start_album_download(self.PAYLOAD)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"), body)

        cid = jobs.start_python.call_args.kwargs["metadata"]["recovery_carrier_id"]
        job_fn = jobs.start_python.call_args[0][0]
        log = []
        error = None
        result = None
        try:
            result = job_fn(log, threading.Event())
        except Exception as ex:  # CONFLICT fails closed; capture rather than fail the run
            error = ex
        return {
            "cid": cid, "result": result, "error": error, "log": log,
            "reimport": reimport, "deleted": deleted, "downloads": downloads,
        }

    def test_no_result_file_survives_and_reaches_slot_tier(self):
        out = self._run(_identity_match("no_result"), forbid_delete=True)

        # The inline Beets import never ran on unverified audio -- it is deferred.
        out["reimport"].assert_not_called()
        self.assertIsNone(out["error"], out["log"])
        self.assertTrue(out["result"], out["log"])
        self.assertIs(out["result"].get("imported"), False)
        self.assertTrue(out["result"].get("staged_for_slot_gate"), out["result"])

        # The NO_RESULT file SURVIVES on disk in the staged download tree.
        survivors = list(out["downloads"].rglob("Last Song.flac"))
        self.assertTrue(survivors, "NO_RESULT download must survive staging")

        # The carrier advanced to an advancer state, STAGED checkpoint reached.
        carrier = recovery_carrier.get_carrier(out["cid"])
        self.assertIsNotNone(carrier)
        self.assertIn(carrier["state"], recovery_carrier.ADVANCER_STATES)

        # The slot-tier entrypoint the defect reduced to "" now sees the folder
        # that still holds the NO_RESULT file -> the DETERMINISTIC_METADATA_SLOT
        # advancer gate is reachable end to end.
        folder = recovery_service._first_staged_folder_with_audio(carrier)
        self.assertTrue(folder, "the slot-tier folder resolver must find the surviving staged audio")
        self.assertTrue(list(Path(folder).glob("Last Song.flac")))

    def test_conflict_wrong_audio_is_still_deleted(self):
        out = self._run(_identity_match("mismatch", score=0.95), forbid_delete=False)

        # Wrong-audio is deleted at the gate and the inline import never runs.
        out["reimport"].assert_not_called()
        self.assertTrue(out["deleted"], "a contradicted wrong-audio file must still be deleted")
        self.assertFalse(list(out["downloads"].rglob("Last Song.flac")),
                         "the contradicted file must not survive staging")


if __name__ == "__main__":
    unittest.main()
