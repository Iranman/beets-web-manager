"""QA #430: the Library payload built from the plugin's fields-limited reads
equals the one built from stock web's full /item/ and /album/ rows.

Real Beets Library + real webmanager plugin (Flask test client); the builder
runs twice over the same temp music root, once per read path. A field missing
from _LIBRARY_ITEM_FIELDS/_LIBRARY_ALBUM_FIELDS reads as "" through RemoteItem
and silently changes the payload, which this catches.
"""
import json
import os
import shutil
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from beets.library import Album, Item, Library
from beetsplug.web import app as beets_web_app
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key_file

import backend.library_service as ls
from backend.beets_adapter import (BeetsAdapter, BeetsAdapterAuthError, BeetsAdapterError,
                                   BeetsAdapterNotFoundError, StockBeetsLibrary)


def setUpModule():
    # backend.app_runtime defaults BEETSDIR to /config, which the test user cannot create.
    cfg = tempfile.mkdtemp()
    patcher = mock.patch.dict(os.environ, {"BEETSDIR": cfg})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    unittest.addModuleCleanup(shutil.rmtree, cfg, True)


class LibraryFieldsParityTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.td, True)
        root = Path(self.td) / "music"
        root.mkdir()
        self.root = root
        self.lib = Library(os.path.join(self.td, "lib.blb"), directory=str(root))
        key_file = os.path.join(self.td, ".key")
        self.token = "d" * 64
        Path(key_file).write_text(self.token + "\n", encoding="utf-8")
        WebManagerPlugin()
        set_api_key_file(key_file)
        self.addCleanup(set_api_key_file, None)
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["INCLUDE_PATHS"] = True
        beets_web_app.config["TESTING"] = True
        self._fixture()
        self.client = beets_web_app.test_client()
        self.adapter = BeetsAdapter(base_url="http://mock-beets:8337", api_key=self.token)
        self.calls = []
        self.adapter._request = self._request

    def _request(self, method, path, params=None, json_data=None, headers=None, timeout=None):
        self.calls.append(path)
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        h = {"Authorization": f"Bearer {self.adapter.api_key}"} if path.startswith("/webmanager") else {}
        resp = self.client.open(path + q, method=method, headers=h)
        if resp.status_code == 404:
            raise BeetsAdapterNotFoundError("nf", status_code=404)
        if resp.status_code == 401:
            raise BeetsAdapterAuthError("auth", status_code=401)
        if resp.status_code >= 400:
            raise BeetsAdapterError("err", status_code=resp.status_code)
        return resp.get_json()

    def _file(self, rel):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        return str(p).encode()

    def _fixture(self):
        lib, add = self.lib, self.lib.add
        # Plain album with art on disk and in Beets, a flexible attr on items and album.
        a1 = Album(album="Plain", albumartist="Alpha", year=2001, albumtype="album", albumtypes="album",
                   mb_albumid="11111111-0000-0000-0000-000000000001",
                   mb_releasegroupid="11111111-0000-0000-0000-0000000000a1",
                   mb_albumartistid="aaaaaaaa-0000-0000-0000-000000000001",
                   albumartist_credit="Alpha!", label="L")
        add(a1)
        art = self.root / "Alpha" / "Plain (2001)" / "cover.jpg"
        art.parent.mkdir(parents=True)
        art.write_bytes(b"jpg")
        a1.artpath = str(art).encode()
        a1["custom_album_flag"] = "yes"
        a1.store()
        for t in range(1, 4):
            it = Item(title=f"P{t}", artist="Alpha", albumartist="Alpha", album="Plain", album_id=a1.id, track=t,
                      tracktotal=4, disc=1, year=2001, mb_trackid=f"t-plain-{t}",
                      path=self._file(f"Alpha/Plain (2001)/0{t} P{t}.flac"))
            it["play_count"] = t
            add(it)
        # Track 4 imported but the file is gone (missing item inside a present folder).
        add(Item(title="P4", artist="Alpha", albumartist="Alpha", album="Plain", album_id=a1.id, track=4,
                 tracktotal=4, disc=1, year=2001, mb_trackid="t-plain-4",
                 path=str(self.root / "Alpha/Plain (2001)/04 P4.flac").encode()))
        # A disk-only file in the same folder.
        self._file("Alpha/Plain (2001)/05 Extra.flac")

        # Multi-disc album in CD 01 / CD 02 sub-folders, no art anywhere.
        a2 = Album(album="Double", albumartist="Alpha", year=2005, albumtype="album",
                   mb_albumid="11111111-0000-0000-0000-000000000002",
                   mb_releasegroupid="11111111-0000-0000-0000-0000000000a2")
        add(a2)
        for d in (1, 2):
            for t in (1, 2):
                add(Item(title=f"D{d}T{t}", artist="Alpha", albumartist="Alpha", album="Double", album_id=a2.id,
                         track=t, tracktotal=2, disc=d, disctotal=2, year=2005, mb_trackid=f"t-dbl-{d}-{t}",
                         path=self._file(f"Alpha/Double (2005)/CD 0{d}/0{t}.flac")))

        # Compilation: albumartist Various Artists, per-track artists differ, EP type.
        a3 = Album(album="Comp", albumartist="Various Artists", comp=True, year=2010, albumtype="compilation",
                   albumtypes="compilation; ep", mb_albumartistid="89ad4ac3-39f7-470e-963a-56509c546377",
                   mb_albumartistids="89ad4ac3-39f7-470e-963a-56509c546377",
                   albumartists="Various Artists", albumartists_credit="VA",
                   mb_albumid="11111111-0000-0000-0000-000000000003",
                   mb_releasegroupid="11111111-0000-0000-0000-0000000000a3")
        add(a3)
        for t, who in enumerate(("Beta", "Gamma"), start=1):
            add(Item(title=f"C{t}", artist=who, albumartist="Various Artists", album="Comp", album_id=a3.id,
                     comp=True, track=t, tracktotal=2, disc=1, year=2010,
                     path=self._file(f"Various Artists/Comp (2010)/0{t}.mp3")))

        # Albumless singleton: a loose file in the artist folder.
        add(Item(title="Loose", artist="Delta", albumartist="", album="", album_id=None, track=0, year=1999,
                 path=self._file("Delta/Loose.mp3")))
        # Albumless item whose file is gone and whose artist has no folder.
        add(Item(title="Ghost", artist="Epsilon", albumartist="", album="", album_id=None, track=1, year=0,
                 path=str(self.root / "Epsilon/Ghost.mp3").encode()))
        # Album row with empty name/artist (builder's album_id fallback), file present.
        a4 = Album(album="", albumartist="", mb_releasegroupid="11111111-0000-0000-0000-0000000000a4")
        add(a4)
        add(Item(title="Nameless", artist="Zeta", albumartist="Zeta", album="Named On Item", album_id=a4.id,
                 track=1, year=2020, path=self._file("Zeta/Named On Item (2020)/01.flac")))
        # Whole album missing from disk (missing-only card).
        a5 = Album(album="Vanished", albumartist="Alpha", year=1990, albumtype="ep",
                   mb_releasegroupid="11111111-0000-0000-0000-0000000000a5")
        add(a5)
        add(Item(title="V1", artist="Alpha", albumartist="Alpha", album="Vanished", album_id=a5.id, track=1,
                 tracktotal=1, year=1990, path=str(self.root / "Alpha/Vanished (1990)/01.flac").encode()))
        # Unicode names.
        a6 = Album(album="Été", albumartist="Ørjan", year=2015)
        add(a6)
        add(Item(title="Ça", artist="Ørjan", albumartist="Ørjan", album="Été", album_id=a6.id, track=1, year=2015,
                 path=self._file("Ørjan/Été (2015)/01 Ça.flac")))
        # F1: album with an empty albumartist and only a flexible `artist`
        # (the builder falls back to it); its file is on disk.
        a7 = Album(album="Thin", albumartist="", year=2021,
                   mb_releasegroupid="11111111-0000-0000-0000-0000000000a7")
        a7["artist"] = "Theta"
        add(a7)
        add(Item(title="Th1", artist="Theta", albumartist="", album="Thin", album_id=a7.id, track=1, year=2021,
                 path=self._file("Theta/Thin (2021)/01.flac")))
        lib._connection().commit()

    def test_album_artist_field_is_null_when_the_flexible_attr_is_absent(self):
        res = self.client.get("/webmanager/fields/album?fields=artist",
                              headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(res.status_code, 200)
        by_album = {r["id"]: r for r in res.get_json()["albums"]}
        artists = sorted((r.get("artist") or "") for r in by_album.values())
        self.assertIn("Theta", artists)
        self.assertTrue(any(r["artist"] is None for r in by_album.values()))

    def _build(self, use_fields):
        if not use_fields:
            self.adapter._no_fields_until = float("inf")  # forces the stock full reads
        with mock.patch.object(ls, "MUSIC_ROOT", self.root), \
                mock.patch.object(ls, "lib", StockBeetsLibrary(self.adapter)), \
                mock.patch.object(ls, "_load_pending_reviews", return_value=[]), \
                mock.patch.object(ls, "_attach_artist_image_cache_urls", lambda result: None):
            payload = ls._build_library_payload()
        payload.pop("library_version", None)
        return payload

    def test_payload_from_fields_read_equals_payload_from_stock_reads(self):
        fields = self._build(use_fields=True)
        self.assertIn("/webmanager/fields/item", self.calls)
        self.assertNotIn("/item/", self.calls)
        self.calls.clear()
        stock = self._build(use_fields=False)
        self.assertIn("/item/", self.calls)
        self.assertNotIn("/webmanager/fields/item", self.calls)
        # Sanity: the fixture actually exercises the interesting shapes.
        albums = {(a["name"], al["album"]): al for a in stock["artists"] for al in a["albums"]}
        self.assertTrue(albums[("Alpha", "Plain")]["artpath"].endswith("cover.jpg"))
        self.assertEqual(albums[("Alpha", "Plain")]["missing"], 1)
        self.assertEqual(albums[("Alpha", "Plain")]["mb_releasegroupid"], "11111111-0000-0000-0000-0000000000a1")
        self.assertEqual(sorted(t["disc"] for t in albums[("Alpha", "Double")]["tracks"]), [1, 1, 2, 2])
        self.assertIn("ep", albums[("Various Artists", "Comp")]["albumtypes"])
        self.assertIn(("Epsilon", "Unknown Album"), albums)
        thin = next(al for (_, name), al in albums.items() if name == "Thin")
        self.assertEqual(thin["albumartist"], "Theta")  # F1
        self.assertEqual(json.dumps(fields, sort_keys=True, default=str),
                         json.dumps(stock, sort_keys=True, default=str))



class FieldsFallbackTests(unittest.TestCase):
    """Only a 404 (plugin before 1.18.0) falls back, and only that is remembered."""

    def _adapter(self, error):
        adapter = BeetsAdapter(base_url="http://mock-beets:8337", api_key="k" * 64)
        calls = []

        def request(method, path, params=None, **kw):
            calls.append(path)
            if path.startswith("/webmanager/fields/") and error is not None:
                raise error
            return {"items": [{"id": 1, "title": "t", "size": 9}]}

        adapter._request = request
        return adapter, calls

    def test_transient_and_auth_errors_propagate_and_are_not_remembered(self):
        from backend.beets_adapter import BeetsAdapterTimeoutError
        for err in (BeetsAdapterError("boom", status_code=500), BeetsAdapterError("bad gw", status_code=502),
                    BeetsAdapterTimeoutError("slow"), BeetsAdapterAuthError("no", status_code=401)):
            adapter, calls = self._adapter(err)
            with self.assertRaises(type(err)):
                adapter.get_fields("item", ["title"])
            self.assertEqual(calls, ["/webmanager/fields/item"], err)  # no silent stock read
            self.assertEqual(getattr(adapter, "_no_fields_until", 0.0), 0.0, err)

    def test_404_is_remembered_for_ten_minutes_then_reprobed(self):
        adapter, calls = self._adapter(BeetsAdapterNotFoundError("nf", status_code=404))
        with mock.patch("backend.beets_adapter.time.monotonic", return_value=1000.0):
            adapter.get_fields("item", ["title"])
            adapter.get_fields("item", ["title"])
        self.assertEqual(calls, ["/webmanager/fields/item", "/item/", "/item/"])
        calls.clear()
        with mock.patch("backend.beets_adapter.time.monotonic", return_value=1000.0 + 601):
            adapter.get_fields("item", ["title"])
        self.assertEqual(calls, ["/webmanager/fields/item", "/item/"])


if __name__ == "__main__":
    unittest.main()
