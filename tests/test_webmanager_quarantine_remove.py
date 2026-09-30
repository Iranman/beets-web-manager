"""Engine op: remove tracked items, moving their files into quarantine.

Real Beets Library and real audio files; the request runs on a fresh thread
(empty music-dir context) like the Beets web server does.
"""

import hashlib
import os
import shutil
import tempfile
import threading
import unittest
import uuid
import wave
from unittest import mock

from beets import config as beets_config
from beets import context as beets_context
from beets.library import Item, Library
from beetsplug.web import app as beets_web_app

import beetsplug.webmanager.operations as ops_mod
import beetsplug.webmanager.replace_ops as replace_mod
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key_file

TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def write_wav(path, value=1):
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(bytes([0, value]) * 8000)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class QuarantineRemoveItemsTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.music = os.path.join(self.td, "music")
        self.quarantine = os.path.join(self.td, "quarantine")
        os.makedirs(os.path.join(self.music, "album"))
        os.makedirs(os.path.join(self.music, "loose"))
        self.lib = Library(os.path.join(self.td, "library.blb"), directory=self.music)
        key_file = os.path.join(self.td, "key")
        with open(key_file, "w", encoding="utf-8") as f:
            f.write(TOKEN + "\n")
        self.plugin = WebManagerPlugin()
        set_api_key_file(key_file)
        ops_mod.set_allowed_roots([self.music])
        replace_mod.set_quarantine_root(self.quarantine)
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["TESTING"] = True
        self.client = beets_web_app.test_client()
        self.auth = {"Authorization": f"Bearer {TOKEN}"}

        keep_path = os.path.join(self.music, "album", "11 Top Notch.wav")
        write_wav(keep_path, 1)
        self.keep = Item.from_path(keep_path)
        self.keep.update({"title": "Top Notch", "track": 11, "disc": 1, "mb_trackid": "rec-1"})
        self.album = self.lib.add_album([self.keep])
        loose_path = os.path.join(self.music, "loose", "top notch (00).wav")
        write_wav(loose_path, 2)
        self.loose = Item.from_path(loose_path)
        self.loose.update({"title": "top notch", "track": 11})
        self.lib.add(self.loose)
        self.loose_path = loose_path
        with open(loose_path, "rb") as f:
            self.loose_bytes = f.read()

    def tearDown(self):
        ops_mod.set_allowed_roots(None)
        replace_mod.set_quarantine_root(None)
        set_api_key_file(None)
        try:
            self.lib._connection().close()
        except Exception:
            pass
        try:
            beets_config["web"]["readonly"] = True
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def _post(self, path, body, key=None):
        out = {}

        def run():
            beets_context.set_music_dir(b"")  # a server thread's fresh state
            out["res"] = self.client.post(path, json=body,
                                          headers={**self.auth, "Idempotency-Key": key or f"op-{uuid.uuid4()}"})
        t = threading.Thread(target=run)
        t.start()
        t.join()
        return out["res"]

    def _remove(self, sha256=None, key=None, item_id=None):
        return self._post("/webmanager/quarantine-remove-items",
                          {"items": [{"item_id": item_id or self.loose.id, "sha256": sha256 or sha(self.loose_path)}]},
                          key=key)

    def test_removes_row_and_quarantines_file(self):
        res = self._remove()
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()
        self.assertIsNone(self.lib.get_item(self.loose.id))
        self.assertFalse(os.path.exists(self.loose_path))
        [removed] = data["removed"]
        self.assertTrue(removed["quarantine_path"].startswith(self.quarantine + os.sep))
        with open(removed["quarantine_path"], "rb") as f:
            self.assertEqual(f.read(), self.loose_bytes)
        self.assertTrue(os.path.isfile(os.path.join(self.quarantine, data["quarantine_id"], "manifest.json")))
        # the keeper and its album are untouched
        keep = self.lib.get_item(self.keep.id)
        self.assertEqual((keep.album_id, keep.track, keep.mb_trackid), (self.album.id, 11, "rec-1"))

    def test_rollback_restores_file_and_row(self):
        data = self._remove().get_json()
        res = self._post("/webmanager/quarantine-remove-items/rollback", {"quarantine_id": data["quarantine_id"]})
        self.assertEqual(res.status_code, 200, res.get_json())
        [restored] = res.get_json()["restored"]
        item = self.lib.get_item(restored["new_item_id"])
        self.assertEqual(os.fsdecode(item.path), self.loose_path)
        self.assertEqual((item.title, item.album_id), ("top notch", None))
        with open(self.loose_path, "rb") as f:
            self.assertEqual(f.read(), self.loose_bytes)

    def test_rollback_puts_an_album_item_back_into_its_album(self):
        extra_path = os.path.join(self.music, "album", "11 Top Notch.1.wav")
        write_wav(extra_path, 3)
        extra = Item.from_path(extra_path)
        extra.update({"title": "Top Notch", "track": 11})
        extra.album_id = self.album.id
        self.lib.add(extra)
        data = self._remove(item_id=extra.id, sha256=sha(extra_path)).get_json()
        res = self._post("/webmanager/quarantine-remove-items/rollback", {"quarantine_id": data["quarantine_id"]})
        [restored] = res.get_json()["restored"]
        self.assertEqual(restored["album_id"], self.album.id)

    def test_changed_file_is_refused_before_any_change(self):
        res = self._remove(sha256="0" * 64)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error_code"], "ITEM_CHANGED")
        self.assertIsNotNone(self.lib.get_item(self.loose.id))
        self.assertTrue(os.path.isfile(self.loose_path))

    def test_one_bad_item_refuses_the_whole_request(self):
        res = self._post("/webmanager/quarantine-remove-items", {"items": [
            {"item_id": self.loose.id, "sha256": sha(self.loose_path)},
            {"item_id": 999999, "sha256": "0" * 64},
        ]})
        self.assertEqual(res.status_code, 404)
        self.assertIsNotNone(self.lib.get_item(self.loose.id))
        self.assertTrue(os.path.isfile(self.loose_path))

    def test_invalid_payloads(self):
        for body, code in (({"items": []}, "INVALID_ITEMS"),
                           ({"items": [{"item_id": "x", "sha256": "0" * 64}]}, "INVALID_IDS"),
                           ({"items": [{"item_id": self.loose.id, "sha256": "short"}]}, "INVALID_SHA256")):
            res = self._post("/webmanager/quarantine-remove-items", body)
            self.assertEqual(res.status_code, 400, body)
            self.assertEqual(res.get_json()["error_code"], code)

    def test_failure_mid_way_restores_file_and_row(self):
        with mock.patch.object(Item, "remove", side_effect=OSError("db locked")):
            res = self._remove()
        self.assertEqual(res.status_code, 500)
        self.assertTrue(os.path.isfile(self.loose_path))
        self.assertIsNotNone(self.lib.get_item(self.loose.id))

    def test_retry_with_same_key_replays(self):
        digest = sha(self.loose_path)
        first = self._remove(key="op-replay-remove", sha256=digest)
        again = self._remove(key="op-replay-remove", sha256=digest)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.get_json()["result"]["quarantine_id"], first.get_json()["quarantine_id"])

    def test_rollback_refuses_a_replace_manifest_and_bad_ids(self):
        res = self._post("/webmanager/quarantine-remove-items/rollback", {"quarantine_id": "../x"})
        self.assertEqual(res.get_json()["error_code"], "INVALID_QUARANTINE_ID")
        res = self._post("/webmanager/quarantine-remove-items/rollback", {"quarantine_id": "a" * 32})
        self.assertEqual(res.status_code, 404)

    def test_capability(self):
        self.assertIn("quarantine_remove_items", ops_mod.get_capabilities())

    # -- a duplicate album row holding only the copy (sibling-row retirement) --

    def _sibling_rows(self, *, dup_release="rel-1", dup_rg="rg-1", extra_in_dup=False):
        """Keeper row: rel-1/rg-1 with slot 1/11 (self.keep). Duplicate row:
        the same release (by default) holding only the copy of that slot."""
        self.album.mb_albumid, self.album.mb_releasegroupid, self.album.album = "rel-1", "rg-1", "Top"
        self.album.store()
        self.keep.load()
        self.keep.update({"disc": 1, "track": 11})
        self.keep.store()
        os.makedirs(os.path.join(self.music, "dup"))
        dup_path = os.path.join(self.music, "dup", "11 Top Notch.wav")
        write_wav(dup_path, 4)
        dup = Item.from_path(dup_path)
        dup.update({"title": "Top Notch", "track": 11, "disc": 1, "mb_trackid": "rec-1"})
        items = [dup]
        if extra_in_dup:
            other_path = os.path.join(self.music, "dup", "12 Other.wav")
            write_wav(other_path, 5)
            other = Item.from_path(other_path)
            other.update({"title": "Other", "track": 12, "disc": 1})
            items.append(other)
        dup_album = self.lib.add_album(items)
        dup_album.mb_albumid, dup_album.mb_releasegroupid, dup_album.album = dup_release, dup_rg, "Top"
        dup_album.store()
        return dup, dup_path, dup_album

    def _retire(self, dup, dup_path, album_id, keeper_id=None, key=None):
        return self._post("/webmanager/quarantine-remove-items", {"items": [{
            "item_id": dup.id, "sha256": sha(dup_path), "retire_album_id": album_id,
            "sibling_keeper_item_id": keeper_id or self.keep.id}]}, key=key)

    def test_last_item_of_a_row_is_refused_without_an_explicit_retirement(self):
        dup, dup_path, dup_album = self._sibling_rows()
        res = self._remove(item_id=dup.id, sha256=sha(dup_path))
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["error_code"], "ALBUM_WOULD_EMPTY")
        self.assertIsNotNone(self.lib.get_album(dup_album.id))
        self.assertTrue(os.path.isfile(dup_path))

    def test_sibling_row_is_retired_and_rollback_restores_the_exact_ids(self):
        dup, dup_path, dup_album = self._sibling_rows()
        dup_id, album_id = dup.id, dup_album.id
        res = self._retire(dup, dup_path, album_id)
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()
        self.assertEqual(data["retired_album_ids"], [album_id])
        self.assertIsNone(self.lib.get_item(dup_id))
        self.assertIsNone(self.lib.get_album(album_id))
        self.assertFalse(os.path.exists(dup_path))
        keep = self.lib.get_item(self.keep.id)
        self.assertEqual((keep.album_id, keep.disc, keep.track), (self.album.id, 1, 11))

        rb = self._post("/webmanager/quarantine-remove-items/rollback", {"quarantine_id": data["quarantine_id"]})
        self.assertEqual(rb.status_code, 200, rb.get_json())
        [restored] = rb.get_json()["restored"]
        self.assertEqual((restored["old_item_id"], restored["new_item_id"], restored["album_id"]),
                         (dup_id, dup_id, album_id))
        album = self.lib.get_album(album_id)
        self.assertEqual((album.mb_albumid, album.mb_releasegroupid, album.album), ("rel-1", "rg-1", "Top"))
        self.assertEqual([i.id for i in self.lib.items(f"album_id:{album_id}")], [dup_id])
        self.assertTrue(os.path.isfile(dup_path))

    def test_retirement_refused_for_another_release_or_a_row_with_other_items(self):
        for kwargs, code in (({"dup_release": "rel-2"}, "NO_SIBLING_KEEPER"),
                             ({"dup_rg": "rg-2"}, "NO_SIBLING_KEEPER"),
                             ({"extra_in_dup": True}, "RETIRE_NOT_SOLE_ITEM")):
            with self.subTest(**kwargs):
                self.tearDown()
                self.setUp()
                dup, dup_path, dup_album = self._sibling_rows(**kwargs)
                res = self._retire(dup, dup_path, dup_album.id)
                self.assertEqual(res.status_code, 409, res.get_json())
                self.assertEqual(res.get_json()["error_code"], code)
                self.assertIsNotNone(self.lib.get_album(dup_album.id))
                self.assertTrue(os.path.isfile(dup_path))

    def test_retirement_refused_when_the_keeper_is_in_the_same_row_or_its_file_is_gone(self):
        dup, dup_path, dup_album = self._sibling_rows()
        res = self._retire(dup, dup_path, dup_album.id, keeper_id=dup.id)
        self.assertEqual(res.get_json()["error_code"], "NO_SIBLING_KEEPER")
        os.remove(os.fsdecode(self.keep.path))
        res = self._retire(dup, dup_path, dup_album.id)
        self.assertEqual(res.get_json()["error_code"], "NO_SIBLING_KEEPER")
        self.assertIsNotNone(self.lib.get_album(dup_album.id))

    def test_failure_after_row_retirement_restores_row_item_and_file(self):
        from beets.library import Album
        dup, dup_path, dup_album = self._sibling_rows()
        dup_id, album_id = dup.id, dup_album.id
        real_remove = Album.remove

        def remove_then_fail(album, *a, **k):
            real_remove(album, *a, **k)
            raise OSError("db locked")

        with mock.patch.object(Album, "remove", remove_then_fail):
            res = self._retire(dup, dup_path, album_id)
        self.assertEqual(res.status_code, 500)
        self.assertIsNotNone(self.lib.get_album(album_id))
        self.assertEqual(self.lib.get_item(dup_id).album_id, album_id)
        self.assertTrue(os.path.isfile(dup_path))

    def test_rollback_refuses_when_the_album_id_was_taken_again(self):
        dup, dup_path, dup_album = self._sibling_rows()
        data = self._retire(dup, dup_path, dup_album.id).get_json()
        from beetsplug.webmanager.merge_ops import _restore_album_row
        _restore_album_row(self.lib, dup_album.id, {"album": "someone else"})
        rb = self._post("/webmanager/quarantine-remove-items/rollback", {"quarantine_id": data["quarantine_id"]})
        self.assertEqual(rb.status_code, 409)
        self.assertEqual(rb.get_json()["error_code"], "ALBUM_ID_OCCUPIED")


if __name__ == "__main__":
    unittest.main()
