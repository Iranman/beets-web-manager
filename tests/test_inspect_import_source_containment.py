"""inspect_import_source() containment (CodeQL py/path-injection #1478/#1482).

The realpath + startswith barrier must not change behaviour: paths inside the
music root or a staging root are inspected and returned canonical; anything
else (including a sibling sharing the root's text prefix) is refused before
the filesystem walk.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as cw


class InspectImportSourceContainmentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(os.path.realpath(tmp.name))
        self.music, self.staging = base / "music", base / "downloads"
        for d in (self.music / "A" / "B", self.staging / "rel", base / "music2", base / "elsewhere"):
            d.mkdir(parents=True)
            (d / "01.flac").write_bytes(b"x")
        self.base = base
        for patcher in (mock.patch.object(cw, "_music_root", return_value=self.music),
                        mock.patch.object(cw, "_get_staging_roots", return_value=[self.staging])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_inside_music_or_staging_is_inspected_and_canonical(self):
        for folder in (self.music / "A" / "B", self.staging / "rel"):
            with self.subTest(folder=folder):
                res = cw.inspect_import_source(str(folder / ".." / folder.name))
                self.assertTrue(res["ok"])
                self.assertEqual(res["path"], os.path.realpath(folder))
                self.assertEqual(res["audio_file_count"], 1)

    def test_outside_roots_and_prefix_siblings_are_refused(self):
        for folder in (self.base / "music2", self.base / "elsewhere", self.music / ".." / "elsewhere"):
            with self.subTest(folder=folder):
                res = cw.inspect_import_source(str(folder))
                self.assertFalse(res["ok"])
                self.assertIn("outside approved roots", res["error"])


if __name__ == "__main__":
    unittest.main()
