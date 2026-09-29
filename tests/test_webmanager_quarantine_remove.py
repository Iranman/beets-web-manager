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


if __name__ == "__main__":
    unittest.main()
