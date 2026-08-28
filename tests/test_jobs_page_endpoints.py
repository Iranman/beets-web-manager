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

    def test_library_health_engine_offline_handling(self):
        with patch('app._library_health_payload', side_effect=BeetsUnavailableError("Control agent down")):
            res = self.client.get('/api/clean/library-health', headers={'X-Beets-CSRF': '1'})
            self.assertEqual(res.status_code, 503)
            data = res.get_json()
            self.assertFalse(data.get('ok'))
            self.assertEqual(data.get('error_code'), 'ENGINE_OFFLINE')
            self.assertEqual(data.get('error'), 'Beets engine is unavailable.')


if __name__ == '__main__':
    unittest.main()
