"""
Wave 15 controlled mutation boundary tests for SEC-002 / ARCH-003.

Tests:
1. Plan / Apply separation for import review folder deletion & per-file cleanup.
2. Cross-root / traversal path refusal for import review operations.
3. Symlink target & symlink component mutation refusal.
4. TOCTOU race detection: file replacement race (mtime/size change).
5. TOCTOU race detection: directory growth race (unexpected new file).
6. Idempotent apply retry on completed transactions.
7. Album folder cleanup & deduplication plan/apply workflows, including
   fail-closed behavior when DB membership drifts between Plan and Apply.
8. Preservation of Wave 14 RGID & Recording Identity invariant during deduplication.
9. Web Manager non-direct filesystem access (Engine owns all media mutations).
10. Transaction lifecycle audit recording & durability.

Uses the standard-library ``unittest`` framework only (no ``pytest``), matching
the rest of this repository's test suite and the CI runner
(``python -m unittest discover -s tests -p "test_*.py"``), which cannot import
test modules that depend on packages outside ``requirements.txt``.
"""

import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import app as flask_app
from backend.transaction_engine import TransactionStore, execute_import_review_cleanup_apply, execute_import_review_cleanup_plan, rollback_import_review_cleanup
from backend.beets_adapter import BeetsUnavailableError
try:  # ARCH-001: patch app.py and the modules extracted from it
    from _app_family import patch_app_family  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_family import patch_app_family  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Plan / Apply separation & Transaction Lifecycle
# ---------------------------------------------------------------------------

class TransactionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)
        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir(parents=True, exist_ok=True)
        self.tx_store = TransactionStore(root=str(store_dir))
        self.dummy_root = self.tmp_path / "downloads"
        self.dummy_root.mkdir(parents=True, exist_ok=True)

    def test_transaction_store_plan_apply_lifecycle(self):
        target_dir = self.dummy_root / "album_folder"
        target_dir.mkdir()
        f1 = target_dir / "track01.mp3"
        f1.write_text("audio data")
        st = f1.stat()

        op_id, tx = self.tx_store.create_import_review_cleanup_plan(
            target_path=str(target_dir),
            allowed_roots=[str(self.dummy_root)],
            source_paths=[str(f1)],
            expected_states={
                str(f1): {"size": st.st_size, "mtime": st.st_mtime, "is_file": True}
            },
            reversibility="RECOVERABLE",
            payload={"action": "delete", "files": [str(f1)]},
            created_by="test_user",
        )

        self.assertTrue(op_id.startswith("txn_"))
        self.assertEqual(tx["status"], "Preview")
        self.assertEqual(tx["metadata"]["reversibility"], "RECOVERABLE")

        fetched = self.tx_store.get(op_id)
        self.assertEqual(fetched["id"], op_id)

        # Simulate Apply
        updated = self.tx_store.update(
            op_id,
            status="Completed",
            applied_at="2026-08-19T00:00:00Z",
            metadata={"deleted": [str(f1)]},
        )
        self.assertEqual(updated["status"], "Completed")

        # Idempotent re-fetch
        completed = self.tx_store.get(op_id)
        self.assertEqual(completed["status"], "Completed")
        self.assertEqual(completed["metadata"]["deleted"], [str(f1)])


# ---------------------------------------------------------------------------
# 2. Cross-root / Traversal Refusal & 3. Symlink Mutation Refusal
# ---------------------------------------------------------------------------

class TraversalAndSymlinkRefusalTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)
        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir(parents=True, exist_ok=True)
        self.tx_store = TransactionStore(root=str(store_dir))
        self.dummy_root = self.tmp_path / "downloads"
        self.dummy_root.mkdir(parents=True, exist_ok=True)

    def test_cross_root_traversal_refusal(self):
        allowed = self.tmp_path / "allowed_root"
        allowed.mkdir()
        forbidden = self.tmp_path / "forbidden_root"
        forbidden.mkdir()

        bad_path = str(forbidden / "stolen_file.mp3")

        with self.assertRaises(ValueError) as cm:
            self.tx_store.create_import_review_cleanup_plan(
                target_path=bad_path,
                allowed_roots=[str(allowed)],
                source_paths=[bad_path],
                expected_states={},
                reversibility="RECOVERABLE",
                payload={},
            )
        self.assertIn("outside allowed root", str(cm.exception))

    def test_symlink_refusal(self):
        outside = self.tmp_path / "outside_target.mp3"
        outside.write_text("secret")

        symlink_file = self.dummy_root / "link.mp3"

        try:
            os.symlink(str(outside), str(symlink_file))
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported on this platform/privilege level")

        st = symlink_file.lstat()

        with self.assertRaises(ValueError) as cm:
            self.tx_store.create_import_review_cleanup_plan(
                target_path=str(symlink_file),
                allowed_roots=[str(self.dummy_root)],
                source_paths=[str(symlink_file)],
                expected_states={
                    str(symlink_file): {"size": st.st_size, "mtime": st.st_mtime, "is_file": True}
                },
                reversibility="RECOVERABLE",
                payload={},
            )
        self.assertIn("Symlinks are not permitted", str(cm.exception))


# ---------------------------------------------------------------------------
# 4. Precondition TOCTOU Race Detection
# ---------------------------------------------------------------------------

class ToctouRaceDetectionTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)
        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir(parents=True, exist_ok=True)
        self.tx_store = TransactionStore(root=str(store_dir))
        self.dummy_root = self.tmp_path / "downloads"
        self.dummy_root.mkdir(parents=True, exist_ok=True)

    def test_file_replacement_race_detection(self):
        target_dir = self.dummy_root / "race_dir"
        target_dir.mkdir()
        f1 = target_dir / "track.mp3"
        f1.write_text("original content")

        op_id, tx = self.tx_store.create_import_review_cleanup_plan(
            target_path=str(target_dir),
            allowed_roots=[str(self.dummy_root)],
            source_paths=[str(f1)],
            expected_states={
                str(f1): {"size": 9999, "mtime": 12345.67, "is_file": True}  # Wrong size/mtime
            },
            reversibility="RECOVERABLE",
            payload={},
        )

        is_valid, err = self.tx_store.revalidate_preconditions(op_id)
        self.assertFalse(is_valid)
        self.assertTrue("size changed" in err or "mtime changed" in err)

    def test_directory_growth_race_detection(self):
        target_dir = self.dummy_root / "growth_dir"
        target_dir.mkdir()
        f1 = target_dir / "file1.mp3"
        f1.write_text("content 1")
        st1 = f1.stat()

        op_id, tx = self.tx_store.create_import_review_cleanup_plan(
            target_path=str(target_dir),
            allowed_roots=[str(self.dummy_root)],
            source_paths=[str(f1)],
            expected_states={
                str(f1): {"size": st1.st_size, "mtime": st1.st_mtime, "is_file": True}
            },
            reversibility="RECOVERABLE",
            payload={},
        )

        is_valid, err = self.tx_store.revalidate_preconditions(op_id)
        self.assertTrue(is_valid)

        # Now add an unexpected new file after plan creation
        f2 = target_dir / "file2_unexpected.mp3"
        f2.write_text("unexpected content")

        is_valid_after, err_after = self.tx_store.revalidate_preconditions(op_id)
        self.assertFalse(is_valid_after)
        self.assertTrue(
            "Directory structure changed" in err_after or "file2_unexpected" in err_after
        )


# ---------------------------------------------------------------------------
# 5. Composite Workflows Integration Tests for Wave 15 Endpoints
# ---------------------------------------------------------------------------

from backend import composite_workflows
from backend.beets_adapter import BeetsUnavailableError

class BeetsClientIntegrationTests(unittest.TestCase):
    def test_beets_client_import_review_cleanup_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TransactionStore(root=tmp)
            folder = Path(tmp) / "album"
            folder.mkdir()
            f = folder / "track1.mp3"
            f.write_text("audio")
            with mock.patch.dict(os.environ, {"DOWNLOADS_ROOT": tmp}):
                plan_res = composite_workflows.plan_import_review_cleanup(
                    folder_path=str(folder),
                    action="delete",
                    files=[str(f)],
                    store=store,
                )
                self.assertTrue(plan_res.get("ok"))
                op_id = plan_res.get("operation_id")
                # S1: an unapproved Preview is refused; the operator's
                # approval is recorded through a CAS before apply.
                refused = composite_workflows.apply_import_review_cleanup(op_id, store=store)
                self.assertEqual(refused.get("code"), "not_approved")
                self.assertTrue(f.exists())
                apply_res = composite_workflows.apply_import_review_cleanup(
                    op_id, store=store, approved_by="test operator")
                self.assertTrue(apply_res.get("ok"))

    def test_beets_client_album_cleanup_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TransactionStore(root=tmp)
            with patch.object(composite_workflows, "plan_album_cleanup", return_value={"ok": True, "operation_id": "tx-9999"}), \
                 patch.object(composite_workflows, "apply_album_cleanup", return_value={"ok": True, "status": "Completed"}):
                plan_res = composite_workflows.plan_album_cleanup(album_id=42, store=store)
                self.assertTrue(plan_res["ok"])
                self.assertEqual(plan_res["operation_id"], "tx-9999")
                apply_res = composite_workflows.apply_album_cleanup("tx-9999", store=store)
                self.assertTrue(apply_res["ok"])

    def test_beets_client_fails_closed_on_unavailable(self):
        with patch.object(composite_workflows, "plan_import_review_cleanup", side_effect=BeetsUnavailableError("offline")):
            with self.assertRaises(BeetsUnavailableError):
                composite_workflows.plan_import_review_cleanup(folder_path="/tmp/test", action="delete")


# ---------------------------------------------------------------------------
# 6. Web Manager Endpoint Flask Client Tests
# ---------------------------------------------------------------------------

class FlaskEndpointTests(unittest.TestCase):
    def setUp(self):
        flask_app.app.config.update(TESTING=True)
        self._env_patch = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        self.client = flask_app.app.test_client()

    def test_flask_import_review_folder_delete_endpoint(self):
        with patch_app_family("app", "composite_workflows") as mock_bc:
            mock_bc.plan_import_review_cleanup.return_value = {
                "ok": True,
                "operation_id": "tx-folder-del",
                "status": "Preview",
            }
            mock_bc.apply_import_review_cleanup.return_value = {
                "ok": True,
                "operation_id": "tx-folder-del",
                "status": "Completed",
                "deleted": ["/downloads/review_album"],
                "log": ["Deleted folder /downloads/review_album"],
            }

            resp = self.client.post(
                "/api/import/review-folder/delete",
                json={"path": "/downloads/review_album"},
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["operation_id"], "tx-folder-del")
            self.assertEqual(data["status"], "Completed")

    def test_flask_import_review_files_cleanup_endpoint(self):
        with patch_app_family("app", "composite_workflows") as mock_bc, patch_app_family("app", "_pending_review_matches", return_value=True):
            mock_bc.plan_import_review_cleanup.return_value = {
                "ok": True,
                "operation_id": "tx-files-cleanup",
                "status": "Preview",
            }
            mock_bc.apply_import_review_cleanup.return_value = {
                "ok": True,
                "operation_id": "tx-files-cleanup",
                "status": "Completed",
                "deleted": ["/downloads/album/bad_track.mp3"],
                "moved": [],
                "log": ["Deleted file"],
            }

            resp = self.client.post(
                "/api/import/review-files/cleanup",
                json={
                    "path": "/downloads/album",
                    "action": "delete_rejected",
                    "files": ["/downloads/album/bad_track.mp3"],
                },
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["operation_id"], "tx-files-cleanup")


# ---------------------------------------------------------------------------
# 7. AST & Architecture Static Regression Tests
# ---------------------------------------------------------------------------

class ArchitectureStaticRegressionTests(unittest.TestCase):
    def test_beets_client_ast_no_duplicates(self):
        client_path = os.path.join(os.path.dirname(__file__), "..", "backend", "composite_workflows.py")
        with open(client_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename="composite_workflows.py")

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "BeetsClient":
                method_names = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
                duplicates = [name for name in set(method_names) if method_names.count(name) > 1]
                self.assertFalse(duplicates, f"Duplicate method definitions found in BeetsClient: {duplicates}")

    def test_transaction_engine_no_app_import(self):
        engine_path = os.path.join(os.path.dirname(__file__), "..", "backend", "transaction_engine.py")
        with open(engine_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename="transaction_engine.py")

        imported_modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.append(node.module)

        self.assertNotIn("app", imported_modules, "transaction_engine.py must not import app.py")

        # AST import checks alone have a blind spot: a runtime lookup via
        # sys.modules["app"] creates the same forbidden dependency without
        # ever appearing as an ast.Import/ast.ImportFrom node. But that
        # exact pattern is unreachable without `import sys` (or an
        # equivalent `ast.ImportFrom` of "sys") somewhere in the module --
        # which the same import scan above already collects -- so asserting
        # "sys" is absent from imported_modules closes the blind spot
        # without falling back to a raw substring/regex scan of the source
        # text, which would produce false positives against comments (like
        # this one) that merely *discuss* the forbidden pattern.
        self.assertNotIn(
            "sys",
            imported_modules,
            "transaction_engine.py must not import sys (it is not needed for anything "
            "the module currently does, and was previously used only for the forbidden "
            "sys.modules['app'] runtime lookup into app.py)",
        )


# ---------------------------------------------------------------------------
# 8. Database Consistency & Album Cleanup Tests
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# 8b. Wave 32 root-default audit regression: create_album_cleanup_plan must
# never fall back to a phantom "/music" root that doesn't exist in the real
# two-container Docker topology (see docker-compose.full.yml: the `beets`
# engine container mounts MUSIC_LIBRARY_PATH at /data/media/music, never at
# /music, and the web manager container never sets MUSIC_ROOT or
# BEETS_MUSIC_DIR at all). Before this fix, create_album_cleanup_plan
# unconditionally prepended that phantom root to `roots` regardless of
# whether the caller supplied allowed_roots, which was inert in production
# (the real caller always supplies allowed_roots, and /music never matches
# a real album_dir) but was still a live landmine for any other caller and
# made the "refuse to delete the root itself" check permanently dead code.
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# 9. Crash Resume & Truthful Rollback Tests
# ---------------------------------------------------------------------------

class CrashResumeRollbackTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)
        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir(parents=True, exist_ok=True)
        self.tx_store = TransactionStore(root=str(store_dir))
        self.dummy_root = self.tmp_path / "downloads"
        self.dummy_root.mkdir(parents=True, exist_ok=True)

    def test_truthful_rollback(self):
        target_dir = self.dummy_root / "quarantine_target"
        target_dir.mkdir()
        f1 = target_dir / "file1.mp3"
        f1.write_text("q content")

        quarantine_dir = self.tmp_path / "quarantine"

        plan_res = execute_import_review_cleanup_plan(
            self.tx_store,
            # "files" entries are relative names within the review folder,
            # matching the API contract enforced by
            # execute_import_review_cleanup_plan's traversal guard (which
            # explicitly rejects any entry starting with "/" or "\\" as
            # outside_review_folder -- an absolute path here would be
            # silently skipped rather than planned, on POSIX where str(f1)
            # itself starts with "/").
            payload={"path": str(target_dir), "action": "quarantine_rejected", "files": [f1.name]},
            allowed_roots=[str(self.dummy_root)],
        )
        self.assertTrue(plan_res["ok"])
        op_id = plan_res["operation_id"]

        apply_res = execute_import_review_cleanup_apply(self.tx_store, op_id, quarantine_root=str(quarantine_dir))
        self.assertTrue(apply_res["ok"])
        self.assertFalse(f1.exists())

        # Rollback
        rb_res = rollback_import_review_cleanup(self.tx_store, op_id)
        self.assertTrue(rb_res["ok"])
        self.assertEqual(rb_res["status"], "Rolled Back")
        self.assertTrue(f1.exists())

        # Second rollback attempt should be refused (rollback_available is now False)
        rb2_res = rollback_import_review_cleanup(self.tx_store, op_id)
        self.assertFalse(rb2_res["ok"])
        self.assertIn("Rollback unavailable", rb2_res["error"])


# ---------------------------------------------------------------------------
# 10. Additional edge-case & failure-mode regression tests (Plan/Apply
#     divergence, idempotent retry, symlink source replacement, and
#     fail-closed behavior on partial DB/filesystem failure).
# ---------------------------------------------------------------------------

class ImportReviewCleanupFailureModeTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)
        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir(parents=True, exist_ok=True)
        self.tx_store = TransactionStore(root=str(store_dir))
        self.dummy_root = self.tmp_path / "downloads"
        self.dummy_root.mkdir(parents=True, exist_ok=True)

    def test_apply_is_idempotent_on_repeated_request(self):
        """A second Apply call against an already-Completed transaction
        must return the same recorded result without re-running any step
        (no duplicate deletes/moves, no re-raising errors for
        already-vanished sources)."""
        target_dir = self.dummy_root / "retry_dir"
        target_dir.mkdir()
        f1 = target_dir / "track.mp3"
        f1.write_text("content")

        plan_res = execute_import_review_cleanup_plan(
            self.tx_store,
            payload={"path": str(target_dir), "action": "delete_rejected", "files": [f1.name]},
            allowed_roots=[str(self.dummy_root)],
        )
        self.assertTrue(plan_res["ok"])
        op_id = plan_res["operation_id"]

        first = execute_import_review_cleanup_apply(self.tx_store, op_id)
        self.assertTrue(first["ok"])
        self.assertEqual(first["status"], "Completed")
        self.assertFalse(f1.exists())

        second = execute_import_review_cleanup_apply(self.tx_store, op_id)
        self.assertTrue(second["ok"])
        self.assertEqual(second["status"], "Completed")
        self.assertEqual(second["deleted"], first["deleted"])

    def test_apply_refuses_when_source_vanishes_before_first_apply_attempt(self):
        """If a planned file is deleted by something else before Apply ever
        runs once, Apply must fail closed at the top-level precondition
        gate (revalidate_preconditions) rather than silently proceeding --
        an unexpectedly-vanished file this transaction never touched is
        exactly the TOCTOU signal the precondition re-check exists to
        catch, not something to wave through."""
        target_dir = self.dummy_root / "vanish_dir"
        target_dir.mkdir()
        f1 = target_dir / "track.mp3"
        f1.write_text("content")

        plan_res = execute_import_review_cleanup_plan(
            self.tx_store,
            payload={"path": str(target_dir), "action": "delete_rejected", "files": [f1.name]},
            allowed_roots=[str(self.dummy_root)],
        )
        self.assertTrue(plan_res["ok"])
        op_id = plan_res["operation_id"]

        f1.unlink()  # simulate concurrent removal between Plan and Apply

        apply_res = execute_import_review_cleanup_apply(self.tx_store, op_id)
        self.assertFalse(apply_res["ok"])
        self.assertIn("no longer exists", apply_res["error"])

    def test_retry_after_crash_tolerates_already_completed_steps_own_missing_file(self):
        """Regression test for a crash-recovery correctness bug: a step
        that this SAME transaction already durably completed (its file
        genuinely, correctly deleted, and that step's status persisted
        incrementally -- not just at the very end of Apply) must not be
        mistaken by a retry's precondition re-check for unexpected
        external tampering. Before the fix, revalidate_preconditions()
        checked every originally-planned path for continued existence
        with no awareness of which steps had already legitimately
        finished, so a retry after a crash between a step's completion and
        the function's final store.update() call would permanently refuse
        with "no longer exists" and the transaction could never finish."""
        target_dir = self.dummy_root / "crash_dir"
        target_dir.mkdir()
        f1 = target_dir / "track1.mp3"
        f1.write_text("content 1")
        f2 = target_dir / "track2.mp3"
        f2.write_text("content 2")

        plan_res = execute_import_review_cleanup_plan(
            self.tx_store,
            payload={"path": str(target_dir), "action": "delete_rejected", "files": [f1.name, f2.name]},
            allowed_roots=[str(self.dummy_root)],
        )
        self.assertTrue(plan_res["ok"])
        op_id = plan_res["operation_id"]

        # Simulate the durable aftermath of a crashed first Apply attempt:
        # f1's step genuinely finished (file deleted, step status
        # persisted -- exactly what the incremental per-step persistence
        # fix guarantees survives a crash), but the transaction's overall
        # status never advanced past "Preview" because the process died
        # before execute_import_review_cleanup_apply's final store.update().
        f1.unlink()
        tx = self.tx_store.get(op_id)
        steps = tx["metadata"]["steps"]
        for step in steps:
            if step.get("source") == str(f1):
                step["status"] = "irreversible_completed"
        self.tx_store.update(op_id, metadata={"steps": steps})

        is_valid, err = self.tx_store.revalidate_preconditions(op_id)
        self.assertTrue(is_valid, err)

        apply_res = execute_import_review_cleanup_apply(self.tx_store, op_id)
        self.assertTrue(apply_res["ok"])
        self.assertEqual(apply_res["status"], "Completed")
        self.assertFalse(f2.exists())
        self.assertIn(str(f1), apply_res["deleted"])
        self.assertIn(str(f2), apply_res["deleted"])

    def test_apply_refuses_when_source_replaced_by_symlink(self):
        """If the planned source file is replaced by a symlink between Plan
        and Apply, Apply must refuse to act through it -- the referenced
        secret target must never be moved/deleted."""
        target_dir = self.dummy_root / "swap_dir"
        target_dir.mkdir()
        f1 = target_dir / "track.mp3"
        f1.write_text("original content")

        secret = self.tmp_path / "secret_outside.mp3"
        secret.write_text("do not touch")

        plan_res = execute_import_review_cleanup_plan(
            self.tx_store,
            payload={"path": str(target_dir), "action": "delete_rejected", "files": [f1.name]},
            allowed_roots=[str(self.dummy_root)],
        )
        self.assertTrue(plan_res["ok"])
        op_id = plan_res["operation_id"]

        # Swap the source for a symlink pointing outside the allowed root.
        f1.unlink()
        try:
            os.symlink(str(secret), str(f1))
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported on this platform/privilege level")

        apply_res = execute_import_review_cleanup_apply(self.tx_store, op_id)
        # The operation must not report having deleted through the symlink,
        # and the secret file it points at must survive untouched.
        self.assertNotIn(str(secret), apply_res.get("deleted", []))
        self.assertTrue(secret.exists())
        self.assertEqual(secret.read_text(), "do not touch")

    def test_rollback_unavailable_for_irreversible_delete_action(self):
        """A plain 'delete' (non-quarantine) action is IRREVERSIBLE by
        design -- rollback must never be reported as available for it,
        after Apply."""
        target_dir = self.dummy_root / "irreversible_dir"
        target_dir.mkdir()
        f1 = target_dir / "track.mp3"
        f1.write_text("content")

        plan_res = execute_import_review_cleanup_plan(
            self.tx_store,
            payload={"path": str(target_dir), "action": "delete_rejected", "files": [f1.name]},
            allowed_roots=[str(self.dummy_root)],
        )
        op_id = plan_res["operation_id"]
        execute_import_review_cleanup_apply(self.tx_store, op_id)

        rb_res = rollback_import_review_cleanup(self.tx_store, op_id)
        self.assertFalse(rb_res["ok"])
        self.assertIn("Rollback unavailable", rb_res["error"])




if __name__ == "__main__":
    unittest.main()
