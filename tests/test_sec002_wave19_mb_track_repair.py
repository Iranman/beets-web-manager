"""SEC-002 / ARCH-003 Wave 19: MusicBrainz Album Track Repair Controlled Mutation Boundary.

Comprehensive focused test suite for album_mb_track_repair_v1 mutation family.
"""
import ast
import unittest

try:  # ARCH-001: app.py module family
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402


class WebManagerMutationProhibitionTests(unittest.TestCase):
    """SEC-002 Wave 19 final review: the initial architecture scan only
    inspected `repair_album_mb_tracks`, but `_repair_album_mbid_sticking_once`
    is a second, automatic production caller of the same mutation family --
    it must be held to the identical no-local-mutation standard."""

    PROHIBITED_STRINGS = [
        "Path.unlink", "os.unlink", "os.remove", "os.rename", "os.replace",
        "shutil.move", "shutil.rmtree", "UPDATE items SET", "UPDATE albums SET",
        "_beet_run", "MediaFile(", "mutagen.File(",
    ]

    def _fn_source(self, name):
        source = app_family_source()  # ARCH-001: app.py module family
        tree = ast.parse(source)
        fn_def = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                fn_def = node
                break
        self.assertIsNotNone(fn_def, f"{name} not found in app.py")
        return ast.get_source_segment(source, fn_def)

    def test_app_py_repair_endpoint_contains_no_direct_mutations(self):
        fn_source = self._fn_source("repair_album_mb_tracks")
        for p in self.PROHIBITED_STRINGS:
            self.assertNotIn(p, fn_source, f"Prohibited call '{p}' found in repair_album_mb_tracks")

    def test_app_py_automatic_repair_helper_contains_no_direct_mutations(self):
        fn_source = self._fn_source("_repair_album_mbid_sticking_once")
        for p in self.PROHIBITED_STRINGS:
            self.assertNotIn(p, fn_source, f"Prohibited call '{p}' found in _repair_album_mbid_sticking_once")


if __name__ == "__main__":
    unittest.main()
