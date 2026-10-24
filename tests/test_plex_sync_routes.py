"""Route tests for routes_plex_sync: audit/heal endpoints start the right
jobs with the right flags, reuse a running job, and refuse a heal that has
every action disabled. The service module is a stub, so no real filesystem
walk or Plex call happens (the service itself is covered by
tests/test_plex_sync_service.py).
"""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

routes_plex_sync = flask_app = None


def setUpModule():
    # Deferred: test_ai_batch_retry_race must be the first to import app.
    global routes_plex_sync, flask_app
    import app
    import routes_plex_sync
    flask_app = app.app


def _stub_service():
    captured = {"audit_calls": [], "heal_calls": []}

    def audit_job(log, cancel=None):
        captured["audit_calls"].append(list(log))
        log.append("[plex-sync] audit stub")
        return {"report": {"ok": True}}

    def apply_heal(log, **flags):
        captured["heal_calls"].append(flags)
        log.append("[plex-sync] heal stub %s" % sorted(flags))
        return {"flags": flags}

    svc = SimpleNamespace(
        PLEX_SYNC_AUTO_CHMOD=False,
        PLEX_SYNC_AUTO_TOUCH=False,
        PLEX_SYNC_AUTO_REFRESH=False,
        PLEX_SYNC_MAX_REPAIR_PATHS=20000,
        plex_sync_audit_job=audit_job,
        apply_plex_sync_heal=apply_heal,
        _captured=captured,
    )
    return svc


class _JobsStub:
    def __init__(self):
        self.runs = []
        self.running = []

    def all(self):
        return self.running

    def start_python(self, fn, label="", metadata=None):
        entry = {
            "job_id": "j%d" % (len(self.runs) + 1),
            "fn": fn,
            "label": label,
            "metadata": dict(metadata or {}),
        }
        self.runs.append(entry)
        return SimpleNamespace(job_id=entry["job_id"], label=label,
                               metadata=dict(metadata or {}), status="running")


class PlexSyncRouteTests(unittest.TestCase):
    def setUp(self):
        self.jobs = _JobsStub()
        self.svc = _stub_service()
        patches = [
            mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}),
            mock.patch.object(routes_plex_sync, "jobs", self.jobs),
            mock.patch.object(routes_plex_sync, "_svc", self.svc),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = flask_app.test_client()

    # ── audit ──

    def test_audit_route_starts_the_read_only_job(self):
        res = self.client.post("/api/plex/sync/audit")
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["job_id"], "j1")
        self.assertNotIn("already_running", payload)
        self.assertEqual(len(self.jobs.runs), 1)
        run = self.jobs.runs[0]
        self.assertEqual(run["metadata"]["type"], "plex-sync-audit")
        self.assertEqual(run["label"], "Plex sync audit")
        self.assertIs(run["fn"], self.svc.plex_sync_audit_job)

    def test_audit_route_reuses_a_running_audit_job(self):
        self.jobs.running = [SimpleNamespace(
            status="running", job_id="j7",
            metadata={"type": "plex-sync-audit"})]
        res = self.client.post("/api/plex/sync/audit")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["already_running"], True)
        self.assertEqual(res.get_json()["job_id"], "j7")
        self.assertEqual(self.jobs.runs, [])

    # ── heal ──

    def test_heal_with_every_action_disabled_is_refused(self):
        res = self.client.post("/api/plex/sync/heal", json={})
        self.assertEqual(res.status_code, 400)
        payload = res.get_json()
        self.assertEqual(payload["ok"], False)
        self.assertIn("every action disabled", payload["error"])
        self.assertEqual(self.jobs.runs, [])

    def test_heal_route_starts_job_with_requested_flags(self):
        res = self.client.post("/api/plex/sync/heal",
                               json={"allow_chmod": True, "max_paths": 5})
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(payload["ok"], True)
        self.assertEqual(len(self.jobs.runs), 1)
        run = self.jobs.runs[0]
        self.assertEqual(run["metadata"]["type"], "plex-sync-heal")
        self.assertTrue(run["metadata"]["allow_chmod"])
        self.assertFalse(run["metadata"]["allow_touch"])
        self.assertFalse(run["metadata"]["allow_refresh"])
        # Request-supplied cap is clamped up to the service minimum (100).
        self.assertEqual(run["metadata"]["max_paths"], 100)
        self.assertIn("chmod", run["label"])
        # The job closure forwards exactly the decided flags to the service.
        log = []
        result = run["fn"](log)
        self.assertEqual(self.svc._captured["heal_calls"],
                         [{"allow_chmod": True, "allow_touch": False,
                           "allow_refresh": False, "max_paths": 100}])
        self.assertIn("[plex-sync] heal requested: allow_chmod", log[0])
        self.assertEqual(result["heal"]["flags"]["allow_chmod"], True)

    def test_heal_route_falls_back_to_environment_defaults(self):
        self.svc.PLEX_SYNC_AUTO_CHMOD = False
        self.svc.PLEX_SYNC_AUTO_TOUCH = True
        res = self.client.post("/api/plex/sync/heal")
        self.assertEqual(res.status_code, 200)
        run = self.jobs.runs[0]
        self.assertTrue(run["metadata"]["allow_touch"])
        self.assertFalse(run["metadata"]["allow_chmod"])
        log = []
        run["fn"](log)
        self.assertEqual(self.svc._captured["heal_calls"],
                         [{"allow_chmod": False, "allow_touch": True,
                           "allow_refresh": False, "max_paths": 20000}])

    def test_heal_route_reuses_a_running_heal_job(self):
        self.jobs.running = [SimpleNamespace(
            status="running", job_id="j9",
            metadata={"type": "plex-sync-heal"})]
        res = self.client.post("/api/plex/sync/heal", json={"allow_chmod": True})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["already_running"], True)
        self.assertEqual(res.get_json()["job_id"], "j9")
        self.assertEqual(self.jobs.runs, [])


if __name__ == "__main__":
    unittest.main()
