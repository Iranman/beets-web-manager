"""
Wave 16 Album Cleanup Web Manager workflow tests (SEC-002 / ARCH-003).

Verifies:
1. Web Manager Flask routes for album cleanup plan/apply and generic
   transaction rollback delegate strictly through BeetsClient (zero direct
   DB/media mutation in app.py), using response shapes that actually match
   what backend/transaction_engine.py produces -- not fabricated ones.
2. Transaction-family enforcement: an Album Cleanup transaction can never be
   rolled back through the Import Review cleanup executor (and vice versa),
   at both the Web Manager route and the engine layer.
3. Error classification is truthful: "nothing was changed" is only ever
   shown when the engine's own "mutated" flag confirms it.
4. Dead/removed surface (album-specific rollback route, engine-wide
   transaction list/detail fallback) stays removed.
5. A structural (not just string-matching) regression guard against
   reintroducing direct mutation in the album cleanup routes.
"""

import ast
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from backend.beets_client import BeetsError, BeetsUnavailableError
from backend.transaction_engine import (
    TransactionStore,
    create_album_cleanup_plan,
    execute_album_cleanup_apply,
    execute_import_review_cleanup_plan,
    execute_import_review_cleanup_apply,
    rollback_import_review_cleanup,
)
import app as flask_app


class Wave16RouteDelegationTests(unittest.TestCase):
    """Route-level tests using response shapes that match what
    transaction_engine.py actually returns (verified against the real
    functions in Wave16RealEngineIntegrationTests below), not invented
    ones."""

    def setUp(self):
        flask_app.app.config["TESTING"] = True
        os.environ["BEETS_WEB_AUTH_DISABLED"] = "1"
        self.client = flask_app.app.test_client()

    @patch("app.beets_client.plan_album_cleanup")
    def test_plan_album_cleanup_delegates_to_beets_client(self, mock_plan):
        # Real create_album_cleanup_plan() success shape.
        mock_plan.return_value = {
            "ok": True,
            "operation_id": "txn_1700000000_abcdef012345",
            "status": "Preview",
            "album_id": 42,
            "target_path": "/music/Artist/Album",
            "file_count": 3,
        }

        resp = self.client.post("/api/albums/42/cleanup/plan")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["operation_id"], "txn_1700000000_abcdef012345")
        mock_plan.assert_called_once_with(42)

    @patch("app.beets_client.plan_album_cleanup")
    def test_plan_album_cleanup_engine_unreachable(self, mock_plan):
        mock_plan.side_effect = BeetsUnavailableError("Control agent down")

        resp = self.client.post("/api/albums/42/cleanup/plan")
        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("Beets engine unavailable", data["error"])

    def test_plan_album_cleanup_invalid_album_id(self):
        resp = self.client.post("/api/albums/cleanup/plan", json={"album_id": 0})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("album_id", data["error"])

    @patch("app.beets_client.apply_album_cleanup")
    def test_apply_album_cleanup_delegates_to_beets_client(self, mock_apply):
        # Real execute_album_cleanup_apply() success shape.
        mock_apply.return_value = {
            "ok": True,
            "status": "Completed",
            "operation_id": "txn_1700000000_abcdef012345",
            "deleted": ["/music/Artist/Album/01.flac"],
            "log": ["Deleted track file /music/Artist/Album/01.flac"],
        }

        resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": "txn_1700000000_abcdef012345"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "Completed")
        mock_apply.assert_called_once_with("txn_1700000000_abcdef012345")

    def test_apply_album_cleanup_missing_operation_id(self):
        resp = self.client.post("/api/albums/cleanup/apply", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("operation_id", resp.get_json()["error"])

    @patch("app.beets_client.apply_album_cleanup")
    def test_apply_album_cleanup_stale_plan_before_mutation_is_truthful(self, mock_apply):
        """A revalidate_preconditions()-style failure (mutated=False) must
        be classified stale_plan and shown as "nothing was changed"."""
        mock_apply.return_value = {
            "ok": False,
            "error": "Path /music/Artist/Album/01.flac no longer exists.",
            "mutated": False,
        }

        resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": "txn_1700000000_abcdef012345"})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error_kind"], "stale_plan")
        self.assertIn("Nothing was changed", data["error"])
        self.assertFalse(data["mutated"])

    @patch("app.beets_client.apply_album_cleanup")
    def test_apply_album_cleanup_partial_mutation_never_reported_as_stale(self, mock_apply):
        """Regression test for the core Wave 16 truthfulness bug: a
        mid-Apply failure (e.g. the DB-membership-drift check, which fires
        *after* file-delete steps for other items already ran) must never
        be shown as "nothing was changed" just because its error text
        contains the word "changed"."""
        mock_apply.return_value = {
            "ok": False,
            "error": (
                "Album 42 membership changed since planning "
                "(planned items [101, 102], now [101, 102, 103]); "
                "refusing to delete DB rows."
            ),
            "mutated": True,
        }

        resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": "txn_1700000000_abcdef012345"})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error_kind"], "partial_mutation")
        self.assertNotIn("Nothing was changed", data["error"])
        self.assertTrue(data["mutated"])

    @patch("app.beets_client.apply_album_cleanup")
    def test_apply_album_cleanup_non_staleness_failure_not_mislabeled(self, mock_apply):
        """A failure that is neither stale nor partially mutated (e.g. a
        missing/misconfigured database) must not be relabeled as either
        "nothing changed" or "partially mutated" -- show it plainly."""
        mock_apply.return_value = {
            "ok": False,
            "error": "Beets database not found at /config/musiclibrary.blb",
            "mutated": False,
        }

        resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": "txn_1700000000_abcdef012345"})
        data = resp.get_json()
        self.assertEqual(data["error_kind"], "other")
        self.assertIn("musiclibrary.blb", data["error"])
        self.assertNotIn("Nothing was changed", data["error"])

    def test_album_specific_rollback_route_removed(self):
        """The dead, unused, family-confused /api/albums/cleanup/rollback
        route must stay removed -- the frontend uses the generic
        /api/transactions/<id>/rollback route instead. Werkzeug reports a
        removed POST-only route as 405 (a broader catch-all path pattern
        still matches, just not for POST) rather than 404; either
        response correctly proves this route no longer accepts requests."""
        resp = self.client.post("/api/albums/cleanup/rollback", json={"operation_id": "txn_x"})
        self.assertIn(resp.status_code, (404, 405))

    @patch("app.beets_client.list_transactions")
    def test_transactions_list_does_not_fall_back_to_engine(self, mock_list_tx):
        """/api/transactions must stay local-only -- nothing in the
        frontend uses an engine-wide transaction browse, and exposing it
        broadens attack surface for no product benefit."""
        resp = self.client.get("/api/transactions")
        self.assertEqual(resp.status_code, 200)
        mock_list_tx.assert_not_called()

    @patch("app.beets_client.get_transaction")
    def test_transaction_detail_does_not_fall_back_to_engine(self, mock_get_tx):
        """/api/transactions/<id> must stay local-only for the same
        reason -- confirm the unknown-locally case 404s cleanly instead of
        proxying into the engine's TransactionStore."""
        resp = self.client.get("/api/transactions/does-not-exist-locally")
        self.assertEqual(resp.status_code, 404)
        mock_get_tx.assert_not_called()


class Wave16RealEngineIntegrationTests(unittest.TestCase):
    """Integration tests routing real requests through the actual Flask
    routes with app.beets_client's methods patched to call the REAL
    transaction_engine.py functions (a real TransactionStore, real SQLite
    DB, real filesystem) -- not fabricated response shapes -- matching the
    established pattern in tests/test_sec002_app_path_import_review_cleanup.py.
    This is what actually proves the Wave 16 routes preserve Wave 15's
    security semantics end to end, not just that they call *something*."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)
        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir(parents=True, exist_ok=True)
        self.tx_store = TransactionStore(root=str(store_dir))

        self.music_root = self.tmp_path / "music"
        self.music_root.mkdir()
        self.album_dir = self.music_root / "Artist" / "TestAlbum"
        self.album_dir.mkdir(parents=True)
        self.track = self.album_dir / "track1.mp3"
        self.track.write_text("audio 1")

        self.db_file = self.tmp_path / "musiclibrary.blb"
        con = sqlite3.connect(self.db_file)
        cur = con.cursor()
        cur.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path TEXT)")
        cur.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, artpath TEXT)")
        cur.execute("INSERT INTO albums (id, album) VALUES (10, 'TestAlbum')")
        cur.execute("INSERT INTO items (id, album_id, path) VALUES (101, 10, ?)", (str(self.track),))
        con.commit()
        con.close()

        self.downloads_root = self.tmp_path / "downloads"
        self.downloads_root.mkdir()
        self.review_folder = self.downloads_root / "review"
        self.review_folder.mkdir()
        self.review_file = self.review_folder / "reject.mp3"
        self.review_file.write_text("rejected content")

        def mock_plan_album_cleanup(album_id, **kwargs):
            return create_album_cleanup_plan(
                self.tx_store, album_id=album_id, db_path=str(self.db_file),
                allowed_roots=[str(self.music_root)],
            )

        def mock_apply_album_cleanup(operation_id, **kwargs):
            return execute_album_cleanup_apply(self.tx_store, operation_id, db_path=str(self.db_file))

        def mock_rollback(operation_id, **kwargs):
            return rollback_import_review_cleanup(self.tx_store, operation_id)

        self.mock_plan_album_cleanup = mock_plan_album_cleanup
        self.mock_apply_album_cleanup = mock_apply_album_cleanup
        self.mock_rollback = mock_rollback

        flask_app.app.config["TESTING"] = True
        self._env_patch = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        self.client = flask_app.app.test_client()

    def _create_real_import_review_transaction(self, action="delete_rejected"):
        plan_res = execute_import_review_cleanup_plan(
            self.tx_store,
            payload={"path": str(self.review_folder), "action": action, "files": [self.review_file.name]},
            allowed_roots=[str(self.downloads_root)],
        )
        self.assertTrue(plan_res["ok"], plan_res)
        return plan_res["operation_id"]

    def _create_real_album_cleanup_transaction(self):
        plan_res = create_album_cleanup_plan(
            self.tx_store, album_id=10, db_path=str(self.db_file), allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(plan_res["ok"], plan_res)
        return plan_res["operation_id"]

    def test_end_to_end_plan_apply_through_real_routes(self):
        with patch.object(flask_app.beets_client, "plan_album_cleanup", side_effect=self.mock_plan_album_cleanup), \
             patch.object(flask_app.beets_client, "apply_album_cleanup", side_effect=self.mock_apply_album_cleanup):
            plan_resp = self.client.post("/api/albums/10/cleanup/plan")
            self.assertEqual(plan_resp.status_code, 200)
            plan_data = plan_resp.get_json()
            self.assertTrue(plan_data["ok"])
            op_id = plan_data["operation_id"]

            # Plan must not have mutated anything.
            self.assertTrue(self.track.exists())
            con = sqlite3.connect(self.db_file)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM items WHERE album_id = 10").fetchone()[0], 1)
            con.close()

            apply_resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": op_id})
            self.assertEqual(apply_resp.status_code, 200)
            apply_data = apply_resp.get_json()
            self.assertTrue(apply_data["ok"])
            self.assertFalse(self.track.exists())
            con = sqlite3.connect(self.db_file)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM items WHERE album_id = 10").fetchone()[0], 0)
            con.close()

    def test_apply_album_id_is_bound_by_the_transaction_not_client_supplied(self):
        """SEC-002 Wave 16 final review (task section 7): the Apply route
        takes only operation_id, never album_id -- confirm the actual
        deletion is scoped to whatever album_id create_album_cleanup_plan
        bound into the transaction's own stored metadata, not anything a
        client could supply at Apply time (there is no album_id parameter
        to supply). A second, unrelated album must survive untouched."""
        other_album_dir = self.music_root / "Artist" / "OtherAlbum"
        other_album_dir.mkdir(parents=True)
        other_track = other_album_dir / "other.mp3"
        other_track.write_text("other audio")
        con = sqlite3.connect(self.db_file)
        con.execute("INSERT INTO albums (id, album) VALUES (20, 'OtherAlbum')")
        con.execute("INSERT INTO items (id, album_id, path) VALUES (201, 20, ?)", (str(other_track),))
        con.commit()
        con.close()

        with patch.object(flask_app.beets_client, "plan_album_cleanup", side_effect=self.mock_plan_album_cleanup), \
             patch.object(flask_app.beets_client, "apply_album_cleanup", side_effect=self.mock_apply_album_cleanup):
            plan_resp = self.client.post("/api/albums/10/cleanup/plan")
            op_id = plan_resp.get_json()["operation_id"]

            apply_resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": op_id})
            self.assertTrue(apply_resp.get_json()["ok"])

        # Album 10's track is gone; album 20's track (never planned) must
        # survive completely untouched by an Apply request that only ever
        # carried an opaque operation_id.
        self.assertFalse(self.track.exists())
        self.assertTrue(other_track.exists())
        con = sqlite3.connect(self.db_file)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM items WHERE album_id = 20").fetchone()[0], 1)
        con.close()

    def test_generic_rollback_route_rejects_album_cleanup_transaction(self):
        """Core Wave 16 finding: an Album Cleanup transaction ID must never
        be accepted by the generic rollback route, which (for any ID the
        local web-manager TransactionStore doesn't recognize) delegates to
        the Import Review-specific rollback executor. Album Cleanup is
        irreversible by design -- this must fail closed with a clear
        message, never silently "succeed" having restored nothing."""
        with patch.object(flask_app.beets_client, "plan_album_cleanup", side_effect=self.mock_plan_album_cleanup), \
             patch.object(flask_app.beets_client, "apply_album_cleanup", side_effect=self.mock_apply_album_cleanup), \
             patch.object(flask_app.beets_client, "rollback_import_review_cleanup", side_effect=self.mock_rollback):
            plan_resp = self.client.post("/api/albums/10/cleanup/plan")
            op_id = plan_resp.get_json()["operation_id"]
            apply_resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": op_id})
            self.assertTrue(apply_resp.get_json()["ok"])

            rollback_resp = self.client.post(f"/api/transactions/{op_id}/rollback")
            self.assertEqual(rollback_resp.status_code, 400)
            data = rollback_resp.get_json()
            self.assertFalse(data["ok"])
            self.assertNotEqual(data.get("status"), "Rolled Back")

    def test_generic_rollback_route_still_works_for_import_review_transaction(self):
        """Confirm the transaction-family fix doesn't collateral-damage the
        legitimate, still-supported Import Review cleanup rollback path."""
        with patch.object(flask_app.beets_client, "plan_import_review_cleanup",
                           side_effect=lambda payload, **kw: execute_import_review_cleanup_plan(
                               self.tx_store, payload, [str(self.downloads_root)])), \
             patch.object(flask_app.beets_client, "apply_import_review_cleanup",
                           side_effect=lambda op_id, **kw: execute_import_review_cleanup_apply(
                               self.tx_store, op_id, quarantine_root=str(self.tmp_path / "quarantine"))), \
             patch.object(flask_app.beets_client, "rollback_import_review_cleanup", side_effect=self.mock_rollback):
            op_id = self._create_real_import_review_transaction(action="quarantine_rejected")
            apply_res = execute_import_review_cleanup_apply(
                self.tx_store, op_id, quarantine_root=str(self.tmp_path / "quarantine"),
            )
            self.assertTrue(apply_res["ok"])
            self.assertFalse(self.review_file.exists())

            rollback_resp = self.client.post(f"/api/transactions/{op_id}/rollback")
            self.assertEqual(rollback_resp.status_code, 200)
            data = rollback_resp.get_json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["status"], "Rolled Back")
            self.assertTrue(self.review_file.exists())

    def test_engine_rollback_directly_rejects_wrong_family(self):
        """Engine-layer (not just Web Manager route) test of the
        transaction-family check added to rollback_import_review_cleanup:
        this is the authoritative enforcement point, since a direct
        engine API call bypasses the Web Manager entirely."""
        op_id = self._create_real_album_cleanup_transaction()
        res = rollback_import_review_cleanup(self.tx_store, op_id)
        self.assertFalse(res["ok"])
        self.assertIn("album_cleanup_v1", res["error"])

    def test_apply_route_rejects_import_review_transaction_id(self):
        """An Import Review cleanup transaction ID must never be acceptable
        to the Album Cleanup Apply route -- execute_album_cleanup_apply's
        own mutation_family check (Wave 15) must still be enforced when
        reached through the new Wave 16 route."""
        with patch.object(flask_app.beets_client, "plan_import_review_cleanup",
                           side_effect=lambda payload, **kw: execute_import_review_cleanup_plan(
                               self.tx_store, payload, [str(self.downloads_root)])), \
             patch.object(flask_app.beets_client, "apply_album_cleanup", side_effect=self.mock_apply_album_cleanup):
            op_id = self._create_real_import_review_transaction()

            resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": op_id})
            self.assertEqual(resp.status_code, 400)
            data = resp.get_json()
            self.assertFalse(data["ok"])
            self.assertIn("album_cleanup_v1", data["error"])
            # Nothing should have been deleted for a rejected-family Apply.
            self.assertTrue(self.review_file.exists())

    def test_apply_rejects_malformed_transaction_id(self):
        with patch.object(flask_app.beets_client, "apply_album_cleanup", side_effect=self.mock_apply_album_cleanup):
            resp = self.client.post("/api/albums/cleanup/apply", json={"operation_id": "../../etc/passwd"})
            self.assertEqual(resp.status_code, 400)
            self.assertFalse(resp.get_json()["ok"])

    def test_repeated_apply_is_idempotent_through_the_route(self):
        with patch.object(flask_app.beets_client, "plan_album_cleanup", side_effect=self.mock_plan_album_cleanup), \
             patch.object(flask_app.beets_client, "apply_album_cleanup", side_effect=self.mock_apply_album_cleanup):
            plan_resp = self.client.post("/api/albums/10/cleanup/plan")
            op_id = plan_resp.get_json()["operation_id"]

            first = self.client.post("/api/albums/cleanup/apply", json={"operation_id": op_id})
            second = self.client.post("/api/albums/cleanup/apply", json={"operation_id": op_id})
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertTrue(second.get_json()["ok"])
            self.assertEqual(second.get_json()["status"], "Completed")

    def test_plan_route_performs_no_mutation(self):
        """Verify pressing "Delete Album" (Plan) alone never touches disk
        or DB -- only Apply does."""
        with patch.object(flask_app.beets_client, "plan_album_cleanup", side_effect=self.mock_plan_album_cleanup):
            resp = self.client.post("/api/albums/10/cleanup/plan")
            self.assertTrue(resp.get_json()["ok"])
        self.assertTrue(self.track.exists())
        con = sqlite3.connect(self.db_file)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM albums WHERE id = 10").fetchone()[0], 1)
        con.close()

    def test_concurrent_apply_for_same_transaction_is_serialized_not_racy(self):
        """The control agent serves requests via ThreadingHTTPServer, so two
        Apply requests for the SAME operation_id can genuinely arrive in
        parallel threads. This calls the real engine function directly
        (the authoritative enforcement point -- see
        backend.transaction_engine._get_apply_lock) from two real threads
        and proves the second call is actually blocked until the first
        releases its per-operation-id lock, not racing through the same
        destructive delete_file step."""
        plan_res = create_album_cleanup_plan(
            self.tx_store, album_id=10, db_path=str(self.db_file), allowed_roots=[str(self.music_root)],
        )
        op_id = plan_res["operation_id"]

        entered_critical_section = threading.Event()
        release_critical_section = threading.Event()
        original_unlink = Path.unlink
        call_count = {"n": 0}

        def slow_unlink(self_path, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                entered_critical_section.set()
                # Hold thread A inside the locked section long enough for
                # the test to prove thread B is genuinely blocked, not just
                # "happened to run after" by scheduling luck.
                release_critical_section.wait(timeout=5)
            return original_unlink(self_path, *args, **kwargs)

        results: dict = {}

        def run_apply(name):
            results[name] = execute_album_cleanup_apply(self.tx_store, op_id, db_path=str(self.db_file))

        with patch.object(Path, "unlink", slow_unlink):
            thread_a = threading.Thread(target=run_apply, args=("A",))
            thread_a.start()
            self.assertTrue(entered_critical_section.wait(timeout=5), "thread A never reached the delete step")

            thread_b = threading.Thread(target=run_apply, args=("B",))
            thread_b.start()

            # Thread A is deliberately still holding the lock (blocked
            # inside slow_unlink). If _get_apply_lock did not exist, thread
            # B would race straight through and very likely finish within
            # this window; with it, thread B must still be blocked waiting
            # to acquire the same operation_id's lock.
            time.sleep(0.3)
            self.assertNotIn("B", results, "thread B ran concurrently with thread A instead of being serialized")

            release_critical_section.set()
            thread_a.join(timeout=5)
            thread_b.join(timeout=5)

        self.assertTrue(results["A"]["ok"])
        self.assertTrue(results["B"]["ok"])
        # Thread B must observe the transaction as already Completed (the
        # idempotent short-circuit), never having re-attempted the delete.
        self.assertEqual(results["B"]["status"], "Completed")
        self.assertEqual(call_count["n"], 1, "the file was unlinked more than once across the two Apply calls")
        self.assertFalse(self.track.exists())


class Wave16AstStructuralTests(unittest.TestCase):
    """Structural (not string-matching) regression guard against direct
    mutation creeping into the album cleanup routes, and against the
    "helper-function delegation" bypass that a narrow call-name blocklist
    alone cannot catch (the exact blind spot Wave 15's original
    no-app-import test had for sys.modules)."""

    TARGET_ROUTES = {
        "plan_album_cleanup_route",
        "apply_album_cleanup_route",
    }

    # Anything touching the filesystem or a database directly. Broader than
    # Wave 16's original 5-item list (unlink/remove/rmtree/delete_album/
    # execute) on purpose.
    FORBIDDEN_CALLS = {
        "unlink", "remove", "rmtree", "rmdir", "move", "copy", "copyfile",
        "copystat", "copytree", "rename", "replace", "mkdir", "makedirs",
        "write_text", "write_bytes", "delete_album", "execute",
        "executemany", "executescript", "commit", "connect", "Popen",
        "run", "call", "check_call", "check_output", "system",
    }

    # Calls to bare local names (not attribute access like beets_client.x)
    # that these routes are allowed to make without triggering a review.
    # Anything else -- in particular any other locally-defined app.py
    # helper function -- must be explicitly added here, forcing a human to
    # notice and justify it rather than silently bypassing the mutation
    # check by routing through a helper. Each entry below has been
    # reviewed and performs no filesystem/DB mutation:
    #   jsonify, int, str, bool -- stdlib/Flask, side-effect-free.
    #   _s -- string coercion helper (defined near the top of app.py).
    #   _classify_album_cleanup_apply_failure -- pure error-text
    #     classification (see its definition just below these routes),
    #     no I/O of any kind.
    ALLOWED_LOCAL_CALLS = {"jsonify", "int", "str", "bool", "_s", "_classify_album_cleanup_apply_failure"}

    def setUp(self):
        app_path = flask_app.__file__
        with open(app_path, "r", encoding="utf-8") as f:
            self.source = f.read()
        self.tree = ast.parse(self.source, filename=app_path)

    def test_no_direct_mutation_calls_in_album_cleanup_routes(self):
        found_routes = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.TARGET_ROUTES:
                found_routes.add(node.name)
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        func_name = ""
                        if isinstance(child.func, ast.Name):
                            func_name = child.func.id
                        elif isinstance(child.func, ast.Attribute):
                            func_name = child.func.attr
                        self.assertNotIn(
                            func_name,
                            self.FORBIDDEN_CALLS,
                            f"Function {node.name} contains a direct mutation-shaped call: {func_name}",
                        )
        self.assertEqual(found_routes, self.TARGET_ROUTES, f"Missing route definitions: {self.TARGET_ROUTES - found_routes}")

    def test_album_cleanup_routes_only_call_known_local_helpers(self):
        """Closes the helper-function-delegation bypass: a call to any
        bare (non-attribute) local name other than the explicit allowlist
        is rejected, so a future change that adds
        `plan_album_cleanup_route` -> `_some_new_helper()` -> (direct
        mutation inside `_some_new_helper`) cannot silently pass this
        test the way a pure call-name blocklist would."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.TARGET_ROUTES:
                for child in ast.walk(node):
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                        self.assertIn(
                            child.func.id,
                            self.ALLOWED_LOCAL_CALLS,
                            f"Function {node.name} calls local helper {child.func.id}() which is not in "
                            "the reviewed allowlist -- add it only after confirming it performs no "
                            "direct filesystem/DB mutation.",
                        )

    def test_album_cleanup_routes_never_import_sqlite3_or_shutil_locally(self):
        """A route could otherwise sidestep the call-name check entirely
        with a local `import sqlite3` / `import shutil` followed by a
        dynamically-built call the AST call-name check above still catches
        via .attr, but this closes the door on any local import statement
        at all inside these routes."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.TARGET_ROUTES:
                for child in ast.walk(node):
                    self.assertNotIsInstance(
                        child, (ast.Import, ast.ImportFrom),
                        f"Function {node.name} must not contain a local import statement.",
                    )


if __name__ == "__main__":
    unittest.main()
