"""Tests for artwork-move and source-folder-cleanup logic in import_folder_with_id."""
import unittest
from pathlib import Path


def _app_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "app.py").read_text(encoding="utf-8")


def _artwork_helper_source(src: str) -> str:
    return src[
        src.index("def _move_artwork_to_target"):
        src.index("def _path_is_under")
    ]


def _cleanup_block_source(src: str) -> str:
    # The cleanup block inside import_folder_with_id, from the comment to end of except block
    start = src.index("# ── Move orphaned artwork to canonical album folder")
    end = src.index("for aid in album_ids:\n            _repair_album_mbid_sticking_once", start)
    return src[start:end]


class ArtworkHelperDefinitionTests(unittest.TestCase):
    """_move_artwork_to_target is defined with the correct shape."""

    def setUp(self):
        self._src = _app_source()

    def test_helper_is_defined(self):
        self.assertIn("def _move_artwork_to_target(src_dir: Path, album_ids: list, log: list)", self._src)

    def test_art_exts_constant_defined(self):
        self.assertIn("_ART_EXTS = frozenset(", self._src)
        self.assertIn("'.jpg'", self._src)
        self.assertIn("'.png'", self._src)
        self.assertIn("'.webp'", self._src)

    def test_art_subdir_names_defined(self):
        # Constant is at module level; helper references it by name
        self.assertIn("_ART_SUBDIR_NAMES = frozenset(", self._src)
        self.assertIn('"artwork"', self._src)
        self.assertIn('"covers"', self._src)
        self.assertIn('"scans"', self._src)
        helper = _artwork_helper_source(self._src)
        self.assertIn("_ART_SUBDIR_NAMES", helper)

    def test_db_lookup_for_canonical_path(self):
        helper = _artwork_helper_source(self._src)
        self.assertIn(
            "SELECT path FROM items WHERE album_id=? AND path IS NOT NULL LIMIT 1",
            helper,
        )

    def test_bytes_path_decoded_with_os_fsdecode(self):
        helper = _artwork_helper_source(self._src)
        self.assertIn("os.fsdecode(raw)", helper)

    def test_identical_file_check_uses_md5(self):
        helper = _artwork_helper_source(self._src)
        self.assertIn("hashlib.md5()", helper)
        self.assertIn("_file_md5(src_file) == _file_md5(dst)", helper)

    def test_identical_file_removed_from_source(self):
        helper = _artwork_helper_source(self._src)
        self.assertIn("src_file.unlink(missing_ok=True)", helper)
        self.assertIn("identical at target", helper)

    def test_conflict_naming_uses_stem_n_suffix(self):
        helper = _artwork_helper_source(self._src)
        self.assertIn('f"{stem}-{n}{sfx}"', helper)

    def test_artwork_subdirs_moved_individually(self):
        helper = _artwork_helper_source(self._src)
        self.assertIn("_ART_SUBDIR_NAMES", helper)
        self.assertIn("dst_sub.mkdir(exist_ok=True)", helper)
        self.assertIn("entry.rglob", helper)

    def test_move_logged_per_file(self):
        helper = _artwork_helper_source(self._src)
        self.assertIn("[artwork] Moved", helper)
        self.assertIn("→", helper)


class CleanupBlockIntegrationTests(unittest.TestCase):
    """The cleanup block in import_folder_with_id calls the helper correctly."""

    def setUp(self):
        self._src = _app_source()
        self._block = _cleanup_block_source(self._src)

    def test_move_artwork_called_before_remaining_check(self):
        move_pos = self._block.index("_move_artwork_to_target(src_dir")
        remaining_pos = self._block.index("remaining_files")
        self.assertLess(move_pos, remaining_pos)

    def test_unknown_files_block_source_removal(self):
        self.assertIn("unknown file(s) in source — not removing", self._block)
        self.assertIn("not in _ART_EXTS", self._block)

    def test_source_folder_removal_logged(self):
        self.assertIn("[cleanup] Source folder removed", self._block)

    def test_source_not_removed_when_unknown_files_present(self):
        # The unknown-files branch must NOT contain rmdir
        block = self._block
        unknown_branch_start = block.index("unknown file(s) in source")
        else_branch_start = block.index("else:", unknown_branch_start)
        unknown_branch = block[unknown_branch_start:else_branch_start]
        self.assertNotIn("rmdir()", unknown_branch)
