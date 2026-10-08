"""D3: a genre edit lands in Beets' ``genres`` field and the file tag.

Beets 2.13+ replaced the single ``genre`` string with the multi-valued
``genres`` field. Writing ``genre`` there only created an unused flexible
attribute: ``$genres`` stayed empty, the file tag was unchanged, and the
job still reported the field as changed. The webmanager plugin now maps
``genre`` to ``genres`` on such a Beets (older Beets keeps ``genre``), the
adapter reads ``genre`` back from ``genres``, and a requested field Beets
cannot store is refused instead of reported as written.

The Web Manager side runs in process against the real plugin and a real
Beets library: BeetsAdapter._request is bridged to the Beets web test client.
"""
import os
import shutil
import struct
import tempfile
import unittest
import wave
from unittest import mock

import mutagen.id3
import mutagen.wave
from beets import config as beets_config
from beets.library import Album, Item, Library
from beetsplug.web import app as beets_web_app
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key_file
from beetsplug.webmanager import schemas
import beetsplug.webmanager.operations as ops_mod

from backend import beets_adapter as adapter_mod
from backend.beets_adapter import BeetsAdapter, BeetsAdapterError, StockBeetsLibrary
import backend.composite_workflows as cw
from backend.transaction_engine import TransactionStore
from backend.transaction_service import _item_transaction_fields

TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
BEETS_HAS_GENRES = "genres" in Item._fields and "genre" not in Item._fields


def _wav(path, genre):
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<h", 0) * 8000)
    mw = mutagen.wave.WAVE(path)
    mw.add_tags()
    for frame in (mutagen.id3.TIT2(encoding=3, text=["Song"]), mutagen.id3.TPE1(encoding=3, text=["Artist"]),
                  mutagen.id3.TALB(encoding=3, text=["Album"]), mutagen.id3.TCON(encoding=3, text=[genre])):
        mw.tags.add(frame)
    mw.save()


def _file_genres(path):
    return [str(g) for g in mutagen.wave.WAVE(path).tags.getall("TCON")[0].text]


class _Bridge(BeetsAdapter):
    def __init__(self, client):
        super().__init__(base_url="http://beets:8337", api_key=TOKEN)
        self._client = client

    def _request(self, method, path, params=None, json_data=None, headers=None, timeout=None):
        res = self._client.open(path, method=method, json=json_data, query_string=params,
                                headers={"Authorization": f"Bearer {TOKEN}", **(headers or {})})
        body = res.get_json(silent=True)
        if res.status_code >= 400:
            raise BeetsAdapterError(f"{res.status_code} {body}", error_code=(body or {}).get("error_code"))
        return body


class GenreEditTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.music = os.path.join(self.td, "music")
        os.makedirs(self.music)
        self._dir = beets_config["directory"].get()
        beets_config["directory"] = self.music
        self.lib = Library(os.path.join(self.td, "lib.blb"), directory=self.music)
        key = os.path.join(self.td, ".key")
        with open(key, "w", encoding="utf-8") as f:
            f.write(TOKEN + "\n")
        WebManagerPlugin()
        set_api_key_file(key)
        ops_mod.set_allowed_roots([self.music])
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["TESTING"] = True
        self.client = beets_web_app.test_client()
        self.path = os.path.join(self.music, "01.wav")
        _wav(self.path, "Jazz")
        item = Item.from_path(self.path)
        self.album = self.lib.add_album([item])
        self.iid = item.id
        self.ad = _Bridge(self.client)
        self.store = TransactionStore(os.path.join(self.td, "tx"))

    def tearDown(self):
        beets_config["directory"] = self._dir
        ops_mod.set_allowed_roots(None)
        set_api_key_file(None)
        try:
            self.lib._connection().close()
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def _item(self):
        return self.lib.get_item(self.iid)

    def _edit(self, fields):
        return cw.update_item_metadata(self.iid, fields, adapter=self.ad, store=self.store)

    @unittest.skipUnless(BEETS_HAS_GENRES, "Beets without the multi-valued genres field")
    def test_genre_edit_reaches_genres_and_file_and_rollback_restores_it(self):
        # What the edit flow captures as the old value (routes_library -> transaction_service).
        before = _item_transaction_fields(StockBeetsLibrary(self.ad).get_item(self.iid))
        self.assertEqual(before["genre"], "Jazz")
        self.assertEqual(self._edit({"genre": "Rock; Pop"})["ok"], True)
        item = self._item()
        self.assertEqual(list(item.genres), ["Rock", "Pop"])
        self.assertNotIn("genre", item._values_flex)
        self.assertEqual(_file_genres(self.path), ["Rock", "Pop"])
        self.assertEqual(self.ad.get_item(self.iid)["genre"], "Rock; Pop")
        # Rollback writes the captured old value back (metadata_restore op).
        with mock.patch.object(cw, "get_default_store", return_value=self.store), \
             mock.patch.object(cw, "beets_adapter", self.ad):
            from backend.library_service import _run_item_metadata_restore
            log = []
            self.assertTrue(_run_item_metadata_restore(self.iid, {"genre": before["genre"]}, log), log)
        self.assertEqual(list(self._item().genres), ["Jazz"])
        self.assertEqual(_file_genres(self.path), ["Jazz"])

    @unittest.skipUnless(BEETS_HAS_GENRES, "Beets without the multi-valued genres field")
    def test_album_genre_edit_reaches_items_and_files(self):
        res = cw.update_album_metadata(self.album.id, {"genre": "Ambient"}, adapter=self.ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(list(self.lib.get_album(self.album.id).genres), ["Ambient"])
        self.assertEqual(list(self._item().genres), ["Ambient"])
        self.assertEqual(_file_genres(self.path), ["Ambient"])

    @unittest.skipUnless(BEETS_HAS_GENRES, "Beets without the multi-valued genres field")
    def test_lastgenre_keeps_existing_genres_and_writes_genres_when_forced(self):
        from beetsplug.webmanager import plugin_ops
        fake = mock.Mock(_get_genre=mock.Mock(return_value=(["Rock", "Pop"], "album")))
        with mock.patch.object(plugin_ops, "_require_plugin", return_value=fake):
            self.assertEqual(plugin_ops.run_lastgenre(self.lib, [self.album.id])["updated_albums"], 0)
            self.assertEqual(plugin_ops.run_lastgenre(self.lib, [self.album.id], force=True)["updated_albums"], 1)
        album = self.lib.get_album(self.album.id)
        self.assertEqual(list(album.genres), ["Rock", "Pop"])
        self.assertNotIn("genre", album._values_flex)

    def test_field_beets_cannot_store_is_refused_and_nothing_changes(self):
        res = self.client.post("/webmanager/modify", headers={"Authorization": f"Bearer {TOKEN}"},
                               json={"album_ids": [self.album.id], "fields": {"media": "CD"}})
        self.assertEqual((res.status_code, res.get_json()["error_code"]), (400, "UNSUPPORTED_FIELDS"))
        self.assertEqual(res.get_json()["fields"], ["media"])
        self.assertNotIn("media", self.lib.get_album(self.album.id)._values_flex)

    def test_failed_file_write_fails_the_apply(self):
        with mock.patch.object(Item, "try_write", side_effect=OSError("read-only")):
            res = self.client.post("/webmanager/modify", headers={"Authorization": f"Bearer {TOKEN}"},
                                   json={"item_ids": [self.iid], "fields": {"title": "New"}})
        self.assertEqual((res.status_code, res.get_json()["error_code"]), (500, "WRITE_FAILED"))
        self.assertEqual(res.get_json()["item_ids"], [self.iid])
        with mock.patch.object(Item, "try_write", side_effect=OSError("read-only")):
            with self.assertRaises(BeetsAdapterError):
                self._edit({"title": "Newer"})


class GenreFieldMappingTests(unittest.TestCase):
    """Older Beets (a fixed ``genre`` field) is left unchanged."""

    def test_mapping_by_beets_version(self):
        old = type("Old", (), {"_fields": {"genre": None, "title": None}})
        new = type("New", (), {"_fields": {"genres": None, "title": None}})
        self.assertEqual(schemas.beets_native_fields(old, {"genre": "Rock; Pop"}), {"genre": "Rock; Pop"})
        self.assertEqual(schemas.beets_native_fields(new, {"genre": "Rock; Pop", "title": "T"}),
                         {"genres": ["Rock", "Pop"], "title": "T"})
        self.assertEqual(schemas.beets_native_fields(new, {"genre": "Rock, Pop"}), {"genres": ["Rock", "Pop"]})
        self.assertEqual(schemas.beets_native_fields(new, {"genre": ""}), {"genres": []})
        self.assertEqual(schemas.unsupported_fields(new, {"genres": [], "title": "", "data_source": "x"}), [])
        self.assertEqual(schemas.unsupported_fields(new, {"genre": "x"}), ["genre"])

    def test_adapter_reads_genre_from_genres(self):
        self.assertEqual(adapter_mod._with_legacy_genre({"genres": ["Rock", "Pop"], "genre": "stale"})["genre"],
                         "Rock; Pop")
        self.assertEqual(adapter_mod._with_legacy_genre({"genre": "Rock"})["genre"], "Rock")


if __name__ == "__main__":
    unittest.main()
