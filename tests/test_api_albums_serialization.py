"""Regression tests for GET /api/albums and album_dict serialization.

Fixes legacy TypeError ('str' object is not callable) caused by calling
album.item_dir() on RemoteAlbum instances where item_dir is a string attribute
rather than a bound method.
"""

import unittest
from unittest import mock

import app as app_module
from backend.beets_client import RemoteAlbum


class FakeBeetsAlbumMethod:
    """Mock native Beets Album where item_dir is a method."""
    def __init__(self, album_id=1, album="Discovery", albumartist="Daft Punk", year=2001, genre="Electronic", mb_albumid="056e4f3e-d505-4dad-8ec1-d04f521cbb56"):
        self.id = album_id
        self.album = album
        self.albumartist = albumartist
        self.year = year
        self.genre = genre
        self.mb_albumid = mb_albumid
        self.path = b"/data/media/music/Daft Punk/Discovery"

    def item_dir(self):
        return b"/data/media/music/Daft Punk/Discovery"


class FakeBeetsAlbumEmptyMethod:
    """Mock native Beets Album where item_dir() raises ValueError (empty album)."""
    def __init__(self, album_id=2):
        self.id = album_id
        self.album = "Empty Album"
        self.albumartist = "Unknown Artist"
        self.year = 2026
        self.genre = ""
        self.mb_albumid = ""
        self.path = b"/data/media/music/Unknown Artist/Empty Album"

    def item_dir(self):
        raise ValueError("empty album for album id 2")


class AlbumDictSerializationTests(unittest.TestCase):
    """Test suite for album_dict and _get_album_item_dir compatibility."""

    def test_album_dict_with_remote_album(self):
        data = {
            "id": 10,
            "album": "Random Access Memories",
            "albumartist": "Daft Punk",
            "year": 2013,
            "genre": "Disco",
            "mb_albumid": "a58688b4-e52a-4224-9f51-cd603bf365cf",
            "item_dir": "/data/media/music/Daft Punk/Random Access Memories",
            "path": "/data/media/music/Daft Punk/Random Access Memories",
        }
        remote_album = RemoteAlbum(data)
        d = app_module.album_dict(remote_album)

        self.assertEqual(d["id"], 10)
        self.assertEqual(d["album"], "Random Access Memories")
        self.assertEqual(d["albumartist"], "Daft Punk")
        self.assertEqual(d["year"], 2013)
        self.assertEqual(d["genre"], "Disco")
        self.assertEqual(d["mb_albumid"], "a58688b4-e52a-4224-9f51-cd603bf365cf")
        self.assertEqual(d["path"], "/data/media/music/Daft Punk/Random Access Memories")

    def test_album_dict_with_native_beets_album_method(self):
        native_album = FakeBeetsAlbumMethod()
        d = app_module.album_dict(native_album)

        self.assertEqual(d["id"], 1)
        self.assertEqual(d["album"], "Discovery")
        self.assertEqual(d["albumartist"], "Daft Punk")
        self.assertEqual(d["year"], 2001)
        self.assertEqual(d["genre"], "Electronic")
        self.assertEqual(d["path"], "/data/media/music/Daft Punk/Discovery")

    def test_album_dict_with_empty_native_album_fallback(self):
        empty_album = FakeBeetsAlbumEmptyMethod()
        d = app_module.album_dict(empty_album)

        self.assertEqual(d["id"], 2)
        self.assertEqual(d["album"], "Empty Album")
        self.assertEqual(d["path"], "/data/media/music/Unknown Artist/Empty Album")

    def test_album_dict_with_plain_dict(self):
        dict_album = {
            "id": 3,
            "album": "Homework",
            "albumartist": "Daft Punk",
            "year": 1997,
            "genre": "House",
            "mb_albumid": "123456",
            "item_dir": "/data/media/music/Daft Punk/Homework",
        }
        d = app_module.album_dict(dict_album)

        self.assertEqual(d["id"], 3)
        self.assertEqual(d["album"], "Homework")
        self.assertEqual(d["path"], "/data/media/music/Daft Punk/Homework")

    def test_album_dict_with_remote_album_bytes_item_dir(self):
        remote_album = RemoteAlbum({
            "id": 4,
            "album": "Cliche",
            "albumartist": "Artiste Accent",
            "year": 2024,
            "genre": "Electronic",
            "mb_albumid": "bytes-mbid",
            "item_dir": "/data/media/music/Artiste Accent/Cliche".encode("utf-8"),
            "path": "/data/media/music/Artiste Accent/Cliche",
        })
        d = app_module.album_dict(remote_album)

        self.assertEqual(d["path"], "/data/media/music/Artiste Accent/Cliche")

    def test_album_dict_with_non_ascii_path(self):
        album_path = "/data/media/music/Artiste/Caf\u00e9 del Mar"
        remote_album = RemoteAlbum({
            "id": 5,
            "album": "Cafe del Mar",
            "albumartist": "Artiste",
            "item_dir": album_path,
        })
        d = app_module.album_dict(remote_album)

        self.assertEqual(d["path"], album_path)

    def test_empty_item_dir_falls_back_to_album_path(self):
        remote_album = RemoteAlbum({
            "id": 6,
            "album": "Fallback",
            "albumartist": "Artist",
            "item_dir": "",
            "path": "/data/media/music/Artist/Fallback",
        })

        self.assertEqual(app_module._get_album_item_dir(remote_album), "/data/media/music/Artist/Fallback")

    def test_item_dir_takes_precedence_over_path(self):
        remote_album = RemoteAlbum({
            "id": 7,
            "album": "Precedence",
            "albumartist": "Artist",
            "item_dir": "/data/media/music/Artist/Precedence",
            "path": "/data/media/music/Artist/Other",
        })

        self.assertEqual(app_module._get_album_item_dir(remote_album), "/data/media/music/Artist/Precedence")

    def test_callable_item_dir_programming_errors_propagate(self):
        class BrokenAlbum(FakeBeetsAlbumMethod):
            def item_dir(self):
                raise TypeError("programming error")

        with self.assertRaises(TypeError):
            app_module._get_album_item_dir(BrokenAlbum())

    def test_get_album_item_dir_helper_safety(self):
        self.assertEqual(app_module._get_album_item_dir(None), "")
        self.assertEqual(app_module._get_album_item_dir({"item_dir": "/path/a"}), "/path/a")
        self.assertEqual(app_module._get_album_item_dir({"path": "/path/b"}), "/path/b")
        self.assertEqual(app_module._get_album_item_dir({"item_dir": None, "path": "/path/fallback"}), "/path/fallback")
        self.assertEqual(app_module._get_album_item_dir(RemoteAlbum({"item_dir": "/path/c"})), "/path/c")
        self.assertEqual(app_module._get_album_item_dir(FakeBeetsAlbumMethod()), "/data/media/music/Daft Punk/Discovery")


class ApiAlbumsRouteTests(unittest.TestCase):
    """Integration/Route tests for GET /api/albums."""

    def setUp(self):
        self.app = app_module.app
        self.client = self.app.test_client()

    def test_get_api_albums_unauthenticated_returns_401(self):
        with mock.patch("app._security_auth_disabled", return_value=False):
            res = self.client.get("/api/albums")
            self.assertEqual(res.status_code, 401)

    def test_get_api_albums_authenticated_returns_200_and_serialized_albums(self):
        mock_data = [
            RemoteAlbum({
                "id": 1,
                "album": "Album 1",
                "albumartist": "Artist 1",
                "year": 2020,
                "genre": "Rock",
                "mb_albumid": "mb1",
                "item_dir": "/data/media/music/Artist 1/Album 1",
            }),
            RemoteAlbum({
                "id": 2,
                "album": "Album 2",
                "albumartist": "Artist 2",
                "year": 2021,
                "genre": "Pop",
                "mb_albumid": "mb2",
                "item_dir": "/data/media/music/Artist 2/Album 2",
            }),
        ]

        with mock.patch("app.lib.albums", return_value=mock_data), \
             mock.patch("app._security_auth_disabled", return_value=True):
            res = self.client.get("/api/albums?limit=10")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data["count"], 2)
            self.assertEqual(len(data["albums"]), 2)
            self.assertEqual(data["albums"][0]["id"], 1)
            self.assertEqual(data["albums"][0]["album"], "Album 1")
            self.assertEqual(data["albums"][0]["path"], "/data/media/music/Artist 1/Album 1")
            self.assertEqual(data["albums"][1]["id"], 2)
            self.assertEqual(data["albums"][1]["album"], "Album 2")
            self.assertEqual(data["albums"][1]["path"], "/data/media/music/Artist 2/Album 2")

    def test_get_api_albums_honors_limit_parameter(self):
        mock_data = [
            RemoteAlbum({"id": i, "album": f"Album {i}", "albumartist": "Artist", "item_dir": f"/path/{i}"})
            for i in range(1, 10)
        ]

        with mock.patch("app.lib.albums", return_value=mock_data), \
             mock.patch("app._security_auth_disabled", return_value=True):
            res = self.client.get("/api/albums?limit=3")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data["count"], 3)
            self.assertEqual(len(data["albums"]), 3)


if __name__ == "__main__":
    unittest.main()
