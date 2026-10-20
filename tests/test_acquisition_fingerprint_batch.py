"""Requested-track fingerprint verification for acquisition downloads.

One unverified file must not discard the batch: verified tracks import,
unverified ones are dropped from staging and reported as still missing.
Synthetic AcoustID data only; no network, no Beets.
"""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import backend.acquisition_service as acq
from backend.acoustid_service import _audio_identity_decision

REC = "33333333-3333-3333-3333-333333333333"
OTHER = "66666666-6666-6666-6666-666666666666"
REL = "11111111-1111-1111-1111-111111111111"

TRACKS = [
    {"title": "Alpha", "track": 1, "mb_trackid": "aaaaaaaa-0000-0000-0000-000000000001"},
    {"title": "Beta", "track": 2, "mb_trackid": "aaaaaaaa-0000-0000-0000-000000000002"},
    {"title": "Gamma", "track": 3, "mb_trackid": "aaaaaaaa-0000-0000-0000-000000000003"},
]


def _fake_match(verified_stems):
    """Accept a file only for the track named by its stem, if verified."""
    def match(path_value, _artist, title, _mbid=""):
        stem = Path(path_value).stem
        ok = stem in verified_stems and stem == title
        action = "accept" if ok else "review"
        return {"ok": ok, "title_score": 1.0 if stem == title else 0.0, "artist_score": 1.0,
                "identity": {"final_action": action, "acoustid_match_score": 0.9}}
    return match


def _delete(path_value):
    os.remove(path_value)


class _Patched(unittest.TestCase):
    def _patch(self, verified_stems):
        patches = [
            mock.patch.object(acq, "_playlist_download_match", side_effect=_fake_match(verified_stems)),
            mock.patch.object(acq, "_playlist_download_audio_allowed", return_value=True),
            mock.patch.object(acq, "_playlist_stamp_download_tags"),
            mock.patch.object(acq, "_playlist_identity_log", side_effect=lambda m: str(m.get("identity"))),
            mock.patch.object(acq.composite_workflows, "delete_file", side_effect=_delete),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])

    @staticmethod
    def _files(folder, stems):
        for stem in stems:
            (Path(folder) / f"{stem}.flac").write_bytes(b"x")


class ValidatorMixedBatch(_Patched):
    def test_mixed_batch_keeps_verified_and_reports_rejected(self):
        self._patch({"Alpha", "Beta"})
        with tempfile.TemporaryDirectory() as tmp:
            self._files(tmp, ["Alpha", "Beta", "Gamma"])
            log = []
            verified, undelivered = acq._validate_wanted_download_identity_before_import(tmp, TRACKS, log)
            self.assertEqual([t["title"] for t in verified], ["Alpha", "Beta"])
            self.assertEqual([t["title"] for t in undelivered], ["Gamma"])
            # Only verified audio is left for Beets to import.
            self.assertEqual(sorted(os.listdir(tmp)), ["Alpha.flac", "Beta.flac"])
            self.assertTrue(any("still missing" in line for line in log), log)

    def test_nothing_verified_still_fails(self):
        self._patch(set())
        with tempfile.TemporaryDirectory() as tmp:
            self._files(tmp, ["Alpha", "Beta"])
            with self.assertRaisesRegex(RuntimeError, "did not fingerprint-verify"):
                acq._validate_wanted_download_identity_before_import(tmp, TRACKS, [])


class DownloadJobMixedBatch(_Patched):
    """Through the real start_album_download job: a mixed batch imports."""

    def test_verified_tracks_continue_to_import(self):
        self._patch({"Alpha", "Beta"})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()

            def download(_artist, _album, _year, dest, _log, tracks, source="ytdlp"):
                Path(dest).mkdir(parents=True, exist_ok=True)
                self._files(dest, [t["title"] for t in tracks])
                return len(tracks)

            with mock.patch.object(acq, "validated_downloads_root", return_value=root), \
                    mock.patch.object(acq, "jobs") as jobs, \
                    mock.patch.object(acq, "_resolve_album_release_for_import", return_value=REL), \
                    mock.patch.object(acq, "_fetch_mb_release_tracklist", return_value={"ok": False}), \
                    mock.patch.object(acq, "_ytdlp_missing_tracks_download", side_effect=download), \
                    mock.patch.object(acq, "_validate_import_source_audio"), \
                    mock.patch.object(acq, "_start_reimport_disk_job_internal", return_value="child") as reimport, \
                    mock.patch.object(acq, "_wait_for_child_job", return_value={"album_ids": [7]}), \
                    mock.patch.object(acq, "acquisition_queue_mark_done"):
                body, _ = acq.start_album_download({
                    "artist": "Artist", "album": "Album", "method": "ytdlp",
                    "missing_tracks": TRACKS,
                })
                self.assertTrue(body["ok"], body)
                job_fn = jobs.start_python.call_args[0][0]
                log = []
                result = job_fn(log, threading.Event())

            reimport.assert_called_once()
            imported = reimport.call_args.kwargs["wanted_tracks"]
            self.assertEqual([t["title"] for t in imported], ["Alpha", "Beta"])
            self.assertTrue(result["imported"], log)
            self.assertEqual([t["title"] for t in result["undelivered_tracks"]], ["Gamma"])
            self.assertTrue(any("still missing" in str(line) and "Gamma" in str(line) for line in log), log)


class RequestedRecordingAcoustIDDecision(unittest.TestCase):
    """The file-level decision the downloads use (_audio_identity_decision)."""

    def _decide(self, candidates):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.flac"
            path.write_bytes(b"x")
            return _audio_identity_decision(
                str(path), expected_artist="21 Savage", expected_title="redrum",
                expected_mb_trackid=REC, acoustid_candidates=candidates)

    @staticmethod
    def _c(rid, score, title):
        return {"mb_trackid": rid, "score": score, "title": title, "artist": "21 Savage"}

    def test_requested_recording_top_among_several_is_accepted(self):
        out = self._decide([self._c(OTHER, 0.95, "née-nah"), self._c(REC, 0.95, "redrum")])
        self.assertEqual(out["final_action"], "accept", out)
        self.assertEqual(out["mb_recording_id_candidate"], REC)

    def test_requested_recording_present_but_not_top_is_not_accepted(self):
        out = self._decide([self._c(OTHER, 0.97, "née-nah"), self._c(REC, 0.95, "redrum")])
        self.assertNotEqual(out["final_action"], "accept", out)

    def test_no_result_is_not_accepted(self):
        out = self._decide([])
        self.assertNotEqual(out["final_action"], "accept", out)
        self.assertEqual(out["acoustid_status"], "no_result")


if __name__ == "__main__":
    unittest.main()
