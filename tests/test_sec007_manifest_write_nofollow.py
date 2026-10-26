"""SEC-007 (Codex security review of the CodeQL path-injection wave): the
engine manifest writers must not follow a symlink planted at their ``.tmp``
name, and the shared resolved-containment helper must reject ``..``/symlink
escapes. These harden the rollback ops against a poisoned engine quarantine
folder (write access to it already requires /config access, but the project's
security bar treats such TOCTOU as worth closing -- cf. ARCH-022 S1).
"""

import os
import tempfile
import unittest

from beetsplug.webmanager import art_ops, restore_ops
from beetsplug.webmanager.engine_common import _inside_folder

_CAN_SYMLINK = bool(getattr(os, "O_NOFOLLOW", 0)) and os.name != "nt"


class InsideFolderTests(unittest.TestCase):
    def test_accepts_child_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            folder = os.path.join(td, "f")
            os.makedirs(folder)
            child = os.path.join(folder, "a.json")
            open(child, "w").close()
            self.assertTrue(_inside_folder(child, folder))
            self.assertFalse(_inside_folder(os.path.join(folder, "..", "..", "x"), folder))

    @unittest.skipUnless(_CAN_SYMLINK, "needs symlink + O_NOFOLLOW support")
    def test_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as td:
            folder = os.path.join(td, "f")
            outside = os.path.join(td, "outside")
            os.makedirs(folder)
            os.makedirs(outside)
            os.symlink(outside, os.path.join(folder, "lnk"))
            self.assertFalse(_inside_folder(os.path.join(folder, "lnk", "x"), folder))


class WriteManifestNoFollowTests(unittest.TestCase):
    def _assert_symlink_tmp_refused(self, write_manifest):
        with tempfile.TemporaryDirectory() as td:
            folder = os.path.join(td, "q")
            os.makedirs(folder)
            victim = os.path.join(td, "victim")
            with open(victim, "w") as f:
                f.write("KEEP")
            os.symlink(victim, os.path.join(folder, "manifest.json.tmp"))
            with self.assertRaises(OSError):
                write_manifest(folder, {"kind": "x"})
            with open(victim) as f:
                self.assertEqual(f.read(), "KEEP")  # the symlink target is untouched

    @unittest.skipUnless(_CAN_SYMLINK, "needs symlink + O_NOFOLLOW support")
    def test_restore_ops_write_manifest_refuses_symlink_tmp(self):
        self._assert_symlink_tmp_refused(restore_ops._write_manifest)

    @unittest.skipUnless(_CAN_SYMLINK, "needs symlink + O_NOFOLLOW support")
    def test_art_ops_write_manifest_refuses_symlink_tmp(self):
        self._assert_symlink_tmp_refused(art_ops._write_manifest)


if __name__ == "__main__":
    unittest.main()
