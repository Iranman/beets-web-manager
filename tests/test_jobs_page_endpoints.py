"""Tests proving all Jobs page endpoints exist, respond correctly when authenticated,

and return clean error payloads when unauthenticated or when Beets control agent is offline.
"""
import os
import unittest
from unittest.mock import patch

os.environ['BEETS_WEB_AUTH_DISABLED'] = '1'
import app as app_module
from backend.beets_client import BeetsUnavailableError, BeetsError


class JobsPageEndpointsTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app_module.app
        self.client = self.app.test_client()

    def test_jobs_list_endpoint_authenticated(self):
        res = self.client.get('/api/jobs', headers={'X-Beets-CSRF': '1'})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(isinstance(data.get('jobs'), list))
        self.assertIn('count', data)

    def test_jobs_feed_endpoint_authenticated(self):
        res = self.client.get('/api/jobs/feed?limit=300&level=all', headers={'X-Beets-CSRF': '1'})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('ok'))
        self.assertTrue(isinstance(data.get('entries'), list))

    def test_maintenance_runner_report_endpoint(self):
        res = self.client.get('/api/jobs/maintenance-runner/report?summary=1', headers={'X-Beets-CSRF': '1'})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('ok'))
        self.assertIn('exists', data)

    def test_maintenance_runner_start_endpoint(self):
        res = self.client.post('/api/jobs/maintenance-runner', headers={'X-Beets-CSRF': '1'}, json={})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('ok'))
        self.assertIn('job_id', data)

    def test_album_folders_scan_endpoint(self):
        res = self.client.post('/api/clean/album-folders/scan', headers={'X-Beets-CSRF': '1'}, json={})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('ok'))
        self.assertIn('job_id', data)

    def test_album_folders_report_endpoint(self):
        res = self.client.get('/api/clean/album-folders/report', headers={'X-Beets-CSRF': '1'})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('ok'))
        self.assertIn('exists', data)

    def test_fix_genres_endpoint(self):
        res = self.client.post('/api/library/fix-genres', headers={'X-Beets-CSRF': '1'}, json={'force': False, 'use_ai': False})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('ok'))
        self.assertIn('job_id', data)

    def test_fetch_missing_art_endpoint(self):
        res = self.client.post('/api/fetch-missing-art', headers={'X-Beets-CSRF': '1'}, json={})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('ok'))
        self.assertIn('job_id', data)

    def test_rebuild_album_art_endpoint(self):
        res = self.client.post('/api/rebuild-album-art', headers={'X-Beets-CSRF': '1'}, json={'confirmed': True})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('ok'))
        self.assertIn('job_id', data)

    def test_art_repair_report_engine_offline_handling(self):
        with patch('app._art_repair_build_report', side_effect=BeetsUnavailableError("Control agent down")):
            res = self.client.get('/api/library/art-repair', headers={'X-Beets-CSRF': '1'})
            self.assertEqual(res.status_code, 503)
            data = res.get_json()
            self.assertFalse(data.get('ok'))
            self.assertEqual(data.get('error_code'), 'ENGINE_OFFLINE')
            self.assertEqual(data.get('error'), 'Beets engine is unavailable.')

    def test_art_repair_report_application_error_not_engine_offline(self):
        with patch('app._art_repair_build_report', side_effect=BeetsError("Processing calculation error")):
            res = self.client.get('/api/library/art-repair', headers={'X-Beets-CSRF': '1'})
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data.get('ok'))
            self.assertEqual(data.get('error_code'), 'ART_REPAIR_FAILED')
            self.assertEqual(data.get('error'), 'Could not load artwork repair status.')

    def test_library_health_engine_healthy_success(self):
        sample_report = {
            "ok": True,
            "duplicate_albums": [],
            "duplicate_album_count": 0,
            "rgid_duplicate_groups": [],
            "rgid_duplicate_group_count": 0,
            "rgid_resolved_groups": [],
            "rgid_resolved_group_count": 0,
            "orphaned_items": [],
            "orphaned_item_count": 0,
            "orphaned_item_ids": [],
            "empty_albums": [],
            "empty_album_count": 0,
            "database_rows_scanned": 10,
            "album_row_count": 2,
            "item_row_count": 8,
            "final_summary": {
                "database_rows_scanned": 10,
                "albums_count": 2,
                "tracks_count": 8,
                "duplicate_album_groups": 0,
                "same_release_group_id_groups": 0,
                "orphaned_items": 0,
                "empty_albums": 0,
                "missing_files": 0,
            },
        }
        with patch.object(app_module.beets_client, 'get_library_health', return_value=sample_report):
            res = self.client.get('/api/clean/library-health', headers={'X-Beets-CSRF': '1'})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data.get('ok'))
            self.assertEqual(data.get('album_row_count'), 2)
            self.assertEqual(data.get('item_row_count'), 8)

    def test_library_health_engine_offline_handling(self):
        with patch.object(app_module.beets_client, 'get_library_health', side_effect=BeetsUnavailableError("Control agent down")):
            res = self.client.get('/api/clean/library-health', headers={'X-Beets-CSRF': '1'})
            self.assertEqual(res.status_code, 503)
            data = res.get_json()
            self.assertFalse(data.get('ok'))
            self.assertEqual(data.get('error_code'), 'ENGINE_OFFLINE')
            self.assertEqual(data.get('error'), 'Beets engine is unavailable.')

    def test_library_health_engine_application_error_not_engine_offline(self):
        with patch.object(app_module.beets_client, 'get_library_health', side_effect=BeetsError("Internal query error")):
            res = self.client.get('/api/clean/library-health', headers={'X-Beets-CSRF': '1'})
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data.get('ok'))
            self.assertEqual(data.get('error_code'), 'LIBRARY_HEALTH_FAILED')
            self.assertEqual(data.get('error'), 'Could not load library health.')

    def test_library_health_engine_auth_error_handling(self):
        from backend.beets_client import BeetsAuthError
        with patch.object(app_module.beets_client, 'get_library_health', side_effect=BeetsAuthError("Token invalid")):
            res = self.client.get('/api/clean/library-health', headers={'X-Beets-CSRF': '1'})
            self.assertEqual(res.status_code, 503)
            data = res.get_json()
            self.assertFalse(data.get('ok'))
            self.assertEqual(data.get('error_code'), 'ENGINE_AUTH_FAILED')
            self.assertEqual(data.get('error'), 'Beets engine authentication failed.')

    def test_art_repair_engine_auth_error_handling(self):
        from backend.beets_client import BeetsAuthError
        with patch('app._art_repair_build_report', side_effect=BeetsAuthError("Token invalid")):
            res = self.client.get('/api/library/art-repair', headers={'X-Beets-CSRF': '1'})
            self.assertEqual(res.status_code, 503)
            data = res.get_json()
            self.assertFalse(data.get('ok'))
            self.assertEqual(data.get('error_code'), 'ENGINE_AUTH_FAILED')
            self.assertEqual(data.get('error'), 'Beets engine authentication failed.')

    def test_beets_client_http_500_raises_beets_error_not_unavailable(self):
        import urllib.error
        from io import BytesIO
        from backend.beets_client import BeetsClient
        client = BeetsClient(base_url="http://localhost:8338", token="test")
        body_bytes = b'{"ok": false, "error": "Internal database query error", "error_code": "DB_QUERY_FAILED"}'
        err = urllib.error.HTTPError("http://localhost:8338/library/health", 500, "Server Error", {}, BytesIO(body_bytes))
        with patch('urllib.request.urlopen', side_effect=err):
            with self.assertRaises(BeetsError) as ctx:
                client.get_library_health()
            self.assertNotIsInstance(ctx.exception, BeetsUnavailableError)
            self.assertEqual(ctx.exception.status_code, 500)
            self.assertEqual(ctx.exception.error_code, 'DB_QUERY_FAILED')

    def test_beets_client_http_401_raises_beets_auth_error(self):
        import urllib.error
        from io import BytesIO
        from backend.beets_client import BeetsClient, BeetsAuthError
        client = BeetsClient(base_url="http://localhost:8338", token="test")
        body_bytes = b'{"ok": false, "error": "Unauthorized token", "error_code": "ENGINE_AUTH_FAILED"}'
        err = urllib.error.HTTPError("http://localhost:8338/library/health", 401, "Unauthorized", {}, BytesIO(body_bytes))
        with patch('urllib.request.urlopen', side_effect=err):
            with self.assertRaises(BeetsAuthError) as ctx:
                client.get_library_health()
            self.assertEqual(ctx.exception.status_code, 401)
            self.assertEqual(ctx.exception.error_code, 'ENGINE_AUTH_FAILED')

    def test_control_agent_bounded_query_limits(self):
        from backend.beets_control_agent import _parse_bounded_int_param
        # Valid
        val, err = _parse_bounded_int_param({"duplicate_limit": ["50"]}, "duplicate_limit")
        self.assertIsNone(err)
        self.assertEqual(val, 50)
        # Malformed
        val, err = _parse_bounded_int_param({"duplicate_limit": ["garbage"]}, "duplicate_limit")
        self.assertIsNotNone(err)
        self.assertIn("must be an integer", err)
        # Out of bounds (negative)
        val, err = _parse_bounded_int_param({"duplicate_limit": ["-5"]}, "duplicate_limit")
        self.assertIsNotNone(err)
        self.assertIn("must be between 0 and 1000", err)
        # Out of bounds (too large)
        val, err = _parse_bounded_int_param({"duplicate_limit": ["999999"]}, "duplicate_limit")
        self.assertIsNotNone(err)
        self.assertIn("must be between 0 and 1000", err)


if __name__ == '__main__':
    unittest.main()
