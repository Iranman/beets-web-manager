"""SEC-002 / ARCH-003 Wave 30 & ARCH-007: `_clean_remove_orphaned_items()` and
`_clean_remove_empty_albums()` migrated to structured BeetsClient methods.
"""

import unittest
from unittest import mock

import app as app_module
from backend.beets_client import BeetsError, BeetsUnavailableError


class RemoveOrphanedItemsTests(unittest.TestCase):
    def setUp(self):
        self._plex_patch = mock.patch.object(app_module, "_trigger_plex_refresh")
        self._plex_patch.start()
        self._invalidate_patch = mock.patch.object(app_module, "_invalidate_lib_cache")
        self._invalidate_patch.start()

    def tearDown(self):
        self._invalidate_patch.stop()
        self._plex_patch.stop()

    def test_dry_run_never_calls_engine_mutation(self):
        with mock.patch.object(
            app_module.beets_client, "clean_orphaned_items",
            return_value={"ok": True, "dry_run": True, "selected": 1, "removed_count": 0, "orphaned_items": [{"id": 101, "artist": "Artist", "title": "Title"}]},
        ) as mock_clean:
            log = []
            res = app_module._clean_remove_orphaned_items([101], dry_run=True, log=log)
        mock_clean.assert_called_once_with(item_ids=[101], dry_run=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["removed"], 1)

    def test_orphaned_item_routes_through_clean_orphaned_items(self):
        with mock.patch.object(
            app_module.beets_client, "clean_orphaned_items",
            return_value={"ok": True, "dry_run": False, "selected": 1, "removed_count": 1, "orphaned_items": [{"id": 101, "artist": "Artist", "title": "Title"}]},
        ) as mock_clean:
            log = []
            res = app_module._clean_remove_orphaned_items([101], dry_run=False, log=log)
        mock_clean.assert_called_once_with(item_ids=[101], dry_run=False)
        self.assertTrue(res["ok"])
        self.assertFalse(res["dry_run"])
        self.assertEqual(res["removed"], 1)

    def test_empty_item_ids_is_noop(self):
        with mock.patch.object(app_module.beets_client, "clean_orphaned_items") as mock_clean:
            log = []
            res = app_module._clean_remove_orphaned_items([], dry_run=False, log=log)
        mock_clean.assert_not_called()
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], 0)

    def test_engine_unavailable_is_logged_and_raises(self):
        with mock.patch.object(
            app_module.beets_client, "clean_orphaned_items",
            side_effect=BeetsUnavailableError("offline"),
        ):
            log = []
            with self.assertRaises(BeetsUnavailableError):
                app_module._clean_remove_orphaned_items([101], dry_run=False, log=log)
        self.assertTrue(any("Engine unavailable" in line for line in log))


class RemoveEmptyAlbumsTests(unittest.TestCase):
    def setUp(self):
        self._plex_patch = mock.patch.object(app_module, "_trigger_plex_refresh")
        self._plex_patch.start()
        self._invalidate_patch = mock.patch.object(app_module, "_invalidate_lib_cache")
        self._invalidate_patch.start()

    def tearDown(self):
        self._invalidate_patch.stop()
        self._plex_patch.stop()

    def test_dry_run_never_calls_engine(self):
        with mock.patch.object(
            app_module.beets_client, "get_album",
            return_value={"id": 1, "albumartist": "Artist", "album": "Album"},
        ), mock.patch.object(
            app_module.beets_client, "find_all_items_by_album_id",
            return_value=[],
        ), mock.patch.object(
            app_module.beets_client, "delete_album",
        ) as mock_del:
            log = []
            res = app_module._clean_remove_empty_albums([1], dry_run=True, log=log)
        mock_del.assert_not_called()
        self.assertEqual(res["removed"], 1)

    def test_non_empty_album_is_skipped_not_deleted(self):
        with mock.patch.object(
            app_module.beets_client, "get_album",
            return_value={"id": 1, "albumartist": "Artist", "album": "Album"},
        ), mock.patch.object(
            app_module.beets_client, "find_all_items_by_album_id",
            return_value=[{"id": 101}],
        ), mock.patch.object(
            app_module.beets_client, "delete_album",
        ) as mock_del:
            log = []
            res = app_module._clean_remove_empty_albums([1], dry_run=False, log=log)
        mock_del.assert_not_called()
        self.assertEqual(res["removed"], 0)
        self.assertEqual(res["skipped"], 1)

    def test_empty_album_routes_through_delete_album(self):
        with mock.patch.object(
            app_module.beets_client, "get_album",
            return_value={"id": 1, "albumartist": "Artist", "album": "Album"},
        ), mock.patch.object(
            app_module.beets_client, "find_all_items_by_album_id",
            return_value=[],
        ), mock.patch.object(
            app_module.beets_client, "delete_album",
            return_value={"ok": True, "status": "completed"},
        ) as mock_del:
            log = []
            res = app_module._clean_remove_empty_albums([1], dry_run=False, log=log)
        mock_del.assert_called_once_with(1, delete_files=False)
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], 1)

    def test_engine_rejection_is_logged_and_does_not_raise(self):
        with mock.patch.object(
            app_module.beets_client, "get_album",
            return_value={"id": 1, "albumartist": "Artist", "album": "Album"},
        ), mock.patch.object(
            app_module.beets_client, "find_all_items_by_album_id",
            return_value=[],
        ), mock.patch.object(
            app_module.beets_client, "delete_album",
            return_value={"ok": False, "error": "boom"},
        ):
            log = []
            res = app_module._clean_remove_empty_albums([1], dry_run=False, log=log)
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], 0)
        self.assertTrue(any("Engine rejected" in line for line in log))

    def test_engine_unavailable_is_logged_and_does_not_raise(self):
        with mock.patch.object(
            app_module.beets_client, "get_album",
            return_value={"id": 1, "albumartist": "Artist", "album": "Album"},
        ), mock.patch.object(
            app_module.beets_client, "find_all_items_by_album_id",
            return_value=[],
        ), mock.patch.object(
            app_module.beets_client, "delete_album",
            side_effect=BeetsError("down"),
        ):
            log = []
            res = app_module._clean_remove_empty_albums([1], dry_run=False, log=log)
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], 0)
        self.assertTrue(any("Engine unavailable" in line for line in log))


if __name__ == "__main__":
    unittest.main()

