"""QA #396: edge cases for the 0.7.0 T5 Import hardening."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import app as APP
import backend.import_review_service as irs
import backend.import_service as isvc
import backend.pending_review_store as prs
import backend.serializers as ser
import routes_import
from job_engine import JobStore


class _Base(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.downloads = self.root / "downloads"
        self.music = self.root / "music"
        self.downloads.mkdir()
        self.music.mkdir()
        self.client = APP.app.test_client()

    def patch(self, target, name, value):
        p = mock.patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)


class SkipLedgerEdgeTests(_Base):
    def setUp(self):
        super().setUp()
        self.ledger = self.root / "import_skipped.json"
        self.patch(prs, "_IMPORT_SKIPPED_FILE", self.ledger)

    def test_prefix_sibling_is_kept(self):
        prs._record_import_skips("/dl", [{"path": "/dl/A"}, {"path": "/dl/AB"}, {"path": "/dl/A/CD1"}])
        self.assertTrue(prs._forget_import_skip("/dl/A/"))
        self.assertEqual([r["path"] for r in prs._load_recorded_import_skips()], ["/dl/AB"])

    def test_unknown_path_is_a_no_op_and_does_not_rewrite(self):
        prs._record_import_skips("/dl", [{"path": "/dl/A"}])
        before = self.ledger.stat().st_mtime_ns
        self.assertFalse(prs._forget_import_skip("/dl/Z"))
        self.assertFalse(prs._forget_import_skip(""))
        self.assertEqual(self.ledger.stat().st_mtime_ns, before)

    def test_write_failure_never_raises_and_keeps_the_ledger(self):
        prs._record_import_skips("/dl", [{"path": "/dl/A"}])
        with mock.patch.object(prs, "_write_recorded_import_skips", side_effect=PermissionError("ro")):
            self.assertFalse(prs._forget_import_skip("/dl/A"))
        self.assertEqual([r["path"] for r in prs._load_recorded_import_skips()], ["/dl/A"])

    def test_missing_or_corrupt_ledger_is_a_no_op(self):
        self.assertFalse(prs._forget_import_skip("/dl/A"))
        self.ledger.write_text("{not json")
        self.assertFalse(prs._forget_import_skip("/dl/A"))


class ImportLogEdgeTests(_Base):
    def test_multibyte_char_split_at_tail_boundary(self):
        log = self.root / "beet.log"
        tail = "ééé last ♫\n"
        body = ("♫" * 10 + "\n") * 5 + tail
        log.write_bytes(body.encode("utf-8"))
        size = log.stat().st_size
        # Cut inside the first multi-byte character of the cut window.
        for max_bytes in range(len(tail.encode()) + 1, size):
            lines = isvc._read_log_tail(str(log), max_bytes=max_bytes)
            self.assertEqual(lines[-1], tail.strip("\n"))
            self.assertFalse(any("�" in line for line in lines), (max_bytes, lines))

    def test_limit_values(self):
        log = self.root / "beet.log"
        log.write_text("".join(f"line{i}\n" for i in range(1500)))
        self.patch(routes_import, "LOG_FILE", str(log))
        self.assertEqual(self.client.get("/api/import-log?limit=-1").status_code, 400)
        self.assertEqual(self.client.get("/api/import-log?limit=1.5").status_code, 400)
        body = self.client.get("/api/import-log?limit=999999").get_json()
        self.assertEqual(len(body["lines"]), 1000)  # clamped to the cap
        self.assertEqual(body["lines"][-1], "line1499")
        self.assertEqual(len(self.client.get("/api/import-log?limit=2").get_json()["lines"]), 2)
        self.assertEqual(self.client.get("/api/import-log?limit=0").get_json()["lines"], [])

    def test_revalidate_limit_negative_is_400_zero_is_default(self):
        self.assertEqual(self.client.post("/api/import-review/revalidate",
                                          json={"all": True, "limit": -1}).status_code, 400)
        self.assertEqual(self.client.post("/api/import-review/revalidate",
                                          json={"review_item_ids": "x", "limit": [1]}).status_code, 400)


class RevalidateJobRunTests(_Base):
    """The real revalidation runner inside a real JobStore: cancel and dedupe."""

    def setUp(self):
        super().setUp()
        self.store = JobStore()
        self.addCleanup(self.store.close, 2)
        self.patch(routes_import, "jobs", self.store)
        self.items = [{"path": f"/dl/{i}", "suggestion": {"mb_releasegroupid": "11111111-1111-1111-1111-111111111111"}}
                      for i in range(40)]
        self.patch(irs, "_load_pending_reviews", lambda **_: self.items)
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        self.seen = []

        def slow_compare(_rid, path, **_):
            self.seen.append(path)
            self.gate.wait(10)
            return {"ok": False, "error": "x"}
        self.patch(irs, "_candidate_track_comparison_payload", slow_compare)

    def _wait(self, cond, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_cancel_mid_run_stops_before_the_next_item(self):
        res = self.client.post("/api/import-review/revalidate", json={"all": True})
        self.assertEqual(res.status_code, 202)
        job = self.store.get(res.get_json()["job_id"])
        self.assertTrue(self._wait(lambda: len(self.seen) == 1))
        job.kill()
        self.gate.set()
        self.assertTrue(self._wait(lambda: job.finished_at is not None))
        self.assertEqual(len(self.seen), 1)
        self.assertIn("[cancelled]", job.log)
        self.assertEqual(job.returncode, -1)

    def test_concurrent_starts_produce_exactly_one_job(self):
        codes = []
        barrier = threading.Barrier(6)

        def go():
            c = APP.app.test_client()
            barrier.wait()
            codes.append(c.post("/api/import-review/revalidate", json={"all": True}).status_code)
        threads = [threading.Thread(target=go) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(sorted(codes), [202] + [409] * 5)
        self.assertEqual(len(self.store.all()), 1)


class AddPendingReviewPathEdgeTests(_Base):
    def setUp(self):
        super().setUp()
        self.pending_file = self.root / "ai_pending_review.json"
        self.pending_file.write_text("[]")
        self.patch(prs, "_AI_PENDING_FILE", self.pending_file)
        self.patch(prs, "_load_pending_reviews", lambda **_: json.loads(self.pending_file.read_text()))
        self.patch(prs, "_library_album_ids_for_folder", lambda *_: [])
        downloads, music = self.downloads, self.music
        self.patch(ser, "_import_review_cleanup_roots",
                   lambda *, allow_music=False: [downloads] + ([music] if allow_music else []))

    def test_symlinks_music_root_and_traversal_are_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.music / "Lib").mkdir()
        cases = [str(self.music), str(self.music / "Lib"), str(self.downloads),
                 str(self.downloads / ".." / "outside"), str(self.downloads) + "/", 123, ""]
        try:
            os.symlink(outside, self.downloads / "link")
            os.symlink(self.music / "Lib", self.downloads / "liblink")
            (self.downloads / "real").mkdir()
            os.symlink(self.downloads / "real", self.downloads / "inner")
            cases += [str(self.downloads / "link"), str(self.downloads / "liblink"), str(self.downloads / "inner")]
        except OSError:
            pass
        for path in cases:
            with self.subTest(path=path):
                res = self.client.post("/api/ai-pending-review", json={"path": path})
                self.assertEqual(res.status_code, 400, res.get_json())
        self.assertEqual(json.loads(self.pending_file.read_text()), [])

    def test_non_dict_body_and_suggestion_are_handled(self):
        self.assertEqual(self.client.post("/api/ai-pending-review", json=["x"]).status_code, 400)
        album = self.downloads / "A"
        album.mkdir()
        res = self.client.post("/api/ai-pending-review",
                               json={"path": str(album), "suggestion": ["bad"], "allow_existing": "yes"})
        self.assertEqual(res.status_code, 200, res.get_json())


if __name__ == "__main__":
    unittest.main()
