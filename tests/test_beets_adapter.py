import os
import shutil
import tempfile
import unittest
from beets.library import Library, Item, Album
from beetsplug.web import app as beets_web_app
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key_file
from backend.beets_adapter import (
    BeetsAdapter,
    BeetsAdapterAuthError,
    BeetsAdapterNotFoundError,
    BeetsAdapterConnectionError,
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

        self.album = Album(album="Discovery", albumartist="Daft Punk", year=2001)
        self.lib.add(self.album)
        self.item = Item(
            title="One More Time",
            artist="Daft Punk",
            album="Discovery",
            albumartist="Daft Punk",
            album_id=self.album.id,
            track=1,
            year=2001,
            path=os.path.join(self.td, "track1.mp3").encode("utf-8"),
        )
        self.lib.add(self.item)
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

    def tearDown(self):
        set_api_key_file(None)
        try:
            self.lib._connection().close()
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def test_adapter_get_stats(self):
        stats = self.adapter.get_stats()
        self.assertEqual(stats["items"], 1)
        self.assertEqual(stats["albums"], 1)

    def test_adapter_get_artists(self):
        artists = self.adapter.get_artists()
        self.assertIn("Daft Punk", artists)

    def test_adapter_get_items_and_album(self):
        # Items
        items = self.adapter.get_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "One More Time")

        # Single item
        single_item = self.adapter.get_item(self.item.id)
        self.assertIsNotNone(single_item)
        self.assertEqual(single_item["title"], "One More Time")

        # Albums
        albums = self.adapter.get_albums()
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["album"], "Discovery")

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


if __name__ == "__main__":
    unittest.main()
