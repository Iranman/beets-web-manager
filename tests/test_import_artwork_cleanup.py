"""Tests for artwork-move and source-folder-cleanup logic in import_folder_with_id."""
import unittest
from pathlib import Path
try:  # ARCH-001: app.py module family (works under discovery and tests.<module> runs)
    from _app_ast_cache import app_family_source, app_unit_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source, app_unit_source  # noqa: E402


def _app_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return app_family_source()


def _artwork_helper_source(src: str) -> str:
    return app_unit_source("_move_artwork_to_target")


def _cleanup_block_source(src: str) -> str:
    # The cleanup block inside import_folder_with_id, from the comment to end of except block
    start = src.index("# ── Move orphaned artwork to canonical album folder")
    end = src.index("if selected_subset_import and partial_remaining_audio:\n            log.append(\"  Pending Review kept", start)
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
            "composite_workflows.find_all_items_by_album_id",
            helper,
        )

    def test_bytes_path_decoded_with_os_fsdecode(self):
        helper = _artwork_helper_source(self._src)
        self.assertIn("os.fsdecode(raw)", helper)

    # SEC-002 / ARCH-003 Wave 22 final review, findings #9/#11: the
    # helper's own local `shutil.move`-based implementation (md5 dedup
    # check, "stem-N" collision naming, per-file move logging) was a
    # silent-fallback mutation path that ran whenever the engine call
    # failed -- including the common case where the engine's "move" mode
    # didn't actually move anything but reported success. It has been
    # removed entirely; the helper now delegates exclusively to
    # `album_artwork_v1` (mode="move") and fails closed (artwork left in
    # place, nothing moved locally) on any engine failure. The equivalent
    # dedup/naming logic now lives engine-side -- see
    # test_beets_transaction_engine.py's album_artwork_v1 coverage and
    # transaction_engine._album_cleanup_verified_same_file / _unique_dest.

    def test_no_local_mutation_fallback(self):
        helper = _artwork_helper_source(self._src)
        self.assertNotIn("shutil.move(", helper)
        self.assertNotIn("hashlib.md5(", helper)
        for banned in ("def _safe_move", "def _file_md5"):
            self.assertNotIn(banned, helper)

    def test_engine_move_call_includes_target_dir(self):
        helper = _artwork_helper_source(self._src)
        self.assertIn('"mode": "move"', helper)
        self.assertIn('"target_dir": str(target_dir)', helper)

    def test_engine_failure_fails_closed_not_locally(self):
        helper = _artwork_helper_source(self._src)
        self.assertIn("artwork left in source", helper)
        # Every non-ok branch returns target_dir without moving anything
        # locally -- there is no local move statement left to fall
        # through to.
        self.assertNotIn("_safe_move(", helper)

    def test_artwork_subdirs_still_scanned_for_candidates(self):
        helper = _artwork_helper_source(self._src)
        self.assertIn("_ART_SUBDIR_NAMES", helper)
        self.assertIn("d.rglob(", helper)

    def test_engine_relocation_logged(self):
        helper = _artwork_helper_source(self._src)
        self.assertIn("Engine controlled artwork relocation applied", helper)


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
