"""QA for CodeQL #1388: POST /api/import/review-folder/delete against the real
engine planner (only the transaction store and roots are redirected to a temp
dir). The response must never carry exception text, paths or the executor log,
and user path text must not steer the refusal classification."""

import json
import unittest
from pathlib import Path

try:
    import test_sec002_app_path_import_review_cleanup as _base_mod
except ImportError:  # pragma: no cover
    from tests import test_sec002_app_path_import_review_cleanup as _base_mod

_Base = _base_mod.ImportReviewCleanupPathBoundaryTests  # borrowed setUp/helpers only

OUTSIDE = "Review folder or file is outside the allowed cleanup roots."
CONTAINS_LIB = "The cleanup folder contains the music library; choose a folder below the downloads root instead."
STEER_NAMES = [
    "x; refusing.",
    "y is inside the music library; deleting it needs the library-delete gate.",
    "Cleanup target z",
    "Allowed cleanup root q",
]


class ReviewFolderDeleteNoLeakQATests(unittest.TestCase):
    setUp = _Base.setUp
    tearDown = _Base.tearDown
    _write_audio = _Base._write_audio
    _write_pending = _Base._write_pending
    _make_symlink = _Base._make_symlink

    def _post(self, folder, confirmed=True):
        resp = self.client.post("/api/import/review-folder/delete",
                                json={"path": str(folder), "confirmed_wrong_library_folder": confirmed})
        return resp.status_code, resp.get_json()

    def _assert_clean(self, data):
        text = json.dumps(data)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(self.root.as_posix(), text)
        self.assertNotIn("log", data)
        self.assertEqual(set(data), {"ok", "error"})

    def test_outside_root_refusal_is_fixed(self):
        folder = self.outside / "album"
        self._write_audio(folder / "t.flac")
        status, data = self._post(folder)
        self.assertEqual(status, 400, data)
        self._assert_clean(data)
        self.assertEqual(data["error"], OUTSIDE)
        self.assertTrue((folder / "t.flac").exists())

    def test_symlink_refusal_does_not_echo_raw_path(self):
        target = self.outside / "real"
        target.mkdir()
        link = self.downloads / "LEAK_SYMLINK"
        self._make_symlink(link, target, directory=True)
        status, data = self._post(link)
        self.assertEqual(status, 400, data)
        self._assert_clean(data)
        self.assertNotIn("LEAK_SYMLINK", json.dumps(data))
        self.assertEqual(data["error"], "Symlinks are not permitted.")

    def test_approved_root_itself_refusal_is_fixed(self):
        status, data = self._post(self.downloads)
        self.assertEqual(status, 400, data)
        self._assert_clean(data)
        self.assertEqual(data["error"], "Cannot clean up an approved root folder itself.")

    def test_target_containing_library_refusal_is_fixed(self):
        status, data = self._post(self.root)
        self.assertEqual(status, 400, data)
        self._assert_clean(data)
        self.assertEqual(data["error"], CONTAINS_LIB)
        self.assertTrue(self.music.exists())

    def test_path_text_cannot_steer_classification(self):
        for name in STEER_NAMES:
            with self.subTest(name=name):
                folder = self.outside / name
                self._write_audio(folder / "t.flac")
                status, data = self._post(folder)
                self.assertEqual(status, 400, data)
                self._assert_clean(data)
                self.assertNotIn(name, json.dumps(data))
                self.assertEqual(data["error"], OUTSIDE)
                self.assertTrue((folder / "t.flac").exists())

    def test_success_has_result_fields_and_no_log(self):
        folder = self.downloads / "good-album"
        self._write_audio(folder / "01.flac")
        self._write_pending(folder)
        status, data = self._post(folder, confirmed=False)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertNotIn("log", data)
        self.assertEqual(data["files_removed"], 1)
        for key in ("operation_id", "deleted", "quarantined", "status"):
            self.assertIn(key, data)
        self.assertFalse((folder / "01.flac").exists())


del _Base  # keep unittest from collecting the borrowed suite twice


class PlanErrorMappingSteeringQATests(unittest.TestCase):
    def test_mapping_never_echoes_and_is_anchored(self):
        import routes_import as r
        f = lambda t: r._import_review_cleanup_plan_error({"error": t}, "DEFAULT")
        lib = "/m"
        cases = {
            # path in the middle containing the other template's suffix
            f"Cleanup target /d/a is inside the music library; deleting it needs the library-delete gate.b contains the music library {lib}; refusing.": CONTAINS_LIB,
            f"/d/x; refusing. is inside the music library; deleting it needs the library-delete gate.":
                r._IMPORT_REVIEW_PLAN_CODE_ERRORS["import_review_library_delete_refused"],
            "Symlinks are not permitted: /d/x is inside the music library; deleting it needs the library-delete gate.":
                "Symlinks are not permitted.",
            "Target path /d/x; refusing. is outside allowed root boundaries.": OUTSIDE,
            "Cleanup did not complete: /d/x; refusing. (status Failed, operation tx)": "DEFAULT",
            "Cleanup target /d/x (status Failed, operation tx)": "DEFAULT",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(f(text), expected)


if __name__ == "__main__":
    unittest.main()
