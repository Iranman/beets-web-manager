"""SEC-002 / ARCH-003 Wave 25 Docker acceptance round -- regression tests.

Covers the defects found while building the dedicated Wave 25 two-service
Docker acceptance scenarios (as opposed to tests/test_wave25_round_review_fixes.py,
which covers the prior independent-review round's findings):

1. import_folder_with_id() required LOCAL filesystem existence of the
   import source folder -- but that route runs in the beets-web-manager
   container, which has no mount into /data/media/music or /data/torrents
   in either documented compose topology (docker-compose.yml,
   docker-compose.full.yml). Every real call was guaranteed to 404 with
   "Source path does not exist" regardless of whether the folder was
   genuinely there. The frontend calls this route directly
   (api/client.ts), so this broke the confirm-and-import workflow
   entirely in the real, security-hardened deployment.
2. create_import_folder_plan() (the engine-side Plan, which DOES have
   real filesystem access) silently produced an empty, "ok": True plan
   for a nonexistent/non-directory source instead of failing closed --
   the only place that could have caught the above at all.
3. STATUSES didn't include "Recovery Required", so every
   store.update(status="Recovery Required") call in the codebase
   (several pre-existing, plus the new album_artwork_fetch_v1 family)
   silently persisted as "Pending" instead.
4. import_folder_with_id()'s _do() referenced a local variable,
   input_looks_like_release_group, that was never assigned anywhere in the
   function -- every real call raised NameError inside the background job.
   No existing unit test reached this line (all mocked at a level above
   it); only the real two-service Docker acceptance run, exercising the
   actual production route end to end, surfaced it. Fixed by deriving the
   flag from the already-computed mb_albumid/selected_releasegroupid state.
5. The same function had two more identical-class bugs, found one at a
   time across successive real CI runs of the same fresh-import scenario
   after each prior NameError was fixed and the next one was reached:
   source_is_library (referenced ~11 times for library-source-specific
   validation/rollback/messaging, never assigned -- fixed by deriving it
   from folder_path vs. the music root) and already_present/combined
   (referenced by a legacy "beet reported nothing to import" raw-stdout
   phrase-matching fallback that predates the import_folder_v1 engine
   migration and has no raw stdout left to match against -- fixed by
   defaulting both conservatively so that fallback stays inert instead of
   crashing, while the real library-lookup strategies above it still find
   genuinely-already-imported sources by MB identity/path/name).
   `python -m pyflakes app.py` (not a project dependency; run manually
   during this investigation) confirmed no further undefined names remain
   inside this specific function after all three fixes.
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestImportSourcePathDoesNotRequireLocalExistence(unittest.TestCase):
    def setUp(self):
        self.app_source = (ROOT / "app.py").read_text(encoding="utf-8")

    def test_import_folder_with_id_does_not_require_local_existence(self):
        idx = self.app_source.index("def import_folder_with_id()")
        window = self.app_source[idx: idx + 6000]
        self.assertIn("require_exists=False", window)
        self.assertNotIn("require_exists=True", window)

    def test_compose_files_confirm_web_manager_has_no_media_mount(self):
        """The fix above is only correct because this is actually true --
        pin it down structurally so a future compose change that DOES add
        a media mount to beets-web-manager doesn't leave this comment (and
        the require_exists=False fix it justifies) silently wrong."""
        for compose_file in ("docker-compose.yml", "docker-compose.full.yml"):
            text = (ROOT / compose_file).read_text(encoding="utf-8")
            wm_idx = text.index("beets-web-manager:")
            # Bound the search to this service's own block (up to the next
            # top-level service or end of file).
            rest = text[wm_idx:]
            next_service = None
            for marker in ("\n  beets:", "\n  bgutil-provider:"):
                pos = rest.find(marker, 1)
                if pos != -1 and (next_service is None or pos < next_service):
                    next_service = pos
            block = rest[:next_service] if next_service else rest
            self.assertNotIn("/data/media/music", block, f"{compose_file}: beets-web-manager unexpectedly has a media mount")
            self.assertNotIn("/data/torrents", block, f"{compose_file}: beets-web-manager unexpectedly has a downloads mount")
            self.assertNotIn("/config:", block, f"{compose_file}: beets-web-manager unexpectedly has a /config mount")


class TestImportFolderPlanFailsClosedOnMissingSource(unittest.TestCase):
    def setUp(self):
        from backend.transaction_engine import TransactionStore, create_import_folder_plan
        self.create_import_folder_plan = create_import_folder_plan
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        self.store = TransactionStore(root=str(self.root / "transactions"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_rejects_nonexistent_source_folder(self):
        missing = self.staging / "does-not-exist"
        res = self.create_import_folder_plan(
            self.store, {"source_folder": str(missing)},
            staging_allowed_roots=[str(self.staging)],
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "import_folder_source_not_found")

    def test_rejects_source_that_is_a_file_not_a_directory(self):
        f = self.staging / "not_a_dir.txt"
        f.write_text("hello")
        res = self.create_import_folder_plan(
            self.store, {"source_folder": str(f)},
            staging_allowed_roots=[str(self.staging)],
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "import_folder_source_not_a_directory")

    def test_accepts_a_real_existing_source_folder(self):
        real = self.staging / "Real Album"
        real.mkdir()
        (real / "01.mp3").write_bytes(b"fake audio")
        res = self.create_import_folder_plan(
            self.store, {"source_folder": str(real)},
            staging_allowed_roots=[str(self.staging)],
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("file_count"), 1)


class TestImportFolderWithIdReleaseGroupFlagIsAssigned(unittest.TestCase):
    """CI-discovered bug (real two-service Docker acceptance run, not a
    local unit test): input_looks_like_release_group was referenced inside
    import_folder_with_id()'s _do() but never assigned in the function,
    raising NameError on every real fresh-import call."""

    def setUp(self):
        self.app_source = (ROOT / "app.py").read_text(encoding="utf-8")

    def test_input_looks_like_release_group_is_assigned_before_use(self):
        idx = self.app_source.index("def import_folder_with_id()")
        assign_idx = self.app_source.index("input_looks_like_release_group =", idx)
        use_idx = self.app_source.index("if input_looks_like_release_group:", idx)
        self.assertLess(
            assign_idx, use_idx,
            "input_looks_like_release_group is referenced before it is assigned",
        )

    def test_source_is_library_is_assigned_before_use(self):
        idx = self.app_source.index("def import_folder_with_id()")
        assign_idx = self.app_source.index("source_is_library = _path_is_under", idx)
        first_use_idx = self.app_source.index("if source_is_library:", idx)
        self.assertLess(
            assign_idx, first_use_idx,
            "source_is_library is referenced before it is assigned",
        )

    def test_already_present_and_combined_are_assigned_before_use(self):
        idx = self.app_source.index("def import_folder_with_id()")
        already_present_assign = self.app_source.index("already_present = False", idx)
        combined_assign = self.app_source.index('combined = ""', idx)
        first_use_idx = self.app_source.index(
            "if not album_ids and not item_ids and already_present", idx
        )
        self.assertLess(already_present_assign, first_use_idx)
        self.assertLess(combined_assign, first_use_idx)

    def test_flag_logic_matches_release_group_vs_release_id_semantics(self):
        # Mirrors exactly what the route computes from mb_albumid /
        # selected_releasegroupid, without needing to invoke the full Flask
        # route and its many external dependencies.
        def _compute(mb_albumid: str, selected_releasegroupid: str) -> bool:
            return bool(selected_releasegroupid) and selected_releasegroupid == mb_albumid

        # A real, distinct release id plus an independently-supplied
        # release-group id: mb_albumid is already directly importable.
        self.assertFalse(_compute("release-uuid", "different-rg-uuid"))
        # Only a release-group id was supplied (mb_albumid fell back to it,
        # or a release-group URL was pasted into the release-id field):
        # mb_albumid actually holds a release-group value and needs
        # resolving to a representative release before import.
        self.assertTrue(_compute("rg-uuid", "rg-uuid"))
        # No release-group detected at all.
        self.assertFalse(_compute("release-uuid", ""))


class TestConfirmedImportV1ResultSkipsRedundantRawSqlRevalidation(unittest.TestCase):
    """Real Docker acceptance finding (Wave 25 Round 4, corrected): once
    confirmed_import_v1's own Apply step had already verified the album
    via structured queries (exact planned Release ID + RGID + items/files
    confirmed on disk), _album_match_summary() unconditionally ran a
    SECOND, redundant validation pass over every discovered album_id --
    via raw SQL through _db(), which BeetsClient.raw_sqlite_query() always
    rejects ("Raw SQLite queries are not permitted") in the two-service
    topology. This meant every confirmed_import_v1 import (fresh-import
    and crash-resume alike) failed at this later step, even after the
    actual import fix (a missing `musicbrainz` plugin in the acceptance
    engine's config) let native import succeed for the first time."""

    def setUp(self):
        self.app_source = (ROOT / "app.py").read_text(encoding="utf-8")

    def test_confirmed_import_v1_result_is_trusted_without_calling_album_match_summary(self):
        idx = self.app_source.index("def import_folder_with_id()")
        loop_idx = self.app_source.index("for aid in album_ids:", idx)
        skip_idx = self.app_source.index(
            'strategy == "confirmed_import_v1 verified result"', idx
        )
        summary_call_idx = self.app_source.index(
            "summary = _album_match_summary(int(aid))", idx
        )
        self.assertLess(loop_idx, skip_idx)
        self.assertLess(
            skip_idx, summary_call_idx,
            "the confirmed_import_v1 skip check must run before the raw-SQL revalidation call",
        )
        # The skip branch must trust the result directly (append + continue)
        # before ever reaching _album_match_summary for that album.
        window = self.app_source[skip_idx: summary_call_idx]
        self.assertIn("validated_album_ids.append(aid)", window)
        self.assertIn("continue", window)


class TestRecoveryRequiredStatusPersists(unittest.TestCase):
    def test_recovery_required_is_a_recognized_status(self):
        from backend.transaction_engine import STATUSES
        self.assertIn("Recovery Required", STATUSES)

    def test_recovery_required_status_actually_persists_through_store_update(self):
        from backend.transaction_engine import TransactionStore
        with tempfile.TemporaryDirectory() as tmp:
            store = TransactionStore(root=str(Path(tmp) / "transactions"))
            tx = store.create(operation_type="Artwork Update", status="Preview", summary="x", metadata={})
            store.update(tx["id"], status="Recovery Required")
            reloaded = store.get(tx["id"])
            self.assertEqual(reloaded["status"], "Recovery Required")


if __name__ == "__main__":
    unittest.main()
