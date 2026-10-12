"""#298 N6: Clean All's release-group merge re-planned the same blocked
issues every batch (101 batches re-proposing 3 empty folders blocked with
"Release Group identity is required"). An issue is tried once per run, and an
empty folder is planned Safe only when the apply's identity gate passes."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.cleanup_service as cs


class ReleaseGroupMergeLoopTests(unittest.TestCase):
    def test_a_blocked_safe_issue_is_tried_once(self):
        issue = {"id": "i1", "safe": True, "artist": "A", "album": "B", "proposed_action": "Remove empty album folder"}
        plans = mock.Mock(return_value={"issues": [issue], "summary": {}})
        applied = mock.Mock(return_value={"status": "Blocked",
                                          "blocking_reasons": ["Release Group identity is required before album cleanup."]})
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(cs, "_album_folder_cleanup_plan", plans), \
                mock.patch.object(cs, "_album_cleanup_apply_issue", applied), \
                mock.patch.object(cs, "METADATA_CACHE_ROOT", Path(td)):
            report = cs._album_folder_cleanup_apply_safe(Path(td), [])
        self.assertEqual(applied.call_count, 1)
        self.assertEqual(report["batches"], 1)

    def test_empty_folder_without_release_group_is_not_planned_safe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Artist" / "Empty Album").mkdir(parents=True)
            with mock.patch.object(cs, "_album_cleanup_db_index", return_value={}):
                plan = cs._album_folder_cleanup_plan(root)
        empties = [i for i in plan["issues"] if "Empty Album" in str(i.get("current_folders"))]
        self.assertTrue(empties, plan["issues"])
        self.assertFalse(any(i["safe"] for i in empties), empties)


if __name__ == "__main__":
    unittest.main()
