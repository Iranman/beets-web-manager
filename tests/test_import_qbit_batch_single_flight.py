"""Only one qBittorrent batch runs at a time: every batch stages a unit into
the same per-unit stage and cleans it, so a second batch (a resumed older one
or a new one) is refused with 409 qbittorrent_batch_active while the first
worker is registered. Local batches are not affected."""
import itertools
import types
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
import backend.ai_service as ai
import routes_import as ri

CODE = "qbittorrent_batch_active"


class QbitBatchSingleFlightTests(unittest.TestCase):
    def setUp(self):
        self.started = []
        ids = itertools.count(1)

        def start_python(fn, **kw):  # the worker never runs: its registration stays
            self.started.append(kw["metadata"]["batch_job_id"])
            return types.SimpleNamespace(job_id=f"job-{next(ids)}", update_state=None)

        for target, name, kw in (
            (ri, "_resolve_import_scan_root", {"side_effect": lambda p: (Path(p), None)}),
            (ri.jobs, "start_python", {"side_effect": start_python}),
            (ri, "_ai_batch_commit", {}),
            (ri, "_ai_batch_persist_job_association", {}),
            (ri, "_ai_batch_load_state", {"return_value": None}),
            (ri, "_ai_batch_public_state", {"return_value": {}}),
            (ri._acquisition_service, "qbit_import_filter", {"return_value": None}),
        ):
            p = mock.patch.object(target, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._forget, ["qa", "qb", "local"])
        ctx = app_module.app.test_request_context()
        ctx.push()
        self.addCleanup(ctx.pop)

    @staticmethod
    def _forget(ids):
        with ai._ai_batch_worker_lock:
            for b in ids:
                ai._ai_batch_active_workers.pop(b, None)
            getattr(ai, "_ai_batch_qbit_slot", {}).clear()

    def start(self, batch_id, source="qbittorrent", recover=False):
        with mock.patch.object(ri.uuid, "uuid4", return_value=types.SimpleNamespace(hex=batch_id)):
            out = ri._start_ai_batch_job("/downloads", recover_batch_job_id=batch_id if recover else "",
                                         source=source)
        resp, code = out if isinstance(out, tuple) else (out, out.status_code)
        return code, resp.get_json()

    def test_second_qbittorrent_batch_refused_while_first_runs(self):
        self.assertEqual(self.start("qa")[0], 200)
        code, body = self.start("qb")
        self.assertEqual(code, 409, body)
        self.assertEqual(body["error_code"], CODE)
        self.assertEqual(body["active_batch_job_id"], "qa")
        self.assertEqual(self.started, ["qa"])
        self.assertNotIn("qb", ai._ai_batch_active_workers)  # reservation released

    def test_resumed_older_batch_refused_while_new_one_runs(self):
        self.assertEqual(self.start("qb")[0], 200)
        code, body = self.start("qa", recover=True)
        self.assertEqual(code, 409, body)
        self.assertEqual(body["error_code"], CODE)
        self.assertEqual(self.started, ["qb"])

    def test_local_batch_unaffected_and_slot_frees_when_worker_ends(self):
        self.assertEqual(self.start("qa")[0], 200)
        self.assertEqual(self.start("local", source="local")[0], 200)
        ai._ai_batch_release_worker("qa", "job-1")  # the first worker finished
        self.assertEqual(self.start("qb")[0], 200)
        self.assertEqual(self.started, ["qa", "local", "qb"])


if __name__ == "__main__":
    unittest.main()
