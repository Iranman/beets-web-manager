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
        window = self.app_source[idx: idx + 4500]
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
