"""Tests for ARCH-003 / SEC-002 Final Closure.

Verifies:
1. import_folder_with_id and import_folder_v1 use engine-owned Beets import without local subprocess fallbacks.
2. Control Agent unavailability causes imports to fail closed immediately.
3. _album_cleanup_apply_issue routes cleanup operations through beets_client transaction calls.
4. Machine-checked mutation inventory CI gate passes with 0 unclassified sinks.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.beets_client import BeetsClient, BeetsUnavailableError
from scripts.verify_arch003_mutation_inventory import verify_mutation_inventory


class TestArch003FinalClosure(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(__file__).parent.parent.resolve()
        self.app_path = self.repo_root / "app.py"
        self.beets_client_path = self.repo_root / "backend" / "beets_client.py"

        self.app_source = self.app_path.read_text(encoding="utf-8")
        self.beets_client_source = self.beets_client_path.read_text(encoding="utf-8")

    def test_app_has_no_local_beet_import_subprocess(self):
        self.assertNotIn('subprocess.run(base_import + ["import"', self.app_source)
        self.assertNotIn("subprocess.run(\n                base_import + [\"import\"", self.app_source)

    def test_beets_client_has_no_in_process_fallback(self):
        self.assertNotIn("_local_engine_fallback", self.beets_client_source)
        self.assertNotIn("transaction_engine", self.beets_client_source)

    def test_import_fails_closed_when_engine_unreachable(self):
        client = BeetsClient(base_url="http://127.0.0.1:59999", timeout=0.5)
        with self.assertRaises(BeetsUnavailableError):
            client.plan_import_folder({"source_folder": "/staging/album1"})

    def test_mutation_inventory_ci_gate_passes(self):
        self.assertTrue(verify_mutation_inventory(self.repo_root, check_mode=True))

    def test_album_cleanup_apply_issue_routes_to_beets_client(self):
        self.assertIn("beets_client.plan_album_maintenance", self.app_source)
        self.assertIn("beets_client.plan_album_artwork", self.app_source)
        self.assertIn("beets_client.plan_folder_cleanup", self.app_source)


if __name__ == "__main__":
    unittest.main()
