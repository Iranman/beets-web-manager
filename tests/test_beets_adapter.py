import os
import shutil
import tempfile
import unittest
import unittest.mock
from beets.library import Library, Item, Album
from beetsplug.web import app as beets_web_app
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key_file
from backend.beets_adapter import (
    BeetsAdapter,
    BeetsAdapterError,
    BeetsAdapterAuthError,
    BeetsAdapterNotFoundError,
    BeetsAdapterConnectionError,
    BeetsAdapterTimeoutError,
    BeetsAdapterBadRequestError,
    StockBeetsLibrary,
    RemoteLibrary,
    RemoteItem,
    RemoteAlbum,
)


class BeetsAdapterTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.td, "musiclibrary.blb")
        self.lib = Library(self.dbpath, directory=self.td)

        self.key_file = os.path.join(self.td, ".webmanager_api_key")
        self.token = "b" * 64
        with open(self.key_file, "w", encoding="utf-8") as f:
            f.write(self.token + "\n")
        set_api_key_file(self.key_file)

        # Setup Beets web plugin
        self.plugin = WebManagerPlugin()
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["INCLUDE_PATHS"] = True
        beets_web_app.config["TESTING"] = True

        self.album = Album(
            album="Discovery",
            albumartist="Daft Punk",
            year=2001,
            mb_albumid="a1111111-1111-1111-1111-111111111111",
            mb_releasegroupid="r1111111-1111-1111-1111-111111111111",
        )
        self.lib.add(self.album)
        self.item = Item(
            title="One More Time",
            artist="Daft Punk",
            album="Discovery",
            albumartist="Daft Punk",
            album_id=self.album.id,
            track=1,
            year=2001,
            format="MP3",
            mb_trackid="t1111111-1111-1111-1111-111111111111",
            path=os.path.join(self.td, "track1.mp3").encode("utf-8"),
        )
        self.lib.add(self.item)

        # Add a second album & item for query/filtering verification
        self.album2 = Album(
            album="Random Access Memories",
            albumartist="Daft Punk",
            year=2013,
            mb_albumid="a2222222-2222-2222-2222-222222222222",
            mb_releasegroupid="r2222222-2222-2222-2222-222222222222",
        )
        self.lib.add(self.album2)
        self.item2 = Item(
            title="Get Lucky",
            artist="Daft Punk",
            album="Random Access Memories",
            albumartist="Daft Punk",
            album_id=self.album2.id,
            track=8,
            year=2013,
            format="FLAC",
            mb_trackid="t2222222-2222-2222-2222-222222222222",
            path=os.path.join(self.td, "track2.flac").encode("utf-8"),
        )
        self.lib.add(self.item2)

        # Add a singleton track without an album
        self.singleton_item = Item(
            title="Musique",
            artist="Daft Punk",
            album="",
            albumartist="",
            album_id=None,
            track=1,
            year=1996,
            format="MP3",
            path=os.path.join(self.td, "singleton.mp3").encode("utf-8"),
        )
        self.lib.add(self.singleton_item)

        self.lib._connection().commit()

        self.client = beets_web_app.test_client()

        # Create BeetsAdapter wired to test client using mock opener
        self.adapter = BeetsAdapter(base_url="http://mock-beets:8337", api_key=self.token)

        # Patch adapter._request to delegate to Flask test client
        adapter_ref = self.adapter
        client_ref = self.client

        def mock_request(method, path, params=None, json_data=None, headers=None, timeout=None):
            h = headers or {}
            if path.startswith("/webmanager") or "/webmanager/" in path:
                if "Authorization" not in h and adapter_ref.api_key:
                    h["Authorization"] = f"Bearer {adapter_ref.api_key}"

            query_str = ""
            if params:
                import urllib.parse
                query_str = "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})

            full_path = f"{path}{query_str}"
            resp = client_ref.open(
                full_path,
                method=method,
                json=json_data,
                headers=h,
            )
            if resp.status_code == 401:
                raise BeetsAdapterAuthError("Auth failed", status_code=401, response_data=resp.get_json())
            if resp.status_code == 404:
                raise BeetsAdapterNotFoundError("Not found", status_code=404, response_data=resp.get_json())
            if resp.status_code >= 400:
                raise Exception(f"HTTP {resp.status_code}: {resp.data}")
            return resp.get_json()

        self.adapter._request = mock_request
        self.stock_lib = StockBeetsLibrary(self.adapter)

    def tearDown(self):
        set_api_key_file(None)
        try:
            self.lib._connection().close()
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def test_adapter_get_stats(self):
        stats = self.adapter.get_stats()
        self.assertEqual(stats["items"], 3)
        self.assertEqual(stats["albums"], 2)

    def test_adapter_get_artists(self):
        artists = self.adapter.get_artists()
        self.assertIn("Daft Punk", artists)

    def test_adapter_get_items_and_album(self):
        # Items
        items = self.adapter.get_items()
        self.assertEqual(len(items), 3)

        # Single item
        single_item = self.adapter.get_item(self.item.id)
        self.assertIsNotNone(single_item)
        self.assertEqual(single_item["title"], "One More Time")

        # Albums
        albums = self.adapter.get_albums()
        self.assertEqual(len(albums), 2)

        # Album with expand
        expanded_album = self.adapter.get_album(self.album.id, expand=True)
        self.assertIsNotNone(expanded_album)
        self.assertEqual(expanded_album["album"], "Discovery")
        self.assertIn("items", expanded_album)
        self.assertEqual(len(expanded_album["items"]), 1)

        # find_all_items_by_album_id helper
        album_items = self.adapter.find_all_items_by_album_id(self.album.id)
        self.assertEqual(len(album_items), 1)
        self.assertEqual(album_items[0]["title"], "One More Time")

    def test_adapter_field_values_and_helpers(self):
        formats = self.adapter.get_unique_field_values("item", "format")
        self.assertIn("MP3", formats)
        self.assertIn("FLAC", formats)

        # Distinct artists
        artists = self.adapter.list_distinct_albumartists()
        self.assertEqual(artists, ["Daft Punk"])

        # Artist counts -- contract-compatible with the legacy BeetsClient:
        # {artist: {"albums": N, "tracks": N}}, not a bare int.
        counts = self.adapter.get_artist_counts()
        self.assertEqual(counts.get("Daft Punk"), {"albums": 2, "tracks": 2})

        # Item paths
        paths = self.adapter.list_distinct_item_paths()
        self.assertEqual(len(paths), 3)

    def test_get_artist_counts_contract_compatibility(self):
        """A3: get_artist_counts() must return {artist: {"albums", "tracks"}},
        matching the legacy BeetsClient contract exactly -- not a bare int."""
        counts = self.adapter.get_artist_counts()
        self.assertIsInstance(counts, dict)
        daft_punk = counts["Daft Punk"]
        self.assertIsInstance(daft_punk, dict)
        self.assertEqual(daft_punk["albums"], 2)
        self.assertEqual(daft_punk["tracks"], 2)

    def test_list_item_paths_details_compatibility(self):
        """A3: list_item_paths(details=...) must support both legacy modes:
        details=False -> list of distinct path strings;
        details=True -> one {"id", "album_id", "path"} record per item."""
        plain_paths = self.adapter.list_item_paths()
        self.assertIsInstance(plain_paths, list)
        self.assertTrue(all(isinstance(p, str) for p in plain_paths))
        self.assertEqual(len(plain_paths), 3)

        plain_paths_explicit = self.adapter.list_item_paths(details=False)
        self.assertEqual(sorted(plain_paths_explicit), sorted(plain_paths))

        detailed = self.adapter.list_item_paths(details=True)
        self.assertEqual(len(detailed), 3)
        record = next(r for r in detailed if r["id"] == self.item.id)
        self.assertEqual(record["album_id"], self.album.id)
        self.assertTrue(record["path"].endswith("track1.mp3"))
        singleton_record = next(r for r in detailed if r["id"] == self.singleton_item.id)
        self.assertIsNone(singleton_record["album_id"])

        # Cleanup index
        cleanup_idx = self.adapter.get_album_cleanup_index()
        self.assertEqual(len(cleanup_idx), 3)
        item1_row = next(r for r in cleanup_idx if r.get("item_id") == self.item.id)
        self.assertEqual(item1_row.get("album_album"), "Discovery")

    def test_adapter_pagination(self):
        page0 = self.adapter.get_items_page(offset=0, limit=2)
        self.assertEqual(len(page0["items"]), 2)
        self.assertEqual(page0["total"], 3)
        self.assertEqual(page0["offset"], 0)
        self.assertEqual(page0["limit"], 2)

        page1 = self.adapter.get_items_page(offset=2, limit=2)
        self.assertEqual(len(page1["items"]), 1)
        self.assertEqual(page1["total"], 3)

    def test_stock_lib_facade_reads(self):
        # get_item
        remote_item = self.stock_lib.get_item(self.item.id)
        self.assertIsInstance(remote_item, RemoteItem)
        self.assertEqual(remote_item.title, "One More Time")
        self.assertEqual(remote_item["artist"], "Daft Punk")
        self.assertTrue(remote_item.path.endswith("track1.mp3"))

        # get_album and album.items()
        remote_album = self.stock_lib.get_album(self.album.id)
        self.assertIsInstance(remote_album, RemoteAlbum)
        self.assertEqual(remote_album.album, "Discovery")
        album_tracks = remote_album.items()
        self.assertEqual(len(album_tracks), 1)
        self.assertIsInstance(album_tracks[0], RemoteItem)
        self.assertEqual(album_tracks[0].title, "One More Time")

        # items() with bare and filtered queries
        all_items = self.stock_lib.items()
        self.assertEqual(len(all_items), 3)

        discovery_items = self.stock_lib.items(f"album_id:{self.album.id}")
        self.assertEqual(len(discovery_items), 1)
        self.assertEqual(discovery_items[0].title, "One More Time")

        # Query with mbid alias translation (mbid: -> mb_trackid:)
        mbid_items = self.stock_lib.items("mbid:t1111111-1111-1111-1111-111111111111")
        self.assertEqual(len(mbid_items), 1)
        self.assertEqual(mbid_items[0].title, "One More Time")

        # List query with AND semantics
        filtered = self.stock_lib.items(["artist:Daft Punk", "format:FLAC"])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].title, "Get Lucky")

        # albums() query
        ram_albums = self.stock_lib.albums("album:Random Access Memories")
        self.assertEqual(len(ram_albums), 1)
        self.assertEqual(ram_albums[0].year, 2013)

        # albums() mbid alias translation (mbid: -> mb_albumid:)
        mbid_albums = self.stock_lib.albums("mbid:a2222222-2222-2222-2222-222222222222")
        self.assertEqual(len(mbid_albums), 1)
        self.assertEqual(mbid_albums[0].album, "Random Access Memories")

    def test_adapter_mutations(self):
        # Modify item
        res = self.adapter.modify(
            fields={"title": "Aerodynamic", "genre": "House"},
            item_ids=[self.item.id],
            write=False,
            move=False,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["modified_items"], 1)

        # Verify
        refreshed = self.lib.get_item(self.item.id)
        self.assertEqual(refreshed.title, "Aerodynamic")

    def test_adapter_auth_failure(self):
        self.adapter._api_key = "invalid_bad_token"
        with self.assertRaises(BeetsAdapterAuthError):
            self.adapter.get_plugin_status()

    def test_adapter_not_found(self):
        self.assertIsNone(self.adapter.get_item(999999))
        self.assertIsNone(self.adapter.get_album(999999))

    def test_adapter_connection_failure_fails_closed(self):
        """When stock Beets is down, adapter must fail closed without fallback to legacy engine."""
        broken_adapter = BeetsAdapter(base_url="http://127.0.0.1:59999", timeout=0.1)
        broken_lib = StockBeetsLibrary(broken_adapter)

        with self.assertRaises(BeetsAdapterConnectionError):
            broken_adapter.get_stats()

        with self.assertRaises(BeetsAdapterConnectionError):
            broken_lib.get_item(1)

        with self.assertRaises(BeetsAdapterConnectionError):
            broken_lib.items()


class BeetsAdapterPaginationCacheTests(unittest.TestCase):
    """A5: get_items_page() genuinely has no real upstream pagination --
    verify it stays truthful (real slicing, real total) while avoiding a
    fresh full-library fetch on every single page request within its
    short TTL, using a realistically sized synthetic library."""

    def setUp(self):
        self.adapter = BeetsAdapter(base_url="http://mock-beets:8337")
        self._synthetic_items = [
            {"id": i, "title": f"Track {i}", "album_id": (i % 50) + 1, "path": f"/music/track{i}.mp3"}
            for i in range(1, 2001)
        ]
        self.get_items_calls = 0

        def _fake_get_items(query=None):
            self.get_items_calls += 1
            return list(self._synthetic_items)

        self.adapter.get_items = _fake_get_items

    def test_pagination_is_real_slicing_over_full_synthetic_library(self):
        page = self.adapter.get_items_page(offset=500, limit=25)
        self.assertEqual(page["total"], 2000)
        self.assertEqual(len(page["items"]), 25)
        self.assertEqual(page["items"][0]["id"], 501)
        self.assertEqual(page["items"][-1]["id"], 525)

    def test_repeated_page_requests_within_ttl_do_not_refetch_full_library(self):
        for offset in range(0, 500, 25):
            self.adapter.get_items_page(offset=offset, limit=25)
        # 20 page requests across the same short window must cost exactly
        # one real full-library fetch, not 20 -- this is the whole point of
        # the bounded cache (a genuine latency/load mitigation, not fake
        # pagination: get_items() itself is still a full fetch each time
        # it's actually called).
        self.assertEqual(self.get_items_calls, 1)

    def test_cache_expires_and_refetches_after_ttl(self):
        self.adapter.get_items_page(offset=0, limit=10)
        self.assertEqual(self.get_items_calls, 1)
        # Force the cached entry to look stale without a real sleep.
        self.adapter._items_page_cache_ts -= (
            self.adapter._ITEMS_PAGE_CACHE_TTL_SECONDS + 1
        )
        self.adapter.get_items_page(offset=0, limit=10)
        self.assertEqual(self.get_items_calls, 2)


class BeetsAdapterErrorSanitizationTests(unittest.TestCase):
    """A4: BeetsAdapter exceptions must never leak raw upstream HTTP bodies,
    HTML, or stack traces into str(ex) -- only stable sanitized fields."""

    def _http_error(self, status: int, body: bytes):
        import io
        import urllib.error

        return urllib.error.HTTPError(
            url="http://mock-beets:8337/item/1",
            code=status,
            msg="error",
            hdrs=None,
            fp=io.BytesIO(body),
        )

    def test_raw_html_body_never_in_exception_message(self):
        sensitive = (
            b"<html><body>Traceback (most recent call last):\n"
            b"  File \"/config/secret_internal_path.py\", line 42\n"
            b"KeyError: 'SUPER_SECRET_TOKEN_VALUE'</body></html>"
        )
        adapter = BeetsAdapter(base_url="http://mock-beets:8337")
        with unittest.mock.patch(
            "urllib.request.urlopen", side_effect=self._http_error(500, sensitive)
        ):
            with self.assertRaises(BeetsAdapterError) as ctx:
                adapter.get_stats()

        message = str(ctx.exception)
        self.assertNotIn("SUPER_SECRET_TOKEN_VALUE", message)
        self.assertNotIn("secret_internal_path.py", message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("<html>", message)
        self.assertEqual(ctx.exception.error_code, "BEETS_UPSTREAM_ERROR")
        self.assertEqual(ctx.exception.status_code, 500)

    def test_bad_request_error_sanitized_with_stable_error_code(self):
        body = b'{"error": "Source path must be a strict child of an import root", "error_code": "PATH_NOT_ALLOWED"}'
        adapter = BeetsAdapter(base_url="http://mock-beets:8337")
        with unittest.mock.patch(
            "urllib.request.urlopen", side_effect=self._http_error(400, body)
        ):
            with self.assertRaises(BeetsAdapterBadRequestError) as ctx:
                adapter._request("POST", "/webmanager/import", json_data={})

        # Our own plugin's structured error_code is carried forward as a
        # sanitized, stable field -- but the raw body text is not embedded
        # in the exception message itself.
        self.assertEqual(ctx.exception.error_code, "PATH_NOT_ALLOWED")
        self.assertNotIn("strict child", str(ctx.exception))

    def test_auth_error_sanitized(self):
        adapter = BeetsAdapter(base_url="http://mock-beets:8337")
        with unittest.mock.patch(
            "urllib.request.urlopen", side_effect=self._http_error(401, b'{"error": "nope"}')
        ):
            with self.assertRaises(BeetsAdapterAuthError) as ctx:
                adapter.get_plugin_status()
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertNotIn("nope", str(ctx.exception))

    def test_connection_error_message_excludes_raw_os_error_text(self):
        adapter = BeetsAdapter(base_url="http://mock-beets:8337", timeout=0.1)
        with unittest.mock.patch(
            "urllib.request.urlopen",
            side_effect=OSError("some very specific internal socket detail xyz123"),
        ):
            with self.assertRaises(BeetsAdapterConnectionError) as ctx:
                adapter.get_stats()
        self.assertNotIn("xyz123", str(ctx.exception))

    def test_to_public_dict_has_stable_sanitized_fields(self):
        body = b'{"error": "boom"}'
        adapter = BeetsAdapter(base_url="http://mock-beets:8337")
        with unittest.mock.patch(
            "urllib.request.urlopen", side_effect=self._http_error(404, body)
        ):
            with self.assertRaises(BeetsAdapterNotFoundError) as ctx:
                adapter.get_stats()
        public = ctx.exception.to_public_dict()
        self.assertEqual(set(public.keys()), {"error", "error_code", "status_code"})
        self.assertEqual(public["status_code"], 404)
        self.assertNotIn("boom", public["error"])


if __name__ == "__main__":
    unittest.main()
