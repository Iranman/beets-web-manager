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
        # Each scenario spins up a fresh embedded Beets import in a subprocess;
        # 300s was tight under parallel CI load (N1), so allow more headroom.
        proc = subprocess.run([sys.executable, str(DRIVER), scenario], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=600)
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

    # Regression for the #819 live failure: a human-confirmed, fingerprint-
    # verified, divergent-title pinned release must import through a REAL
    # `beet import --search-id` session -- exercising choose_match /
    # _pinned_partial_match, not a direct call with _matches pre-injected.
    def test_divergent_titles_force_import_on_human_confirmed_path(self):
        # The #825 live failure: NO fingerprint evidence, text-divergent titles,
        # human-confirmed Import-with-ID. Pre-fix this was refused ("no confident
        # match"); now the human's explicit pin forces it past Beets' text bar.
        # The source file count (2) lines up with the release track count (2),
        # so the tracklist-sanity rail passes. The import writes the release's
        # MusicBrainz metadata over the divergent source tags -- the items come
        # back with the release's titles, not the classical-style source titles.
        out = self._run("fp_primary_divergent")
        self.assertNotIn("import_error", out)
        self.assertEqual(out["import"]["skipped"], [])
        self.assertEqual(len(out["import"]["imported_album_ids"]), 1)
        self.assertEqual(len(out["import"]["forced_pin"]), 1)
        # No fingerprint evidence, so the fingerprint-primary flag stays empty:
        # the forced-pin rail, not fingerprint identity, accepted this.
        self.assertEqual(out["import"]["confirmed_fingerprint_primary"], [])
        self.assertEqual(out["albums"], [{"album": "Jóga", "albumartist": "Björk",
                                          "mb_albumid": REL, "mb_releasegroupid": RG}])
        # Tag correction: the divergent source titles are gone, replaced by the
        # pinned release's MusicBrainz track titles.
        self.assertEqual(out["items"], ["Jóga (Introvert 4/4 mix)", "Jóga (Introvert Breaks mix)"])
        self.assertEqual(out["stats_after"], {"albums": 1, "items": 2})

    def test_divergent_titles_on_the_auto_path_are_still_refused(self):
        # Cardinal rule (#820/#822): the AUTOMATIC path never forces. Same
        # divergent-title folder, no fingerprint evidence, but human_confirmed
        # False -> Beets' strong text bar still refuses it, no album, no flag.
        out = self._run("auto_divergent")
        self.assertNotIn("import_error", out)
        self.assertEqual(out["import"]["imported_album_ids"], [])
        self.assertEqual(out["import"]["forced_pin"], [])
        self.assertEqual([s["reason"] for s in out["import"]["skipped"]], ["no_strong_match"])
        self.assertEqual(out["import"]["confirmed_fingerprint_primary"], [])
        self.assertEqual((out["albums"], out["items"]), ([], []))

    def test_divergent_titles_import_with_web_manager_fingerprint_evidence(self):
        # Same divergent-title release, now with Web Manager's own per-file
        # AcoustID evidence threaded in (fingerprint_confirmed). The override
        # engages and the release imports past the text-distance ceiling.
        out = self._run("fp_primary_confirmed")
        self.assertNotIn("import_error", out)
        self.assertEqual(out["import"]["skipped"], [])
        self.assertEqual(len(out["import"]["imported_album_ids"]), 1)
        self.assertEqual(len(out["import"]["confirmed_fingerprint_primary"]), 1)
        self.assertEqual(out["albums"], [{"album": "Jóga", "albumartist": "Björk",
                                          "mb_albumid": REL, "mb_releasegroupid": RG}])
        self.assertEqual(out["stats_after"], {"albums": 1, "items": 2})


if __name__ == "__main__":
    unittest.main()
