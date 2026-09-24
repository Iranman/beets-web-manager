"""Unit tests for backend/composite_workflows.py."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.beets_adapter import BeetsAdapter
from backend.composite_workflows import (
    apply_album_cleanup,
    apply_album_duplicate_merge,
    apply_album_mb_track_repair,
    apply_artist_folder_reconcile,
    apply_folder_cleanup,
    apply_track_replacement,
    clean_empty_albums,
    clean_orphaned_items,
    delete_album_art,
    delete_playlist_m3u,
    delete_staging_file,
    export_playlist_m3u,
    fetch_and_embed_album_art,
    move_staging_file,
    plan_album_cleanup,
    plan_album_duplicate_merge,
    plan_album_mb_track_repair,
    plan_artist_folder_reconcile,
    plan_folder_cleanup,
    plan_track_replacement,
    read_playlist_m3u,
    rollback_album_duplicate_merge,
    rollback_album_mb_track_repair,
    rollback_artist_folder_reconcile,
    rollback_track_replacement,
    sync_deleted_files,
)
from backend.transaction_engine import TransactionStore


class TestCompositeWorkflows(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.env_patcher = patch.dict("os.environ", {"WEB_MANAGER_DATA_DIR": self.tmpdir.name})
        self.env_patcher.start()
        self.tx_dir = Path(self.tmpdir.name) / "transactions"
        self.store = TransactionStore(str(self.tx_dir))
        self.mock_adapter = MagicMock(spec=BeetsAdapter)

    def tearDown(self):
        self.env_patcher.stop()
        self.tmpdir.cleanup()

    def test_merge_album_plan_apply_rollback(self):
        # Setup mock albums and items
        self.mock_adapter.get_album.side_effect = lambda aid: {
            1: {"id": 1, "album": "Target Album", "albumartist": "Artist A", "mb_albumid": "mb-1", "mb_releasegroupid": "rg-1"},
            2: {"id": 2, "album": "Source Album", "albumartist": "Artist A", "mb_albumid": "mb-2", "mb_releasegroupid": "rg-1"},
        }.get(aid)
        self.mock_adapter.find_all_items_by_album_id.side_effect = lambda aid: {
            1: [{"id": 101, "title": "Track 1", "album_id": 1, "path": "/music/t1.flac"}],
            2: [{"id": 102, "title": "Track 2", "album_id": 2, "path": "/music/t2.flac"}],
        }.get(aid, [])

        plan_res = plan_album_duplicate_merge(
            {"target_album_id": 1, "source_album_ids": [2]},
            adapter=self.mock_adapter,
            store=self.store,
        )
        self.assertTrue(plan_res["ok"])
        op_id = plan_res["operation_id"]

        # Apply merge
        apply_res = apply_album_duplicate_merge(op_id, adapter=self.mock_adapter, store=self.store)
        self.assertTrue(apply_res["ok"])
        self.mock_adapter.modify.assert_called()
        self.mock_adapter.remove.assert_called_with(album_ids=[2], delete_files=False)

        # Rollback merge
        rb_res = rollback_album_duplicate_merge(op_id, adapter=self.mock_adapter, store=self.store)
        self.assertTrue(rb_res["ok"])

    def test_artist_folder_reconcile_flow(self):
        self.mock_adapter.get_album.return_value = {
            "id": 10, "album": "Album 1", "albumartist": "Artist Variant", "artist": "Artist Variant"
        }
        plan_res = plan_artist_folder_reconcile(
            {"album_ids": [10], "canonical_name": "Canonical Artist"},
            adapter=self.mock_adapter,
            store=self.store,
        )
        self.assertTrue(plan_res["ok"])
        op_id = plan_res["operation_id"]

        apply_res = apply_artist_folder_reconcile(op_id, adapter=self.mock_adapter, store=self.store)
        self.assertTrue(apply_res["ok"])
        self.mock_adapter.modify.assert_called_with(
            fields={"albumartist": "Canonical Artist"},
            album_ids=[10],
            write=True,
            move=True,
        )

        rb_res = rollback_artist_folder_reconcile(op_id, adapter=self.mock_adapter, store=self.store)
        self.assertTrue(rb_res["ok"])

    def test_track_replacement_flow(self):
        stg_file = Path(self.tmpdir.name) / "better_quality.flac"
        stg_file.write_bytes(b"FLAC_DATA_NEW")
        target_file = Path(self.tmpdir.name) / "old_quality.mp3"
        target_file.write_bytes(b"MP3_DATA_OLD")

        self.mock_adapter.get_item.return_value = {
            "id": 55, "title": "Song", "path": str(target_file)
        }

        plan_res = plan_track_replacement(
            {"item_id": 55, "source_path": str(stg_file)},
            adapter=self.mock_adapter,
            store=self.store,
        )
        self.assertTrue(plan_res["ok"])
        op_id = plan_res["operation_id"]

        apply_res = apply_track_replacement(op_id, adapter=self.mock_adapter, store=self.store)
        self.assertTrue(apply_res["ok"])
        self.assertEqual(target_file.read_bytes(), b"FLAC_DATA_NEW")

        # Rollback restores old data
        rb_res = rollback_track_replacement(op_id, adapter=self.mock_adapter, store=self.store)
        self.assertTrue(rb_res["ok"])
        self.assertEqual(target_file.read_bytes(), b"MP3_DATA_OLD")

    def test_album_mb_track_repair_flow(self):
        self.mock_adapter.get_album.return_value = {"id": 20, "album": "Repair Album"}
        self.mock_adapter.find_all_items_by_album_id.return_value = [
            {"id": 201, "title": "Track 1", "mb_trackid": "old-mbid-1"}
        ]

        plan_res = plan_album_mb_track_repair(
            {"album_id": 20}, adapter=self.mock_adapter, store=self.store
        )
        self.assertTrue(plan_res["ok"])
        op_id = plan_res["operation_id"]

        apply_res = apply_album_mb_track_repair(op_id, adapter=self.mock_adapter, store=self.store)
        self.assertTrue(apply_res["ok"])
        self.mock_adapter.mbsync.assert_called_with(album_ids=[20], write=True, move=True)

        rb_res = rollback_album_mb_track_repair(op_id, adapter=self.mock_adapter, store=self.store)
        self.assertTrue(rb_res["ok"])

    def test_folder_and_album_cleanup_flow(self):
        sub_dir = Path(self.tmpdir.name) / "empty_dir"
        sub_dir.mkdir()
        plan_res = plan_folder_cleanup({"source": str(sub_dir), "action": "remove_empty"}, store=self.store)
        self.assertTrue(plan_res["ok"])
        apply_res = apply_folder_cleanup(plan_res["operation_id"], store=self.store)
        self.assertTrue(apply_res["ok"])
        self.assertFalse(sub_dir.exists())

        self.mock_adapter.get_album.return_value = {"id": 30, "album": "Delete Me"}
        self.mock_adapter.find_all_items_by_album_id.return_value = []
        p_alb = plan_album_cleanup(30, adapter=self.mock_adapter, store=self.store)
        self.assertTrue(p_alb["ok"])
        a_alb = apply_album_cleanup(p_alb["operation_id"], adapter=self.mock_adapter, store=self.store)
        self.assertTrue(a_alb["ok"])
        self.mock_adapter.remove.assert_called_with(album_ids=[30], delete_files=True)

    def test_clean_all_helpers(self):
        # Test sync_deleted_files
        self.mock_adapter.list_item_paths.return_value = [
            {"id": 1, "path": "/nonexistent/path/song.mp3"},
        ]
        res = sync_deleted_files(dry_run=False, adapter=self.mock_adapter)
        self.assertTrue(res["ok"])
        self.assertEqual(res["missing_count"], 1)
        self.mock_adapter.remove.assert_called_with(item_ids=[1], delete_files=False)

        # Test clean_orphaned_items
        self.mock_adapter.get_albums.return_value = [{"id": 1}]
        self.mock_adapter.get_items.return_value = [
            {"id": 10, "album_id": 999},  # orphan
        ]
        res_orph = clean_orphaned_items(dry_run=False, adapter=self.mock_adapter)
        self.assertTrue(res_orph["ok"])
        self.mock_adapter.remove.assert_called_with(item_ids=[10], delete_files=True)

        # Test clean_empty_albums
        self.mock_adapter.find_all_orphan_albums.return_value = [{"id": 99}]
        res_empty = clean_empty_albums(dry_run=False, adapter=self.mock_adapter)
        self.assertTrue(res_empty["ok"])
        self.mock_adapter.remove.assert_called_with(album_ids=[99], delete_files=False)

    def test_playlist_m3u_operations(self):
        items = [
            {"id": 1, "path": "/music/artist/album/01.flac", "title": "Track 1", "artist": "Artist 1", "length": 180}
        ]
        exp_res = export_playlist_m3u("test_playlist", "Test Playlist", items)
        self.assertTrue(exp_res["ok"])

        read_res = read_playlist_m3u("test_playlist")
        self.assertTrue(read_res["ok"])
        self.assertTrue(read_res["exists"])
        self.assertEqual(len(read_res["tracks"]), 1)

        del_res = delete_playlist_m3u("test_playlist")
        self.assertTrue(del_res["ok"])
        read_after = read_playlist_m3u("test_playlist")
        self.assertFalse(read_after["exists"])

    def test_staging_safety_rejects_music_root(self):
        with patch.dict("os.environ", {"MUSIC_ROOT": str(self.tmpdir.name)}):
            music_file = Path(self.tmpdir.name) / "song.mp3"
            music_file.write_bytes(b"data")
            with self.assertRaises(ValueError):
                delete_staging_file(str(music_file))

    def test_run_command_security_gates(self):
        from backend.composite_workflows import run_command
        # Allowed command succeeds
        res = run_command("mbsubmit", ["album_id:123"])
        self.assertTrue(res["ok"])
        self.assertIn("mbsubmit album_id:123", res["stdout"])

        # Prohibited commands raise ValueError
        for bad_cmd in ["sh", "bash", "rm", "python", "import", "eval", "docker", "drop table"]:
            with self.assertRaises(ValueError):
                run_command(bad_cmd, ["arg"])

        # Injection characters in arguments raise ValueError
        for bad_arg in ["1; rm -rf /", "album_id:1 | cat", "1 & echo hacked", "$USER", "`id`", "1\nrm -rf /"]:
            with self.assertRaises(ValueError):
                run_command("mbsubmit", [bad_arg])


if __name__ == "__main__":
    unittest.main()
