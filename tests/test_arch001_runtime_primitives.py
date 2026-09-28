"""ARCH-001 foundation-layer primitives.

Found while extracting app.py: `_s` and `_extract_mb_uuid` were each defined
twice. The later `_extract_mb_uuid` (strict full-match) silently shadowed the
documented URL-capable parser, so a pasted MusicBrainz URL resolved to ""
wherever the parser was used. One canonical definition of each now lives in
backend/app_runtime.py and app.py re-exports it.
"""

import ast
import unittest
from pathlib import Path

from backend import app_runtime

ROOT = Path(__file__).resolve().parents[1]
UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class RuntimePrimitiveTests(unittest.TestCase):
    def test_s_decodes_bytes(self):
        self.assertEqual(app_runtime._s(b"/music/A\xc3\xa9"), "/music/Aé")
        self.assertEqual(app_runtime._s(None), "")
        self.assertEqual(app_runtime._s(0), "")
        self.assertEqual(app_runtime._s(12), "12")

    def test_extract_mb_uuid_accepts_uuid_and_musicbrainz_urls(self):
        self.assertEqual(app_runtime._extract_mb_uuid(UUID.upper()), UUID)
        self.assertEqual(app_runtime._extract_mb_uuid(f"https://musicbrainz.org/release-group/{UUID}"), UUID)
        self.assertEqual(app_runtime._extract_mb_uuid("not a uuid"), "")

    def test_app_uses_the_runtime_definitions(self):
        import app
        self.assertIs(app._s, app_runtime._s)
        self.assertIs(app._extract_mb_uuid, app_runtime._extract_mb_uuid)

    def test_no_duplicate_top_level_definitions_across_the_app_family(self):
        seen = {}
        dupes = []
        paths = [ROOT / "app.py", ROOT / "backend" / "app_runtime.py"]
        for path in paths:
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name in seen:
                        dupes.append(f"{node.name}: {seen[node.name]} and {path.name}:{node.lineno}")
                    seen[node.name] = f"{path.name}:{node.lineno}"
        self.assertEqual(dupes, [])


if __name__ == "__main__":
    unittest.main()
