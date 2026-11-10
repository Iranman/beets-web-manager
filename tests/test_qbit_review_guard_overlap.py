"""#568: with ALLOW_TORRENT_SOURCE_MOVE=1, a qBittorrent Review row protects
its folder, the folders inside it (``CD 01``) and the folders above it: a
Review import of any of them copies, and reimport-disk copies too. Unrelated
folders (a sibling with a longer name included) still move."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.import_service as isvc
import backend.library_service as libsvc
import backend.pending_review_store as prs

try:
    import test_import_paths_beets_native as native
    import test_review_partial_import_torrent_preserve as preserve
except ImportError:  # pragma: no cover
    from tests import test_import_paths_beets_native as native
    from tests import test_review_partial_import_torrent_preserve as preserve

QBIT = {"source_mode": "qbittorrent"}


class QbitRowOverlapFolderImportTests(preserve.QbitReviewRowWholeFolderImportTests):
    """The imported folder is ``self.src``; the qBittorrent row sits above,
    inside or beside it."""

    def test_folder_inside_qbit_row_is_copied(self):
        # The row is the torrent; the import is one of its disc folders.
        self.rows = [{"path": str(self.src.parent), "evidence": QBIT}]
        before = self._run()
        self._assert_copied_and_untouched(before)
        self.assertEqual((self.src / "02 - Two.flac").read_bytes(), b"torrent-bytes-2")

    def test_parent_of_qbit_row_is_copied(self):
        cd = self.src / "CD 01"
        cd.mkdir()
        self.rows = [{"path": str(cd), "evidence": QBIT}]
        before = self._run()
        self._assert_copied_and_untouched(before)

    def test_sibling_qbit_row_does_not_protect(self):
        sibling = self.src.parent / (self.src.name + " 2")
        sibling.mkdir()
        self.rows = [{"path": str(sibling), "evidence": QBIT}]
        self._run()
        self.assertEqual([p["use_move"] for p in self.plan_payloads], [True])

    test_qbittorrent_row_is_copied_and_kept_with_move_allowed = None
    test_row_without_qbittorrent_origin_still_moves = None
    test_unreadable_queue_copies = None


class QbitRowReimportDiskTests(native.ConfirmedImportJobTests):
    """reimport-disk of a downloads folder with ALLOW_TORRENT_SOURCE_MOVE=1."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.torrent = Path(tmp.name) / "Torrent"
        (self.torrent / "CD 01").mkdir(parents=True)
        self.queue = Path(tmp.name) / "ai_pending.json"
        self.validate = mock.MagicMock()
        for target, name, kw in (
            (prs, "_AI_PENDING_FILE", {"new": self.queue}),
            (libsvc, "TORRENT_SOURCE_MOVE_ALLOWED", {"new": True}),
            (isvc, "_validate_import_source_audio", {"new": self.validate}),
        ):
            patcher = mock.patch.object(target, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _reimport(self, aldir, rows):
        self.queue.write_text(json.dumps(rows))
        self.ad = native._adapter({"id": 9, "mb_albumid": native.REL, "mb_releasegroupid": native.RG})
        with mock.patch.object(isvc.composite_workflows, "inspect_import_source",
                               return_value={"ok": True, "path": str(aldir), "audio_file_count": 2}), \
             mock.patch.object(isvc, "_resolve_album_release_for_import", return_value=native.REL):
            body, code = isvc.start_reimport_disk(
                {"aldir": str(aldir), "mb_albumid": native.REL, "skip_import_lock": True})
        self.assertEqual(code, 200, body)
        self.assertEqual(self.result["status"], "completed", self.result)
        kw = self.ad.run_import.call_args.kwargs
        return (kw["copy"], kw["move"]), self.validate.call_args.kwargs.get("preserve_source")

    def test_qbit_row_folder_is_copied(self):
        self.assertFalse(isvc._preserve_torrent_source_path(self.torrent))  # the flag alone would move
        mode, preserve_source = self._reimport(self.torrent, [{"path": str(self.torrent), "evidence": QBIT}])
        self.assertEqual(mode, (True, False))
        self.assertIs(preserve_source, True)

    def test_disc_folder_and_parent_of_qbit_row_are_copied(self):
        row = [{"path": str(self.torrent), "evidence": QBIT}]
        self.assertEqual(self._reimport(self.torrent / "CD 01", row)[0], (True, False))
        self.assertEqual(self._reimport(self.torrent.parent, row)[0], (True, False))

    def test_unrelated_folder_still_moves(self):
        mode, preserve_source = self._reimport(self.torrent, [{"path": str(self.torrent), "evidence": {}}])
        self.assertEqual(mode, (False, True))
        self.assertIsNone(preserve_source)

    def test_unreadable_queue_copies(self):
        with mock.patch.object(libsvc, "_pending_review_torrent_rows",
                               side_effect=prs.PendingReviewLoadError("unreadable")):
            mode, _ = self._reimport(self.torrent, [])
        self.assertEqual(mode, (True, False))

    test_reimport_disk_keeps_the_album_and_verifies_the_release_group = None
    test_reimport_disk_verification_mismatch_keeps_rows_and_fails = None
    test_import_with_id_keeps_the_album_and_verifies_the_release_group = None
    test_import_with_id_verification_mismatch_keeps_rows_and_fails = None


if __name__ == "__main__":
    unittest.main()
