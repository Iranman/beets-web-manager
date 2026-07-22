"""Proves the actual precedence semantics of Beets' `pluginpath` loading,
directly through Beets' own plugin-loading implementation -- not assumed.

Step 11 of the setup-diagnostics fix: routes_setup.py's
_BEETS_PLUGINPATH_CONFIG (see app.py) configures `/config/beetsplug` before
`/app/beetsplug` so that a same-named user plugin in /config overrides the
bundled one in /app. This test verifies that ordering assumption against
Beets' real `beetsplug.__path__`/import-machinery behavior (beets/plugins.py
`get_plugin_names()` does `beetsplug.__path__ = paths + list(beetsplug.__path__)`,
i.e. configured pluginpath entries are *prepended*, so the first configured
path wins for a same-named module) using two temporary, synthetic,
same-named plugin directories -- never the real /config or /app paths, and
never the real music library.
"""
import importlib.util
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_BEETS_AVAILABLE = importlib.util.find_spec("beets") is not None


@unittest.skipUnless(_BEETS_AVAILABLE, "beets is not importable in this environment")
class PluginPathPrecedenceTests(unittest.TestCase):
    def _run_precedence_probe(self, pluginpath_dirs):
        script = textwrap.dedent(
            f"""
            import beets, beets.plugins
            beets.config['pluginpath'] = {[str(p) for p in pluginpath_dirs]!r}
            beets.config['plugins'] = ['precedencecheck']
            beets.plugins.load_plugins()
            instances = beets.plugins.find_plugins()
            sources = [getattr(p, 'SOURCE', None) for p in instances]
            print('SOURCES=' + ','.join(s for s in sources if s))
            """
        )
        return subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_user_pluginpath_listed_first_takes_precedence_over_bundled(self):
        """A same-named synthetic plugin in the "user" directory (listed
        first in pluginpath, matching /config/beetsplug's position) must be
        the one Beets actually loads -- not the "bundled" directory's
        version (matching /app/beetsplug's position)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_dir = root / "user_beetsplug"
            bundled_dir = root / "bundled_beetsplug"
            user_dir.mkdir()
            bundled_dir.mkdir()
            (user_dir / "precedencecheck.py").write_text(
                "from beets.plugins import BeetsPlugin\n"
                "class PrecedenceCheckPlugin(BeetsPlugin):\n"
                "    SOURCE = 'user'\n",
                encoding="utf-8",
            )
            (bundled_dir / "precedencecheck.py").write_text(
                "from beets.plugins import BeetsPlugin\n"
                "class PrecedenceCheckPlugin(BeetsPlugin):\n"
                "    SOURCE = 'bundled'\n",
                encoding="utf-8",
            )

            proc = self._run_precedence_probe([user_dir, bundled_dir])

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SOURCES=user", proc.stdout)
        self.assertNotIn("bundled", proc.stdout)

    def test_reversing_pluginpath_order_reverses_which_copy_wins(self):
        """Control case proving the result above is really about *order*,
        not some other difference between the two directories: swap which
        directory is listed first and the opposite copy must win."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dir_a = root / "dir_a_beetsplug"
            dir_b = root / "dir_b_beetsplug"
            dir_a.mkdir()
            dir_b.mkdir()
            (dir_a / "precedencecheck.py").write_text(
                "from beets.plugins import BeetsPlugin\n"
                "class PrecedenceCheckPlugin(BeetsPlugin):\n"
                "    SOURCE = 'dir_a'\n",
                encoding="utf-8",
            )
            (dir_b / "precedencecheck.py").write_text(
                "from beets.plugins import BeetsPlugin\n"
                "class PrecedenceCheckPlugin(BeetsPlugin):\n"
                "    SOURCE = 'dir_b'\n",
                encoding="utf-8",
            )

            proc = self._run_precedence_probe([dir_b, dir_a])

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SOURCES=dir_b", proc.stdout)
        self.assertNotIn("dir_a", proc.stdout)


if __name__ == "__main__":
    unittest.main()
