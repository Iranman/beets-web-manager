"""Engine op: replace an album slot's audio file with another tracked copy.

Real Beets Library, real audio files: the album track is a WAV (standing in
for the lossy album copy), the replacement is a minimal valid FLAC singleton.
"""

import os
import shutil
import struct
import tempfile
import unittest
import uuid
import wave

from beets import config as beets_config
from beets.library import Item, Library
from beetsplug.web import app as beets_web_app
from mediafile import MediaFile

import beetsplug.webmanager.operations as ops_mod
import beetsplug.webmanager.replace_ops as replace_mod
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key_file

TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
REC = "2513c401-c500-42fe-9113-ce9d9c3295d5"
REL = "22222222-2222-2222-2222-222222222222"
RG = "33333333-3333-3333-3333-333333333333"


def write_wav(path):
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x01" * 8000)


def write_flac(path):
    """fLaC marker + a single (last) STREAMINFO block: 1 s, 44.1 kHz, 16-bit stereo."""
    packed = (44100 << 44) | (1 << 41) | (15 << 36) | 44100
    streaminfo = struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + packed.to_bytes(8, "big") + b"\x00" * 16
    with open(path, "wb") as f:
        f.write(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)


class ReplaceItemFileTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.music = os.path.join(self.td, "music")
        self.quarantine = os.path.join(self.td, "quarantine")
        os.makedirs(os.path.join(self.music, "loose"))
        os.makedirs(os.path.join(self.music, "album"))
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

        mp3ish = os.path.join(self.music, "album", "17 Exotic.wav")
        write_wav(mp3ish)
        self.target = Item.from_path(mp3ish)
        self.target.update({
            "title": "Exotic", "artist": "BossMan Dlow", "albumartist": "BossMan Dlow",
            "album": "2 Slippery", "track": 17, "disc": 1, "mb_trackid": REC,
            "mb_albumid": REL, "mb_releasegroupid": RG,
        })
        self.album = self.lib.add_album([self.target])
        self.target.load()
        self.old_target_path = os.fsdecode(self.target.path)
        self.old_format = self.target.format
        with open(self.old_target_path, "rb") as f:
            self.old_bytes = f.read()

        flac = os.path.join(self.music, "loose", "bossman dlow - exotic (00).flac")
        write_flac(flac)
        self.source = Item.from_path(flac)
        self.source.update({"title": "exotic (00)", "artist": "bossman dlow", "track": 17, "disc": 0})
        self.lib.add(self.source)
        self.source_path = flac

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

    def _replace(self, key=None, **overrides):
        key = key or f"op-{uuid.uuid4()}"
        body = {"target_item_id": self.target.id, "source_item_id": self.source.id, **overrides}
        return self.client.post("/webmanager/replace-item-file", json=body,
                                headers={**self.auth, "Idempotency-Key": key})

    def test_replace_moves_flac_into_album_slot_and_keeps_identity(self):
        res = self._replace()
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()

        item = self.lib.get_item(self.target.id)
        new_path = os.fsdecode(item.path)
        self.assertEqual(new_path, data["new_target_path"])
        self.assertTrue(new_path.endswith(".flac"), new_path)
        self.assertTrue(os.path.isfile(new_path))
        self.assertEqual(item.format, "FLAC")
        # identity preserved
        self.assertEqual(item.album_id, self.album.id)
        self.assertEqual((item.mb_trackid, item.mb_albumid, item.mb_releasegroupid), (REC, REL, RG))
        self.assertEqual((item.disc, item.track, item.title), (1, 17, "Exotic"))
        # canonical Beets path, not the loose folder
        self.assertEqual(new_path, os.fsdecode(item.destination()))
        self.assertFalse(os.path.exists(self.source_path))
        # the album tags were written into the FLAC
        tags = MediaFile(new_path)
        self.assertEqual((tags.mb_trackid, tags.title, tags.track), (REC, "Exotic", 17))
        # the replacement's row is gone; the album still has exactly one track 17
        self.assertIsNone(self.lib.get_item(self.source.id))
        self.assertEqual([i.track for i in self.lib.get_album(self.album.id).items()], [17])
        # old file quarantined intact, never deleted
        self.assertFalse(os.path.exists(self.old_target_path))
        self.assertTrue(data["quarantine_path"].startswith(self.quarantine + os.sep))
        with open(data["quarantine_path"], "rb") as f:
            self.assertEqual(f.read(), self.old_bytes)

    def test_retry_with_same_key_replays_instead_of_failing(self):
        self.replay_key = f"op-{uuid.uuid4()}"
        first = self._replace(key=self.replay_key)
        self.assertEqual(first.status_code, 200)
        again = self._replace(key=self.replay_key)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.get_json()["result"]["new_target_path"], first.get_json()["new_target_path"])

    def test_rollback_restores_both_items_and_files(self):
        data = self._replace().get_json()
        res = self.client.post("/webmanager/replace-item-file/rollback", headers=self.auth, json={
            "target_item_id": self.target.id,
            "target_snapshot": data["target_snapshot"],
            "source_snapshot": data["source_snapshot"],
            "quarantine_path": data["quarantine_path"],
        })
        self.assertEqual(res.status_code, 200, res.get_json())
        out = res.get_json()
        item = self.lib.get_item(self.target.id)
        self.assertEqual(os.fsdecode(item.path), self.old_target_path)
        self.assertEqual(item.format, self.old_format)
        self.assertEqual(item.album_id, self.album.id)
        with open(self.old_target_path, "rb") as f:
            self.assertEqual(f.read(), self.old_bytes)
        recreated = self.lib.get_item(out["recreated_source_item_id"])
        self.assertEqual(os.fsdecode(recreated.path), self.source_path)
        self.assertEqual((recreated.format, recreated.title, recreated.album_id), ("FLAC", "exotic (00)", None))
        self.assertTrue(os.path.isfile(self.source_path))

    def test_rollback_refuses_a_quarantine_path_outside_the_engine_folder(self):
        data = self._replace().get_json()
        res = self.client.post("/webmanager/replace-item-file/rollback", headers=self.auth, json={
            "target_item_id": self.target.id,
            "target_snapshot": data["target_snapshot"],
            "source_snapshot": data["source_snapshot"],
            "quarantine_path": os.path.join(self.td, "elsewhere.wav"),
        })
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "QUARANTINE_PATH_INVALID")

    def test_rejects_a_target_that_is_not_in_an_album(self):
        res = self.client.post("/webmanager/replace-item-file", headers=self.auth, json={
            "target_item_id": self.source.id, "source_item_id": self.target.id})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "TARGET_NOT_IN_ALBUM")
        self.assertTrue(os.path.exists(self.old_target_path))
        self.assertTrue(os.path.exists(self.source_path))

    def test_rejects_same_item_and_source_outside_allowed_roots(self):
        res = self._replace(key=None, source_item_id=self.target.id)
        self.assertEqual(res.get_json()["error_code"], "SAME_ITEM")
        ops_mod.set_allowed_roots([os.path.join(self.music, "album")])
        res = self._replace(key=None)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error_code"], "SOURCE_PATH_INVALID")
        self.assertTrue(os.path.exists(self.old_target_path))

    def test_requires_auth(self):
        res = self.client.post("/webmanager/replace-item-file",
                               json={"target_item_id": self.target.id, "source_item_id": self.source.id})
        self.assertEqual(res.status_code, 401)
        self.assertTrue(os.path.exists(self.old_target_path))

    def test_capability_is_advertised(self):
        self.assertIn("replace_item_file", ops_mod.get_capabilities())


if __name__ == "__main__":
    unittest.main()
