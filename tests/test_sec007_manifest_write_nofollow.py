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
from beetsplug.webmanager.engine_common import _inside_folder, set_quarantine_root

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
        # victim_outside: the .tmp symlink points outside quarantine.
        # victim_inside: it points to another file *inside* quarantine --
        # containment alone would let that through, so this pins that
        # O_NOFOLLOW (not just containment) still rejects the final component
        # and the write is never redirected to a sibling recovery record.
        for victim_rel in ("../victim", "keeper"):
            with tempfile.TemporaryDirectory() as td:
                set_quarantine_root(td)
                try:
                    folder = os.path.join(td, "q")
                    os.makedirs(folder)
                    victim = os.path.normpath(os.path.join(folder, victim_rel))
                    with open(victim, "w") as f:
                        f.write("KEEP")
                    os.symlink(victim, os.path.join(folder, "manifest.json.tmp"))
                    with self.assertRaises(OSError):
                        write_manifest(folder, {"kind": "x"})
                    with open(victim) as f:
                        # the symlink target is untouched (not truncated/moved)
                        self.assertEqual(f.read(), "KEEP")
                finally:
                    set_quarantine_root(None)

    @unittest.skipUnless(_CAN_SYMLINK, "needs symlink + O_NOFOLLOW support")
    def test_restore_ops_write_manifest_refuses_symlink_tmp(self):
        self._assert_symlink_tmp_refused(restore_ops._write_manifest)

    @unittest.skipUnless(_CAN_SYMLINK, "needs symlink + O_NOFOLLOW support")
    def test_art_ops_write_manifest_refuses_symlink_tmp(self):
        self._assert_symlink_tmp_refused(art_ops._write_manifest)

    def test_write_manifest_refuses_folder_outside_quarantine_root(self):
        # Portable (no symlink needed): the inline containment barrier must
        # reject a folder that resolves outside the quarantine root, so the
        # os.open sink is never reached with an escaping path.
        for write_manifest in (art_ops._write_manifest, restore_ops._write_manifest):
            with tempfile.TemporaryDirectory() as td:
                set_quarantine_root(os.path.join(td, "root"))
                os.makedirs(os.path.join(td, "root"))
                try:
                    outside = os.path.join(td, "elsewhere")
                    os.makedirs(outside)
                    with self.assertRaises(ValueError):
                        write_manifest(outside, {"kind": "x"})
                    # a folder inside the root is accepted and written
                    inside = os.path.join(td, "root", "abc")
                    os.makedirs(inside)
                    write_manifest(inside, {"kind": "x"})
                    with open(os.path.join(inside, "manifest.json")) as f:
                        self.assertEqual(f.read(), '{"kind": "x"}')
                finally:
                    set_quarantine_root(None)


if __name__ == "__main__":
    unittest.main()
