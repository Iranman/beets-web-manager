"""Regression tests for the control agent's bytes-safe JSON serialization.

Beets stores SQLite path columns (items.path, albums.path, albums.artpath)
as raw bytes. Any control-agent response that echoes a row straight from
the database -- e.g. a query result or an item/album dict -- previously hit
`TypeError: Object of type bytes is not JSON serializable` the moment a
path column was included, because `json.dumps(data, indent=2)` had no
`default=` handler. `_json_default` fixes this by decoding bytes as UTF-8
(with a `str(obj)` fallback for anything that can't be decoded), and
`_send_json` now passes it to `json.dumps` as `default=`.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.beets_control_agent import _json_default  # noqa: E402


class JsonDefaultBytesTests(unittest.TestCase):
    def test_utf8_bytes_are_decoded_to_str(self):
        self.assertEqual(_json_default(b"/data/media/music/Artist/Album/01 Title.flac"), "/data/media/music/Artist/Album/01 Title.flac")

    def test_non_utf8_bytes_fall_back_to_str_repr_without_raising(self):
        result = _json_default(b"\xff\xfe\x00bad")
        self.assertIsInstance(result, str)

    def test_non_bytes_still_raises_typeerror(self):
        class Unserializable:
            pass

        with self.assertRaises(TypeError):
            _json_default(Unserializable())

    def test_json_dumps_with_default_handles_bytes_path_column(self):
        payload = {
            "item_id": 42,
            "path": b"/data/media/music/Artist/Album/01 Title.flac",
            "artpath": b"/data/media/music/Artist/Album/cover.jpg",
            "title": "Title",
        }
        body = json.dumps(payload, indent=2, default=_json_default)
        decoded = json.loads(body)
        self.assertEqual(decoded["path"], "/data/media/music/Artist/Album/01 Title.flac")
        self.assertEqual(decoded["artpath"], "/data/media/music/Artist/Album/cover.jpg")

    def test_json_dumps_without_default_would_have_raised(self):
        # Documents the exact regression this fix resolves: a raw bytes
        # value in a response payload used to crash the whole request.
        payload = {"path": b"/data/media/music/Artist/Album/01 Title.flac"}
        with self.assertRaises(TypeError):
            json.dumps(payload, indent=2)


if __name__ == "__main__":
    unittest.main()
