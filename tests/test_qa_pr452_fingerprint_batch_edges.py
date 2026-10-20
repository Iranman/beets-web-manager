"""QA edge cases for PR #452: mixed-batch requested-track verification.

Synthetic data only; no network, no Beets.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

acq = None


def setUpModule():
    # Deferred: test_ai_batch_retry_race must be the first to import app.
    global acq
    import app  # noqa: F401
    import backend.acquisition_service as acq

TRACKS = [
    {"title": "Alpha", "track": 1, "mb_trackid": "aaaaaaaa-0000-0000-0000-000000000001"},
    {"title": "Beta", "track": 2, "mb_trackid": "aaaaaaaa-0000-0000-0000-000000000002"},
]


def _match(verified):
    def match(path_value, _artist, title, _mbid=""):
        stem = Path(path_value).stem
        ok = stem in verified and stem == title
        return {"ok": ok, "title_score": 1.0 if stem == title else 0.0, "artist_score": 1.0,
                "identity": {"final_action": "accept" if ok else "review", "acoustid_match_score": 0.9}}
    return match


class _Base(unittest.TestCase):
    def _run(self, stems, verified, *, allowed=lambda p: True, delete=os.remove):
        patches = [
            mock.patch.object(acq, "_playlist_download_match", side_effect=_match(verified)),
            mock.patch.object(acq, "_playlist_download_audio_allowed",
                              side_effect=lambda p, _log: allowed(p)),
            mock.patch.object(acq, "_playlist_stamp_download_tags"),
            mock.patch.object(acq, "_playlist_filter_preview_downloads", side_effect=lambda paths, _log: list(paths)),
            mock.patch.object(acq, "_playlist_identity_log", side_effect=lambda m: "x"),
            mock.patch.object(acq.composite_workflows, "delete_file", side_effect=delete),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        for s in stems:
            (Path(tmp) / f"{s}.flac").write_bytes(b"x")
        log = []
        return tmp, log


class FormatRejectedFile(_Base):
    def test_format_rejected_verified_file_leaves_track_missing(self):
        tmp, log = self._run(["Alpha", "Beta"], {"Alpha", "Beta"},
                             allowed=lambda p: Path(p).stem != "Beta")
        verified, undelivered = acq._validate_wanted_download_identity_before_import(tmp, TRACKS, log)
        self.assertEqual([t["title"] for t in verified], ["Alpha"])
        self.assertEqual([t["title"] for t in undelivered], ["Beta"])
        self.assertEqual(os.listdir(tmp), ["Alpha.flac"])


class DuplicateFileForOneTrack(_Base):
    def test_second_copy_of_verified_track_is_dropped_not_double_counted(self):
        def match(path_value, _artist, title, _mbid=""):
            ok = title == "Alpha"
            return {"ok": ok, "title_score": 1.0 if ok else 0.0, "artist_score": 1.0,
                    "identity": {"final_action": "accept" if ok else "review"}}
        tmp, log = self._run(["Alpha", "Alpha (1)"], set())
        with mock.patch.object(acq, "_playlist_download_match", side_effect=match):
            verified, undelivered = acq._validate_wanted_download_identity_before_import(tmp, TRACKS, log)
        self.assertEqual([t["title"] for t in verified], ["Alpha"])
        self.assertEqual([t["title"] for t in undelivered], ["Beta"])
        self.assertEqual(len(os.listdir(tmp)), 1)


class StagingDeleteFailure(_Base):
    def test_unverified_file_never_left_for_import_when_delete_fails(self):
        def refuse(_path):
            raise ValueError("Refusing to delete outside staging roots")
        tmp, log = self._run(["Alpha", "Beta"], {"Alpha"}, delete=refuse)
        try:
            acq._validate_wanted_download_identity_before_import(tmp, TRACKS, log)
        except RuntimeError:
            return  # fail closed: acceptable
        # If the validator returns, no unverified audio may remain for Beets.
        self.assertEqual(sorted(os.listdir(tmp)), ["Alpha.flac"],
                         "unverified Beta.flac is still staged for import after a failed delete")


if __name__ == "__main__":
    unittest.main()
