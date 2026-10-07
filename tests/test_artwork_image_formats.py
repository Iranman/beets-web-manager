"""Artwork bytes from users or providers are decoded only by the Pillow
plugins for the accepted types (JPEG, PNG, WebP). Any other format -- PSD,
FITS, GD, McIdas, ... whose parsers carried the 2026 Pillow memory-safety
advisories -- is refused at identification, before its plugin parses the
payload.

Sites: backend.artwork_service._validate_album_art_bytes and
backend.transaction_engine._validate_image_bytes.
"""
import io
import struct
import unittest
from unittest import mock

from PIL import Image, PsdImagePlugin

from backend import artwork_service
from backend.artwork_service import AlbumArtRequestError


def _encoded(fmt, size=(48, 48), mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, size, (10, 120, 200) if mode == "RGB" else 1).save(buf, format=fmt)
    return buf.getvalue()


def _psd_bytes(width=48, height=48):
    """A minimal, well-formed 8-bit RGB PSD (raw, uncompressed) that
    unrestricted Pillow identifies as PSD."""
    header = b"8BPS" + struct.pack(">H6xHIIHH", 1, 3, height, width, 8, 3)
    sections = struct.pack(">I", 0) * 3  # color mode data, image resources, layer/mask info
    pixels = struct.pack(">H", 0) + bytes(3 * width * height)
    return header + sections + pixels


class PsdFixtureSanityTests(unittest.TestCase):
    def test_unrestricted_pillow_would_parse_the_psd_fixture(self):
        # Proves the payload really reaches the PSD parser without formats=,
        # so the refusals below are due to the restriction, not a bad fixture.
        with Image.open(io.BytesIO(_psd_bytes())) as im:
            self.assertEqual(im.format, "PSD")


class _PsdParserSpy:
    def __enter__(self):
        self.patch = mock.patch.object(PsdImagePlugin.PsdImageFile, "_open",
                                       autospec=True, side_effect=AssertionError("PSD parser reached"))
        self.spy = self.patch.start()
        return self.spy

    def __exit__(self, *exc):
        self.patch.stop()
        return False


class ArtworkServiceFormatTests(unittest.TestCase):
    def test_psd_is_refused_before_its_parser_runs(self):
        with _PsdParserSpy() as spy:
            with self.assertRaises(AlbumArtRequestError) as ctx:
                artwork_service._validate_album_art_bytes(_psd_bytes())
        spy.assert_not_called()
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("Unsupported image type", ctx.exception.message)

    def test_other_non_allowed_formats_are_refused(self):
        for fmt in ("GIF", "BMP", "TIFF", "ICO"):
            with self.subTest(fmt=fmt):
                with self.assertRaises(AlbumArtRequestError) as ctx:
                    artwork_service._validate_album_art_bytes(_encoded(fmt))
                self.assertIn("Unsupported image type", ctx.exception.message)

    def test_unrecognisable_bytes_are_reported_as_undecodable(self):
        with self.assertRaises(AlbumArtRequestError) as ctx:
            artwork_service._validate_album_art_bytes(b"NOT_AN_IMAGE_FILE" * 4)
        self.assertEqual(ctx.exception.message, "Cover image could not be safely decoded")

    def test_allowed_formats_still_validate(self):
        for fmt in ("JPEG", "PNG", "WEBP"):
            with self.subTest(fmt=fmt):
                info = artwork_service._validate_album_art_bytes(_encoded(fmt))
                self.assertEqual(info["format"], fmt)
                self.assertEqual((info["width"], info["height"]), (48, 48))




if __name__ == "__main__":
    unittest.main()
