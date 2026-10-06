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

    def test_merge_album_refuses_a_different_edition_without_touching_the_library(self):
        """The retired in-place merge rewrote the source items' Release ID to
        the target's. An album-row merge is now ownership-only and another
        edition is refused (full coverage: tests/test_arch020_legacy_merge_callers.py)."""
        self.mock_adapter.get_album.side_effect = lambda aid, expand=True: {
            1: {"id": 1, "album": "Target Album", "albumartist": "Artist A", "mb_albumid": "mb-1", "mb_releasegroupid": "rg-1"},
            2: {"id": 2, "album": "Source Album", "albumartist": "Artist A", "mb_albumid": "mb-2", "mb_releasegroupid": "rg-1"},
        }.get(aid)
        plan_res = plan_album_duplicate_merge(
            {"target_album_id": 1, "source_album_ids": [2]},
            adapter=self.mock_adapter,
            store=self.store,
        )
        self.assertFalse(plan_res["ok"])
        self.assertEqual(plan_res["code"], "edition_differs")
        self.mock_adapter.modify.assert_not_called()
        self.mock_adapter.remove.assert_not_called()
        self.assertTrue(callable(apply_album_duplicate_merge) and callable(rollback_album_duplicate_merge))

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
        """Preview -> Approve -> Apply through the engine op -> Rollback."""
        rec, rel, rg = "rec-1", "rel-1", "rg-1"
        album_item = {"id": 55, "title": "Exotic", "album_id": 1935, "mb_trackid": rec, "mb_albumid": rel,
                      "mb_releasegroupid": rg, "disc": 1, "track": 17, "format": "MP3",
                      "path": "/music/BossMan Dlow/2 Slippery/17 Exotic.mp3"}
        flac_item = {"id": 77, "title": "exotic (00)", "album_id": None, "mb_trackid": "", "format": "FLAC",
                     "disc": 0, "track": 17, "path": "/music/loose/exotic (00).flac"}
        items = {55: dict(album_item), 77: dict(flac_item)}
        self.mock_adapter.get_item.side_effect = lambda iid: items.get(iid)

        plan_res = plan_track_replacement(
            {"original_item_id": 55, "replacement_item_id": 77},
            adapter=self.mock_adapter, store=self.store,
        )
        self.assertTrue(plan_res["ok"], plan_res)
        op_id = plan_res["operation_id"]
        self.assertEqual(self.store.get(op_id)["status"], "Preview")
        self.mock_adapter.replace_item_file.assert_not_called()

        # Apply refuses until the transaction is approved.
        not_yet = apply_track_replacement(op_id, adapter=self.mock_adapter, store=self.store)
        self.assertEqual(not_yet["code"], "not_approved")
        self.mock_adapter.replace_item_file.assert_not_called()

        self.store.update(op_id, status="Approved")
        new_path = "/music/BossMan Dlow/2 Slippery/17 Exotic.flac"
        self.mock_adapter.replace_item_file.return_value = {
            "operation_id": op_id, "success": True, "new_target_path": new_path,
            "quarantine_id": "0" * 32,
            "quarantine_path": "/config/webmanager-quarantine/x/17 Exotic.mp3",
            "target_snapshot": {"id": 55, "path": album_item["path"]},
            "source_snapshot": {"id": 77, "path": flac_item["path"]},
        }
        items[55] = {**album_item, "format": "FLAC", "path": new_path}
        items.pop(77)
        apply_res = apply_track_replacement(op_id, adapter=self.mock_adapter, store=self.store)
        self.assertTrue(apply_res["ok"], apply_res)
        self.assertEqual(apply_res["status"], "Completed")
        self.assertEqual(apply_res["new_path"], new_path)
        self.mock_adapter.replace_item_file.assert_called_once_with(55, 77, idempotency_key=op_id, displace_destination_sha256=None)
        self.assertEqual(self.store.get(op_id)["status"], "Completed")

        # A second apply (e.g. after a re-approve) never re-runs the engine op.
        self.store.update(op_id, status="Approved")
        again = apply_track_replacement(op_id, adapter=self.mock_adapter, store=self.store)
        self.assertEqual(again["code"], "already_applied")
        self.assertEqual(self.mock_adapter.replace_item_file.call_count, 1)

        self.mock_adapter.rollback_replace_item_file.return_value = {
            "success": True, "restored_target_path": album_item["path"],
            "recreated_source_item_id": 78, "recreated_source_path": flac_item["path"],
        }
        rb_res = rollback_track_replacement(op_id, adapter=self.mock_adapter, store=self.store)
        self.assertTrue(rb_res["ok"])
        self.assertEqual(rb_res["recreated_source_item_id"], 78)
        self.mock_adapter.rollback_replace_item_file.assert_called_once_with(
            "0" * 32, idempotency_key=f"{op_id}:rollback",
        )
        self.assertEqual(self.store.get(op_id)["status"], "Rolled Back")

    def test_track_replacement_flags_identity_drift_after_apply(self):
        album_item = {"id": 55, "album_id": 1935, "mb_trackid": "rec-1", "disc": 1, "track": 17, "path": "/music/a.mp3"}
        items = {55: dict(album_item), 77: {"id": 77, "path": "/music/b.flac"}}
        self.mock_adapter.get_item.side_effect = lambda iid: items.get(iid)
        op_id = plan_track_replacement({"item_id": 55, "source_item_id": 77}, adapter=self.mock_adapter,
                                       store=self.store)["operation_id"]
        self.store.update(op_id, status="Approved")
        self.mock_adapter.replace_item_file.return_value = {"new_target_path": "/music/a.flac"}
        items[55] = {**album_item, "track": 3, "path": "/music/a.flac"}
        res = apply_track_replacement(op_id, adapter=self.mock_adapter, store=self.store)
        self.assertFalse(res["ok"])
        self.assertEqual(res["verification_problems"], ["track"])
        self.assertEqual(self.store.get(op_id)["status"], "Recovery Required")

    def test_track_replacement_accepts_library_relative_paths_after_apply(self):
        """The stock Beets web API reports paths relative to the library;
        the engine returns the absolute path. That is not identity drift."""
        album_item = {"id": 55, "album_id": 1935, "mb_trackid": "rec-1", "disc": 1, "track": 17,
                      "path": "Artist/Album/17 Song.mp3"}
        items = {55: dict(album_item), 77: {"id": 77, "path": "Artist/Album/song (00).flac"}}
        self.mock_adapter.get_item.side_effect = lambda iid: items.get(iid)
        op_id = plan_track_replacement({"item_id": 55, "source_item_id": 77}, adapter=self.mock_adapter,
                                       store=self.store)["operation_id"]
        self.store.update(op_id, status="Approved")
        self.mock_adapter.replace_item_file.return_value = {"new_target_path": "/music/Artist/Album/17 Song.flac"}
        items[55] = {**album_item, "path": "Artist/Album/17 Song.flac"}
        res = apply_track_replacement(op_id, adapter=self.mock_adapter, store=self.store)
        self.assertTrue(res["ok"], res)
        # ...but a different file is still caught.
        from backend.composite_workflows import _same_library_path
        self.assertFalse(_same_library_path("Album/17 Song.flac", "/music/Other/17 Song.flac.bak"))
        self.assertFalse(_same_library_path("Song.flac", "/music/Artist/Album/17 Song.flac"))

    def test_track_replacement_from_staged_file_fails_closed(self):
        res = plan_track_replacement(
            {"original_item_id": 55, "replacement_path": "/data/downloads/new.flac"},
            adapter=self.mock_adapter, store=self.store,
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "staged_replacement_unsupported")
        self.mock_adapter.get_item.assert_not_called()

    def test_track_replacement_requires_an_album_slot_target(self):
        self.mock_adapter.get_item.side_effect = lambda iid: {"id": iid, "album_id": None, "path": f"/music/{iid}.mp3"}
        res = plan_track_replacement({"original_item_id": 55, "replacement_item_id": 77},
                                     adapter=self.mock_adapter, store=self.store)
        self.assertEqual(res["code"], "target_not_in_album")

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
        # LT-4 (Wave 0): a Preview plan cannot be applied ...
        refused = apply_album_cleanup(p_alb["operation_id"], adapter=self.mock_adapter, store=self.store)
        self.assertEqual(refused["code"], "not_approved")
        self.mock_adapter.remove.assert_not_called()
        # ... and an Approved one removes rows only (files kept) by default.
        self.store.transition(p_alb["operation_id"], "Preview", "Approved")
        self.mock_adapter.get_album.side_effect = [{"id": 30, "album": "Delete Me"}, None]
        a_alb = apply_album_cleanup(p_alb["operation_id"], adapter=self.mock_adapter, store=self.store)
        self.assertTrue(a_alb["ok"], a_alb)
        self.mock_adapter.remove.assert_called_with(album_ids=[30], delete_files=False,
                                                    idempotency_key=p_alb["operation_id"])

    def test_clean_all_helpers(self):
        """Wave 0 (LT-1/LT-2): row-only removal of re-verified missing files;
        never file deletion, never a widening of an empty selection."""
        music = Path(self.tmpdir.name) / "music"
        music.mkdir()
        present = music / "present.mp3"
        present.write_bytes(b"x")
        with patch.dict("os.environ", {"MUSIC_ROOT": str(music)}):
            self.mock_adapter.list_item_paths.return_value = [
                {"id": 1, "path": str(music / "missing.mp3")},
                {"id": 2, "path": str(present)}, {"id": 3, "path": str(present)},
            ]
            res = sync_deleted_files(dry_run=False, adapter=self.mock_adapter, item_ids=[1], store=self.store)
            self.assertTrue(res["ok"])
            self.assertEqual(res["missing_count"], 1)
            self.mock_adapter.remove.assert_called_with(item_ids=[1], delete_files=False)

            self.mock_adapter.remove.reset_mock()
            self.mock_adapter.get_item.side_effect = lambda iid: {
                10: {"id": 10, "album_id": 999, "path": str(music / "gone.mp3")},
                11: {"id": 11, "album_id": 1, "path": str(present)},
            }.get(iid)
            self.mock_adapter.get_stats.return_value = {"items": 10}
            res_orph = clean_orphaned_items(item_ids=[10, 11], dry_run=False, adapter=self.mock_adapter,
                                            store=self.store)
            self.assertTrue(res_orph["ok"])
            self.mock_adapter.remove.assert_called_once_with(item_ids=[10], delete_files=False)
            self.assertFalse(clean_orphaned_items(dry_run=False, adapter=self.mock_adapter)["ok"])

        self.mock_adapter.get_album.side_effect = lambda aid, expand=True: {"id": aid}
        self.mock_adapter.find_all_items_by_album_id.return_value = []
        res_empty = clean_empty_albums(album_ids=[99], dry_run=False, adapter=self.mock_adapter)
        self.assertTrue(res_empty["ok"])
        self.mock_adapter.remove.assert_called_with(album_ids=[99], delete_files=False)
        self.assertFalse(clean_empty_albums(dry_run=False, adapter=self.mock_adapter)["ok"])

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
        # Allowed command passes validation but is not executed: the helper
        # must report not_supported instead of fabricating stdout (Wave 0 LT-12).
        res = run_command("mbsubmit", ["album_id:123"])
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("code"), "not_supported")
        self.assertNotIn("mbsubmit album_id:123", res.get("stdout", ""))

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
