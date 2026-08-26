"""SEC-002 / ARCH-003 Wave 25 independent review round -- regression tests.

Structural (AST/source-pattern) tests proving the concrete defects found by
independent review of PR #100 (starting head
c2b08840bfa227872e84a9a6e3ab7d12c162529a) are actually fixed, and cannot
silently return. These follow the same source-scanning style already
established by tests/test_arch003_boundary_enforcement.py for exactly this
reason: the buggy functions live deep inside giant, heavily-mocked Flask
route handlers (reimport_disk, import_folder_with_id) where a full
functional regression test would require disproportionate fixture setup
for what is fundamentally a "this exact call shape must never appear
again" guarantee. backend/beets_client.py's new fetch_and_embed_album_art
gets a real functional unit test since it is a small, self-contained,
easily-isolated method.

Every structural assertion in this file was verified to FAIL against
c2b08840bfa227872e84a9a6e3ab7d12c162529a before the corresponding fix.
"""
import ast
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestWave25ReviewStructuralFixes(unittest.TestCase):
    def setUp(self):
        self.app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.client_source = (ROOT / "backend" / "beets_client.py").read_text(encoding="utf-8")
        self.generator_source = (ROOT / "scripts" / "generate_arch003_mutation_inventory.py").read_text(encoding="utf-8")

    # ── Bug: repair_album_artwork() does not exist on BeetsClient ──────────

    def test_nonexistent_repair_album_artwork_call_is_gone(self):
        self.assertNotIn("beets_client.repair_album_artwork(", self.app_source)

    def test_beets_client_has_real_artwork_fetch_embed_method(self):
        self.assertIn("def fetch_and_embed_album_art(", self.client_source)

    def test_artwork_fetch_is_a_real_controlled_transaction_not_bare_run_command(self):
        """Docker acceptance round: fetch_and_embed_album_art must compose
        the real album_artwork_fetch_v1 Plan/Apply family, not a bare
        run_command()/POST /commands/execute pair with no transaction
        record at all."""
        self.assertIn("def plan_album_artwork_fetch(", self.client_source)
        self.assertIn("def apply_album_artwork_fetch(", self.client_source)
        idx = self.client_source.index("def fetch_and_embed_album_art(")
        end_idx = self.client_source.index("\n    def ", idx + 1)
        body = self.client_source[idx:end_idx]
        self.assertIn("self.plan_album_artwork_fetch(", body)
        self.assertIn("self.apply_album_artwork_fetch(", body)

    def test_app_calls_the_real_artwork_method(self):
        self.assertIn("beets_client.fetch_and_embed_album_art(", self.app_source)

    # ── Bug: item_id passed where album_id was required ────────────────────

    def test_no_item_id_passed_directly_to_album_scoped_calls(self):
        """The specific buggy shapes from the reimport_disk item_ids loop --
        update_album_metadata(int(iid), ...) and relocate_album(int(iid),
        ...) -- must never reappear anywhere in app.py. A real album_id
        must always be resolved first."""
        self.assertNotIn("update_album_metadata(int(iid)", self.app_source)
        self.assertNotIn("relocate_album(int(iid)", self.app_source)

    def test_reimport_disk_item_loop_resolves_real_album_id(self):
        """The fixed loop must re-derive a genuine album_id (queried fresh
        from the items table) before calling any album-scoped engine
        method, and must skip items that have none rather than substitute
        the item's own id."""
        idx = self.app_source.index("_item_repaired_album_ids: set = set()")
        window = self.app_source[idx: idx + 2200]
        self.assertIn('SELECT album_id FROM items WHERE id = ?', window)
        self.assertIn("_real_aid = int(_aid_row[0])", window)
        self.assertIn("if _real_aid <= 0:", window)
        self.assertIn('beets_client.plan_album_mb_track_repair({"album_id": _real_aid', window)
        self.assertIn("beets_client.update_album_metadata(_real_aid,", window)
        self.assertIn("beets_client.relocate_album(_real_aid,", window)

    # ── Bug: plan_album_mb_track_repair() called without album_id ──────────

    def test_every_plan_album_mb_track_repair_call_supplies_album_id(self):
        """A Plan call missing album_id is always rejected by
        create_album_mb_track_repair_plan() (payload.get("album_id") <= 0
        -> {"ok": False}), and the reimport_disk item loop used to check
        `if p_res.get("ok")` and silently skip on that expected rejection
        -- a permanent, silent no-op disguised as best-effort. Every call
        site in app.py must pass album_id in the same payload literal."""
        tree = ast.parse(self.app_source)
        offending = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "plan_album_mb_track_repair"
            ):
                if not node.args:
                    offending.append(node.lineno)
                    continue
                payload_arg = node.args[0]
                if not isinstance(payload_arg, ast.Dict):
                    # Not a literal dict -- can't statically verify the key,
                    # skip (none of the current call sites do this).
                    continue
                keys = [k.value for k in payload_arg.keys if isinstance(k, ast.Constant)]
                if "album_id" not in keys:
                    offending.append(node.lineno)
        self.assertEqual(offending, [], f"plan_album_mb_track_repair() called without a literal album_id key at line(s): {offending}")

    # ── Bug: plan_folder_cleanup() called with a payload shape its Plan ────
    # ── never reads (silent permanent no-op success) ────────────────────────

    def test_no_folder_cleanup_delete_stale_items_no_op_shape(self):
        """create_folder_cleanup_plan() only reads action in
        {remove_empty_source, remove_empty, safe_rename, rename_folder,
        merge_source_files, merge} and payload["source"/"source_folder"];
        action="delete_stale_items" (which it silently no-ops on) must
        never appear again as a live plan_folder_cleanup payload literal
        (explanatory prose mentioning the retired string is fine)."""
        tree = ast.parse(self.app_source)
        offending = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "plan_folder_cleanup"
                and node.args
                and isinstance(node.args[0], ast.Dict)
            ):
                for k, v in zip(node.args[0].keys, node.args[0].values):
                    if (
                        isinstance(k, ast.Constant) and k.value == "action"
                        and isinstance(v, ast.Constant) and v.value == "delete_stale_items"
                    ):
                        offending.append(node.lineno)
        self.assertEqual(offending, [], f"plan_folder_cleanup called with the no-op delete_stale_items shape at line(s): {offending}")

    def test_no_folder_cleanup_wrong_payload_key(self):
        """plan_folder_cleanup() previously received {"path": ..., "action":
        "delete"} -- a key/action pair create_folder_cleanup_plan() never
        recognizes (it reads "source"/"source_folder", and "delete" is not
        an implemented action), which unconditionally rejected the Plan."""
        self.assertNotIn('beets_client.plan_folder_cleanup({"path":', self.app_source)

    def test_staged_item_and_orphan_cleanup_use_playlist_media_cleanup(self):
        """Both real DB-row-deletion call sites (_delete_album_items_under_folder's
        failed-staged-import cleanup, and import_folder_with_id's pre-import
        orphan-row cleanup) must route through playlist_media_cleanup_v1 --
        the family that actually reads item_ids and deletes both the file
        (quarantined) and the DB row (with any now-empty album) as one
        transaction -- not the no-op folder_cleanup_v1 shape."""
        self.assertIn('beets_client.plan_playlist_media_cleanup({"item_ids": delete_ids})', self.app_source)
        self.assertIn('beets_client.plan_playlist_media_cleanup({"item_ids": orphan_ids})', self.app_source)

    # ── Dead code: _delete_row_file (raw delete_file on library media, ─────
    # ── never actually called) ──────────────────────────────────────────────

    def test_dead_delete_row_file_helper_removed(self):
        self.assertNotIn("def _delete_row_file(", self.app_source)
        self.assertNotIn("_delete_row_file(row", self.app_source)

    # ── Redundant/unsafe raw delete_file before a real transaction ─────────

    def test_cleanup_failed_import_copy_relies_on_the_real_transaction(self):
        """_cleanup_failed_import_copy previously called the generic,
        DB-unaware beets_client.delete_file() on each item's file BEFORE
        calling plan_album_cleanup/apply_album_cleanup -- but
        album_cleanup_v1's own Apply already deletes both the DB rows and
        the files itself (with its own symlink/TOCTOU checks). The raw
        pre-delete bypassed those checks and was purely redundant."""
        idx = self.app_source.index("def _cleanup_failed_import_copy")
        end_idx = self.app_source.index("def _rollback_failed_library_source_import")
        body = self.app_source[idx:end_idx]
        self.assertNotIn("beets_client.delete_file(", body)
        self.assertIn("beets_client.plan_album_cleanup(", body)
        self.assertIn("beets_client.apply_album_cleanup(", body)

    # ── Inventory classification truthfulness (reimport_source) ────────────

    def test_reimport_source_not_falsely_labeled_import_folder_v1(self):
        """reimport_source_atomic()/preserve_import_source() reference
        neither TransactionStore nor transaction_engine nor
        mutation_family nor operation_id anywhere -- they do not enter
        import_folder_v1's Plan/Apply/Rollback contract. The generator
        must not claim that transaction family for them."""
        self.assertNotIn('"import_folder_v1", "import_folder_v1-backing-implementation"', self.generator_source)
        self.assertNotIn('return "ENGINE_CONTROLLED_TRANSACTION", "import_folder_v1", "beets-client-reimport-source-ipc"', self.generator_source)

        control_agent_source = (ROOT / "backend" / "beets_control_agent.py").read_text(encoding="utf-8")
        start = control_agent_source.index("def reimport_source_atomic(")
        # Bound the search to this function's body (next top-level def).
        rest = control_agent_source[start + 1:]
        next_def = rest.index("\ndef ")
        body = control_agent_source[start:start + 1 + next_def]
        for forbidden in ("TransactionStore", "mutation_family", "operation_id", "transaction_engine"):
            self.assertNotIn(forbidden, body, f"reimport_source_atomic unexpectedly references {forbidden!r} -- the ENGINE_NATIVE_BEETS classification (not import_folder_v1) may need re-review")

    def test_app_state_broad_hints_no_longer_include_import_review_pending(self):
        """The broad substring hints ("recent_import", "pending",
        "auto_import", "import_review", "import_all") matched against
        BOTH call-site text and the enclosing function name with no path-
        context check at all -- a real media-mutation function landing in
        this file with one of those substrings in its name would have
        been silently classified as non-blocking app state. Replaced with
        an explicit, individually-verified function-name allowlist."""
        self.assertIn("_APP_STATE_VERIFIED_FUNCTIONS", self.generator_source)
        hints_start = self.generator_source.index("_APP_STATE_HINTS = {")
        hints_end = self.generator_source.index("}", hints_start)
        hints_block = self.generator_source[hints_start:hints_end]
        for risky in ("recent_import", '"pending"', "auto_import", "import_review", "import_all"):
            self.assertNotIn(risky, hints_block)

    def test_app_state_classifier_rejects_a_disguised_media_mutation_function(self):
        """A real media-mutation function whose NAME happens to contain one
        of the old broad hint substrings (e.g. a hypothetical
        import_review_delete_media) must be classified as a blocker, not
        silently waved through as APP_STATE, purely because it is not in
        the explicit verified-function allowlist."""
        sys.path.insert(0, str(ROOT / "scripts"))
        import importlib
        gen = importlib.import_module("generate_arch003_mutation_inventory")
        importlib.reload(gen)

        sink = gen.MutationSink(
            file="app.py",
            function="import_review_delete_media",
            lineno=1,
            kind="filesystem",
            call_text='os.remove(item_path)',
            key="app.py:import_review_delete_media:1",
        )
        classification, family, rule = gen._classify(sink)
        self.assertNotEqual(classification, "APP_STATE", f"disguised media-mutation function was misclassified as APP_STATE via rule {rule!r}")


class TestFetchAndEmbedAlbumArt(unittest.TestCase):
    """Real functional unit tests for BeetsClient.fetch_and_embed_album_art.

    Wave 25 Docker acceptance round: this is now a thin wrapper over the
    real album_artwork_fetch_v1 Plan/Apply transaction family (see
    tests/test_album_artwork_fetch_v1.py for the transaction_engine.py
    logic itself), not a bare run_command()/POST /commands/execute
    composition -- fetchart/embedart genuinely mutate authoritative
    library state and needed a real controlled-mutation boundary, not just
    a restored call. These tests prove the client-level wrapper composes
    Plan -> Apply correctly and propagates failure/Recovery-Required
    status truthfully; still replaces the nonexistent repair_album_artwork()."""

    def setUp(self):
        from backend.beets_client import BeetsClient
        self.client = BeetsClient(base_url="http://engine.invalid:8338", token="test-token")

    def test_success_plans_then_applies(self):
        calls = []

        def fake_request(method, path, payload=None, timeout=None):
            calls.append(path)
            if path == "/albums/artwork/fetch/plan":
                self.assertEqual(payload, {"album_id": 42})
                return {"ok": True, "operation_id": "txn_1_aaaaaaaaaaaaaaaa"}
            if path == "/albums/artwork/fetch/apply":
                self.assertEqual(payload, {"operation_id": "txn_1_aaaaaaaaaaaaaaaa"})
                return {"ok": True, "status": "Completed", "artpath": "/music/a/cover.jpg"}
            raise AssertionError(f"unexpected path {path}")

        with unittest.mock.patch.object(self.client, "_request", side_effect=fake_request):
            res = self.client.fetch_and_embed_album_art(42)

        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("artpath"), "/music/a/cover.jpg")
        self.assertEqual(calls, ["/albums/artwork/fetch/plan", "/albums/artwork/fetch/apply"])

    def test_plan_rejection_short_circuits_before_apply(self):
        calls = []

        def fake_request(method, path, payload=None, timeout=None):
            calls.append(path)
            return {"ok": False, "error": "Album 42 not found"}

        with unittest.mock.patch.object(self.client, "_request", side_effect=fake_request):
            res = self.client.fetch_and_embed_album_art(42)

        self.assertFalse(res.get("ok"))
        self.assertEqual(calls, ["/albums/artwork/fetch/plan"], "apply must not run if plan was rejected")

    def test_apply_recovery_required_status_is_surfaced_not_swallowed(self):
        def fake_request(method, path, payload=None, timeout=None):
            if path == "/albums/artwork/fetch/plan":
                return {"ok": True, "operation_id": "txn_1_bbbbbbbbbbbbbbbb"}
            return {"ok": False, "error": "embedart did not confirm", "status": "Recovery Required"}

        with unittest.mock.patch.object(self.client, "_request", side_effect=fake_request):
            res = self.client.fetch_and_embed_album_art(42)

        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("status"), "Recovery Required")


if __name__ == "__main__":
    unittest.main()
