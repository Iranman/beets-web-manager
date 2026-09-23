"""Migration & Architecture Invariant Tests.

Guarantees that:
1. Architecture invariant validator passes with zero violations.
2. Web Manager contains NO direct SQLite access to musiclibrary.blb.
3. No Docker socket mounts exist.
4. Stock LinuxServer Beets image is used in docker-compose.
5. In Phase 5, requirements.txt will fail this test if Beets runtime is not retired.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class MigrationInvariantsTests(unittest.TestCase):
    def test_architecture_invariants_script(self):
        """Verify scripts/validate_architecture_invariants.py exits cleanly."""
        script_path = ROOT / "scripts" / "validate_architecture_invariants.py"
        self.assertTrue(script_path.exists())
        res = subprocess.run([sys.executable, str(script_path)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Validator failed:\nstdout: {res.stdout}\nstderr: {res.stderr}")

    def test_requirements_phase5_marker(self):
        """Documents Beets runtime dependency status.
        
        In Phase 1-4: Beets runtime is retained in requirements.txt as migration scaffolding.
        In Phase 5: Beets runtime MUST be removed from requirements.txt.
        """
        req_file = ROOT / "requirements.txt"
        content = req_file.read_text(encoding="utf-8")
        
        # Check if Phase 5 migration flag is set
        phase5_active = os.environ.get("PHASE5_RETIRE_BEETS_RUNTIME") == "1"
        if phase5_active:
            self.assertNotIn("beets==", content, "Phase 5 Invariant Violation: beets runtime must be removed from requirements.txt")

    def test_version_consistency(self):
        """Verify version.py exists and matches version strings across plugin."""
        from beetsplug.webmanager.version import PLUGIN_VERSION, PROTOCOL_VERSION
        from beetsplug.webmanager import PLUGIN_VERSION as INIT_PV, PROTOCOL_VERSION as INIT_PROT
        from beetsplug.webmanager.operations import PLUGIN_VERSION as OP_PV, PROTOCOL_VERSION as OP_PROT

        self.assertEqual(PLUGIN_VERSION, "1.0.0")
        self.assertEqual(PROTOCOL_VERSION, "1.0")
        self.assertEqual(INIT_PV, PLUGIN_VERSION)
        self.assertEqual(INIT_PROT, PROTOCOL_VERSION)
        self.assertEqual(OP_PV, PLUGIN_VERSION)
        self.assertEqual(OP_PROT, PROTOCOL_VERSION)

    def test_phase2_reads_use_stock_beets_adapter(self):
        """Verify that lib in backend.beets_client uses StockBeetsLibrary with BeetsAdapter."""
        from backend.beets_client import lib
        from backend.beets_adapter import StockBeetsLibrary, BeetsAdapter

        self.assertIsInstance(lib, StockBeetsLibrary)
        self.assertIsInstance(lib.adapter, BeetsAdapter)

    def test_phase2_read_outage_fails_closed(self):
        """Verify that when stock Beets is unreachable, read routes fail closed with 503 ENGINE_OFFLINE without fallback."""
        import app as app_module
        from backend.beets_adapter import BeetsAdapterConnectionError, StockBeetsLibrary
        from unittest import mock

        orig_lib = getattr(app_module, "lib", None)
        app_module.lib = StockBeetsLibrary(app_module.beets_adapter)
        try:
            with mock.patch.dict("os.environ", {"BEETS_WEB_AUTH_DISABLED": "1"}):
                with app_module.app.test_client() as client:
                    with mock.patch.object(app_module.beets_adapter, "get_stats", side_effect=BeetsAdapterConnectionError("Down")):
                        res = client.get("/api/stats")
                        self.assertEqual(res.status_code, 503)
                        data = res.get_json()
                        self.assertEqual(data.get("error_code"), "ENGINE_OFFLINE")
                        self.assertEqual(data.get("status"), "unavailable")

                    with mock.patch.object(app_module.beets_adapter, "get_items", side_effect=BeetsAdapterConnectionError("Down")):
                        res = client.get("/api/items")
                        self.assertEqual(res.status_code, 503)
                        data = res.get_json()
                        self.assertEqual(data.get("error_code"), "ENGINE_OFFLINE")

                    with mock.patch.object(app_module.beets_adapter, "get_artists", side_effect=BeetsAdapterConnectionError("Down")):
                        res = client.get("/api/artists")
                        self.assertEqual(res.status_code, 503)
                        data = res.get_json()
                        self.assertEqual(data.get("error_code"), "ENGINE_OFFLINE")
        finally:
            app_module.lib = orig_lib


if __name__ == "__main__":
    unittest.main()
