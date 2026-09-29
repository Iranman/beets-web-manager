"""Engine ops for untracked files (ARCH-021) on a real Beets library:
attach (into a free album slot or as a singleton) and quarantine, each with
rollback; requests run on fresh threads like the Beets web server."""

import hashlib
import os
import shutil
import struct
import tempfile
import threading
import unittest
import uuid
from unittest import mock

from beets import config as beets_config
from beets import context as beets_context
from beets.library import Item, Library
from beetsplug.web import app as beets_web_app
from mediafile import MediaFile

import beetsplug.webmanager.operations as ops_mod
import beetsplug.webmanager.replace_ops as replace_mod
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key_file

TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
REL = "45347542-db98-422a-a307-ae95d5371f60"
RG = "ef4b6576-ac7c-4f72-bee6-e7a6b6cf019d"


def rec(n):
    return f"00000000-0000-0000-0000-{n:012d}"


def write_flac(path, *, track, mb_trackid, mb_albumid=REL, samples=44100):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    packed = (44100 << 44) | (1 << 41) | (15 << 36) | samples
    info = struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + packed.to_bytes(8, "big") + b"\x00" * 16
    with open(path, "wb") as f:
        f.write(b"fLaC" + bytes([0x80]) + len(info).to_bytes(3, "big") + info)
    mf = MediaFile(path)
    mf.title, mf.track, mf.disc = f"Track {track}", track, 1
    mf.mb_trackid, mf.mb_albumid, mf.mb_releasegroupid = mb_trackid, mb_albumid, RG
    mf.album, mf.albumartist, mf.artist = "2 Slippery", "BossMan Dlow", "BossMan Dlow"
    mf.save()


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class UntrackedOpsTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.music = os.path.join(self.td, "music")
        self.album_dir = os.path.join(self.music, "BossMan Dlow", "2 Slippery")
        self.lib = Library(os.path.join(self.td, "library.blb"), directory=self.music)
        key_file = os.path.join(self.td, "key")
        with open(key_file, "w", encoding="utf-8") as f:
            f.write(TOKEN + "\n")
        self.plugin = WebManagerPlugin()
        set_api_key_file(key_file)
        ops_mod.set_allowed_roots([self.music])
        replace_mod.set_quarantine_root(os.path.join(self.td, "quarantine"))
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["TESTING"] = True
        self.client = beets_web_app.test_client()
        self.auth = {"Authorization": f"Bearer {TOKEN}"}
        tracked = os.path.join(self.album_dir, "11 Top Notch.flac")
        write_flac(tracked, track=11, mb_trackid=rec(11))
        item = Item.from_path(tracked)
        self.album = self.lib.add_album([item])
        self.album.update({"mb_albumid": REL, "mb_releasegroupid": RG})
        self.album.store()
        self.missing = os.path.join(self.album_dir, "BossMan Dlow - 2 Slippery - 13 - Parmesan.flac")
        write_flac(self.missing, track=13, mb_trackid=rec(13), samples=44000)

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
            beets_context.set_music_dir(b"")
            out["res"] = self.client.post(path, json=body, headers={**self.auth,
                                                                    "Idempotency-Key": key or f"tx-{uuid.uuid4()}"})
        t = threading.Thread(target=run)
        t.start()
        t.join()
        return out["res"]

    def _attach(self, key=None, **overrides):
        body = {"path": self.missing, "sha256": sha(self.missing), "album_id": self.album.id,
                "expected": {"mb_trackid": rec(13), "mb_albumid": REL, "disc": 1, "track": 13}}
        body.update(overrides)
        return self._post("/webmanager/untracked/attach", body, key=key)

    def _library(self):
        return sorted((i.id, i.album_id, os.fsdecode(i.path)) for i in self.lib.items())

    def test_attach_into_a_free_album_slot_and_roll_back(self):
        before, file_before = self._library(), (sha(self.missing), os.stat(self.missing).st_mtime_ns)
        res = self._attach()
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()
        item = self.lib.get_item(data["item_id"])
        self.assertEqual((item.album_id, item.track, item.mb_trackid), (self.album.id, 13, rec(13)))
        self.assertEqual(os.path.realpath(os.fsdecode(item.path)), os.path.realpath(self.missing))
        self.assertEqual((sha(self.missing), os.stat(self.missing).st_mtime_ns), file_before)  # nothing written
        rb = self._post("/webmanager/untracked/rollback", {"record_id": data["record_id"]})
        self.assertEqual(rb.status_code, 200, rb.get_json())
        self.assertEqual(self._library(), before)
        self.assertEqual((sha(self.missing), os.stat(self.missing).st_mtime_ns), file_before)
        again = self._post("/webmanager/untracked/rollback", {"record_id": data["record_id"]})
        self.assertTrue(again.get_json()["replayed"])

    def test_attach_as_a_singleton(self):
        data = self._attach(album_id=None).get_json()
        self.assertIsNone(self.lib.get_item(data["item_id"]).album_id)

    def test_attach_refusals(self):
        self.assertEqual(self._attach(expected={"mb_trackid": rec(99), "mb_albumid": REL, "disc": 1, "track": 13})
                         .get_json()["error_code"], "IDENTITY_MISMATCH")
        self.assertEqual(self._attach(sha256="0" * 64).get_json()["error_code"], "CONTENT_DRIFT")
        self.assertEqual(self._attach(path=os.path.join(self.td, "outside.flac")).get_json()["error_code"],
                         "PATH_INVALID")
        self.assertEqual(self._attach(path=self.missing + "/../../../../x.flac").get_json()["error_code"],
                         "PATH_INVALID")
        tracked = os.path.join(self.album_dir, "11 Top Notch.flac")
        self.assertEqual(self._attach(path=tracked, sha256=sha(tracked)).get_json()["error_code"], "FILE_IS_TRACKED")
        occupant = os.path.join(self.album_dir, "dup 11.flac")
        write_flac(occupant, track=11, mb_trackid=rec(11), samples=43000)
        self.assertEqual(self._attach(path=occupant, sha256=sha(occupant),
                                      expected={"mb_trackid": rec(11), "mb_albumid": REL, "disc": 1, "track": 11})
                         .get_json()["error_code"], "SLOT_OCCUPIED")
        self.assertEqual(len(list(self.lib.items())), 1)

    def test_attach_replays_after_a_restart_instead_of_adding_twice(self):
        key = f"tx-{uuid.uuid4()}"
        first = self._attach(key=key).get_json()
        ops_mod._operations.clear()
        again = self._attach(key=key)
        self.assertTrue(again.get_json()["replayed"])
        self.assertEqual(again.get_json()["item_id"], first["item_id"])
        self.assertEqual(len(list(self.lib.items())), 2)

    def test_attach_failure_keeps_nothing(self):
        before = self._library()
        with mock.patch.object(Library, "get_item", side_effect=RuntimeError("db gone")):
            res = self._attach()
        self.assertEqual(res.status_code, 500)
        self.assertEqual(self._library(), before)

    def test_quarantine_and_roll_back(self):
        digest = sha(self.missing)
        res = self._post("/webmanager/untracked/quarantine", {"files": [{"path": self.missing, "sha256": digest}]})
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()
        self.assertFalse(os.path.exists(self.missing))
        qpath = data["quarantined"][0]["quarantine_path"]
        self.assertEqual(sha(qpath), digest)
        rb = self._post("/webmanager/untracked/rollback", {"record_id": data["record_id"]})
        self.assertEqual(rb.status_code, 200, rb.get_json())
        self.assertEqual(sha(self.missing), digest)

    def test_quarantine_refusals_touch_nothing(self):
        tracked = os.path.join(self.album_dir, "11 Top Notch.flac")
        for files, code in (([{"path": tracked, "sha256": sha(tracked)}], "FILE_IS_TRACKED"),
                            ([{"path": self.missing, "sha256": "0" * 64}], "CONTENT_DRIFT"),
                            ([{"path": self.missing}], "INVALID_SHA256"),
                            ([{"path": "/etc/passwd", "sha256": "0" * 64}], "PATH_INVALID")):
            res = self._post("/webmanager/untracked/quarantine", {"files": files})
            self.assertEqual(res.get_json()["error_code"], code)
        self.assertTrue(os.path.exists(self.missing))

    def test_rollback_refuses_rather_than_half_succeeding(self):
        data = self._post("/webmanager/untracked/quarantine",
                          {"files": [{"path": self.missing, "sha256": sha(self.missing)}]}).get_json()
        os.remove(data["quarantined"][0]["quarantine_path"])
        rb = self._post("/webmanager/untracked/rollback", {"record_id": data["record_id"]})
        self.assertEqual(rb.get_json()["error_code"], "QUARANTINE_FILE_MISSING")

    def test_status_endpoint(self):
        data = self._attach().get_json()
        res = self.client.get(f"/webmanager/untracked/{data['record_id']}", headers=self.auth)
        self.assertEqual((res.get_json()["kind"], res.get_json()["status"]), ("attach", "applied"))

    def test_capabilities(self):
        caps = ops_mod.get_capabilities()
        self.assertIn("untracked_attach", caps)
        self.assertIn("untracked_quarantine", caps)


if __name__ == "__main__":
    unittest.main()
