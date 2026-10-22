"""The folder-MBID-name apply route must start its heavy work in a job, not in
the request thread.

Live incident: ``POST /api/library/folder-mbid-names/apply`` took 173s and
timed out a 120s client because the album-folder walk under MUSIC_ROOT
(~9,022 dirs over NFS) and the Lidarr resolver snapshot ran synchronously in
the request thread before the job was started. Both must run INSIDE the job
body so the POST returns ``{ok, job_id}`` immediately.

The resolver/walk is faked here (no Beets, no Lidarr, no disk walk): the fake
records which thread the engine function runs on and blocks until released, so
a synchronous handler would be caught by the elapsed-time and thread asserts.
"""
import os
import threading
import time
import unittest
from unittest import mock

routes_library = flask_app = None


def setUpModule():
    global routes_library, flask_app
    import test_ai_batch_retry_race  # noqa: F401  sets the suite temp env + imports app first
    import app
    import routes_library as rl
    routes_library = rl
    flask_app = app.app


def _ok_result():
    return {
        "ok": True, "applied": [], "operations": [], "skipped": [],
        "already_correct": [], "errors": [], "done_folders": [],
        "final_summary": {
            "total_selected": 0, "would_change_count": 0, "changed_count": 0,
            "already_correct_count": 0, "skipped_count": 0, "error_count": 0,
            "remaining_count": 0, "skipped_by_reason": {},
        },
    }


class FolderMbidAsyncStart(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        p.start()
        self.addCleanup(p.stop)
        self.client = flask_app.test_client()

    def test_apply_returns_job_id_without_running_engine_in_request_thread(self):
        """Default run (folders omitted -> full walk). The POST returns a job
        id promptly; the walk/resolver run on the job thread, not the request
        thread."""
        request_thread = threading.current_thread()
        ran_on = {}
        started = threading.Event()
        release = threading.Event()

        def fake_apply(folders, **kw):
            ran_on["thread"] = threading.current_thread()
            started.set()
            # Block the job thread. A synchronous handler would block the POST
            # here and the elapsed-time assert below would fail.
            release.wait(10)
            return _ok_result()

        with mock.patch.object(routes_library, "_apply_folder_mbid_names",
                               side_effect=fake_apply):
            t0 = time.monotonic()
            resp = self.client.post("/api/library/folder-mbid-names/apply",
                                    json={"dry_run": True})
            elapsed = time.monotonic() - t0
            try:
                body = resp.get_json()
                self.assertEqual(resp.status_code, 200, body)
                self.assertTrue(body.get("ok"), body)
                self.assertTrue(body.get("job_id"), body)
                # Handler returned without doing the walk: sub-second, not 173s.
                self.assertLess(elapsed, 3.0,
                                f"handler blocked for {elapsed:.1f}s (walk ran in request thread)")
                # The engine ran, but on the job thread -- never the request thread.
                self.assertTrue(started.wait(10), "engine function never ran in the job")
                self.assertIsNot(ran_on["thread"], request_thread,
                                 "engine ran in the request thread, not the job")
            finally:
                release.set()

    def test_confirmed_gate_still_returns_400_fast(self):
        """A non-dry-run without confirmed is rejected before any job starts,
        without touching the walk/resolver."""
        with mock.patch.object(routes_library, "_apply_folder_mbid_names",
                               side_effect=AssertionError("engine must not run")):
            resp = self.client.post("/api/library/folder-mbid-names/apply",
                                    json={"dry_run": False})
            self.assertEqual(resp.status_code, 400, resp.get_json())


if __name__ == "__main__":
    unittest.main()
