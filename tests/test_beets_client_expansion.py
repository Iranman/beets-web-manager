"""Unit tests for BeetsClient expansion and helper methods (Milestone 3 / Wave 29)."""

import unittest
from unittest.mock import MagicMock, patch

from backend.beets_client import BeetsClient, BeetsError


class TestBeetsClientExpansion(unittest.TestCase):
    def setUp(self):
        self.client = BeetsClient(base_url="http://127.0.0.1:8338", token="test-token")

    def test_plan_folder_cleanup_with_keyword_args(self):
        with patch.object(self.client, "_request", return_value={"ok": True, "operation_id": "op-123"}) as mock_req:
            res = self.client.plan_folder_cleanup(
                action="safe_rename",
                source_path="/music/Old Artist",
                target_path="/music/New Artist",
                preview_token="pt-abc",
            )
            self.assertTrue(res["ok"])
            mock_req.assert_called_once_with(
                "POST",
                "/folders/cleanup/plan",
                {
                    "action": "safe_rename",
                    "source_path": "/music/Old Artist",
                    "target_path": "/music/New Artist",
                    "preview_token": "pt-abc",
                },
                timeout=30.0,
            )

    def test_apply_folder_cleanup_with_confirmed(self):
        with patch.object(self.client, "_request", return_value={"ok": True, "status": "Completed"}) as mock_req:
            res = self.client.apply_folder_cleanup("op-123", confirmed=True)
            self.assertTrue(res["ok"])
            mock_req.assert_called_once_with(
                "POST",
                "/folders/cleanup/apply",
                {"operation_id": "op-123", "confirmed": True},
                timeout=60.0,
            )

    def test_plan_existing_album_reconcile_with_keyword_args(self):
        with patch.object(self.client, "_request", return_value={"ok": True, "operation_id": "op-456"}) as mock_req:
            res = self.client.plan_existing_album_reconcile(
                existing_album_id=10,
                imported_album_id=20,
                move_item_ids=[1, 2, 3],
                allow_different_releasegroup=True,
                retire_imported_album=True,
            )
            self.assertTrue(res["ok"])
            mock_req.assert_called_once_with(
                "POST",
                "/albums/existing-reconcile/plan",
                {
                    "existing_album_id": 10,
                    "imported_album_id": 20,
                    "move_item_ids": [1, 2, 3],
                    "allow_different_releasegroup": True,
                    "retire_imported_album": True,
                },
                timeout=30.0,
            )

    def test_plan_album_mb_track_repair_with_keyword_args(self):
        track_matches = [{"track": 1, "title": "Track 1", "mb_trackid": "mb-1"}]
        album_meta = {"year": 2024, "country": "US"}
        with patch.object(self.client, "_request", return_value={"ok": True, "operation_id": "op-789"}) as mock_req:
            res = self.client.plan_album_mb_track_repair(
                album_id=55,
                track_matches=track_matches,
                album_metadata=album_meta,
                zero_unmatched=True,
            )
            self.assertTrue(res["ok"])
            mock_req.assert_called_once_with(
                "POST",
                "/albums/mb-track-repair/plan",
                {
                    "album_id": 55,
                    "track_matches": track_matches,
                    "album_metadata": album_meta,
                    "zero_unmatched": True,
                },
                timeout=30.0,
            )

    def test_plan_library_cleanup_with_keyword_args(self):
        with patch.object(self.client, "_request", return_value={"ok": True, "operation_id": "op-lib"}) as mock_req:
            res = self.client.plan_library_cleanup(
                action="dedup_cleanup",
                paths=["/music/track1.mp3", "/music/track2.mp3"],
            )
            self.assertTrue(res["ok"])
            mock_req.assert_called_once_with(
                "POST",
                "/library/cleanup/plan",
                {
                    "action": "dedup_cleanup",
                    "paths": ["/music/track1.mp3", "/music/track2.mp3"],
                },
                timeout=30.0,
            )

    def test_delete_album_routes_to_album_maintenance(self):
        with patch.object(self.client, "get_album", return_value={"id": 42, "items": [{"id": 101}, {"id": 102}]}), \
             patch.object(self.client, "plan_album_maintenance", return_value={"ok": True, "operation_id": "op-del"}) as mock_plan, \
             patch.object(self.client, "apply_album_maintenance", return_value={"ok": True, "status": "Completed"}) as mock_apply:
            res = self.client.delete_album(42, delete_files=True)
            self.assertTrue(res["ok"])
            self.assertEqual(res["items_deleted"], 2)
            mock_plan.assert_called_once_with({
                "mode": "remove_tracks",
                "album_id": 42,
                "item_ids": [101, 102],
                "delete_files": True,
                "clean_empty_folders": True,
            })
            mock_apply.assert_called_once_with("op-del")

    def test_delete_empty_album_routes_to_remove_album_mode(self):
        with patch.object(self.client, "get_album", return_value={"id": 42, "items": []}), \
             patch.object(self.client, "plan_album_maintenance", return_value={"ok": True, "operation_id": "op-del-empty"}) as mock_plan, \
             patch.object(self.client, "apply_album_maintenance", return_value={"ok": True, "status": "Completed"}) as mock_apply:
            res = self.client.delete_album(42, delete_files=False)
            self.assertTrue(res["ok"])
            self.assertEqual(res["items_deleted"], 0)
            mock_plan.assert_called_once_with({
                "mode": "remove_album",
                "album_id": 42,
                "item_ids": [],
                "delete_files": False,
                "clean_empty_folders": True,
            })
            mock_apply.assert_called_once_with("op-del-empty")


if __name__ == "__main__":
    unittest.main()
