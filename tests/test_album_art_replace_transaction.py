"""Album artwork upload / URL replace through Beets (album_art_replace_v1).

Web Manager side: plan -> the operator's upload is the approval -> apply
through BeetsAdapter.set_album_art -> verify -> rollback through the engine.
The end-to-end class drives the real BeetsAdapter against the real webmanager
plugin and a real Beets library (Flask test client in place of HTTP).
"""

import http.client
import io
import os
import shutil
import socket
import tempfile
import unittest
import urllib.parse
from unittest import mock

from PIL import Image

import backend.artwork_service as artwork_service
import backend.composite_workflows as cw
import backend.transaction_recovery as recovery
from backend.artwork_service import AlbumArtRequestError
from backend.beets_adapter import (
    BeetsAdapter,
    BeetsAdapterBadRequestError,
    BeetsAdapterError,
    BeetsAdapterNotFoundError,
    BeetsAdapterTimeoutError,
)
from backend.transaction_engine import TransactionStore

RG = "33333333-3333-3333-3333-333333333333"


def jpeg(color=(200, 10, 10)):
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, format="JPEG")
    return buf.getvalue()


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self.store = TransactionStore(os.path.join(self.td, "tx"))
        self.locks = mock.patch("backend.resource_locks._override", None)
        os.environ.setdefault("WEB_MANAGER_DATA_DIR", self.td)


class ReplaceAlbumArtTransactionTests(_Base):
    def adapter(self, artpath="/music/A/cover.jpg"):
        ad = mock.MagicMock()
        ad.get_album.side_effect = [
            {"id": 7, "album": "Album", "mb_releasegroupid": RG, "artpath": "/music/A/old.png"},
            {"id": 7, "album": "Album", "mb_releasegroupid": RG, "artpath": artpath},
        ]
        ad.set_album_art.return_value = {"operation_id": "x", "success": True, "art_id": "a" * 32,
                                         "artpath": "/music/A/cover.jpg", "embedded_items": 2, "item_count": 2}
        ad.rollback_album_art.return_value = {"success": True, "restored_artpath": "/music/A/old.png",
                                              "restored_embedded_items": 2}
        return ad

    def test_upload_is_planned_approved_applied_and_rolled_back(self):
        ad, image = self.adapter(), jpeg()
        res = cw.replace_album_art(7, image, adapter=ad, store=self.store, source="user_upload",
                                   expected_mb_releasegroupid=RG)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["status"], "Completed")
        op = res["operation_id"]
        ad.set_album_art.assert_called_once_with(7, image, expected_mb_releasegroupid=RG, idempotency_key=op)
        tx = self.store.get(op)
        self.assertEqual(tx["operation_type"], "Artwork Update")
        self.assertEqual(tx["metadata"]["mutation_family"], cw.ALBUM_ART_REPLACE_FAMILY)
        self.assertEqual(tx["metadata"]["approved_by"], "operator artwork user_upload")
        self.assertEqual(tx["metadata"]["before"], {"artpath": "/music/A/old.png"})
        self.assertEqual(tx["metadata"]["engine_result"]["art_id"], "a" * 32)
        self.assertNotIn("image_b64", str(tx))  # the image itself is never stored

        rb = cw.rollback_album_art_replace(op, adapter=ad, store=self.store)
        self.assertTrue(rb["ok"], rb)
        ad.rollback_album_art.assert_called_once_with("a" * 32, idempotency_key=f"{op}:rollback")
        self.assertEqual(self.store.get(op)["status"], "Rolled Back")

    def test_base64_input_is_accepted(self):
        import base64
        ad, image = self.adapter(), jpeg()
        res = cw.replace_album_art(7, base64.b64encode(image).decode(), adapter=ad, store=self.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(ad.set_album_art.call_args[0][1], image)

    def test_changed_release_group_is_refused_before_beets(self):
        ad = self.adapter()
        res = cw.replace_album_art(7, jpeg(), adapter=ad, store=self.store, expected_mb_releasegroupid="other")
        self.assertEqual(res["code"], "identity_changed")
        ad.set_album_art.assert_not_called()

    def test_engine_refusal_fails_with_fixed_text(self):
        ad = self.adapter()
        ad.set_album_art.side_effect = BeetsAdapterBadRequestError("raw", error_code="INVALID_IMAGE")
        res = cw.replace_album_art(7, jpeg(), adapter=ad, store=self.store)
        self.assertFalse(res["ok"])
        self.assertIs(res["mutated"], False)
        self.assertEqual(res["error"], cw._ART_REFUSALS["INVALID_IMAGE"])
        self.assertEqual(self.store.get(res["operation_id"])["status"], "Failed")
        self.assertEqual(cw.rollback_album_art_replace(res["operation_id"], adapter=ad, store=self.store)["code"],
                         "not_applied")

    def test_unknown_engine_failure_does_not_claim_nothing_changed(self):
        ad = self.adapter()
        ad.set_album_art.side_effect = BeetsAdapterError("raw", status_code=500, error_code="ALBUM_ART_FAILED")
        res = cw.replace_album_art(7, jpeg(), adapter=ad, store=self.store)
        self.assertIsNone(res["mutated"])
        self.assertIn("check the album", res["error"])
        tx = self.store.get(res["operation_id"])
        self.assertEqual(tx["status"], "Failed")
        self.assertIn("check the album", " ".join(str(line) for line in tx.get("logs") or []))

    def test_old_plugin_names_the_required_version(self):
        ad = self.adapter()
        ad.set_album_art.side_effect = BeetsAdapterNotFoundError("raw")
        res = cw.replace_album_art(7, jpeg(), adapter=ad, store=self.store)
        self.assertIn("1.12.0", res["error"])

    def test_transport_error_is_recovery_required(self):
        ad = self.adapter()
        ad.set_album_art.side_effect = BeetsAdapterTimeoutError("t")
        with self.assertRaises(BeetsAdapterTimeoutError):
            cw.replace_album_art(7, jpeg(), adapter=ad, store=self.store)
        rows, _ = self.store.list(status="Recovery Required")
        self.assertEqual(len(rows), 1)

    def test_verification_mismatch_is_recovery_required(self):
        ad = self.adapter(artpath="/music/A/something-else.jpg")
        res = cw.replace_album_art(7, jpeg(), adapter=ad, store=self.store)
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "Recovery Required")

    def test_generic_apply_without_the_image_changes_nothing(self):
        ad = self.adapter()
        plan = cw.plan_album_art_replace(7, jpeg(), adapter=ad, store=self.store)
        self.store.transition(plan["operation_id"], "Preview", "Approved")
        res = cw.apply_album_art_replace(plan["operation_id"], adapter=ad, store=self.store)
        self.assertEqual(res["code"], "image_unavailable")
        res = cw.apply_album_art_replace(plan["operation_id"], adapter=ad, store=self.store, image=jpeg((1, 1, 1)))
        self.assertEqual(res["code"], "image_unavailable")
        ad.set_album_art.assert_not_called()
        self.assertEqual(self.store.get(plan["operation_id"])["status"], "Approved")

    def test_restart_recovery_finishes_from_the_engine_registry(self):
        ad = self.adapter()
        plan = cw.plan_album_art_replace(7, jpeg(), adapter=ad, store=self.store)
        op = plan["operation_id"]
        self.store.update(op, status="Running", metadata={"engine_request": {"operation_id": op}})
        ad.get_operation.return_value = {"status": "succeeded", "result": ad.set_album_art.return_value}
        out = recovery.resolve_transaction(self.store.get(op), adapter=ad, store=self.store)
        self.assertEqual(out["status"], "Completed")
        self.assertEqual(self.store.get(op)["metadata"]["engine_result"]["art_id"], "a" * 32)

    def test_artwork_service_surfaces_refusals_and_hides_engine_errors(self):
        with mock.patch.object(cw, "replace_album_art",
                               return_value={"ok": False, "mutated": False, "code": "invalid_image",
                                             "error": cw._ART_REFUSALS["INVALID_IMAGE"]}):
            with self.assertRaises(AlbumArtRequestError) as ctx:
                artwork_service._replace_album_art_bytes(7, jpeg(), source="user_upload")
        self.assertEqual(ctx.exception.message, cw._ART_REFUSALS["INVALID_IMAGE"])
        with mock.patch.object(cw, "replace_album_art", side_effect=BeetsAdapterError("secret body")):
            with self.assertRaises(RuntimeError) as ctx:
                artwork_service._replace_album_art_bytes(7, jpeg(), source="user_upload")
        self.assertEqual(str(ctx.exception), "Could not update album artwork")


class _FakeResponse(io.BytesIO):
    def __init__(self, body, ctype):
        super().__init__(body)
        self.status, self.reason = 200, "OK"
        self.headers = http.client.HTTPMessage()
        self.headers["Content-Type"] = ctype

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


class UrlFetchLimitTests(unittest.TestCase):
    def _fetch(self, body, ctype):
        addr = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with mock.patch("backend.security.socket.getaddrinfo", return_value=addr), \
                mock.patch("backend.security._send_pinned", return_value=_FakeResponse(body, ctype)):
            return artwork_service._download_album_art_bytes("https://img.example.test/cover")

    def test_non_image_content_type_is_refused(self):
        with self.assertRaises(AlbumArtRequestError) as ctx:
            self._fetch(jpeg(), "text/html; charset=utf-8")
        self.assertEqual(ctx.exception.message, "That URL is not an image")

    def test_oversize_body_is_refused(self):
        with self.assertRaises(AlbumArtRequestError) as ctx:
            self._fetch(jpeg() + b"\x00" * artwork_service._ALBUM_ART_UPLOAD_MAX_BYTES, "image/jpeg")
        self.assertIn("15 MB", ctx.exception.message)

    def test_private_and_loopback_urls_are_refused(self):
        for url in ("http://127.0.0.1/a.jpg", "http://10.0.0.5/a.jpg", "http://[::1]/a.jpg",
                    "file:///etc/passwd", "http://169.254.169.254/latest/meta-data/"):
            with self.subTest(url=url), self.assertRaises(AlbumArtRequestError):
                artwork_service._download_album_art_bytes(url)


class EndToEndThroughPluginTests(unittest.TestCase):
    """Real BeetsAdapter -> real webmanager plugin -> real Beets library."""

    def setUp(self):
        from beets.library import Item, Library
        from beets.plugins import BeetsPlugin
        from beetsplug.web import app as beets_web_app
        import beetsplug.webmanager.operations as ops_mod
        from beetsplug.embedart import EmbedCoverArtPlugin
        from beetsplug.webmanager import WebManagerPlugin
        from beetsplug.webmanager.auth import set_api_key_file
        from beetsplug.webmanager.engine_common import set_quarantine_root
        from tests.test_webmanager_album_art import TOKEN, write_flac

        self.td = tempfile.mkdtemp()
        music = os.path.join(self.td, "music")
        self.album_dir = os.path.join(music, "A", "B")
        os.makedirs(self.album_dir)
        self.lib = Library(os.path.join(self.td, "lib.blb"), directory=music)
        key = os.path.join(self.td, "key")
        with open(key, "w", encoding="utf-8") as f:
            f.write(TOKEN + "\n")
        WebManagerPlugin()
        set_api_key_file(key)
        ops_mod.set_allowed_roots([music])
        set_quarantine_root(os.path.join(self.td, "q"))
        before = list(BeetsPlugin.listeners["art_set"])
        EmbedCoverArtPlugin()
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["INCLUDE_PATHS"] = True
        beets_web_app.config["TESTING"] = True
        client = beets_web_app.test_client()
        path = os.path.join(self.album_dir, "01.flac")
        write_flac(path)
        item = Item.from_path(path)
        item.update({"title": "T", "album": "B", "albumartist": "A", "mb_releasegroupid": RG})
        self.album = self.lib.add_album([item])
        self.store = TransactionStore(os.path.join(self.td, "tx"))

        errors = {400: BeetsAdapterBadRequestError, 404: BeetsAdapterNotFoundError}

        def bridge(method, path, params=None, json_data=None, headers=None, timeout=None):
            url = path + ("?" + urllib.parse.urlencode(params) if params else "")
            r = client.open(url, method=method, json=json_data,
                            headers={**(headers or {}), "Authorization": f"Bearer {TOKEN}"})
            body = r.get_json(silent=True) or {}
            if r.status_code >= 400:
                raise errors.get(r.status_code, BeetsAdapterError)(
                    "x", status_code=r.status_code, error_code=body.get("error_code") or "BEETS_NOT_FOUND")
            return body

        self.ad = BeetsAdapter(base_url="http://beets.test:8337", api_key=TOKEN)
        self.ad._request = bridge

        def cleanup():
            BeetsPlugin.listeners["art_set"][:] = before
            ops_mod.set_allowed_roots(None)
            set_quarantine_root(None)
            set_api_key_file(None)
            self.lib._connection().close()
            shutil.rmtree(self.td, ignore_errors=True)
        self.addCleanup(cleanup)

    def test_upload_sets_cover_artpath_and_embedded_art_and_rolls_back(self):
        from mediafile import MediaFile
        image = jpeg()
        res = cw.replace_album_art(self.album.id, image, adapter=self.ad, store=self.store,
                                   source="user_upload", expected_mb_releasegroupid=RG)
        self.assertTrue(res["ok"], res)
        cover = os.path.join(self.album_dir, "cover.jpg")
        with open(cover, "rb") as f:
            self.assertEqual(f.read(), image)
        self.album.load()
        self.assertEqual(os.path.normcase(os.fsdecode(self.album.artpath)), os.path.normcase(cover))
        item_path = os.path.join(self.album_dir, "01.flac")
        self.assertEqual(len(MediaFile(item_path).images), 1)

        rb = cw.rollback_album_art_replace(res["operation_id"], adapter=self.ad, store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.album.load()
        self.assertFalse(self.album.artpath)
        self.assertFalse(os.path.exists(cover))
        self.assertFalse(MediaFile(item_path).images)

    def test_bad_input_is_refused_by_the_engine(self):
        res = cw.replace_album_art(self.album.id, b"GIF89a" + b"\x00" * 64, adapter=self.ad, store=self.store)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "invalid_image")
        self.assertEqual(os.listdir(self.album_dir), ["01.flac"])


if __name__ == "__main__":
    unittest.main()
