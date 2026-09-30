"""Routes for the new-album recovery (ARCH-021): the read-only album
candidates page and the attach_album plan, which runs as a read-only job
(it asks MusicBrainz and AcoustID once per file) whose result is the plan."""

import time
import unittest
from unittest import mock

import app as app_module
import routes_cleanup
import routes_maintenance
import backend.untracked_recovery_service as svc


class UntrackedAlbumAttachRouteTests(unittest.TestCase):
    def _call(self, fn, path, method="GET", json=None):
        with app_module.app.test_request_context(path, method=method, json=json):
            out = fn()
        body, status = out if isinstance(out, tuple) else (out, 200)
        return status, body.get_json()

    def test_album_candidates_is_a_paged_read(self):
        page = {"ok": True, "total": 1, "files": 3, "offset": 0, "limit": 5, "rows": [{"folder": "A/B", "files": 3}]}
        with mock.patch.object(svc, "album_candidates", return_value=page) as listing:
            status, body = self._call(routes_cleanup.library_untracked_recovery_album_candidates,
                                      "/api/library/untracked-recovery/album-candidates?limit=5")
        self.assertEqual((status, body["rows"][0]["folder"]), (200, "A/B"))
        listing.assert_called_once_with(limit=5, offset=0)
        with mock.patch.object(svc, "album_candidates", return_value={"ok": False, "code": "no_inventory", "rows": []}):
            status, _body = self._call(routes_cleanup.library_untracked_recovery_album_candidates,
                                       "/api/library/untracked-recovery/album-candidates")
        self.assertEqual(status, 404)
        status, _body = self._call(routes_cleanup.library_untracked_recovery_album_candidates,
                                   "/api/library/untracked-recovery/album-candidates?limit=x")
        self.assertEqual(status, 400)

    def test_attach_album_plan_runs_as_a_read_only_job_whose_result_is_the_plan(self):
        plan = {"ok": True, "operation_id": "tx-album", "status": "Preview", "files": 3, "excluded": []}
        with mock.patch.object(svc, "plan_album_attach", return_value=plan) as planner:
            status, body = self._call(routes_cleanup.library_untracked_recovery_plan,
                                      "/api/library/untracked-recovery/plan", method="POST",
                                      json={"action": "attach_album", "paths": ["Artist/Album (2020)"]})
            self.assertEqual(status, 202)
            job = routes_cleanup.jobs.get(body["job_id"])
            deadline = time.time() + 10
            while job.status == "running" and time.time() < deadline:
                time.sleep(0.01)
        self.assertEqual(job.status, "success")
        self.assertEqual(job.result, plan)
        self.assertEqual(planner.call_args.args, ("Artist/Album (2020)",))
        self.assertEqual((job.metadata["type"], job.metadata["mutating"]), ("untracked-album-plan", False))

    def test_attach_album_plan_takes_exactly_one_folder(self):
        with mock.patch.object(svc, "plan_album_attach") as planner:
            status, _body = self._call(routes_cleanup.library_untracked_recovery_plan,
                                       "/api/library/untracked-recovery/plan", method="POST",
                                       json={"action": "attach_album", "paths": ["a", "b"]})
        self.assertEqual(status, 400)
        planner.assert_not_called()

    def test_apply_and_rollback_dispatch_to_the_recovery_authority(self):
        apply_fn, rollback_fn = routes_maintenance._ENGINE_FAMILIES[svc.ATTACH_ALBUM_FAMILY]
        self.assertIs(apply_fn, svc.apply_recovery)
        self.assertIs(rollback_fn, svc.rollback_recovery)


if __name__ == "__main__":
    unittest.main()
