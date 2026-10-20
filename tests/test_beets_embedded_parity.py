"""ADR 0006 Phase 1 QA: the in-process transport matches the HTTP transport,
and stays correct under the threaded Waitress/job model.

Each driver runs in a fresh interpreter against a temporary BEETSDIR,
library and music folder (Beets' config and plugin registry are
process-global). No live data.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "embedded_parity"


def _beets_available() -> bool:
    try:
        import beets  # noqa: F401
    except ImportError:
        return False
    return shutil.which("beet") is not None


@unittest.skipUnless(_beets_available(), "needs beets==2.14.1 and the `beet` CLI (requirements.txt)")
class EmbeddedParityTests(unittest.TestCase):
    def _run(self, driver: str) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BEETS_", "BEETSDIR"))}
        env.update({"BWM_ROOT": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"})
        proc = subprocess.run([sys.executable, str(FIXTURES / driver)], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr[-4000:])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_in_process_reads_errors_and_streams_match_the_http_transport(self):
        # Same library, adapter switched between a real `beet web` process
        # (BEETS_ENGINE=http) and the in-process engine (embedded).
        out = self._run("golden_driver.py")
        diverged = {k: v for k, v in out.items() if not (v == "SAME" or (isinstance(v, list) and v[0] == "SAME"))}
        self.assertEqual(diverged, {})
        # The comparisons are not vacuous.
        self.assertEqual(out["items"][1], 6)
        self.assertEqual(out["q_unicode"][1], 2)
        self.assertEqual(out["q_slash"][1], 2)

    def test_threaded_reads_writes_and_imports_and_nested_flask_context(self):
        out = self._run("concurrency_driver.py")
        self.assertEqual(out["concurrency_errors"], [])
        self.assertGreater(out["counts"]["writes"], 0)
        self.assertEqual(out["albums_after"], ["Album0", "Album1", "Album2", "Album3"])
        self.assertEqual(out["items_after"], 12)
        self.assertEqual(out["async_ops"], ["succeeded"])
        # Called from inside another Flask app's request (a Web Manager route):
        # the outer `g` survives and Beets' `g.lib` does not leak into it.
        self.assertEqual(out["nested"]["marker"], "outer")
        self.assertFalse(out["nested"]["has_lib_on_outer_g"])
        self.assertGreater(out["nested"]["bytes"], 0)
        # Default transport never imports Beets.
        self.assertEqual(json.loads(out["http_default"]), {"beets_loaded": False, "enabled": False})
        # Rollback: the embedded-written DB has the stock `beet` schema and the CLI reads it.
        self.assertTrue(out["schema_equal"], out["schema_diff"])
        self.assertEqual(out["beet_cli_on_embedded_db"][0], 0)
        self.assertIn("Tracks: 12", out["beet_cli_on_embedded_db"][1])


if __name__ == "__main__":
    unittest.main()
