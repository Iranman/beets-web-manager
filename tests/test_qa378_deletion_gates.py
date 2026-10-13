"""Independent QA for PR #378 (0.5.1 deletion gates): edge cases the
implementer's suite does not cover."""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

try:
    import test_deletion_gates_051 as _gates
    import test_sec002_app_path_import_review_cleanup as _route_mod
except ImportError:  # pragma: no cover
    from tests import test_deletion_gates_051 as _gates
    from tests import test_sec002_app_path_import_review_cleanup as _route_mod

import backend.library_service as ls

_Src = _gates.SourceDeletionProofGateTests
_Route = _route_mod.ImportReviewCleanupPathBoundaryTests


class SourceProofEdgeQATests(unittest.TestCase):
    setUp = _Src.setUp
    _run = _Src._run
    _fingerprint_all = staticmethod(_Src._fingerprint_all)

    def test_byte_proof_never_deletes_a_folder_inside_the_library(self):
        # A "source" that is another folder of the music library, byte-identical
        # to the target album, is proven but must still never be deleted.
        src = ls.MUSIC_ROOT / "Other" / "Copy"
        src.mkdir(parents=True)
        for item in self.items:
            (src / Path(item["path"]).name).write_bytes(Path(item["path"]).read_bytes())
        self.src = src
        ev, _ = self._run()
        self.assertTrue(ev["proven"])
        self.assertFalse(ev["deleted"])
        self.assertTrue(any(src.iterdir()))
        self.assertEqual(self.deleted, [])

    def test_album_lookup_failure_keeps_the_folder(self):
        with mock.patch.object(ls.composite_workflows, "get_album", side_effect=RuntimeError("beets down")):
            ev, _ = self._run(self._fingerprint_all)
        self.assertEqual((ev["deleted"], ev["reason"]), (False, "evidence_unavailable: RuntimeError"))
        self.assertTrue(self.src.exists())

    def test_no_album_id_keeps_the_folder(self):
        log = []
        with mock.patch.object(ls, "same_recording_proof", side_effect=self._fingerprint_all):
            ev = ls._delete_source_if_proven_in_library(str(self.src), 0, _gates.RG, log)
        self.assertFalse(ev["deleted"])
        self.assertTrue(self.src.exists())

    def test_missing_source_folder_is_not_an_error(self):
        log = []
        ev = ls._delete_source_if_proven_in_library(str(self.src / "gone"), 7, _gates.RG, log)
        self.assertFalse(ev["deleted"])
        self.assertTrue(ev["reason"].startswith("evidence_unavailable"))

    def test_os_junk_file_keeps_the_folder(self):
        # Known, accepted (security N5): fail closed on unlisted files.
        (self.src / ".DS_Store").write_bytes(b"junk")
        ev, _ = self._run(self._fingerprint_all)
        self.assertEqual((ev["deleted"], ev["reason"]), (False, "unrecognised_file"))

    def test_release_group_comparison_ignores_case_and_whitespace(self):
        ev, _ = self._run(self._fingerprint_all, rg=" " + _gates.RG.upper() + " ")
        self.assertTrue(ev["deleted"])

    def test_proven_folder_without_direct_audio_is_not_deleted(self):
        # Leaf rule: a parent holding disc subfolders is never deleted.
        cd1 = self.src / "CD1"
        cd1.mkdir()
        for f in list(self.src.glob("*.flac")):
            f.rename(cd1 / f.name)
        ev, _ = self._run(self._fingerprint_all)
        self.assertTrue(ev["proven"])
        self.assertFalse(ev["deleted"])
        self.assertTrue(cd1.exists())


@unittest.skipIf(os.name == "nt", "POSIX symlinks")
class ReviewFolderDeleteRealPathQATests(unittest.TestCase):
    setUp = _Route.setUp
    tearDown = _Route.tearDown
    _write_audio = _Route._write_audio
    _write_pending = _Route._write_pending

    def test_pending_path_symlinked_into_the_library_is_refused(self):
        lib_album = self.music / "Artist" / "Album"
        self._write_audio(lib_album / "01.flac")
        (self.downloads / "link").symlink_to(self.music / "Artist")
        via_link = self.downloads / "link" / "Album"
        self._write_pending(via_link)
        resp = self.client.post("/api/import/review-folder/delete", json={"path": str(via_link)})
        self.assertEqual(resp.status_code, 400, resp.get_json())
        self.assertTrue((lib_album / "01.flac").exists())

    def test_missing_pending_file_refuses_every_delete(self):
        # Before the fix an absent queue file skipped the Pending Review check.
        folder = self.downloads / "album"
        self._write_audio(folder / "01.flac")
        self.pending_file.unlink()
        resp = self.client.post("/api/import/review-folder/delete", json={"path": str(folder)})
        self.assertEqual(resp.status_code, 400, resp.get_json())
        self.assertTrue((folder / "01.flac").exists())

    def test_empty_path_is_refused(self):
        for body in ({}, {"path": ""}, [], "x"):
            with self.subTest(body=body):
                resp = self.client.post("/api/import/review-folder/delete", json=body)
                self.assertEqual(resp.status_code, 400)

    def test_folder_stats_reports_library_membership_from_server(self):
        album = self._write_audio(self.music / "A" / "B" / "01.flac").parent
        resp = self.client.get("/api/import/folder-stats", query_string={"path": str(album)})
        self.assertIs(resp.get_json().get("is_library_path"), True, resp.get_json())

    def test_clear_pending_with_no_body_keeps_the_queue(self):
        folder = self.downloads / "album"
        self._write_pending(folder)
        before = json.loads(self.pending_file.read_text())
        for kw in ({}, {"json": {}}, {"json": {"all": 1}}):
            with self.subTest(kw=kw):
                resp = self.client.delete("/api/ai-pending-review", **kw)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(json.loads(self.pending_file.read_text()), before)


# Borrowed only for setUp/helpers; keep the loader from running them twice.
del _Src, _Route


if __name__ == "__main__":
    unittest.main()
