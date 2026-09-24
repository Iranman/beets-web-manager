"""Tests for routes_submissions.py: AcoustID submit and MBID attach, migrated
onto the stock-Beets integration plugin (BeetsAdapter).

1. _start_acoustid_submit_job() delegates to beets_adapter.mbsubmit() (the
   real chroma plugin's submit_items(), running inside stock Beets).
2. attach_album_mbids() writes MusicBrainz IDs directly via
   beets_adapter.modify(), verifying against the library afterward -- no
   local Plan/Apply ceremony, since stock Beets now owns the write.
"""

import ast
import json
import unittest
from unittest import mock

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
    @mock.patch("backend.beets_adapter.beets_adapter.mbsubmit")
    def test_album_acoustid_submit_calls_beets_adapter_mbsubmit(self, mock_submit, mock_get_album, mock_ready):
        mock_ready.return_value = self._readiness_mock
        item1 = mock.Mock(id=1, mb_trackid="11111111-1111-1111-1111-111111111111")
        mock_album = mock.Mock(id=10, albumartist="Artist", album="Album", items=lambda: [item1])
        mock_get_album.return_value = mock_album
        mock_submit.return_value = {"ok": True, "submitted_items": 1}

        with app.test_request_context("/api/albums/10/acoustid-submit", method="POST"):
            resp, captured = self._execute_job(routes_submissions.album_acoustid_submit, 10)

        data = json.loads(resp.get_data(as_text=True))
        self.assertTrue(data["ok"])
        mock_submit.assert_called_once_with([1], api_key=mock.ANY)
        self.assertIn("Submitted 1 fingerprint(s) to AcoustID.", captured["log"])

    @mock.patch.object(routes_submissions, "_submission_readiness")
    @mock.patch.object(routes_submissions.lib, "get_item")
    @mock.patch("backend.beets_adapter.beets_adapter.mbsubmit")
    def test_item_acoustid_submit_singleton_calls_beets_adapter(self, mock_submit, mock_get_item, mock_ready):
        mock_ready.return_value = self._readiness_mock
        mock_item = mock.Mock(id=5, album_id=None, artist="A", title="T", mb_trackid="11111111-1111-1111-1111-111111111111")
        mock_get_item.return_value = mock_item
        mock_submit.return_value = {"ok": True, "submitted_items": 1}

        with app.test_request_context("/api/items/5/acoustid-submit", method="POST"):
            resp, captured = self._execute_job(routes_submissions.item_acoustid_submit, 5)

        data = json.loads(resp.get_data(as_text=True))
        self.assertTrue(data["ok"])
        mock_submit.assert_called_once_with([5], api_key=mock.ANY)

    @mock.patch.object(routes_submissions, "_submission_readiness")
    @mock.patch.object(routes_submissions.lib, "get_album")
    @mock.patch("backend.beets_adapter.beets_adapter.mbsubmit")
    def test_acoustid_submit_engine_offline_fails_closed(self, mock_submit, mock_get_album, mock_ready):
        mock_ready.return_value = self._readiness_mock
        mock_album = mock.Mock(id=10, items=lambda: [mock.Mock(id=1, mb_trackid="11111111-1111-1111-1111-111111111111")])
        mock_get_album.return_value = mock_album
        mock_submit.side_effect = routes_submissions.BeetsUnavailableError("engine offline")

        with app.test_request_context("/api/albums/10/acoustid-submit", method="POST"):
            with self.assertRaises(RuntimeError):
                self._execute_job(routes_submissions.album_acoustid_submit, 10)

    @mock.patch.object(routes_submissions, "_submission_readiness")
    @mock.patch.object(routes_submissions.lib, "get_album")
    @mock.patch("backend.beets_adapter.beets_adapter.mbsubmit")
    def test_acoustid_submit_missing_recording_mbid_blocks_before_job(self, mock_submit, mock_get_album, mock_ready):
        mock_ready.return_value = self._readiness_mock
        mock_album = mock.Mock(id=10, albumartist="Artist", album="Album", items=lambda: [mock.Mock(id=1, mb_trackid="")])
        mock_get_album.return_value = mock_album

        with app.test_request_context("/api/albums/10/acoustid-submit", method="POST"):
            resp = routes_submissions.album_acoustid_submit(10)

        data = json.loads(resp[0].get_data(as_text=True)) if isinstance(resp, tuple) else json.loads(resp.get_data(as_text=True))
        self.assertFalse(data["ok"])
        mock_submit.assert_not_called()


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
    @mock.patch("backend.beets_adapter.beets_adapter.modify")
    def test_attach_album_mbids_success_writes_via_beets_adapter_modify(
        self, mock_modify, mock_get_item, mock_get_album, mock_inval
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
        mock_modify.return_value = {"ok": True}

        payload = {
            "mb_albumartistid": "11111111-1111-1111-1111-111111111111",
            "mb_releasegroupid": "22222222-2222-2222-2222-222222222222",
            "mb_albumid": "33333333-3333-3333-3333-333333333333",
            "recordings": [{"item_id": 101, "mb_trackid": "44444444-4444-4444-4444-444444444444"}],
        }

        resp, captured = self._execute_job(7, payload)
        data = json.loads(resp.get_data(as_text=True))
        self.assertTrue(data["ok"])

        mock_modify.assert_any_call(
            fields={
                "mb_albumartistid": "11111111-1111-1111-1111-111111111111",
                "mb_releasegroupid": "22222222-2222-2222-2222-222222222222",
                "mb_albumid": "33333333-3333-3333-3333-333333333333",
            },
            album_ids=[7],
            write=True,
            move=False,
        )
        mock_modify.assert_any_call(
            fields={"mb_trackid": "44444444-4444-4444-4444-444444444444"},
            item_ids=[101],
            write=True,
            move=False,
        )
        mock_inval.assert_called()
        self.assertTrue(captured["result"]["verified"])

    @mock.patch.object(routes_submissions.lib, "get_album")
    def test_attach_album_mbids_invalid_uuid_returns_400(self, mock_get_album):
        mock_get_album.return_value = mock.Mock(id=7, items=lambda: [])
        payload = {"mb_albumartistid": "invalid-uuid", "mb_releasegroupid": "22222222-2222-2222-2222-222222222222"}
        with app.test_request_context("/api/submissions/albums/7/attach-mbids", method="POST", json=payload):
            resp = routes_submissions.attach_album_mbids(7)
        self.assertEqual(resp[1] if isinstance(resp, tuple) else resp.status_code, 400)

    @mock.patch.object(routes_submissions, "_invalidate_lib_cache")
    @mock.patch.object(routes_submissions.lib, "get_album")
    @mock.patch.object(routes_submissions.lib, "get_item")
    @mock.patch("backend.beets_adapter.beets_adapter.modify")
    def test_attach_album_mbids_apply_rejection_raises(
        self, mock_modify, mock_get_item, mock_get_album, mock_inval
    ):
        mock_item = mock.Mock(id=101, mb_trackid="44444444-4444-4444-4444-444444444444")
        mock_get_album.return_value = mock.Mock(id=7, items=lambda: [mock_item])
        mock_get_item.return_value = mock_item
        mock_modify.return_value = {"ok": False, "error": "album_metadata_album_not_found"}

        payload = {
            "mb_albumartistid": "11111111-1111-1111-1111-111111111111",
            "mb_releasegroupid": "22222222-2222-2222-2222-222222222222",
            "recordings": [{"item_id": 101, "mb_trackid": "44444444-4444-4444-4444-444444444444"}],
        }

        with self.assertRaises(RuntimeError):
            self._execute_job(7, payload)


class SubmissionsStructuralASTTests(unittest.TestCase):
    def test_zero_beet_bin_or_beet_run_or_beets_client_in_routes_submissions(self):
        import inspect
        source = inspect.getsource(routes_submissions)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in ("BEET_BIN", "_beet_run", "beets_client"):
                self.fail(f"Found prohibited symbol '{node.id}' in routes_submissions.py")
            if isinstance(node, ast.Attribute) and node.attr in ("Popen",):
                self.fail("Found prohibited Popen call in routes_submissions.py")
        self.assertNotIn("beets_client", source)
        self.assertNotIn(":8338", source)


if __name__ == "__main__":
    unittest.main()
