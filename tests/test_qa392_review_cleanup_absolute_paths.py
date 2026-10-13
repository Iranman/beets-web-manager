"""QA #392: adversarial absolute-path entries for Import Review file cleanup.

Real temp files, real route, real engine plan/apply (only the store and roots
are injected by the borrowed SEC-002 fixture). Every case asserts what moved,
what stayed, and that nothing was permanently deleted."""

import os
import unittest

try:
    from test_sec002_app_path_import_review_cleanup import ImportReviewCleanupPathBoundaryTests as _Base
except ImportError:  # pragma: no cover
    from tests.test_sec002_app_path_import_review_cleanup import ImportReviewCleanupPathBoundaryTests as _Base

from backend.transaction_engine import rollback_import_review_cleanup

DATA = b"not-real-audio-but-extension-is-enough"


class AbsolutePathAttackTests(unittest.TestCase):
    setUp = _Base.setUp
    tearDown = _Base.tearDown
    _write_audio = _Base._write_audio
    _write_pending = _Base._write_pending
    _post_cleanup = _Base._post_cleanup
    _make_symlink = _Base._make_symlink

    def _q(self):
        return sorted(p.name for p in self.quarantine.rglob("*") if p.is_file())

    def _run(self, folder, files, action="quarantine_rejected"):
        resp = self._post_cleanup(folder, files, action=action, allow_delete=action.startswith("delete_"))
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["deleted_count"], 0, data)
        steps = (self.tx_store.get(data["operation_id"]).get("metadata") or {}).get("steps") or []
        self.assertTrue(all(s["type"] == "move_quarantine" for s in steps), steps)
        return data

    def _folder(self, name="Artist - Album"):
        folder = self.downloads / name
        self._write_audio(folder / "keep.flac")
        self._write_pending(folder)
        return folder

    def _assert_nothing_moved(self, folder, files):
        data = self._run(folder, files)
        self.assertEqual(data["quarantined_count"], 0, data)
        self.assertEqual(self._q(), [])
        self.assertTrue((folder / "keep.flac").exists())
        return data

    def test_prefix_sibling_is_skipped(self):
        folder = self._folder()
        sib = self._write_audio(self.downloads / "Artist - Album 2" / "x.flac")
        sib2 = self._write_audio(self.downloads / "Artist - Albumx.flac")
        self._assert_nothing_moved(folder, [str(sib), str(sib2)])
        self.assertTrue(sib.exists() and sib2.exists())

    def test_folder_itself_and_trailing_slash_are_skipped(self):
        folder = self._folder()
        self._assert_nothing_moved(folder, [str(folder), str(folder) + "/", str(folder) + "//"])

    def test_case_difference_is_skipped(self):
        folder = self._folder()
        self._write_audio(folder / "bad.flac")
        self._assert_nothing_moved(folder, [str(folder).upper() + "/bad.flac",
                                            str(folder / "BAD.FLAC")])
        self.assertTrue((folder / "bad.flac").exists())

    def test_double_slash_forms(self):
        folder = self._folder()
        bad = self._write_audio(folder / "bad.flac")
        # Leading "//" is kept by POSIX normpath: no match, skipped (safe).
        self._assert_nothing_moved(folder, ["/" + str(bad)])
        # Interior "//" normalizes onto the same in-folder file: exactly it moves.
        data = self._run(folder, [str(folder) + "//bad.flac"])
        self.assertEqual(data["quarantined_count"], 1, data)
        self.assertEqual(self._q(), ["bad.flac"])
        self.assertFalse(bad.exists())

    def test_dotdot_and_dot_are_skipped_even_when_they_land_inside(self):
        folder = self._folder()
        bad = self._write_audio(folder / "bad.flac")
        victim = self._write_audio(self.downloads / "victim.flac")
        self._assert_nothing_moved(folder, [
            str(folder) + "/../victim.flac",
            str(folder) + "/../" + folder.name + "/bad.flac",
            str(folder) + "/./bad.flac",
            str(folder) + "/sub/../../victim.flac",
        ])
        self.assertTrue(bad.exists() and victim.exists())

    def test_encoded_separator_is_literal_not_decoded(self):
        folder = self._folder()
        literal = self._write_audio(folder / "a%2Fb.flac")
        decoded = self._write_audio(folder / "a" / "b.flac")
        victim = self._write_audio(self.downloads / "victim.flac")
        data = self._run(folder, [str(folder) + "/a%2Fb.flac",
                                  str(folder) + "%2F..%2Fvictim.flac",
                                  str(folder) + "%2Fkeep.flac"])
        self.assertEqual(data["quarantined_count"], 1, data)
        self.assertEqual(self._q(), ["a%2Fb.flac"])
        self.assertFalse(literal.exists())
        self.assertTrue(decoded.exists() and victim.exists() and (folder / "keep.flac").exists())

    def test_backslash_absolute_is_skipped(self):
        folder = self._folder()
        bad = self._write_audio(folder / "bad.flac")
        self._assert_nothing_moved(folder, [str(bad).replace("/", "\\")])
        self.assertTrue(bad.exists())

    def test_file_symlink_to_outside_is_skipped_and_target_untouched(self):
        folder = self._folder()
        target = self._write_audio(self.outside / "precious.flac")
        link = folder / "link.flac"
        self._make_symlink(link, target)
        data = self._assert_nothing_moved(folder, [str(link)])
        # Refused by the resolved-containment check before the symlink check.
        self.assertEqual(data["skipped_count"], 1, data)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(target.read_bytes(), DATA)

    def test_dir_symlink_inside_folder_is_skipped(self):
        folder = self._folder()
        target = self._write_audio(self.outside / "d" / "precious.flac")
        self._make_symlink(folder / "d", target.parent, directory=True)
        self._assert_nothing_moved(folder, [str(folder / "d" / "precious.flac")])
        self.assertEqual(target.read_bytes(), DATA)

    def test_symlinked_review_folder_is_refused(self):
        real = self.other_downloads / "Real"
        self._write_audio(real / "bad.flac")
        link = self.downloads / "Linked"
        self._make_symlink(link, real, directory=True)
        self._write_pending(link)
        resp = self._post_cleanup(link, [str(link / "bad.flac")])
        self.assertEqual(resp.status_code, 400, resp.get_json())
        self.assertTrue((real / "bad.flac").exists())
        self.assertEqual(self._q(), [])

    def test_symlinked_parent_raw_and_resolved_forms_both_map_to_folder(self):
        real_parent = self.other_downloads / "realparent"
        real_folder = real_parent / "Album"
        a = self._write_audio(real_folder / "a.flac")
        b = self._write_audio(real_folder / "b.flac")
        keep = self._write_audio(real_folder / "keep.flac")
        sib = self._write_audio(real_parent / "Album 2" / "a.flac")
        link_parent = self.downloads / "lp"
        self._make_symlink(link_parent, real_parent, directory=True)
        folder = link_parent / "Album"  # raw path; resolves under other_downloads
        self._write_pending(folder)
        data = self._run(folder, [str(folder / "a.flac"), str(b),
                                  str(link_parent / "Album 2" / "a.flac"), str(sib)])
        self.assertEqual(data["quarantined_count"], 2, data)
        self.assertFalse(a.exists() or b.exists())
        self.assertTrue(keep.exists() and sib.exists())

    def test_every_action_is_restorable_and_rollback_restores_bytes(self):
        for action in ("delete_rejected", "delete_duplicate", "quarantine_rejected", "quarantine_duplicate"):
            with self.subTest(action=action):
                folder = self._folder("F-" + action)
                bad = self._write_audio(folder / "bad.flac")
                nested = self._write_audio(folder / "CD2" / "n.flac")
                data = self._run(folder, [str(bad), str(nested)], action=action)
                self.assertEqual(data["quarantined_count"], 2, data)
                self.assertFalse(bad.exists() or nested.exists())
                rb = rollback_import_review_cleanup(self.tx_store, data["operation_id"])
                self.assertTrue(rb.get("ok"), rb)
                self.assertEqual(bad.read_bytes(), DATA)
                self.assertEqual(nested.read_bytes(), DATA)


del _Base


if __name__ == "__main__":
    unittest.main()
