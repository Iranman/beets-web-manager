"""N4: autotag import through Beets inside Web Manager, with the real
musicbrainz plugin answered from a recorded MusicBrainz response
(tests/fixtures/embedded_autotag). Each scenario runs in a fresh interpreter;
real sockets are blocked, so nothing reaches the network.
"""

import json
import os
import site
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "fixtures" / "embedded_autotag" / "driver.py"
REL = "169bf2aa-1b2a-4506-b872-3aebfa52a5dc"
RG = "ecb0a296-2813-36cb-9470-dae14b44fda3"


def _beets_available() -> bool:
    try:
        import beetsplug.musicbrainz  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_beets_available(), "needs beets==2.14.1 (requirements.txt)")
class EmbeddedAutotagTests(unittest.TestCase):
    def _run(self, scenario: str) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BEETS_", "BEETSDIR"))}
        env.update({"BWM_ROOT": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONUSERBASE": os.environ.get("PYTHONUSERBASE") or site.getuserbase()})
        proc = subprocess.run([sys.executable, str(DRIVER), scenario], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr[-4000:])
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        # Only the recorded release lookup is asked for, and the engine stays healthy.
        self.assertEqual(out["requests"], [f"https://musicbrainz.org/ws/2/release/{REL}"])
        self.assertTrue(out["status_ok"])
        self.assertTrue(out["status_ok_after"])
        self.assertEqual(out["source_left"], ["01.wav", "02.wav"], "a copy import never touches the source")
        return out

    def _assert_nothing_imported(self, out: dict, reason: str) -> None:
        self.assertNotIn("import_error", out)
        self.assertEqual(out["import"]["imported_album_ids"], [])
        self.assertEqual([s["reason"] for s in out["import"]["skipped"]], [reason])
        self.assertEqual((out["albums"], out["items"]), ([], []))
        self.assertEqual(out["stats_after"], {"albums": 0, "items": 0})

    def test_matching_files_import_with_the_release_group(self):
        out = self._run("match")
        self.assertNotIn("import_error", out)
        self.assertEqual(out["import"]["skipped"], [])
        self.assertEqual(len(out["import"]["imported_album_ids"]), 1)
        self.assertEqual(out["albums"], [{"album": "Jóga", "albumartist": "Björk",
                                          "mb_albumid": REL, "mb_releasegroupid": RG}])
        self.assertEqual(out["items"], ["Jóga (Introvert 4/4 mix)", "Jóga (Introvert Breaks mix)"])
        self.assertEqual(out["stats_after"], {"albums": 1, "items": 2})

    def test_files_that_do_not_fit_the_release_are_refused(self):
        self._assert_nothing_imported(self._run("mismatch"), "no_strong_match")

    def test_provider_down_skips_and_leaves_the_engine_usable(self):
        self._assert_nothing_imported(self._run("provider_down"), "no_candidates")

    def test_unknown_release_is_skipped(self):
        self._assert_nothing_imported(self._run("not_found"), "no_candidates")


if __name__ == "__main__":
    unittest.main()
