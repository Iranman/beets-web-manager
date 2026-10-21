"""The webmanager plugin mounted on the real stock `beetsplug.web` app, as
Web Manager runs it in-process (docs/adr/0006-embedded-beets.md)."""
import os
import shutil
import tempfile
import unittest

from beets.library import Library, Item, Album
from beetsplug.web import app as beets_web_app
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key
import beetsplug.webmanager.operations as ops_mod


class StockBeetsInProcessAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.music_dir = os.path.join(self.td, "music")
        self.downloads_dir = os.path.join(self.td, "downloads")
        self.config_dir = os.path.join(self.td, "config")
        os.makedirs(self.music_dir, exist_ok=True)
        os.makedirs(self.downloads_dir, exist_ok=True)
        os.makedirs(self.config_dir, exist_ok=True)

        self.dbpath = os.path.join(self.config_dir, "musiclibrary.blb")
        self.lib = Library(self.dbpath, directory=self.music_dir)

        self.token = "a" * 64
        set_api_key(self.token)
        ops_mod.set_allowed_roots([self.music_dir, self.downloads_dir])
        ops_mod.set_import_roots([self.downloads_dir])

        # Configure Beets web app
        self.plugin = WebManagerPlugin()
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["INCLUDE_PATHS"] = True
        beets_web_app.config["READONLY"] = True
        beets_web_app.config["TESTING"] = True
        from beets import config as beets_config
        beets_config["web"]["readonly"] = True

        # Pre-populate sample library data
        self.album = Album(
            album="Echoes of Time",
            albumartist="Chronos",
            year=2023,
            genre="Ambient",
        )
        self.lib.add(self.album)

        self.sample_file = os.path.join(self.music_dir, "track1.mp3")
        with open(self.sample_file, "wb") as f:
            f.write(b"ID3\x03\x00\x00\x00\x00\x00\x00MP3_DATA_PAYLOAD")

        self.item1 = Item(
            title="Sands of Time",
            artist="Chronos",
            album="Echoes of Time",
            albumartist="Chronos",
            album_id=self.album.id,
            track=1,
            genre="Ambient",
            year=2023,
            path=self.sample_file.encode("utf-8"),
        )
        self.lib.add(self.item1)
        self.lib._connection().commit()

        self.client = beets_web_app.test_client()

    def tearDown(self):
        ops_mod.set_allowed_roots(None)
        ops_mod.set_import_roots(None)
        set_api_key(None)
        try:
            self.lib._connection().close()
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def test_native_upstream_reads(self):
        """Verify native upstream endpoints match the expected Beets REST API contracts."""
        # 1. GET /stats
        res = self.client.get("/stats")
        self.assertEqual(res.status_code, 200)
        stats = res.get_json()
        self.assertEqual(stats["items"], 1)
        self.assertEqual(stats["albums"], 1)

        # 2. GET /artist/
        res = self.client.get("/artist/")
        self.assertEqual(res.status_code, 200)
        artists = res.get_json()
        self.assertIn("artist_names", artists)
        self.assertIn("Chronos", artists["artist_names"])

        # 3. GET /item/
        res = self.client.get("/item/")
        self.assertEqual(res.status_code, 200)
        items_data = res.get_json()
        self.assertIn("items", items_data)
        self.assertEqual(len(items_data["items"]), 1)
        self.assertEqual(items_data["items"][0]["title"], "Sands of Time")

        # 4. GET /item/<id>
        res = self.client.get(f"/item/{self.item1.id}")
        self.assertEqual(res.status_code, 200)
        item_res = res.get_json()
        self.assertEqual(item_res["title"], "Sands of Time")

        # 5. GET /album/
        res = self.client.get("/album/")
        self.assertEqual(res.status_code, 200)
        albums_data = res.get_json()
        self.assertIn("albums", albums_data)
        self.assertEqual(len(albums_data["albums"]), 1)
        self.assertEqual(albums_data["albums"][0]["album"], "Echoes of Time")

        # 6. GET /album/<id>?expand
        res = self.client.get(f"/album/{self.album.id}?expand")
        self.assertEqual(res.status_code, 200)
        expanded = res.get_json()
        self.assertEqual(expanded["album"], "Echoes of Time")
        self.assertIn("items", expanded)
        self.assertEqual(len(expanded["items"]), 1)
        self.assertEqual(expanded["items"][0]["title"], "Sands of Time")

        # 7. GET /item/values/<key>
        res = self.client.get("/item/values/artist")
        self.assertEqual(res.status_code, 200)
        values = res.get_json()
        self.assertIn("values", values)
        self.assertIn("Chronos", values["values"])

    def test_native_file_stream(self):
        """Verify native audio binary streaming endpoint."""
        res = self.client.get(f"/item/{self.item1.id}/file")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"MP3_DATA_PAYLOAD", res.data)

    def test_webmanager_plugin_security_and_operations(self):
        """Verify WebManager plugin authentication, status handshake, modification, and path containment."""
        # 1. Unauthenticated request must fail
        res = self.client.get("/webmanager/status")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.get_json()["error_code"], "UNAUTHORIZED")

        # 2. Authenticated status check
        auth_headers = {"Authorization": f"Bearer {self.token}"}
        res = self.client.get("/webmanager/status", headers=auth_headers)
        self.assertEqual(res.status_code, 200)
        status_data = res.get_json()
        self.assertEqual(status_data["protocol_version"], "1.0")
        self.assertEqual(status_data["plugin_version"], "1.18.4")
        self.assertTrue(status_data["upstream_web_readonly"])
        self.assertTrue(status_data["plugin_mutations_enabled"])
        self.assertIn("import", status_data["capabilities"])
        # 1.6.0: path diagnostics exposed only to authenticated callers (401 checked above)
        self.assertIn("allowed_roots", status_data)
        self.assertIn("import_roots", status_data)

        # 3. Path containment verification on /webmanager/import
        outside_path = os.path.join(self.td, "..", "etc", "passwd")
        res = self.client.post(
            "/webmanager/import",
            headers=auth_headers,
            json={"paths": [outside_path]},
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "PATH_NOT_ALLOWED")

        # Root self-import rejected
        res_root = self.client.post(
            "/webmanager/import",
            headers=auth_headers,
            json={"paths": [self.downloads_dir]},
        )
        self.assertEqual(res_root.status_code, 400)
        self.assertEqual(res_root.get_json()["error_code"], "PATH_NOT_ALLOWED")

        # /music is an allowed_root but NOT an import_root -- must be rejected as an import source
        music_child = os.path.join(self.music_dir, "not_a_valid_import_source")
        os.makedirs(music_child, exist_ok=True)
        res_music = self.client.post(
            "/webmanager/import",
            headers=auth_headers,
            json={"paths": [music_child]},
        )
        self.assertEqual(res_music.status_code, 400)
        self.assertEqual(res_music.get_json()["error_code"], "PATH_NOT_ALLOWED")

        # 4. Modify tags
        res = self.client.post(
            "/webmanager/modify",
            headers=auth_headers,
            json={
                "item_ids": [self.item1.id],
                "fields": {"title": "Sands of Time (Remastered)", "genre": "Chillout"},
                "write": False,
                "move": False,
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["success"])

        # 5. Verify modification through native upstream read
        res = self.client.get(f"/item/{self.item1.id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["title"], "Sands of Time (Remastered)")
        self.assertEqual(res.get_json()["genres"], ["Chillout"])  # Beets 2.13+ field


if __name__ == "__main__":
    unittest.main()
