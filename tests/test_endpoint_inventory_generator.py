"""Regression coverage for scripts/generate_endpoint_inventory.py's
human-review/machine-fact separation.

Wave 24 final review round 3, section 14/15/16: the generator used to read
its own previous *generated output* as the "previous review" source, which
let a corrupted, fully-NEEDS_REVIEW inventory silently regenerate and
re-launder itself forever (this actually happened once on this PR -- see
docs/TECHNICAL_DEBT.md). The fix moved human-reviewed judgment fields into a
separate file, security/endpoint_review_manifest.json, that the generator
only ever reads, never writes. These tests build a small synthetic repo
layout (a copy of the generator script plus minimal fake route files and a
fake manifest) so each scenario can be proven without touching the real
app.py / security/endpoint_review_manifest.json.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR_SRC = ROOT / "scripts" / "generate_endpoint_inventory.py"

_FAKE_APP_PY = '''
from flask import Flask
app = Flask(__name__)

_AUTH_PUBLIC_ENDPOINTS = {("GET", "health")}
_FIRST_RUN_PUBLIC_ENDPOINTS = set()

@app.get("/api/health")
def health():
    return "ok"

@app.post("/api/widgets")
def create_widget():
    return "ok"

@app.delete("/api/widgets/<int:wid>")
def delete_widget(wid):
    return "ok"
'''

_EMPTY_ROUTES_PY = "# no routes in this fixture module\n"

_BASE_MANIFEST = {
    "manifest_version": 1,
    "entries": [
        {
            "method": "GET", "endpoint": "health",
            "security_classification": "PUBLIC", "purpose": "Health check.",
            "required_role_or_privilege": "none", "handles_secrets": False,
            "accesses_files": False, "creates_background_job": False,
            "invokes_external_services_or_subprocesses": False,
            "destructive_or_high_impact": False, "rate_limit_required": False,
            "rate_limit_implemented": False, "existing_security_tests": [],
        },
        {
            "method": "POST", "endpoint": "create_widget",
            "security_classification": "ADMIN_ONLY", "purpose": "Create a widget.",
            "required_role_or_privilege": "owner/admin", "handles_secrets": False,
            "accesses_files": False, "creates_background_job": False,
            "invokes_external_services_or_subprocesses": False,
            "destructive_or_high_impact": False, "rate_limit_required": True,
            "rate_limit_implemented": True, "existing_security_tests": ["tests/test_widgets.py"],
        },
        {
            "method": "DELETE", "endpoint": "delete_widget",
            "security_classification": "ADMIN_ONLY", "purpose": "Delete a widget.",
            "required_role_or_privilege": "owner/admin", "handles_secrets": False,
            "accesses_files": False, "creates_background_job": False,
            "invokes_external_services_or_subprocesses": False,
            "destructive_or_high_impact": True, "rate_limit_required": True,
            "rate_limit_implemented": True, "existing_security_tests": ["tests/test_widgets.py"],
        },
    ],
}


class _FixtureRepoTestCase(unittest.TestCase):
    """Builds a disposable copy of the generator + a minimal fake route/
    manifest layout under a temp directory, so each test can mutate its own
    copy freely."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "scripts").mkdir()
        (self.repo / "security").mkdir()
        shutil.copy(GENERATOR_SRC, self.repo / "scripts" / "generate_endpoint_inventory.py")
        (self.repo / "app.py").write_text(_FAKE_APP_PY, encoding="utf-8")
        (self.repo / "routes_jobs.py").write_text(_EMPTY_ROUTES_PY, encoding="utf-8")
        (self.repo / "routes_lidarr.py").write_text(_EMPTY_ROUTES_PY, encoding="utf-8")
        (self.repo / "routes_setup.py").write_text(_EMPTY_ROUTES_PY, encoding="utf-8")
        (self.repo / "routes_submissions.py").write_text(_EMPTY_ROUTES_PY, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _write_manifest(self, manifest: dict) -> None:
        (self.repo / "security" / "endpoint_review_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

    def _write_inventory(self, inventory: dict) -> None:
        (self.repo / "security" / "endpoint_inventory.json").write_text(
            json.dumps(inventory, indent=2), encoding="utf-8"
        )

    def _run_generator(self, *extra_args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "scripts/generate_endpoint_inventory.py", *extra_args],
            cwd=str(self.repo), capture_output=True, text=True,
        )

    def _read_inventory(self) -> dict:
        return json.loads((self.repo / "security" / "endpoint_inventory.json").read_text(encoding="utf-8"))


class EndpointInventoryGeneratorTests(_FixtureRepoTestCase):
    def test_a_fully_reviewed_manifest_yields_zero_needs_review(self):
        """Positive control: a complete manifest produces needs_review_route_count == 0."""
        self._write_manifest(_BASE_MANIFEST)
        proc = self._run_generator()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        inv = self._read_inventory()
        self.assertEqual(inv["needs_review_route_count"], 0)
        self.assertEqual(inv["route_count"], 3)

    def test_b_new_endpoint_not_in_manifest_is_needs_review(self):
        """A route with no manifest entry is flagged, not silently guessed,
        and fails the reviewed-route count check."""
        self._write_manifest(_BASE_MANIFEST)
        (self.repo / "app.py").write_text(
            _FAKE_APP_PY + '\n@app.get("/api/new-thing")\ndef brand_new_endpoint():\n    return "ok"\n',
            encoding="utf-8",
        )
        proc = self._run_generator()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        inv = self._read_inventory()
        self.assertEqual(inv["route_count"], 4)
        self.assertEqual(inv["needs_review_route_count"], 1)
        new_route = next(r for r in inv["routes"] if r["endpoint"] == "brand_new_endpoint")
        self.assertEqual(new_route["security_classification"], "NEEDS_REVIEW")

    def test_c_deleting_a_manifest_entry_reverts_that_route_to_needs_review(self):
        """Dropping an existing reviewed classification from the manifest
        (accidentally or otherwise) must surface as NEEDS_REVIEW again --
        it must never be silently forgiven by residual state in the
        generated file."""
        manifest = json.loads(json.dumps(_BASE_MANIFEST))  # deep copy
        manifest["entries"] = [e for e in manifest["entries"] if e["endpoint"] != "create_widget"]
        self._write_manifest(manifest)
        # Seed a stale generated inventory that still claims create_widget
        # was reviewed -- the generator must not treat this file as a
        # source of truth for review data.
        stale = {
            "route_count": 3, "needs_review_route_count": 0,
            "routes": [
                {"method": "POST", "endpoint": "create_widget", "route": "/api/widgets",
                 "source_file": "app.py", "line": 1, "security_classification": "ADMIN_ONLY",
                 "purpose": "stale claim from a corrupted prior run"},
            ],
        }
        self._write_inventory(stale)
        proc = self._run_generator()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        inv = self._read_inventory()
        widget_route = next(r for r in inv["routes"] if r["endpoint"] == "create_widget")
        self.assertEqual(widget_route["security_classification"], "NEEDS_REVIEW")
        self.assertEqual(inv["needs_review_route_count"], 1)

    def test_d_needs_review_placed_directly_in_manifest_propagates(self):
        """The manifest is authoritative -- if a reviewer explicitly marks
        an entry NEEDS_REVIEW there, the generator must not paper over it."""
        manifest = json.loads(json.dumps(_BASE_MANIFEST))
        for e in manifest["entries"]:
            if e["endpoint"] == "delete_widget":
                e["security_classification"] = "NEEDS_REVIEW"
        self._write_manifest(manifest)
        proc = self._run_generator()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        inv = self._read_inventory()
        self.assertEqual(inv["needs_review_route_count"], 1)
        route = next(r for r in inv["routes"] if r["endpoint"] == "delete_widget")
        self.assertEqual(route["security_classification"], "NEEDS_REVIEW")

    def test_e_changed_method_does_not_silently_inherit_the_old_reviews(self):
        """If a route's HTTP method changes (a real semantic change --
        different CSRF/idempotency/caching posture), the old method+endpoint
        review must not silently carry over to the new key."""
        self._write_manifest(_BASE_MANIFEST)
        # Flip create_widget from POST to PUT -- a materially different
        # method, and thus a different (method, endpoint) review key.
        changed_app = _FAKE_APP_PY.replace('@app.post("/api/widgets")', '@app.put("/api/widgets")')
        (self.repo / "app.py").write_text(changed_app, encoding="utf-8")
        proc = self._run_generator()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        inv = self._read_inventory()
        route = next(r for r in inv["routes"] if r["endpoint"] == "create_widget")
        self.assertEqual(route["method"], "PUT")
        self.assertEqual(route["security_classification"], "NEEDS_REVIEW",
                          "a method change must not inherit the old method's review")
        self.assertEqual(inv["needs_review_route_count"], 1)

    def test_f_check_cannot_pass_by_the_generated_file_agreeing_with_itself(self):
        """The core regression this file exists to prevent: a fully
        NEEDS_REVIEW generated inventory must NOT cause --check to pass just
        because the stale file already matches what regeneration would
        produce from itself. Regeneration must be driven by the manifest,
        so a real (non-corrupted) manifest regenerates real data even when
        the on-disk generated file was corrupted, and --check on the
        corrupted file must fail against that real regeneration.
        """
        self._write_manifest(_BASE_MANIFEST)
        # A self-consistent, but fully corrupted (all NEEDS_REVIEW),
        # generated file -- the exact shape a naive self-referential
        # generator would treat as already "reviewed" and happily
        # reproduce forever.
        corrupted_routes = []
        for method, endpoint, route in (
            ("GET", "health", "/api/health"),
            ("POST", "create_widget", "/api/widgets"),
            ("DELETE", "delete_widget", "/api/widgets/<int:wid>"),
        ):
            corrupted_routes.append({
                "method": method, "route": route, "endpoint": endpoint,
                "source_file": "app.py", "line": 1,
                "auth_required": True, "public_pre_setup_only": False,
                "changes_state": method != "GET", "csrf_required": method != "GET",
                "security_classification": "NEEDS_REVIEW", "purpose": "NEEDS_REVIEW",
                "required_role_or_privilege": "NEEDS_REVIEW", "handles_secrets": "NEEDS_REVIEW",
                "accesses_files": "NEEDS_REVIEW", "creates_background_job": "NEEDS_REVIEW",
                "invokes_external_services_or_subprocesses": "NEEDS_REVIEW",
                "destructive_or_high_impact": "NEEDS_REVIEW", "rate_limit_required": "NEEDS_REVIEW",
                "rate_limit_implemented": "NEEDS_REVIEW", "existing_security_tests": "NEEDS_REVIEW",
            })
        corrupted = {
            "generated_by": "scripts/generate_endpoint_inventory.py",
            "scope": "fixture", "auth_model": "fixture", "limitations": [],
            "route_count": 3, "needs_review_route_count": 3,
            "public_endpoint_allowlist": [], "first_run_public_endpoint_allowlist": [],
            "routes": corrupted_routes,
        }
        self._write_inventory(corrupted)

        # --check against the corrupted file, with a REAL manifest present,
        # must fail (the file is stale relative to what the manifest says
        # it should be) -- it must not pass just because the corrupted
        # file is internally self-consistent.
        proc = self._run_generator("--check")
        self.assertNotEqual(proc.returncode, 0,
                             "--check must not pass against a corrupted file merely because "
                             "it already matches its own prior (corrupted) output")

        # Regenerating for real must restore the manifest's real review
        # data, not perpetuate the corrupted file's NEEDS_REVIEW values.
        proc2 = self._run_generator()
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        inv = self._read_inventory()
        self.assertEqual(inv["needs_review_route_count"], 0)
        widget_route = next(r for r in inv["routes"] if r["endpoint"] == "create_widget")
        self.assertEqual(widget_route["security_classification"], "ADMIN_ONLY")


if __name__ == "__main__":
    unittest.main()
