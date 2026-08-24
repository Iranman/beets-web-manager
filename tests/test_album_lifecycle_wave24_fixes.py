"""Regression tests for SEC-002 / ARCH-003 Wave 24 final review fixes.

PR #98 ("LARGE ALBUM LIFECYCLE CONTROLLED-MUTATION CONSOLIDATION") migrated
several album-lifecycle routes in app.py onto engine transaction-family
calls. Independent verification of the actual diff (not the PR's own
claims) found three real, confirmed defects this file guards against:

1. album_fix_metadata bundled the non-editable `mb_albumid` field into the
   same generic PATCH payload as the editable `album`/`albumartist`/`year`
   fields. The control agent's PATCH handler rejects the *entire* request
   (HTTP 403) if any field in the payload is not in ALBUM_EDITABLE_FIELDS
   -- so supplying a new mb_albumid alongside a title/artist/year edit
   silently dropped all of them, every time.

2. album_rename routed to album_maintenance_v1's "filename_cleanup" mode
   with no "candidates" in the payload. That mode is a caller-supplied-
   candidate move primitive with no path-template computation of its own;
   with an empty candidate list it always plans zero moves and the route
   reported job success while renaming nothing.

3. album_move_to_library routed to existing_album_reconcile_v1 with only
   a single `album_id`. That family requires two distinct album ids
   (`imported_album_id`/`existing_album_id`, duplicate-merge semantics)
   and rejects any call missing either -- so the plan call failed on
   every invocation, yet the route continued on to art-repair and
   reported job success regardless, silently moving nothing.

Fixes: (1) mb_albumid is now handled exclusively via the dedicated
album_mb_track_repair_v1 transaction, never through the generic PATCH
payload. (2)/(3) both routes now fail closed (raise, so the job is
marked Failed) instead of silently reporting success while doing
nothing -- honest failure beats a false green checkmark on a
destructive-adjacent library operation.
"""

import ast
from pathlib import Path
import unittest

REPO_ROOT = Path(__file__).parent.parent
APP_SOURCE = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
_APP_TREE = ast.parse(APP_SOURCE)


def _top_level_function_source(name: str) -> str:
    for node in _APP_TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(APP_SOURCE, node) or ""
    raise AssertionError(f"{name} not found as a top-level function in app.py")


class AlbumFixMetadataIdentityFieldSplitTests(unittest.TestCase):
    def setUp(self):
        self.src = _top_level_function_source("album_fix_metadata")

    def test_mb_albumid_is_not_assigned_into_the_generic_updates_dict(self):
        tree = ast.parse(self.src)
        offending = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "updates"
                    ):
                        key = target.slice
                        if isinstance(key, ast.Constant) and key.value == "mb_albumid":
                            offending.append(node.lineno)
        self.assertEqual(offending, [], "updates['mb_albumid'] must not be assigned -- it would be "
                                         "bundled into the generic PATCH payload and rejected wholesale")

    def test_editable_fields_go_through_the_controlled_metadata_family(self):
        """Wave 24 final review section 29: album/albumartist/year used to
        go through the generic, untransacted update_album_fields (PATCH
        /albums/<id>) bypass instead of the controlled
        album_metadata_repair_v1 family the rest of this route already
        uses for mb_albumid."""
        self.assertIn("beets_client.update_album_metadata(aid, updates)", self.src)
        self.assertNotIn("beets_client.update_album_fields(aid, updates)", self.src)

    def test_mb_albumid_is_still_synced_via_the_dedicated_transaction(self):
        self.assertIn("plan_album_mb_track_repair", self.src)
        self.assertIn("apply_album_mb_track_repair", self.src)


class AlbumRenameFailsClosedTests(unittest.TestCase):
    def setUp(self):
        self.src = _top_level_function_source("album_rename")

    def test_route_raises_instead_of_silently_reporting_success(self):
        tree = ast.parse(self.src)
        raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
        self.assertTrue(raises, "album_rename's _do must raise when it cannot actually rename anything")

    def test_route_no_longer_calls_filename_cleanup_with_no_candidates(self):
        # The prior (broken) implementation called plan_album_maintenance
        # with mode=filename_cleanup and no "candidates" key at all, which
        # always plans zero moves. Guard against that exact call shape
        # reappearing without an accompanying "candidates" payload key.
        if "plan_album_maintenance(" in self.src and "filename_cleanup" in self.src:
            self.assertIn("candidates", self.src,
                          "filename_cleanup requires explicit candidates or it silently no-ops")


class AlbumMoveToLibraryFailsClosedTests(unittest.TestCase):
    def setUp(self):
        self.src = _top_level_function_source("album_move_to_library")

    def test_route_raises_instead_of_silently_reporting_success(self):
        tree = ast.parse(self.src)
        raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
        self.assertTrue(raises, "album_move_to_library's _do must raise when it cannot actually relocate anything")

    def test_route_no_longer_calls_existing_album_reconcile_with_a_single_album_id(self):
        # The prior (broken) implementation called
        # plan_existing_album_reconcile({"album_id": aid}) -- that family
        # requires imported_album_id AND existing_album_id, so the call
        # always failed. Guard against that exact single-id call shape.
        if "plan_existing_album_reconcile(" in self.src:
            self.assertIn("imported_album_id", self.src)
            self.assertIn("existing_album_id", self.src)


class AlbumDeduplicateDeadCodeTests(unittest.TestCase):
    def test_no_duplicate_unreachable_return_after_album_not_found(self):
        src = _top_level_function_source("album_deduplicate")
        self.assertEqual(
            src.count('return jsonify({"ok": False, "error": "Album not found"}), 404'), 1,
            "duplicate unreachable return statement should have been removed",
        )


if __name__ == "__main__":
    unittest.main()
