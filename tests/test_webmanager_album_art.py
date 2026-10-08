"""Engine op: set an album's cover through Beets (Album.set_art + embedart).

Real Beets Library, real FLAC files, the real embedart plugin listening on
``art_set``; rollback must restore the previous cover file, artpath and
embedded images exactly.
"""

import base64
import hashlib
import json
import io
import os
import shutil
import struct
import tempfile
import unittest
import uuid

from beets import config as beets_config
from beets.library import Item, Library
from beets.plugins import BeetsPlugin
from beetsplug.web import app as beets_web_app
from mediafile import Image as MFImage
from mediafile import MediaFile
from PIL import Image

import beetsplug.webmanager.art_ops as art_mod
import beetsplug.webmanager.operations as ops_mod
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key_file
from beetsplug.webmanager.engine_common import set_quarantine_root

TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
RG = "33333333-3333-3333-3333-333333333333"


def write_flac(path):
    packed = (44100 << 44) | (1 << 41) | (15 << 36) | 44100
    streaminfo = struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + packed.to_bytes(8, "big") + b"\x00" * 16
    with open(path, "wb") as f:
        f.write(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)


def image_bytes(fmt="JPEG", color=(200, 10, 10)):
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(buf, format=fmt)
    return buf.getvalue()


def sha(data):
    return hashlib.sha256(data).hexdigest()


class AlbumArtEngineTests(unittest.TestCase):
    embed = True

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.music = os.path.join(self.td, "music")
        self.quarantine = os.path.join(self.td, "quarantine")
        os.makedirs(os.path.join(self.music, "Artist", "Album"))
        self.lib = Library(os.path.join(self.td, "library.blb"), directory=self.music)
        key_file = os.path.join(self.td, "key")
        with open(key_file, "w", encoding="utf-8") as f:
            f.write(TOKEN + "\n")
        self.plugin = WebManagerPlugin()
        set_api_key_file(key_file)
        ops_mod.set_allowed_roots([self.music])
        set_quarantine_root(self.quarantine)
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["TESTING"] = True
        self.client = beets_web_app.test_client()
        self.auth = {"Authorization": f"Bearer {TOKEN}"}
        self._listeners_before = list(BeetsPlugin.listeners["art_set"])
        if self.embed:
            from beetsplug.embedart import EmbedCoverArtPlugin
            self.embedart = EmbedCoverArtPlugin()

        self.items = []
        for n in (1, 2):
            path = os.path.join(self.music, "Artist", "Album", f"0{n} Track.flac")
            write_flac(path)
            item = Item.from_path(path)
            item.update({"title": f"Track {n}", "artist": "Artist", "albumartist": "Artist",
                         "album": "Album", "track": n, "mb_releasegroupid": RG})
            self.items.append(item)
        self.album = self.lib.add_album(self.items)
        self.album_dir = os.path.join(self.music, "Artist", "Album")

    def tearDown(self):
        BeetsPlugin.listeners["art_set"][:] = self._listeners_before
        ops_mod.set_allowed_roots(None)
        set_quarantine_root(None)
        set_api_key_file(None)
        try:
            self.lib._connection().close()
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def _set(self, data, key=None, **overrides):
        body = {"album_id": self.album.id, "image_b64": base64.b64encode(data).decode("ascii"),
                "image_sha256": sha(data), **overrides}
        return self.client.post("/webmanager/album-art", json=body,
                                headers={**self.auth, "Idempotency-Key": key or f"op-{uuid.uuid4()}"})

    def _rollback(self, art_id, key=None):
        return self.client.post("/webmanager/album-art/rollback", json={"art_id": art_id},
                                headers={**self.auth, "Idempotency-Key": key or f"rb-{uuid.uuid4()}"})

    def _embedded(self):
        return [[sha(i.data) for i in (MediaFile(os.fsdecode(it.path)).images or [])]
                for it in self.lib.items(f"album_id:{self.album.id}")]

    def _artpath(self):
        return os.fsdecode(self.lib.get_album(self.album.id).artpath or b"")

    def test_new_cover_is_set_embedded_and_rolled_back(self):
        new = image_bytes()
        res = self._set(new, expected_mb_releasegroupid=RG)
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()
        cover = os.path.join(self.album_dir, "cover.jpg")
        self.assertEqual(os.path.normcase(self._artpath()), os.path.normcase(cover))
        with open(cover, "rb") as f:
            self.assertEqual(f.read(), new)
        if self.embed:
            self.assertEqual(data["embedded_items"], 2)
            self.assertTrue(all(e for e in self._embedded()))
        else:
            self.assertEqual(self._embedded(), [[], []])

        rb = self._rollback(data["art_id"])
        self.assertEqual(rb.status_code, 200, rb.get_json())
        self.assertEqual(self._artpath(), "")
        self.assertFalse(os.path.exists(cover))
        self.assertTrue(os.path.isfile(os.path.join(self.quarantine, data["art_id"], "replaced", "cover.jpg")))
        self.assertEqual(self._embedded(), [[], []])
        # a second rollback with a new key finds nothing applied
        self.assertEqual(self._rollback(data["art_id"]).status_code, 404)

    def test_existing_cover_and_embedded_art_are_restored_exactly(self):
        old = image_bytes("PNG", (1, 2, 3))
        old_cover = os.path.join(self.album_dir, "cover.png")
        with open(old_cover, "wb") as f:
            f.write(old)
        self.album.artpath = os.fsencode(old_cover)
        self.album.store()
        for it in self.items:
            it.write(tags={"images": [MFImage(data=old, desc="orig", type=3)]})
        before = self._embedded()

        new = image_bytes()
        res = self._set(new)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue(self._artpath().endswith("cover.jpg"))

        rb = self._rollback(res.get_json()["art_id"])
        self.assertEqual(rb.status_code, 200, rb.get_json())
        self.assertEqual(os.path.normcase(self._artpath()), os.path.normcase(old_cover))
        with open(old_cover, "rb") as f:
            self.assertEqual(f.read(), old)
        self.assertFalse(os.path.exists(os.path.join(self.album_dir, "cover.jpg")))
        self.assertEqual(self._embedded(), before)

    def test_same_name_cover_is_replaced_and_restored(self):
        old = image_bytes("JPEG", (9, 9, 9))
        cover = os.path.join(self.album_dir, "cover.jpg")
        with open(cover, "wb") as f:
            f.write(old)
        self.album.artpath = os.fsencode(cover)
        self.album.store()
        new = image_bytes("JPEG", (250, 250, 0))
        res = self._set(new)
        self.assertEqual(res.status_code, 200, res.get_json())
        with open(cover, "rb") as f:
            self.assertEqual(f.read(), new)
        self.assertEqual(self._rollback(res.get_json()["art_id"]).status_code, 200)
        with open(cover, "rb") as f:
            self.assertEqual(f.read(), old)
        self.assertEqual(os.path.normcase(self._artpath()), os.path.normcase(cover))

    def test_untracked_file_at_the_destination_comes_back(self):
        stray = image_bytes("JPEG", (7, 7, 7))
        cover = os.path.join(self.album_dir, "cover.jpg")
        with open(cover, "wb") as f:
            f.write(stray)
        res = self._set(image_bytes())
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self._rollback(res.get_json()["art_id"]).status_code, 200)
        with open(cover, "rb") as f:
            self.assertEqual(f.read(), stray)
        self.assertEqual(self._artpath(), "")

    def test_replay_returns_the_stored_result(self):
        new = image_bytes()
        key = f"same-{uuid.uuid4()}"
        first = self._set(new, key=key)
        again = self._set(new, key=key)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.get_json()["result"]["art_id"], first.get_json()["art_id"])
        self.assertEqual(len(os.listdir(self.quarantine)), 1)


class AlbumArtWithoutEmbedartTests(AlbumArtEngineTests):
    embed = False


class AlbumArtRefusalTests(AlbumArtEngineTests):
    embed = False

    def assertRefused(self, res, status, code):
        self.assertEqual(res.status_code, status, res.get_json())
        self.assertEqual(res.get_json()["error_code"], code)
        self.assertEqual(self._artpath(), "")
        self.assertEqual(os.listdir(self.album_dir), ["01 Track.flac", "02 Track.flac"])

    def test_non_image_is_refused(self):
        self.assertRefused(self._set(b"<svg xmlns='http://www.w3.org/2000/svg'/>" * 4), 400, "INVALID_IMAGE")

    def test_oversize_image_is_refused(self):
        big = image_bytes()[:3] + b"\x00" * (art_mod.MAX_IMAGE_BYTES + 1)
        self.assertRefused(self._set(big), 413, "IMAGE_TOO_LARGE")

    def test_hash_mismatch_is_refused(self):
        self.assertRefused(self._set(image_bytes(), image_sha256="0" * 64), 400, "IMAGE_HASH_MISMATCH")

    def test_bad_base64_is_refused(self):
        res = self.client.post("/webmanager/album-art", headers=self.auth,
                               json={"album_id": self.album.id, "image_b64": "@@@", "image_sha256": "0" * 64})
        self.assertRefused(res, 400, "INVALID_IMAGE")

    def test_request_without_content_length_is_refused(self):
        data = image_bytes()
        body = json.dumps({"album_id": self.album.id, "image_b64": base64.b64encode(data).decode("ascii"),
                           "image_sha256": sha(data)}).encode()
        res = self.client.post("/webmanager/album-art", input_stream=io.BytesIO(body),
                               headers={**self.auth, "Content-Type": "application/json",
                                        "Transfer-Encoding": "chunked"})
        self.assertRefused(res, 411, "LENGTH_REQUIRED")

    def test_too_many_pixels_is_refused(self):
        buf = io.BytesIO()
        Image.new("1", (art_mod.MAX_IMAGE_SIDE + 1, 1)).save(buf, format="PNG")
        self.assertRefused(self._set(buf.getvalue()), 400, "INVALID_IMAGE")

    def test_refused_rollback_can_be_retried_with_the_same_key(self):
        """QA D5-R1: Web Manager always sends '<txn>:rollback'; a precondition
        refusal must not be replayed once the cause is gone."""
        res = self._set(image_bytes())
        art_id = res.get_json()["art_id"]
        applied = self.lib.get_album(self.album.id).artpath
        self.album.load()
        self.album.artpath = os.fsencode(os.path.join(self.album_dir, "other.jpg"))
        self.album.store()
        self.assertEqual(self._rollback(art_id, key="txn-1:rollback").status_code, 409)
        self.album.load()
        self.album.artpath = applied
        self.album.store()
        rb = self._rollback(art_id, key="txn-1:rollback")
        self.assertEqual(rb.status_code, 200, rb.get_json())
        self.assertEqual(self._artpath(), "")
        replay = self._rollback(art_id, key="txn-1:rollback")
        self.assertEqual((replay.status_code, replay.get_json()["status"]), (200, "succeeded"))

    def test_tampered_manifest_is_refused_before_any_write(self):
        res = self._set(image_bytes())
        art_id = res.get_json()["art_id"]
        manifest_path = os.path.join(self.quarantine, art_id, "manifest.json")
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        for embedded in ({"1": [{"sha256": "../../key"}]}, {"1": [{"sha256": "0" * 64, "type": "x"}]}):
            manifest["embedded"] = embedded
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f)
            rb = self._rollback(art_id)
            self.assertEqual(rb.status_code, 400, rb.get_json())
            self.assertEqual(rb.get_json()["error_code"], "SNAPSHOT_PATH_INVALID")
            self.assertTrue(self._artpath().endswith("cover.jpg"))
            self.assertTrue(os.path.isfile(os.path.join(self.album_dir, "cover.jpg")))

    def test_changed_release_group_is_refused(self):
        self.assertRefused(self._set(image_bytes(), expected_mb_releasegroupid="4" * 8), 409, "IDENTITY_CHANGED")

    def test_current_cover_outside_roots_is_never_touched(self):
        outside = os.path.join(self.td, "elsewhere.jpg")
        with open(outside, "wb") as f:
            f.write(image_bytes())
        self.album.artpath = os.fsencode(os.path.join(self.music, "..", "elsewhere.jpg"))
        self.album.store()
        res = self._set(image_bytes("PNG"))
        self.assertEqual(res.status_code, 400, res.get_json())
        self.assertEqual(res.get_json()["error_code"], "OLD_ART_PATH_INVALID")
        self.assertTrue(os.path.isfile(outside))

    def test_album_folder_symlinked_outside_roots_is_refused(self):
        outside = os.path.join(self.td, "outside")
        os.makedirs(outside)
        link = os.path.join(self.music, "Linked")
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        path = os.path.join(link, "01.flac")
        write_flac(path)
        item = Item.from_path(path)
        album = self.lib.add_album([item])
        body_image = image_bytes()
        res = self.client.post("/webmanager/album-art", headers=self.auth, json={
            "album_id": album.id, "image_b64": base64.b64encode(body_image).decode("ascii"),
            "image_sha256": sha(body_image)})
        self.assertEqual(res.status_code, 400, res.get_json())
        self.assertEqual(res.get_json()["error_code"], "DESTINATION_PATH_INVALID")
        self.assertEqual(os.listdir(outside), ["01.flac"])

    def test_rollback_refuses_bad_ids_and_changed_art(self):
        self.assertEqual(self._rollback("../../etc").status_code, 400)
        self.assertEqual(self._rollback("0" * 32).status_code, 404)
        res = self._set(image_bytes())
        art_id = res.get_json()["art_id"]
        self.album.load()
        self.album.artpath = os.fsencode(os.path.join(self.album_dir, "other.jpg"))
        self.album.store()
        rb = self._rollback(art_id)
        self.assertEqual(rb.status_code, 409, rb.get_json())
        self.assertEqual(rb.get_json()["error_code"], "ART_CHANGED")
        self.assertTrue(os.path.isfile(os.path.join(self.album_dir, "cover.jpg")))

    def test_failure_after_set_art_restores_everything(self):
        old = image_bytes("JPEG", (5, 5, 5))
        cover = os.path.join(self.album_dir, "cover.jpg")
        with open(cover, "wb") as f:
            f.write(old)
        self.album.artpath = os.fsencode(cover)
        self.album.store()
        from unittest import mock
        real, calls = art_mod._write_manifest, []

        def flaky(folder, manifest):
            calls.append(manifest.get("status"))
            if manifest.get("status") == "applied":
                raise OSError("disk full")
            real(folder, manifest)

        with mock.patch.object(art_mod, "_write_manifest", side_effect=flaky):
            res = self._set(image_bytes("JPEG", (1, 200, 1)))
        self.assertEqual(calls, ["applying", "applied", "compensated"])
        self.assertEqual(res.status_code, 500)
        self.assertEqual(res.get_json()["error_code"], "ALBUM_ART_FAILED")
        with open(cover, "rb") as f:
            self.assertEqual(f.read(), old)
        self.assertEqual(os.path.normcase(self._artpath()), os.path.normcase(cover))

    # The happy-path tests of the base class are not repeated here.
    test_new_cover_is_set_embedded_and_rolled_back = None
    test_existing_cover_and_embedded_art_are_restored_exactly = None
    test_same_name_cover_is_replaced_and_restored = None
    test_replay_returns_the_stored_result = None
    test_untracked_file_at_the_destination_comes_back = None


if __name__ == "__main__":
    unittest.main()
