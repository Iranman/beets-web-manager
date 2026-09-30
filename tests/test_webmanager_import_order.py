"""Every engine op module must import on its own, in a fresh interpreter.

operations.py imports the op modules at its end and each op module imports
operations, so a helper shared between op modules has to live in the leaf
module engine_common -- otherwise whichever module is imported first finds
its sibling half-initialised (this shipped once: remove_ops imported two
helpers from merge_ops, and importing merge_ops first failed).
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = ("engine_common", "operations", "replace_ops", "remove_ops", "merge_ops", "untracked_ops", "plugin_ops")


class EngineModuleImportOrderTests(unittest.TestCase):
    def test_each_module_imports_first_in_a_fresh_interpreter(self):
        for name in MODULES:
            with self.subTest(module=name):
                res = subprocess.run([sys.executable, "-c", f"import beetsplug.webmanager.{name}"],
                                     cwd=str(ROOT), capture_output=True, text=True, timeout=120)
                self.assertEqual(res.returncode, 0, res.stderr[-600:])

    def test_engine_common_imports_nothing_from_the_package_at_load_time(self):
        source = (ROOT / "beetsplug" / "webmanager" / "engine_common.py").read_text(encoding="utf-8")
        top_level = [line for line in source.splitlines() if line.startswith(("from .", "import ."))]
        self.assertEqual(top_level, [])


if __name__ == "__main__":
    unittest.main()
