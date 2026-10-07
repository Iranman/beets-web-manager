"""SEC-002 / ARCH-003 Wave 24 final review round 4 (CodeQL closure)
regression tests.

CodeQL's py/path-injection query flagged 47 sinks in
backend/transaction_engine.py, concentrated in the album_artwork_v1
family, because it does not model this module's custom containment/
symlink-rejection helpers as sanitizers. Manual per-alert audit (see the
Round 4 PR body / final report) found no exploitable flow: every sink is
reached only after root-containment (Path.relative_to()-based, not
string-prefix) and symlink-component checks. This file exercises the new
`validate_path_under_allowed_roots` primitive the artwork family was
refactored around, and the adversarial cases the audit's threat model
requires -- independent of whether CodeQL itself can see the proof.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.transaction_engine import validate_path_under_allowed_roots


class TestValidatePathUnderAllowedRootsPrimitive(unittest.TestCase):
    """Direct unit tests of the primitive itself -- section 11 minimums."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.music_root = self.root / "music"
        self.other_root = self.root / "music-other"  # sibling-prefix trap
        self.music_root.mkdir(parents=True)
        self.other_root.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_normal_in_root_existing_path_accepted(self):
        p = self.music_root / "Artist" / "Album"
        p.mkdir(parents=True)
        result = validate_path_under_allowed_roots(p, [self.music_root])
        self.assertIsNotNone(result)
        self.assertTrue(result.exists())

    def test_normal_in_root_new_destination_accepted(self):
        p = self.music_root / "Artist" / "New Album"  # does not exist yet
        result = validate_path_under_allowed_roots(p, [self.music_root])
        self.assertIsNotNone(result)
        self.assertFalse(p.exists())

    def test_outside_absolute_path_rejected(self):
        outside = self.root / "elsewhere" / "secret"
        result = validate_path_under_allowed_roots(outside, [self.music_root])
        self.assertIsNone(result)

    def test_dot_dot_traversal_rejected(self):
        traversal = self.music_root / ".." / "music-other" / "target"
        result = validate_path_under_allowed_roots(traversal, [self.music_root])
        self.assertIsNone(result)

    def test_sibling_prefix_path_rejected(self):
        # "/music-other" starts with the string "/music" but is NOT
        # contained in it -- a string-prefix check alone would wrongly
        # accept this; component-aware containment must reject it.
        result = validate_path_under_allowed_roots(self.other_root / "x", [self.music_root])
        self.assertIsNone(result)

    def test_symlink_file_rejected(self):
        real_file = self.other_root / "real.jpg"
        real_file.write_bytes(b"x")
        link = self.music_root / "cover.jpg"
        os.symlink(str(real_file), str(link))
        result = validate_path_under_allowed_roots(link, [self.music_root])
        self.assertIsNone(result)

    def test_symlink_directory_component_rejected(self):
        real_dir = self.other_root / "real_album"
        real_dir.mkdir()
        linked_dir = self.music_root / "Artist" / "Album"
        linked_dir.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(real_dir), str(linked_dir), target_is_directory=True)
        candidate = linked_dir / "cover.jpg"
        result = validate_path_under_allowed_roots(candidate, [self.music_root])
        self.assertIsNone(result)

    def test_returned_value_is_contained_without_symlink_rejection(self):
        """#249 review note: normpath collapses ".." before symlinks are
        followed. With reject_symlinks=False, music/a/hop/../evil resolves
        inside the root (hop -> a/sub/deep), but its lexical form music/a/evil
        is a link out of the root. The returned value must never be that."""
        a = self.music_root / "a"
        (a / "sub" / "deep").mkdir(parents=True)
        os.symlink(str(a / "sub" / "deep"), str(a / "hop"), target_is_directory=True)
        os.symlink(str(self.other_root), str(a / "evil"), target_is_directory=True)
        candidate = Path(str(a / "hop") + os.sep + ".." + os.sep + "evil")
        result = validate_path_under_allowed_roots(candidate, [self.music_root], reject_symlinks=False)
        self.assertIsNone(result)

    def test_nonexistent_leaf_under_root_accepted(self):
        candidate = self.music_root / "Artist" / "Album" / "does_not_exist.jpg"
        result = validate_path_under_allowed_roots(candidate, [self.music_root])
        self.assertIsNotNone(result)

    def test_fails_closed_on_empty_allowed_roots(self):
        result = validate_path_under_allowed_roots(self.music_root / "x", [])
        self.assertIsNone(result)

    def test_multiple_allowed_roots_selects_containing_one(self):
        p = self.other_root / "x"
        result = validate_path_under_allowed_roots(p, [self.music_root, self.other_root])
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
