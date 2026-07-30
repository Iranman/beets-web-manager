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

    def test_nested_bytes_inside_lists_and_dicts_are_decoded(self):
        payload = {
            "albums": [
                {"id": 1, "artpath": b"/data/media/music/A/cover.jpg"},
                {"id": 2, "artpath": None},
            ],
            "paths": [b"/a/one.flac", b"/a/two.flac"],
        }
        decoded = json.loads(json.dumps(payload, default=_json_default))
        self.assertEqual(decoded["albums"][0]["artpath"], "/data/media/music/A/cover.jpg")
        self.assertIsNone(decoded["albums"][1]["artpath"])
        self.assertEqual(decoded["paths"], ["/a/one.flac", "/a/two.flac"])

    def test_normal_json_types_pass_through_default_unchanged(self):
        # _json_default is only ever invoked for values json can't already
        # serialize itself -- confirms it's never called for, and doesn't
        # alter, ordinary strings/ints/floats/bools/lists/dicts/None.
        payload = {
            "name": "Artist Name",
            "count": 42,
            "score": 0.875,
            "ok": True,
            "missing": None,
            "tags": ["a", "b"],
            "nested": {"x": 1},
        }
        body = json.dumps(payload, default=_json_default)
        self.assertEqual(json.loads(body), payload)

    def test_default_never_receives_or_leaks_credential_shaped_strings(self):
        # This is a serialization-safety check, not a secrecy mechanism:
        # _json_default only ever transforms bytes -> str; a token that is
        # already a plain str in the payload passes through json.dumps
        # untouched by this function, and _json_default itself never reads
        # environment variables, config, or any credential source.
        payload = {"token": "already-a-string-token-value"}
        body = json.dumps(payload, default=_json_default)
        self.assertEqual(json.loads(body)["token"], "already-a-string-token-value")
        import inspect

        source = inspect.getsource(_json_default)
        for forbidden in ("os.environ", "getenv", "TOKEN", "PASSWORD", "SECRET"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
