"""ARCH-001 architecture guards: app.py decomposed into owned services.

Dependency direction is routes -> services -> domain -> adapters:

* routes_*.py        HTTP handlers (register on app; may import services)
* backend/*_service.py, backend/app_runtime.py, ...   owned services, in the
  layer order recorded in docs/arch001_app_ownership.json ("extracted_modules")
* backend/matching/, import_reconciliation, duplicate_identity, identity_contract
  domain logic (no Flask)
* backend/beets_adapter, backend/composite_workflows   the one stock Beets engine

These tests keep that shape from eroding: nothing in backend/ imports app.py,
app.py stays application glue, and the v0.1.30 duplicate-deletion safety rule
still holds behind the moved code.
"""

import ast
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

INVENTORY = json.loads((ROOT / "docs" / "arch001_app_ownership.json").read_text(encoding="utf-8"))
FAMILY = list(INVENTORY["extracted_modules"])
SERVICES = [m for m in FAMILY if m.startswith("backend/")]
ROUTE_FILES = sorted(p.name for p in ROOT.glob("routes_*.py"))
DOMAIN_MODULES = sorted(
    [str(p.relative_to(ROOT).as_posix()) for p in (ROOT / "backend" / "matching").glob("*.py")]
    + ["backend/import_reconciliation.py", "backend/duplicate_identity.py", "backend/identity_contract.py"]
)
# HTTP-facing platform services: request authentication / first-run gating and
# JSON response helpers. Every other service is request-free.
WEB_LAYER_SERVICES = {"backend/auth_service.py", "backend/setup_service.py", "backend/serializers.py"}


def _tree(rel):
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)


def _imported_modules(rel):
    out = set()
    for node in ast.walk(_tree(rel)):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module)
    return out


def _modname(rel):
    return rel.removesuffix(".py").replace("/", ".")


class DependencyDirectionTests(unittest.TestCase):
    def test_no_backend_module_imports_app(self):
        offenders = []
        for path in sorted((ROOT / "backend").rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding="utf-8")
            mods = _imported_modules(rel)
            if "app" in mods or re.search(r"import_module\(\s*[\"']app[\"']\s*\)", text):
                offenders.append(rel)
        self.assertEqual(offenders, [])

    def test_services_import_only_lower_layers(self):
        """Extraction order is the layer order: a service may import only
        services extracted before it (never a higher layer, never a route)."""
        index = {_modname(m): i for i, m in enumerate(FAMILY)}
        violations = []
        for i, rel in enumerate(SERVICES):
            for mod in _imported_modules(rel):
                if mod.startswith("routes_") or mod == "app":
                    violations.append(f"{rel} imports {mod}")
                elif mod in index and index[mod] >= index[_modname(rel)] and mod != _modname(rel):
                    violations.append(f"{rel} imports higher layer {mod}")
        self.assertEqual(violations, [])

    def test_route_modules_do_not_import_each_other_at_load_time(self):
        # Route modules load in app.ROUTE_MODULES order; a module-level import
        # of another route module would couple that order. (A call-time lookup
        # such as api_auth_me reading routes_setup's marker path is fine.)
        violations = []
        for rel in ROUTE_FILES:
            for node in _tree(rel).body:
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("routes_"):
                    violations.append(f"{rel} imports {node.module}")
                elif isinstance(node, ast.Import) and any(a.name.startswith("routes_") for a in node.names):
                    violations.append(f"{rel} imports a route module")
        self.assertEqual(violations, [])

    def test_domain_modules_never_touch_flask(self):
        offenders = [rel for rel in DOMAIN_MODULES if any(m == "flask" or m.startswith("flask.") for m in _imported_modules(rel))]
        self.assertEqual(offenders, [])

    def test_only_web_layer_services_import_flask(self):
        offenders = sorted(rel for rel in SERVICES
                           if any(m == "flask" or m.startswith("flask.") for m in _imported_modules(rel))
                           and rel not in WEB_LAYER_SERVICES)
        self.assertEqual(offenders, [])


class AppPyIsGlueTests(unittest.TestCase):
    SRC = (ROOT / "app.py").read_text(encoding="utf-8")

    def test_container_image_ships_every_route_module(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("routes_*.py", dockerfile)
        self.assertIn("COPY backend/ ./backend/", dockerfile)

    def test_app_py_is_small(self):
        self.assertLess(len(self.SRC.splitlines()), 800)

    def test_no_sqlite_subprocess_docker_or_beet_in_app_py(self):
        mods = _imported_modules("app.py")
        for banned in ("sqlite3", "subprocess", "docker"):
            self.assertNotIn(banned, mods)
        for token in ("sqlite3.", "subprocess.", "docker.sock", "/var/run/docker", "BEET_BIN", '"beet"', "'beet'"):
            self.assertNotIn(token, self.SRC)

    def test_no_matching_policy_in_app_py(self):
        import audit_arch002_callers as audit
        hits = [line for line in self.SRC.splitlines() if audit.PATTERN.search(line)]
        self.assertEqual(hits, [])
        self.assertFalse(any(m == "backend.matching" or m.startswith("backend.matching.") for m in _imported_modules("app.py")))

    def test_every_app_py_function_is_application_glue(self):
        import audit_arch001_ownership as ownership
        rows = [r for r in ownership.analyze(INVENTORY.get("overrides") or {}) if r["module"] == "app.py"]
        self.assertTrue(rows)
        self.assertEqual([r["name"] for r in rows if r["migration_status"] != "APP_GLUE"], [])

    def test_compatibility_resolver_covers_every_owned_module(self):
        import app
        self.assertEqual(list(app._ARCH001_OWNED_MODULES), [_modname(m) for m in FAMILY])
        self.assertEqual(sorted(app.ROUTE_MODULES), [f.removesuffix(".py") for f in ROUTE_FILES])

    def test_moved_names_still_resolve_on_app(self):
        import app
        from backend import dedup_service, library_service
        self.assertIs(app.start_dedup_scan, dedup_service.start_dedup_scan)
        self.assertIs(app._invalidate_lib_cache, library_service._invalidate_lib_cache)
        with self.assertRaises(AttributeError):
            app.definitely_not_a_real_name_arch001  # noqa: B018


class OwnershipInventoryTests(unittest.TestCase):
    def test_ownership_check_passes(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        import audit_arch001_ownership as ownership
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(ownership.main([]), 0, err.getvalue())

    def test_inventory_is_current(self):
        import audit_arch001_ownership as ownership
        live = {(r["module"], r["name"], r["domain"], r["migration_status"])
                for r in ownership.analyze(INVENTORY.get("overrides") or {})}
        committed = {(r["module"], r["name"], r["domain"], r["migration_status"]) for r in INVENTORY["functions"]}
        self.assertEqual(live - committed, set(), "run scripts/audit_arch001_ownership.py --write")
        self.assertEqual(committed - live, set(), "run scripts/audit_arch001_ownership.py --write")


class GlobalStateTests(unittest.TestCase):
    def test_no_dynamic_globals_lookup(self):
        """A globals()-based lookup silently loses names once code leaves
        app.py's namespace (found in playlist_service during ARCH-001)."""
        offenders = []
        for rel in SERVICES + ROUTE_FILES + ["app.py"]:
            for node in ast.walk(_tree(rel)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "globals":
                    offenders.append(f"{rel}:{node.lineno}")
        # acoustid_service only checks whether the stdlib `sys` module is bound
        offenders = [o for o in offenders if not o.startswith("backend/acoustid_service.py")]
        self.assertEqual(offenders, [])

    def test_no_global_rebinding_of_a_name_other_modules_import(self):
        """`global X; X = ...` in one module is invisible to modules that
        imported X; shared mutable state lives on objects instead
        (backend/library_cache.LibraryCache, app_runtime.jobs/transactions)."""
        imported_elsewhere = {}
        for rel in SERVICES + ROUTE_FILES:
            for node in ast.walk(_tree(rel)):
                if isinstance(node, ast.ImportFrom) and node.module:
                    for a in node.names:
                        imported_elsewhere.setdefault((node.module, a.name), set()).add(rel)
        violations = []
        for rel in SERVICES:
            for node in ast.walk(_tree(rel)):
                if isinstance(node, ast.Global):
                    for name in node.names:
                        users = imported_elsewhere.get((_modname(rel), name))
                        if users:
                            violations.append(f"{rel}: global {name} is imported by {sorted(users)}")
        self.assertEqual(violations, [])


class DuplicateSafetyInvariantTests(unittest.TestCase):
    """Part 10: unattended duplicate deletion needs a shared fingerprint
    recording identity or byte-identical files, plus the release-slot
    safeguards. A shared embedded Recording ID is never enough."""

    def _pair(self, root, *, same_bytes):
        lib = root / "lib.flac"
        src = root / "src.flac"
        lib.write_bytes(b"library-copy")
        src.write_bytes(b"library-copy" if same_bytes else b"different-bytes")
        return src, lib

    def _dup(self, src, lib, **overrides):
        dup = {"source_path": str(src), "lib_path": str(lib), "source_item_id": 20, "lib_id": 10,
               "release_relation": "same_release_position", "match_type": "MB Track ID", "confidence": "high"}
        dup.update(overrides)
        return dup

    def test_embedded_recording_id_alone_never_deletes(self):
        from backend.duplicate_identity import select_unattended_cleanup_paths
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, lib = self._pair(root, same_bytes=False)
            chosen = select_unattended_cleanup_paths({"duplicates": [self._dup(src, lib)]}, root, lambda p, r: True)
            self.assertEqual(chosen, [])

    def test_fingerprint_or_byte_identity_is_required_and_sufficient_with_same_slot(self):
        from backend.duplicate_identity import select_unattended_cleanup_paths
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, lib = self._pair(root, same_bytes=False)
            fp = self._dup(src, lib, fingerprint_verified=True)
            self.assertEqual(select_unattended_cleanup_paths({"duplicates": [fp]}, root, lambda p, r: True),
                             [str(src.resolve())])
            other_slot = self._dup(src, lib, fingerprint_verified=True, release_relation="different_release")
            self.assertEqual(select_unattended_cleanup_paths({"duplicates": [other_slot]}, root, lambda p, r: True), [])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, lib = self._pair(root, same_bytes=True)
            exact = self._dup(src, lib, match_type="identical file size")
            self.assertEqual(select_unattended_cleanup_paths({"duplicates": [exact]}, root, lambda p, r: True),
                             [str(src.resolve())])

    def test_scheduled_duplicate_cleanup_delegates_to_the_identity_rules(self):
        import inspect
        from backend import dedup_service
        selector = inspect.getsource(dedup_service._maintenance_duplicate_plan)
        self.assertIn("_duplicate_identity.plan_unattended_cleanup(", selector)
        self.assertIn("_maintenance_duplicate_plan(", inspect.getsource(dedup_service._maintenance_full_duplicate_scan))


class ApiContractTests(unittest.TestCase):
    """Part 8: the HTTP surface is unchanged by the decomposition."""

    def test_url_map_matches_the_v0_1_30_baseline(self):
        import app
        baseline = json.loads((ROOT / "tests" / "arch001_route_baseline.json").read_text(encoding="utf-8"))["rules"]
        live = {(r.rule, r.endpoint, tuple(sorted(r.methods))) for r in app.app.url_map.iter_rules()}
        base = {(a, b, tuple(c)) for a, b, c in baseline}
        # Every v0.1.30 route survives unchanged; new routes are listed here explicitly.
        added_since = {
            ("/api/dedup/unattended-cleanup", "dedup_unattended_cleanup_status"),
            ("/api/dedup/unattended-cleanup", "dedup_unattended_cleanup_set"),
            ("/api/dedup/maintenance-run", "dedup_maintenance_run"),
            ("/api/dedup/reviewed-cleanup/plan", "dedup_reviewed_cleanup_plan"),
            ("/api/library/album-duplicate-analysis", "library_album_duplicate_analysis_last"),
            ("/api/library/album-duplicate-analysis", "library_album_duplicate_analysis_run"),
            ("/api/library/untracked-inventory", "library_untracked_inventory_last"),
            ("/api/library/untracked-inventory", "library_untracked_inventory_run"),
        }
        self.assertEqual(base - live, set())
        self.assertEqual({(rule, endpoint) for rule, endpoint, _m in live - base}, added_since)

    def test_route_results_keep_their_historical_shape(self):
        import app
        from backend.serializers import json_route_result
        with app.app.test_request_context("/"):
            ok = json_route_result({"ok": True}, 200)
            err = json_route_result({"ok": False}, 400)
        self.assertEqual(ok.get_json(), {"ok": True})
        self.assertIsInstance(err, tuple)
        self.assertEqual(err[1], 400)


if __name__ == "__main__":
    unittest.main()
