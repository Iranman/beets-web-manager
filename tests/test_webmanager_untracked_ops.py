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


REL_NEW = "9d3c1b2a-7e4f-4c6d-8a1b-2f3e4d5c6b7a"
RG_NEW = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


def write_new_release_flac(path, *, track, rel=REL_NEW, rg=RG_NEW):
    write_flac(path, track=track, mb_trackid=rec(100 + track), mb_albumid=rel, samples=44000 + track)
    mf = MediaFile(path)
    mf.mb_releasegroupid, mf.album, mf.albumartist = rg, "New Album", "New Artist"
    mf.save()


class AttachAlbumTests(UntrackedOpsTests):
    """A release with no album row yet becomes a new album row, in place."""

    def setUp(self):
        super().setUp()
        self.new_dir = os.path.join(self.music, "New Artist", "New Album")
        self.files = []
        for track in (1, 2, 3):
            path = os.path.join(self.new_dir, f"{track:02d}.flac")
            write_new_release_flac(path, track=track)
            self.files.append(path)

    def _body(self, paths=None, **overrides):
        body = {"release_id": REL_NEW, "release_group_id": RG_NEW, "files": [
            {"path": p, "sha256": sha(p),
             "expected": {"mb_trackid": rec(100 + int(os.path.basename(p)[:2])), "disc": 1,
                          "track": int(os.path.basename(p)[:2])}} for p in (paths or self.files)]}
        body.update(overrides)
        return body

    def _attach_album(self, key=None, **kwargs):
        return self._post("/webmanager/untracked/attach-album", self._body(**kwargs), key=key)

    def _files_state(self):
        return {p: (sha(p), os.stat(p).st_mtime_ns) for p in self.files}

    def test_attach_album_creates_one_row_in_place_and_rollback_removes_it(self):
        before, files_before = self._library(), self._files_state()
        albums_before = len(list(self.lib.albums()))
        res = self._attach_album()
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()
        album = self.lib.get_album(data["album_id"])
        self.assertEqual((album.mb_albumid, album.mb_releasegroupid), (REL_NEW, RG_NEW))
        self.assertEqual(sorted(os.fsdecode(i.path) for i in album.items()), sorted(self.files))
        self.assertEqual(sorted(i.track for i in album.items()), [1, 2, 3])
        self.assertEqual(len(list(self.lib.albums())), albums_before + 1)
        self.assertEqual(self._files_state(), files_before)  # no tag write, no move

        rb = self._post("/webmanager/untracked/rollback", {"record_id": data["record_id"]})
        self.assertEqual(rb.status_code, 200, rb.get_json())
        self.assertTrue(rb.get_json()["files_untouched"])
        self.assertEqual(self._library(), before)
        self.assertEqual(len(list(self.lib.albums())), albums_before)
        self.assertEqual(self._files_state(), files_before)
        again = self._post("/webmanager/untracked/rollback", {"record_id": data["record_id"]})
        self.assertTrue(again.get_json()["replayed"])

    def test_attach_album_replays_by_key(self):
        first = self._attach_album(key="tx-album").get_json()
        again = self._attach_album(key="tx-album").get_json()
        self.assertTrue(again["replayed"])
        self.assertEqual(again["album_id"], first["album_id"])
        self.assertEqual(len([a for a in self.lib.albums() if a.mb_albumid == REL_NEW]), 1)

    def test_attach_album_refusals_change_nothing(self):
        before = self._library()
        cases = []
        cases.append(("ALBUM_ROW_EXISTS", dict(release_id=REL, release_group_id=RG)))
        cases.append(("EDITION_DIFFERS", dict(release_group_id=RG)))
        cases.append(("IDENTITY_REQUIRED", dict(release_group_id="")))
        cases.append(("INVALID_FILES", dict(files=[])))
        for code, overrides in cases:
            with self.subTest(code=code):
                res = self._attach_album(**overrides)
                self.assertEqual(res.get_json()["error_code"], code, res.get_json())
        body = self._body()
        body["files"][0]["sha256"] = "0" * 64
        self.assertEqual(self._post("/webmanager/untracked/attach-album", body).get_json()["error_code"], "CONTENT_DRIFT")
        body = self._body()
        body["files"][1]["expected"]["mb_trackid"] = rec(999)
        self.assertEqual(self._post("/webmanager/untracked/attach-album", body).get_json()["error_code"], "IDENTITY_MISMATCH")
        body = self._body()
        body["files"].append(dict(body["files"][0]))
        self.assertEqual(self._post("/webmanager/untracked/attach-album", body).get_json()["error_code"], "DUPLICATE_PATH")
        body = self._body()
        body["files"][0]["path"] = os.path.join(self.td, "outside.flac")
        self.assertEqual(self._post("/webmanager/untracked/attach-album", body).get_json()["error_code"], "PATH_INVALID")
        self.assertEqual(self._library(), before)

    def test_two_files_in_one_slot_are_refused(self):
        twin = os.path.join(self.new_dir, "01 (copy).flac")
        write_new_release_flac(twin, track=1)
        body = self._body()
        body["files"].append({"path": twin, "sha256": sha(twin),
                              "expected": {"mb_trackid": rec(101), "disc": 1, "track": 1}})
        res = self._post("/webmanager/untracked/attach-album", body)
        self.assertEqual(res.get_json()["error_code"], "SLOT_OVERLAP")

    def test_a_tracked_file_is_refused(self):
        self.lib.add(Item.from_path(self.files[0]))
        self.assertEqual(self._attach_album().get_json()["error_code"], "FILE_IS_TRACKED")

    def test_failure_mid_way_leaves_no_rows(self):
        before = self._library()
        albums_before = len(list(self.lib.albums()))
        with mock.patch("beetsplug.webmanager.untracked_ops._fspath", side_effect=RuntimeError("boom")):
            res = self._attach_album()
        self.assertEqual(res.status_code, 500)
        self.assertEqual(self._library(), before)
        self.assertEqual(len(list(self.lib.albums())), albums_before)

    def test_rollback_refuses_when_the_album_row_changed(self):
        data = self._attach_album().get_json()
        extra = os.path.join(self.new_dir, "04.flac")
        write_new_release_flac(extra, track=4)
        item = Item.from_path(extra)
        item.album_id = data["album_id"]
        self.lib.add(item)
        rb = self._post("/webmanager/untracked/rollback", {"record_id": data["record_id"]})
        self.assertEqual((rb.status_code, rb.get_json()["error_code"]), (409, "ALBUM_DRIFT"))

    def test_capability_is_advertised(self):
        self.assertIn("untracked_attach_album", ops_mod.get_capabilities())


if __name__ == "__main__":
    unittest.main()
