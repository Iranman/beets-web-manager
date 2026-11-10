"""QA regression (N-1): a partial Review import of a qBittorrent-origin row
with ALLOW_TORRENT_SOURCE_MOVE=1 copies and keeps every torrent file, also a
selected file that Music Format Preferences reject (never deleted or
quarantined)."""
import json
import unittest
from unittest import mock

import backend.import_service as isvc
import backend.library_service as libsvc
import backend.pending_review_store as prs
from tests import test_review_partial_import_torrent_preserve as base

REL, RG = base.REL, base.RG

_REAL_FILTER = isvc._filter_import_review_selected_audio_files


class QbitPartialRejectedFileTests(base.ReviewPartialImportSourceSafetyTests):
    def setUp(self):
        super().setUp()
        accepted = self.src / "02 - Two.flac"
        rows = [{"path": str(self.src), "evidence": {"source_mode": "qbittorrent"}}]
        for target, name, kw in (
            (isvc, "_folder_import_selected_sources", {"side_effect": lambda f, p: ([self.selected, accepted], None)}),
            (prs, "_AI_PENDING_FILE", {"new": self.src.parent / "ai_pending.json"}),
            (isvc, "_app_managed_download_path", {"return_value": False}),
            (libsvc, "TORRENT_SOURCE_MOVE_ALLOWED", {"new": True}),
            (isvc, "_music_format_preferences", {"return_value": {"rejected_download_handling": "delete"}}),
            (isvc, "_validate_audio_file_preferences",
             {"side_effect": lambda p, prefs: {"ok": not p.endswith("One.flac"), "message": "Rejected download"}}),
            (isvc, "_audio_validation_inspection_failed", {"return_value": False}),
            (isvc, "_filter_import_review_selected_audio_files", {"new": _REAL_FILTER}),
            (isvc, "_preserve_torrent_source_path", {"new": libsvc._preserve_torrent_source_path}),
        ):
            p = mock.patch.object(target, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        prs._AI_PENDING_FILE.write_text(json.dumps(rows))

    def test_rejected_selected_torrent_file_is_kept(self):
        self.assertFalse(libsvc._preserve_torrent_source_file(self.selected))  # the flag alone would delete
        body, code = isvc.start_folder_import_with_id(
            {"path": str(self.src), "mb_albumid": REL, "mb_releasegroupid": RG, "move": True})
        self.assertEqual(code, 200, body)
        self.assertEqual(self.result.get("status"), "completed", self.result)
        self.assertEqual(self.selected.read_bytes(), b"torrent-bytes-1", self.result.get("log"))
        self.assertEqual([p["use_move"] for p in self.plan_payloads], [False])

    # The parent's cases are covered in its own module.
    test_preserved_torrent_source_is_copied_and_kept = None
    test_hardlinked_file_is_copied_and_kept = None
    test_preserve_rule_raising_fails_closed_to_copy = None
    test_app_staging_source_is_still_moved_and_cleaned = None
    test_rule_unit = None


if __name__ == "__main__":
    unittest.main()
