"""Tests for the Same Release Group ID cluster resolution endpoints.

Flask/beets aren't importable in this test environment, so — consistent with
the rest of this test suite — we assert against the source text of app.py
rather than executing it.
"""
import unittest
from pathlib import Path


def _app_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "app.py").read_text(encoding="utf-8")


def _section_source(src: str) -> str:
    return src[
        src.index("# ── Same Release Group ID cluster resolution"):
        src.index("def _clean_remove_orphaned_items(item_ids: List[int], *,")
    ]


class RgidResolutionFixesTheBugTests(unittest.TestCase):
    """'Review details' used to just redirect to Library with no way to fix anything."""

    def setUp(self):
        self._section = _section_source(_app_source())

    def test_detail_route_exists(self):
        self.assertIn('@app.get("/api/clean/rgid-group/<rgid>")', self._section)

    def test_merge_route_exists_and_is_not_text_gated(self):
        self.assertIn('@app.post("/api/clean/rgid-group/merge")', self._section)
        merge_fn = self._section[self._section.index("def clean_rgid_group_merge"):
                                  self._section.index("def clean_rgid_group_keep_separate")]
        # Must validate by release-group id membership, not normalized (artist, album) text equality.
        self.assertNotIn("duplicateGroupKey", merge_fn)
        self.assertIn("ids_in_group", merge_fn)

    def test_keep_separate_route_persists(self):
        self.assertIn('@app.post("/api/clean/rgid-group/keep-separate")', self._section)
        self.assertIn("_set_rgid_resolution(rgid,", self._section)

    def test_undo_resolution_route_exists(self):
        self.assertIn('@app.post("/api/clean/rgid-group/undo-resolution")', self._section)
        self.assertIn("_clear_rgid_resolution(rgid)", self._section)

    def test_assign_representative_release_route_exists(self):
        self.assertIn('@app.post("/api/clean/rgid-group/assign-representative-release")', self._section)

    def test_relink_route_exists(self):
        self.assertIn('@app.post("/api/clean/rgid-group/relink")', self._section)

    def test_send_to_repair_route_exists(self):
        self.assertIn('@app.post("/api/clean/rgid-group/send-to-repair")', self._section)


class RgidResolutionSafetyTests(unittest.TestCase):
    """Merges must reuse the existing safety-evaluation helper, not duplicate logic."""

    def setUp(self):
        self._section = _section_source(_app_source())

    def test_detail_route_uses_shared_safety_helper(self):
        detail_fn = self._section[self._section.index("def clean_rgid_group_detail"):
                                   self._section.index("def clean_rgid_group_merge")]
        self.assertIn("_library_duplicate_merge_safety(rows, items_by_album)", detail_fn)

    def test_merge_route_recomputes_safety_before_writing(self):
        merge_fn = self._section[self._section.index("def clean_rgid_group_merge"):
                                  self._section.index("def clean_rgid_group_keep_separate")]
        self.assertIn("_library_duplicate_merge_safety(pair_rows, items_by_album)", merge_fn)
        self.assertIn('if not safety.get("merge_safe"):', merge_fn)

    def test_merge_rejects_mismatched_target(self):
        merge_fn = self._section[self._section.index("def clean_rgid_group_merge"):
                                  self._section.index("def clean_rgid_group_keep_separate")]
        self.assertIn("target_id != int(safety.get(\"merge_target_album_id\") or 0)", merge_fn)

    def test_assign_release_refuses_mismatched_release_group(self):
        assign_fn = self._section[self._section.index("def clean_rgid_group_assign_release"):
                                   self._section.index("def clean_rgid_group_relink")]
        self.assertIn("cand_rgid != rgid", assign_fn)
        self.assertIn("refusing to assign a mismatched release", assign_fn)


class RgidResolutionReuseTests(unittest.TestCase):
    """New endpoints should reuse existing repair machinery, not reimplement it."""

    def setUp(self):
        self._section = _section_source(_app_source())

    def test_assign_release_reuses_repair_once_helper(self):
        assign_fn = self._section[self._section.index("def clean_rgid_group_assign_release"):
                                   self._section.index("def clean_rgid_group_relink")]
        self.assertIn("_repair_album_mbid_sticking_once(", assign_fn)
        self.assertIn("write_tags=True", assign_fn)

    def test_relink_reuses_repair_once_helper_and_release_group_resolution(self):
        relink_fn = self._section[self._section.index("def clean_rgid_group_relink"):
                                   self._section.index("def clean_rgid_group_send_to_repair")]
        self.assertIn("_repair_album_mbid_sticking_once(", relink_fn)
        self.assertIn("_resolve_release_group_to_release(target_rgid, log)", relink_fn)
        self.assertIn("_mb_release_search(", relink_fn)

    def test_send_to_repair_reuses_import_resolution_and_source_folder_helper(self):
        repair_fn = self._section[self._section.index("def clean_rgid_group_send_to_repair"):]
        self.assertIn("_resolve_album_release_for_import(", repair_fn)
        self.assertIn("_album_source_folder(album_id)", repair_fn)
        self.assertIn("_repair_album_mbid_sticking_once(", repair_fn)

    def test_send_to_repair_gives_a_reason_when_unresolved(self):
        repair_fn = self._section[self._section.index("def clean_rgid_group_send_to_repair"):]
        self.assertIn("needs manual review", repair_fn)


class RgidResolutionIdempotencyTests(unittest.TestCase):
    """All mutating endpoints must be safe to call repeatedly."""

    def setUp(self):
        self._section = _section_source(_app_source())

    def test_merge_clears_persisted_resolution_after_success(self):
        merge_fn = self._section[self._section.index("def clean_rgid_group_merge"):
                                  self._section.index("def clean_rgid_group_keep_separate")]
        self.assertIn("_clear_rgid_resolution(rgid)", merge_fn)

    def test_keep_separate_overwrites_not_duplicates(self):
        # _set_rgid_resolution stores by rgid key in a dict, so repeated calls
        # for the same rgid overwrite rather than accumulate.
        self.assertIn("state[rgid] = {", _app_source())

    def test_all_mutating_endpoints_invalidate_lib_cache(self):
        for fn_name in (
            "def clean_rgid_group_merge",
            "def clean_rgid_group_assign_release",
            "def clean_rgid_group_relink",
            "def clean_rgid_group_send_to_repair",
        ):
            start = self._section.index(fn_name)
            # Slice to next top-level def or end of section.
            rest = self._section[start:]
            next_def = rest.index("\ndef ", 1) if "\ndef " in rest[1:] else len(rest)
            fn_src = rest[:next_def]
            self.assertIn("_invalidate_lib_cache()", fn_src, msg=f"{fn_name} missing cache invalidation")


class RgidGroupAlbumsHelperTests(unittest.TestCase):
    def setUp(self):
        self._section = _section_source(_app_source())

    def test_rgid_group_albums_helper_groups_by_release_group_id(self):
        self.assertIn("def _rgid_group_albums(rgid: str)", self._section)
        self.assertIn("lower(COALESCE(a.mb_releasegroupid,''))=?", self._section)


class RgidPersistenceHelpersTests(unittest.TestCase):
    """Persistence layer added for 'keep separate' decisions."""

    def setUp(self):
        self._src = _app_source()

    def test_resolution_state_file_constant_exists(self):
        self.assertIn("RGID_RESOLUTION_STATE_FILE = METADATA_CACHE_ROOT /", self._src)

    def test_load_save_set_clear_helpers_exist(self):
        for fn in (
            "def _load_rgid_resolutions() -> Dict[str, Any]:",
            "def _save_rgid_resolutions(state: Dict[str, Any]) -> None:",
            "def _set_rgid_resolution(",
            "def _clear_rgid_resolution(",
        ):
            self.assertIn(fn, self._src)

    def test_save_uses_atomic_write_pattern(self):
        save_fn = self._src[self._src.index("def _save_rgid_resolutions("):
                             self._src.index("def _set_rgid_resolution(")]
        self.assertIn(".tmp", save_fn)
        self.assertIn(".replace(RGID_RESOLUTION_STATE_FILE)", save_fn)


class LibraryHealthPayloadSplitTests(unittest.TestCase):
    """Scan must separate active duplicate clusters from resolved (keep-separate) ones."""

    def setUp(self):
        self._src = _app_source()

    def test_resolved_groups_excluded_from_active_groups(self):
        self.assertIn("rgid_resolved_groups: List[Dict[str, Any]] = []", self._src)
        self.assertIn('resolution.get("decision") == "keep_separate"', self._src)

    def test_resolved_groups_exposed_in_payload(self):
        self.assertIn('"rgid_resolved_groups": rgid_resolved_groups[:duplicate_limit]', self._src)
        self.assertIn('"rgid_resolved_group_count": len(rgid_resolved_groups)', self._src)


class MbReleaseGroupCandidatesHelperTests(unittest.TestCase):
    """helpers_mb.py must expose all candidate releases for manual selection, not just the top pick."""

    def _helpers_mb_source(self) -> str:
        root = Path(__file__).resolve().parents[1]
        return (root / "helpers_mb.py").read_text(encoding="utf-8")

    def test_candidates_helper_exists(self):
        src = self._helpers_mb_source()
        self.assertIn("def _mb_release_group_candidates(rg_mbid", src)
        self.assertIn("release-group/{rg_mbid}", src)

    def test_imported_into_app(self):
        src = _app_source()
        self.assertIn("_mb_release_group_candidates,", src)


if __name__ == "__main__":
    unittest.main()
