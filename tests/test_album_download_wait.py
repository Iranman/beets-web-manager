"""An album download that is still making progress is not killed at its soft
deadline (a 38 Spesh retry downloaded 8/8 tracks and was cancelled at 2400s)."""

import time
import unittest
from pathlib import Path

import tests.test_041_qa_regressions as qa
import tests.test_041_transaction_followups as tf

ROOT = Path(__file__).resolve().parents[1]


class AlbumDownloadWaitTests(qa.ChildJobQaTests):
    for _name in [n for n in dir(qa.ChildJobQaTests) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def test_progressing_child_past_the_deadline_finishes(self):
        def child_body(log, cancel):
            end = time.time() + 0.6
            while time.time() < end:
                log.append("[slskd] 4/8 file(s) downloaded")
                time.sleep(0.02)
            return {"ok": True, "imported": 8}
        child = self._child(child_body)
        job = self._run_parent(child, timeout=0.1, idle_timeout=5)
        self.assertTrue(tf._wait(lambda: job.status != "running"))
        self.assertEqual(job.result, {"ok": True, "imported": 8}, "\n".join(job.log))
        self.assertIn("Still making progress", " ".join(job.log))

    def test_album_download_waiters_use_the_progress_aware_wait(self):
        # above the silent inner caps (import-slot wait, SpotiFLAC subprocess: 1200s each)
        self.assertGreater(self.js.ALBUM_DOWNLOAD_WAIT["idle_timeout"], 1200)
        for rel in ("routes_acquisition.py", "backend/replacement_service.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("**ALBUM_DOWNLOAD_WAIT", src, rel)
            self.assertNotIn("timeout=2400", src, rel)


if __name__ == "__main__":
    unittest.main()
