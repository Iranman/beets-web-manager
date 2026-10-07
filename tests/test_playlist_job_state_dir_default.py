"""#276: PLAYLIST_JOB_STATE_DIR must never mean the working directory.

Path("") is Path("."), which is truthy, so ``Path(env.get(..., "")) or default``
never used the default. app_runtime reads the environment at import time, so
each case imports it in a fresh interpreter.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _job_state_dir(value):
    code = "import backend.app_runtime as rt; print(rt.PLAYLIST_JOB_STATE_DIR.resolve())"
    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        env = {k: v for k, v in os.environ.items() if k != "PLAYLIST_JOB_STATE_DIR"}
        env.update({"WEB_MANAGER_DATA_DIR": data_dir, "PYTHONPATH": str(REPO)})
        if value is not None:
            env["PLAYLIST_JOB_STATE_DIR"] = value
        # cwd is not the repo, so a working-directory fallback is detectable.
        out = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env,
                             capture_output=True, text=True, timeout=120, check=True).stdout
        return Path(out.strip().splitlines()[-1]), Path(data_dir).resolve(), Path(cwd).resolve()


class PlaylistJobStateDirDefault(unittest.TestCase):
    def test_unset_empty_or_relative_use_data_dir(self):
        for value in (None, "", "   ", ".", "jobs", "./pl-jobs"):
            with self.subTest(value=value):
                got, data_dir, cwd = _job_state_dir(value)
                self.assertEqual(got, data_dir / "playlists" / "jobs")
                self.assertNotEqual(got, cwd)

    def test_absolute_value_is_used(self):
        with tempfile.TemporaryDirectory() as custom:
            got, _, _ = _job_state_dir(custom)
            self.assertEqual(got, Path(custom).resolve())

    def test_checkpoint_save_and_resume_scan_use_data_dir(self):
        # Real save path, then a fresh interpreter (restart) finds it again.
        jid = "pl-0123456789abcdef0123"
        save = ("from backend import playlist_service as ps; ps._playlist_save_job_state("
                "{'job_id': '%s', 'status': 'running', 'playlist': 'QA Mix', 'job_key': {'name': 'QA Mix'}, 'log': []})" % jid)
        scan = ("from backend import playlist_service as ps; "
                "print([r['job_id'] for r in ps._playlist_saved_job_states_for_name('QA Mix')])")
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
            env = {k: v for k, v in os.environ.items() if k != "PLAYLIST_JOB_STATE_DIR"}
            env.update({"WEB_MANAGER_DATA_DIR": data_dir, "PYTHONPATH": str(REPO)})
            for code in (save, scan):
                out = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env,
                                     capture_output=True, text=True, timeout=120, check=True).stdout
            self.assertEqual(list(Path(cwd).rglob("pl-*.json")), [])
            self.assertTrue((Path(data_dir) / "playlists" / "jobs" / f"{jid}.json").is_file())
            self.assertIn(jid, out.strip().splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
