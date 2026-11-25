"""Starter Beets config written on a fresh install (issue #603).

``beets_embedded.write_starter_config_if_missing()`` is the one place the app
writes the user's ``config.yaml`` -- and only when none exists. Beets' config,
plugin registry and the stock web app are process-global, so (as in
``test_beets_embedded``) each scenario runs in a fresh interpreter against a
temporary BEETSDIR and a synthetic MUSIC_ROOT. No live data, no network.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# A fake secret planted in the environment: the writer must never copy it (or
# anything secret) into the generated file (issue #603 hard constraint).
FAKE_SECRET = "ACOUSTIDKEYsnyth0123456789abcdef"

DRIVER = r'''
import json, os, sys
from backend import beets_embedded
from beets import config

scenario = sys.argv[1]
target = os.fsdecode(config.user_config_path())
out = {"target": target}

if scenario == "existing":
    # Pre-create a sentinel config; the writer must leave it byte-for-byte.
    os.makedirs(os.path.dirname(target), exist_ok=True)
    sentinel = b"# my own beets config\nplugins: [inline]\n"
    with open(target, "wb") as f:
        f.write(sentinel)
    out["result"] = beets_embedded.write_starter_config_if_missing()
    out["bytes"] = open(target, "rb").read().decode("utf-8")
    out["unchanged"] = open(target, "rb").read() == sentinel

elif scenario == "idempotent":
    out["first"] = beets_embedded.write_starter_config_if_missing()
    first_bytes = open(target, "rb").read()
    out["second"] = beets_embedded.write_starter_config_if_missing()
    out["unchanged"] = open(target, "rb").read() == first_bytes
    out["text"] = first_bytes.decode("utf-8")

elif scenario == "config_missing_clears":
    beets_embedded.write_starter_config_if_missing()
    # Equivalent unit check: re-read beets' config and confirm the file is now
    # present at exactly the path beets_embedded's `config_missing` check uses.
    config.clear()
    config.read(user=True, defaults=True)
    out["config_present"] = os.path.isfile(os.fsdecode(config.user_config_path()))

else:  # "fresh"
    out["result"] = beets_embedded.write_starter_config_if_missing()
    out["exists"] = os.path.isfile(target)
    out["text"] = open(target, "r", encoding="utf-8").read()

print(json.dumps(out, default=str))
'''


def _run(scenario, beetsdir, music_root):
    env = dict(os.environ)
    env["BEETSDIR"] = str(beetsdir)
    env["MUSIC_ROOT"] = str(music_root)
    env["ACOUSTID_API_KEY"] = FAKE_SECRET  # must not leak into the file
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-c", DRIVER, scenario],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"driver failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


class StarterConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.beetsdir = Path(self.tmp.name) / "config"
        self.music = Path(self.tmp.name) / "music"
        self.music.mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def test_writes_starter_when_absent(self):
        import yaml
        res = _run("fresh", self.beetsdir, self.music)
        self.assertEqual(res["result"], res["target"])
        self.assertTrue(res["exists"])
        doc = yaml.safe_load(res["text"])  # must be valid YAML beets can read
        self.assertEqual(doc["directory"], str(self.music))
        self.assertEqual(doc["library"], str(self.beetsdir / "musiclibrary.blb"))
        self.assertEqual(doc["plugins"], ["web", "webmanager", "chroma", "fetchart", "embedart"])
        self.assertEqual(doc["import"],
                         {"write": True, "copy": True, "resume": "ask", "duplicate_action": "skip"})
        # Header comments survive (they explain the file is the user's to edit).
        self.assertIn("created by Beets Web Manager", res["text"])
        self.assertIn("managed by Web Manager", res["text"])

    def test_never_overwrites_existing_config(self):
        res = _run("existing", self.beetsdir, self.music)
        self.assertIsNone(res["result"])
        self.assertTrue(res["unchanged"])
        self.assertIn("inline", res["bytes"])  # the user's content, untouched

    def test_output_contains_no_secret(self):
        res = _run("fresh", self.beetsdir, self.music)
        self.assertNotIn(FAKE_SECRET, res["text"])
        self.assertNotIn("ACOUSTID_API_KEY", res["text"])

    def test_idempotent_second_run_is_a_noop(self):
        res = _run("idempotent", self.beetsdir, self.music)
        self.assertEqual(res["first"], res["target"])
        self.assertIsNone(res["second"])
        self.assertTrue(res["unchanged"])

    def test_config_missing_clears_after_write(self):
        res = _run("config_missing_clears", self.beetsdir, self.music)
        self.assertTrue(res["config_present"])


if __name__ == "__main__":
    unittest.main()
