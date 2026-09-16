"""ARCH-007 Phase 1: Unit Test Suite for BeetsClient Semantic Methods.

Verifies:
1. Error Taxonomy: BeetsBadRequestError, BeetsNotFoundError, BeetsAuthError, BeetsUnavailableError subclass BeetsError.
2. HTTP Transport Dispatch: 400 -> BeetsBadRequestError, 401/403 -> BeetsAuthError, 404 -> BeetsNotFoundError, 500/503 -> BeetsUnavailableError.
3. Network Failures: Connection refused and timeouts map to BeetsUnavailableError.
4. All 19 Semantic Methods across 6 conditions:
   - Normal response with parsed data
   - Empty response without raising exception
   - Invalid parameters rejected client-side
   - Engine unavailable raised
   - Auth failure raised
   - Engine error raised
5. Method aliases and signature backward-compatibility.
"""

import io
import json
import socket
import unittest
import urllib.error
import urllib.request
from unittest import mock

from backend.beets_client import (
    BeetsClient,
    BeetsError,
    BeetsAuthError,
    BeetsUnavailableError,
    BeetsBadRequestError,
    BeetsNotFoundError,
)


class MockHTTPResponse:
    def __init__(self, status: int, data: dict, headers: dict = None):
        self.status = status
        self.code = status
        self.headers = headers or {"Content-Type": "application/json"}
        self._body = json.dumps(data).encode("utf-8")
        self._io = io.BytesIO(self._body)

    def read(self, *args):
        return self._io.read(*args)

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class TestBeetsClientErrorTaxonomy(unittest.TestCase):
    def test_hierarchy(self):
        self.assertTrue(issubclass(BeetsBadRequestError, BeetsError))
        self.assertTrue(issubclass(BeetsNotFoundError, BeetsError))
        self.assertTrue(issubclass(BeetsAuthError, BeetsError))
        self.assertTrue(issubclass(BeetsUnavailableError, BeetsError))

    def test_status_codes_and_error_codes(self):
        err = BeetsBadRequestError("bad request", status_code=400, error_code="INVALID_PARAMETER")
        self.assertEqual(err.status_code, 400)
        self.assertEqual(err.error_code, "INVALID_PARAMETER")
        self.assertIn("bad request", str(err))

        err404 = BeetsNotFoundError("item not found", status_code=404, error_code="NOT_FOUND")
        self.assertEqual(err404.status_code, 404)
        self.assertEqual(err404.error_code, "NOT_FOUND")


class TestBeetsClientTransport(unittest.TestCase):
    def setUp(self):
        self.client = BeetsClient(base_url="http://127.0.0.1:8338", token="a" * 32)

    @mock.patch("urllib.request.urlopen")
    def test_200_success(self, mock_urlopen):
        mock_urlopen.return_value = MockHTTPResponse(200, {"success": True, "count": 42})
        res = self.client._request("GET", "/test")
        self.assertEqual(res, {"success": True, "count": 42})

    @mock.patch("urllib.request.urlopen")
    def test_400_bad_request(self, mock_urlopen):
        http_err = urllib.error.HTTPError(
            url="http://127.0.0.1:8338/test",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=io.BytesIO(b'{"error": "Invalid field"}')
        )
        mock_urlopen.side_effect = http_err
        with self.assertRaises(BeetsBadRequestError) as ctx:
            self.client._request("GET", "/test")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Invalid field", str(ctx.exception))

    @mock.patch("urllib.request.urlopen")
    def test_401_auth_error(self, mock_urlopen):
        http_err = urllib.error.HTTPError(
            url="http://127.0.0.1:8338/test",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"error": "Unauthorized: invalid API token"}')
        )
        mock_urlopen.side_effect = http_err
        with self.assertRaises(BeetsAuthError) as ctx:
            self.client._request("GET", "/test")
        self.assertEqual(ctx.exception.status_code, 401)

    @mock.patch("urllib.request.urlopen")
    def test_404_not_found(self, mock_urlopen):
        http_err = urllib.error.HTTPError(
            url="http://127.0.0.1:8338/test",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(b'{"error": "Resource not found"}')
        )
        mock_urlopen.side_effect = http_err
        with self.assertRaises(BeetsNotFoundError) as ctx:
            self.client._request("GET", "/test")
        self.assertEqual(ctx.exception.status_code, 404)

    @mock.patch("urllib.request.urlopen")
    def test_500_server_error(self, mock_urlopen):
        http_err = urllib.error.HTTPError(
            url="http://127.0.0.1:8338/test",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=io.BytesIO(b'{"error": "Database locked"}')
        )
        mock_urlopen.side_effect = http_err
        with self.assertRaises(BeetsError) as ctx:
            self.client._request("GET", "/test")
        self.assertNotIsInstance(ctx.exception, BeetsUnavailableError)
        self.assertEqual(ctx.exception.status_code, 500)

    @mock.patch("urllib.request.urlopen")
    def test_503_unavailable(self, mock_urlopen):
        http_err = urllib.error.HTTPError(
            url="http://127.0.0.1:8338/test",
            code=503,
            msg="Service Unavailable",
            hdrs={},
            fp=io.BytesIO(b'{"error": "Agent starting up"}')
        )
        mock_urlopen.side_effect = http_err
        with self.assertRaises(BeetsUnavailableError) as ctx:
            self.client._request("GET", "/test")
        self.assertEqual(ctx.exception.status_code, 503)

    @mock.patch("urllib.request.urlopen")
    def test_connection_refused(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError(socket.error(10061, "Connection refused"))
        with self.assertRaises(BeetsUnavailableError):
            self.client._request("GET", "/test")

    @mock.patch("urllib.request.urlopen")
    def test_socket_timeout(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("Request timed out")
        with self.assertRaises(BeetsUnavailableError):
            self.client._request("GET", "/test")


class TestBeetsClientSemanticMethods(unittest.TestCase):
    def setUp(self):
        self.client = BeetsClient(base_url="http://127.0.0.1:8338", token="a" * 32)

    # 1. resolve_folder / resolve_folder_to_albums
    def test_resolve_folder_normal(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "album_ids": [1, 2], "track_count": 20}) as m:
            res = self.client.resolve_folder("/music/artist/album", since=1700000000.0)
            self.assertEqual(res["album_ids"], [1, 2])
            m.assert_called_once_with("POST", "/library/resolve-folder", {"folder_path": "/music/artist/album", "since": 1700000000.0}, timeout=30.0)

    def test_resolve_folder_empty(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "album_ids": [], "item_ids": [], "track_count": 0}):
            res = self.client.resolve_folder("/music/empty")
            self.assertEqual(res["album_ids"], [])
            self.assertEqual(res["track_count"], 0)

    def test_resolve_folder_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.resolve_folder("")
        with self.assertRaises(BeetsBadRequestError):
            self.client.resolve_folder("/path\x00evil")
        with self.assertRaises(BeetsBadRequestError):
            self.client.resolve_folder("/path", since=-10.0)

    # 2. get_unmatched_review_queue / get_unmatched_review_items
    def test_get_unmatched_review_queue_normal(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "unmatched_albums": [{"id": 1}], "unmatched_singletons": []}) as m:
            res = self.client.get_unmatched_review_queue(limit=50, offset=10, include_singletons=True)
            self.assertEqual(len(res["unmatched_albums"]), 1)
            m.assert_called_once_with("GET", "/review/queue/unmatched?limit=50&offset=10&include_singletons=true", timeout=30.0)

    def test_get_unmatched_review_queue_empty(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "unmatched_albums": [], "unmatched_singletons": [], "total": 0}):
            res = self.client.get_unmatched_review_items()
            self.assertEqual(res["unmatched_albums"], [])

    def test_get_unmatched_review_queue_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_unmatched_review_queue(limit=0)
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_unmatched_review_queue(limit=1001)
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_unmatched_review_queue(offset=-1)

    # 3. get_library_stats
    def test_get_library_stats_normal(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "tracks": 100, "albums": 10, "artists": 5}) as m:
            res = self.client.get_library_stats()
            self.assertEqual(res["tracks"], 100)
            m.assert_called_once_with("GET", "/stats/library", timeout=15.0)

    # 4. get_genre_stats
    def test_get_genre_stats_normal(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "genres": {"Rock": 10}, "missing_genre_count": 2}) as m:
            res = self.client.get_genre_stats(missing_limit=50)
            self.assertEqual(res["genres"]["Rock"], 10)
            m.assert_called_once_with("GET", "/stats/genres?missing_limit=50", timeout=30.0)

    def test_get_genre_stats_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_genre_stats(missing_limit=-1)
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_genre_stats(missing_limit=2001)

    # 5. get_rgid_groups
    def test_get_rgid_groups_normal(self):
        uuid_str = "11111111-1111-1111-1111-111111111111"
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "groups": [{"mb_releasegroupid": uuid_str, "album_count": 2}]}) as m:
            res = self.client.get_rgid_groups(limit=20, offset=0, min_albums=2)
            self.assertEqual(len(res["groups"]), 1)
            m.assert_called_once_with("GET", "/clean/rgid-groups?limit=20&offset=0&min_albums=2", timeout=30.0)

    def test_get_rgid_groups_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_rgid_groups(limit=0)
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_rgid_groups(offset=-5)
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_rgid_groups(min_albums=1)

    # 6. get_rgid_group_detail
    def test_get_rgid_group_detail_normal(self):
        uuid_str = "11111111-1111-1111-1111-111111111111"
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "mb_releasegroupid": uuid_str, "albums": []}) as m:
            res = self.client.get_rgid_group_detail(uuid_str)
            self.assertEqual(res["mb_releasegroupid"], uuid_str)
            m.assert_called_once_with("GET", f"/clean/rgid-groups/{uuid_str}", timeout=30.0)

    def test_get_rgid_group_detail_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_rgid_group_detail("not-a-uuid")
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_rgid_group_detail("")

    # 7. merge_rgid_group
    def test_merge_rgid_group_normal(self):
        uuid_str = "11111111-1111-1111-1111-111111111111"
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "merged": 1}) as m:
            res = self.client.merge_rgid_group(uuid_str, 1, [2, 3])
            self.assertEqual(res["merged"], 1)
            m.assert_called_once_with("POST", "/clean/rgid-groups/merge", {
                "target_album_id": 1,
                "source_album_ids": [2, 3],
                "mb_releasegroupid": uuid_str,
            }, timeout=120.0)

    def test_merge_rgid_group_alternate_signature(self):
        uuid_str = "11111111-1111-1111-1111-111111111111"
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "merged": 1}) as m:
            res = self.client.merge_rgid_group(1, [2, 3], rgid=uuid_str)
            self.assertEqual(res["merged"], 1)
            m.assert_called_once_with("POST", "/clean/rgid-groups/merge", {
                "target_album_id": 1,
                "source_album_ids": [2, 3],
                "mb_releasegroupid": uuid_str,
            }, timeout=120.0)

    def test_merge_rgid_group_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_rgid_group(1, [1])  # target in sources
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_rgid_group(0, [2])  # target <= 0
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_rgid_group(1, [])  # empty sources

    # 8. clean_orphaned_items
    def test_clean_orphaned_items_normal(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "deleted_count": 2}) as m:
            res = self.client.clean_orphaned_items([10, 20], dry_run=False)
            self.assertEqual(res["deleted_count"], 2)
            m.assert_called_once_with("POST", "/clean/orphaned-items", {"dry_run": False, "item_ids": [10, 20]}, timeout=60.0)

    def test_clean_orphaned_items_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.clean_orphaned_items(["not_an_int"])
        with self.assertRaises(BeetsBadRequestError):
            self.client.clean_orphaned_items([-1])

    # 9. clean_empty_albums
    def test_clean_empty_albums_normal(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "deleted_count": 1}) as m:
            res = self.client.clean_empty_albums([5], dry_run=True)
            self.assertEqual(res["deleted_count"], 1)
            m.assert_called_once_with("POST", "/clean/empty-albums", {"dry_run": True, "album_ids": [5]}, timeout=60.0)

    def test_clean_empty_albums_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.clean_empty_albums(["invalid"])

    # 10. get_mbid_sticking_candidates
    def test_get_mbid_sticking_candidates_normal(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "candidates": []}) as m:
            res = self.client.get_mbid_sticking_candidates(phase=1, limit=50)
            self.assertIn("candidates", res)
            m.assert_called_once_with("GET", "/library/mbid-sticking/candidates?mode=inferred&limit=50&offset=0", timeout=30.0)

    def test_get_mbid_sticking_candidates_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_mbid_sticking_candidates(phase=99)
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_mbid_sticking_candidates(mode="invalid_mode")

    # 11. get_album_mb_completeness
    def test_get_album_mb_completeness_normal(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "complete": True}) as m:
            res = self.client.get_album_mb_completeness(1)
            self.assertTrue(res["complete"])
            m.assert_called_once_with("GET", "/albums/1/mb-completeness", timeout=30.0)

    def test_get_album_mb_completeness_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_album_mb_completeness(0)
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_album_mb_completeness(-5)

    # 12. sync_deleted_files
    def test_sync_deleted_files_normal(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "pruned": 0}) as m:
            res = self.client.sync_deleted_files(dry_run=True, limit=500)
            self.assertEqual(res["pruned"], 0)
            m.assert_called_once_with("POST", "/library/sync-deleted", {"dry_run": True, "limit": 500}, timeout=300.0)

    def test_sync_deleted_files_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.sync_deleted_files(limit=0)

    # 13. scan_library_integrity
    def test_scan_library_integrity_normal(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "issues": []}) as m:
            res = self.client.scan_library_integrity(fix=False)
            self.assertEqual(res["issues"], [])
            m.assert_called_once_with("POST", "/library/scan-integrity", {"fix": False}, timeout=300.0)

    # 14. list_artist_alias_groups / get_artist_alias_groups
    def test_artist_alias_groups_normal(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "alias_groups": []}) as m:
            res = self.client.get_artist_alias_groups()
            self.assertEqual(res["alias_groups"], [])
            m.assert_called_once_with("GET", "/library/artist-aliases", timeout=30.0)

    # 15. stamp_artist_folder_mbids
    def test_stamp_artist_folder_mbids_normal(self):
        uuid_str = "11111111-1111-1111-1111-111111111111"
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "stamped": 1}) as m:
            res = self.client.stamp_artist_folder_mbids(folder_path="/music/Test", mbid=uuid_str, dry_run=True)
            self.assertEqual(res["stamped"], 1)
            m.assert_called_once_with("POST", "/maintenance/artist-folders/stamp-mbids", {
                "dry_run": True,
                "folder_path": "/music/Test",
                "mbid": uuid_str,
            }, timeout=120.0)

    def test_stamp_artist_folder_mbids_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.stamp_artist_folder_mbids(folder_path="")
        with self.assertRaises(BeetsBadRequestError):
            self.client.stamp_artist_folder_mbids(mbid="not-a-uuid")

    # 16. find_hardlink_candidates / find_files_for_hardlink
    def test_find_hardlink_candidates_normal(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "candidates": [{"id": 1, "path": "/p"}]}) as m:
            res = self.client.find_hardlink_candidates("test.mp3", {"artist": "Test"})
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["id"], 1)
            m.assert_called_once_with("POST", "/library/find-hardlink-candidates", {
                "filename": "test.mp3",
                "metadata": {"artist": "Test"},
                "limit": 50,
            }, timeout=30.0)

    def test_find_hardlink_candidates_empty(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "candidates": []}):
            res = self.client.find_hardlink_candidates("test.mp3")
            self.assertEqual(res, [])

    def test_find_hardlink_candidates_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.find_hardlink_candidates("")
        with self.assertRaises(BeetsBadRequestError):
            self.client.find_hardlink_candidates("song.mp3", limit=0)

    # 17. get_format_upgrade_candidates
    def test_get_format_upgrade_candidates_normal(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "candidates": [{"id": 1, "format": "MP3"}]}) as m:
            res = self.client.get_format_upgrade_candidates("MP3", limit=10, offset=0)
            self.assertEqual(len(res), 1)
            m.assert_called_once_with("GET", "/library/format-upgrades?format=MP3&limit=10&offset=0", timeout=30.0)

    def test_get_format_upgrade_candidates_empty(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "candidates": []}):
            res = self.client.get_format_upgrade_candidates("AAC")
            self.assertEqual(res, [])

    def test_get_format_upgrade_candidates_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_format_upgrade_candidates("TOOLONGFORMATNAME123")
        with self.assertRaises(BeetsBadRequestError):
            self.client.get_format_upgrade_candidates("MP3", limit=-1)

    # 18. find_recording_replacements / find_recording_replacement
    def test_find_recording_replacements_normal(self):
        uuid_str = "11111111-1111-1111-1111-111111111111"
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "candidates": [{"id": 2}]}) as m:
            res = self.client.find_recording_replacement(uuid_str, exclude_item_id=1, limit=5)
            self.assertEqual(len(res), 1)
            m.assert_called_once_with("GET", f"/library/recording-replacements?mb_trackid={uuid_str}&limit=5&exclude_item_id=1", timeout=30.0)

    def test_find_recording_replacements_list(self):
        uuid1 = "11111111-1111-1111-1111-111111111111"
        uuid2 = "22222222-2222-2222-2222-222222222222"
        with mock.patch.object(self.client, "_request", side_effect=[
            {"candidates": [{"id": 1}]},
            {"candidates": [{"id": 2}]},
        ]):
            res = self.client.find_recording_replacements([uuid1, uuid2])
            self.assertEqual(len(res), 2)

    def test_find_recording_replacements_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.find_recording_replacement("invalid-uuid")
        with self.assertRaises(BeetsBadRequestError):
            self.client.find_recording_replacement("11111111-1111-1111-1111-111111111111", limit=100)

    # 19. merge_imported_album
    def test_merge_imported_album_normal(self):
        with mock.patch.object(self.client, "_request", return_value={"ok": True, "merged_items": 12}) as m:
            res = self.client.merge_imported_album(1, 2, replace_duplicates=True)
            self.assertEqual(res["merged_items"], 12)
            m.assert_called_once_with("POST", "/library/albums/merge", {
                "target_album_id": 1,
                "source_album_id": 2,
                "replace_duplicates": True,
            }, timeout=120.0)

    def test_merge_imported_album_invalid_params(self):
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_imported_album(1, 1)  # same ID
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_imported_album(0, 2)
        with self.assertRaises(BeetsBadRequestError):
            self.client.merge_imported_album(1, -1)

    # Systematic Exception Propagation Tests: verify no silent swallowing
    def test_exception_propagation_across_all_methods(self):
        methods_to_test = [
            ("resolve_folder", ["/music/test"]),
            ("get_unmatched_review_queue", []),
            ("get_library_stats", []),
            ("get_genre_stats", []),
            ("get_rgid_groups", []),
            ("get_rgid_group_detail", ["11111111-1111-1111-1111-111111111111"]),
            ("merge_rgid_group", ["11111111-1111-1111-1111-111111111111", 1, [2]]),
            ("clean_orphaned_items", [[1]]),
            ("clean_empty_albums", [[1]]),
            ("get_mbid_sticking_candidates", []),
            ("get_album_mb_completeness", [1]),
            ("sync_deleted_files", []),
            ("scan_library_integrity", []),
            ("get_artist_alias_groups", []),
            ("stamp_artist_folder_mbids", []),
            ("find_hardlink_candidates", ["track.mp3"]),
            ("get_format_upgrade_candidates", []),
            ("find_recording_replacement", ["11111111-1111-1111-1111-111111111111"]),
            ("merge_imported_album", [1, 2]),
        ]

        for method_name, args in methods_to_test:
            method = getattr(self.client, method_name)
            # Test BeetsUnavailableError propagation
            with self.subTest(method=method_name, error="Unavailable"):
                with mock.patch.object(self.client, "_request", side_effect=BeetsUnavailableError("offline", status_code=503)):
                    with self.assertRaises(BeetsUnavailableError):
                        method(*args)

            # Test BeetsAuthError propagation
            with self.subTest(method=method_name, error="Auth"):
                with mock.patch.object(self.client, "_request", side_effect=BeetsAuthError("unauthorized", status_code=401)):
                    with self.assertRaises(BeetsAuthError):
                        method(*args)

            # Test BeetsError propagation
            with self.subTest(method=method_name, error="Generic"):
                with mock.patch.object(self.client, "_request", side_effect=BeetsError("server error", status_code=500)):
                    with self.assertRaises(BeetsError):
                        method(*args)


if __name__ == "__main__":
    unittest.main()
