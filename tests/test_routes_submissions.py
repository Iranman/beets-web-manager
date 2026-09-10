"""Tests for routes_submissions.py: AcoustID submit and MBID attach engine migration.

Milestone 2 eliminates all BEET_BIN and _beet_run calls from routes_submissions.py:
1. _start_acoustid_submit_job() delegates to beets_client.acoustid_submit()
2. attach_album_mbids() delegates to album_metadata_repair_v1:
   beets_client.plan_album_metadata() + beets_client.apply_album_metadata()
"""

import ast
import json
import unittest
from unittest import mock

from backend.beets_client import BeetsUnavailableError
import routes_submissions
from app import app


class AcoustidSubmitJobTests(unittest.TestCase):
    def setUp(self):
        self._readiness_mock = {
            "plugins": {"chroma": True},
            "fpcalc_available": True,
            "pyacoustid_available": True,
            "acoustid_key_configured": True,
        }

    def _execute_job(self, route_fn, *args):
        captured = {}
        def fake_start_python(fn_inner, label=None):
            log = []
            res = fn_inner(log, cancel_event=None)
            captured["log"] = log
            captured["result"] = res
            return mock.Mock(job_id="job-submit-test")

        with mock.patch.object(routes_submissions.jobs, "start_python", side_effect=fake_start_python):
            resp = route_fn(*args)
        return resp, captured

    @mock.patch.object(routes_submissions, "_submission_readiness")
    @mock.patch.object(routes_submissions.lib, "get_album")
    @mock.patch.object(routes_submissions.beets_client, "acoustid_submit")
    def test_album_acoustid_submit_calls_beets_client_acoustid_submit(self, mock_submit, mock_get_album, mock_ready):
        mock_ready.return_value = self._readiness_mock
        item1 = mock.Mock(id=1, mb_trackid="11111111-1111-1111-1111-111111111111")
        mock_album = mock.Mock(id=10, albumartist="Artist", album="Album", items=lambda: [item1])
        mock_get_album.return_value = mock_album
        mock_submit.return_value = {"ok": True, "returncode": 0, "stdout": "1 fingerprints submitted"}

        with app.test_request_context("/api/albums/10/acoustid-submit", method="POST"):
            resp, captured = self._execute_job(routes_submissions.album_acoustid_submit, 10)

        data = json.loads(resp.get_data(as_text=True))
        self.assertTrue(data["ok"])
        mock_submit.assert_called_once_with(query="album_id:10", api_key=mock.ANY, timeout=300.0)
        self.assertIn("1 fingerprints submitted", captured["log"])

    @mock.patch.object(routes_submissions, "_submission_readiness")
    @mock.patch.object(routes_submissions.lib, "get_item")
    @mock.patch.object(routes_submissions.beets_client, "acoustid_submit")
    def test_item_acoustid_submit_singleton_calls_beets_client(self, mock_submit, mock_get_item, mock_ready):
        mock_ready.return_value = self._readiness_mock
        mock_item = mock.Mock(id=5, album_id=None, artist="A", title="T", mb_trackid="11111111-1111-1111-1111-111111111111")
        mock_get_item.return_value = mock_item
        mock_submit.return_value = {"ok": True, "returncode": 0, "stdout": "submitted"}

        with app.test_request_context("/api/items/5/acoustid-submit", method="POST"):
            resp, captured = self._execute_job(routes_submissions.item_acoustid_submit, 5)

        data = json.loads(resp.get_data(as_text=True))
        self.assertTrue(data["ok"])
        mock_submit.assert_called_once_with(query="id:5", api_key=mock.ANY, timeout=300.0)

    @mock.patch.object(routes_submissions, "_submission_readiness")
    @mock.patch.object(routes_submissions.lib, "get_album")
    @mock.patch.object(routes_submissions.beets_client, "acoustid_submit")
    def test_acoustid_submit_engine_offline_fails_closed(self, mock_submit, mock_get_album, mock_ready):
        mock_ready.return_value = self._readiness_mock
        mock_album = mock.Mock(id=10, items=lambda: [mock.Mock(id=1, mb_trackid="11111111-1111-1111-1111-111111111111")])
        mock_get_album.return_value = mock_album
        mock_submit.side_effect = BeetsUnavailableError("engine offline")

        with app.test_request_context("/api/albums/10/acoustid-submit", method="POST"):
            with self.assertRaises(RuntimeError):
                self._execute_job(routes_submissions.album_acoustid_submit, 10)

    @mock.patch.object(routes_submissions, "_submission_readiness")
    @mock.patch.object(routes_submissions.lib, "get_album")
    @mock.patch.object(routes_submissions.beets_client, "acoustid_submit")
    def test_acoustid_submit_nonzero_exit_raises(self, mock_submit, mock_get_album, mock_ready):
        mock_ready.return_value = self._readiness_mock
        mock_album = mock.Mock(id=10, items=lambda: [mock.Mock(id=1, mb_trackid="11111111-1111-1111-1111-111111111111")])
        mock_get_album.return_value = mock_album
        mock_submit.return_value = {"ok": False, "returncode": 1, "stderr": "network failure"}

        with app.test_request_context("/api/albums/10/acoustid-submit", method="POST"):
            with self.assertRaises(RuntimeError):
                self._execute_job(routes_submissions.album_acoustid_submit, 10)


class AttachAlbumMbidsTests(unittest.TestCase):
    def _execute_job(self, aid, payload):
        captured = {}
        def fake_start_python(fn_inner, label=None, metadata=None):
            log = []
            res = fn_inner(log, cancel_event=None, update_state=None)
            captured["log"] = log
            captured["result"] = res
            return mock.Mock(job_id="job-attach-test")

        with app.test_request_context(
            f"/api/submissions/albums/{aid}/attach-mbids",
            method="POST",
            json=payload,
        ), mock.patch.object(routes_submissions.jobs, "start_python", side_effect=fake_start_python):
            resp = routes_submissions.attach_album_mbids(aid)
        return resp, captured

    @mock.patch.object(routes_submissions, "_invalidate_lib_cache")
    @mock.patch.object(routes_submissions.lib, "get_album")
    @mock.patch.object(routes_submissions.lib, "get_item")
    @mock.patch.object(routes_submissions.beets_client, "plan_album_metadata")
    @mock.patch.object(routes_submissions.beets_client, "apply_album_metadata")
    def test_attach_album_mbids_success_calls_plan_and_apply(
        self, mock_apply, mock_plan, mock_get_item, mock_get_album, mock_inval
    ):
        mock_item = mock.Mock(id=101, mb_trackid="44444444-4444-4444-4444-444444444444")
        mock_album = mock.Mock(
            id=7,
            items=lambda: [mock_item],
            mb_albumartistid="11111111-1111-1111-1111-111111111111",
            mb_releasegroupid="22222222-2222-2222-2222-222222222222",
            mb_albumid="33333333-3333-3333-3333-333333333333",
        )
        mock_get_album.return_value = mock_album
        mock_get_item.return_value = mock_item

        mock_plan.return_value = {"ok": True, "operation_id": "op-mbid-7", "token": "op-mbid-7"}
        mock_apply.return_value = {"ok": True}

        payload = {
            "mb_albumartistid": "11111111-1111-1111-1111-111111111111",
            "mb_releasegroupid": "22222222-2222-2222-2222-222222222222",
            "mb_albumid": "33333333-3333-3333-3333-333333333333",
            "recordings": [{"item_id": 101, "mb_trackid": "44444444-4444-4444-4444-444444444444"}],
        }

        resp, captured = self._execute_job(7, payload)
        data = json.loads(resp.get_data(as_text=True))
        self.assertTrue(data["ok"])

        mock_plan.assert_called_once_with(
            album_id=7,
            album_fields={
                "mb_albumartistid": "11111111-1111-1111-1111-111111111111",
                "mb_releasegroupid": "22222222-2222-2222-2222-222222222222",
                "mb_albumid": "33333333-3333-3333-3333-333333333333",
            },
            track_fields={
                "101": {"mb_trackid": "44444444-4444-4444-4444-444444444444"},
            },
        )
        mock_apply.assert_called_once_with(plan_token="op-mbid-7", force_write_tags=True)
        mock_inval.assert_called()
        self.assertTrue(captured["result"]["verified"])

    @mock.patch.object(routes_submissions.lib, "get_album")
    def test_attach_album_mbids_invalid_uuid_returns_400(self, mock_get_album):
        mock_get_album.return_value = mock.Mock(id=7, items=lambda: [])
        payload = {"mb_albumartistid": "invalid-uuid", "mb_releasegroupid": "22222222-2222-2222-2222-222222222222"}
        with app.test_request_context("/api/submissions/albums/7/attach-mbids", method="POST", json=payload):
            resp = routes_submissions.attach_album_mbids(7)
        self.assertEqual(resp[1] if isinstance(resp, tuple) else resp.status_code, 400)

    @mock.patch.object(routes_submissions.lib, "get_album")
    @mock.patch.object(routes_submissions.beets_client, "plan_album_metadata")
    def test_attach_album_mbids_plan_rejection_raises(self, mock_plan, mock_get_album):
        mock_item = mock.Mock(id=101)
        mock_get_album.return_value = mock.Mock(id=7, items=lambda: [mock_item])
        mock_plan.return_value = {"ok": False, "error": "album_metadata_album_not_found"}

        payload = {
            "mb_albumartistid": "11111111-1111-1111-1111-111111111111",
            "mb_releasegroupid": "22222222-2222-2222-2222-222222222222",
            "recordings": [{"item_id": 101, "mb_trackid": "44444444-4444-4444-4444-444444444444"}],
        }

        with self.assertRaises(RuntimeError):
            self._execute_job(7, payload)


class SubmissionsStructuralASTTests(unittest.TestCase):
    def test_zero_beet_bin_or_beet_run_in_routes_submissions(self):
        import inspect
        source = inspect.getsource(routes_submissions)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in ("BEET_BIN", "_beet_run"):
                self.fail(f"Found prohibited symbol '{node.id}' in routes_submissions.py")
            if isinstance(node, ast.Attribute) and node.attr in ("Popen",):
                self.fail("Found prohibited Popen call in routes_submissions.py")


if __name__ == "__main__":
    unittest.main()
