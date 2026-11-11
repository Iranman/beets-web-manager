"""Music-format preferences revision guard (#623 item 2 / #617 item 4).

A save built from an older load must not overwrite newer stored preferences.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class MusicFormatRevisionRouteTests(unittest.TestCase):
    def setUp(self):
        import app as app_module  # noqa: F401  (registers the routes)
        import routes_system
        self.app, self.system = app_module.app, routes_system
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "prefs.json"
        env = mock.patch.dict(os.environ, {"MUSIC_FORMAT_PREFS_FILE": str(self.path)})
        env.start()
        self.addCleanup(env.stop)

    def _get(self):
        with self.app.test_request_context("/api/settings/music-format"):
            return self.system.get_music_format_preferences().get_json()

    def _post(self, body):
        with self.app.test_request_context("/api/settings/music-format", method="POST", json=body):
            rv = self.system.save_music_format_preferences_route()
        resp, status = rv if isinstance(rv, tuple) else (rv, rv.status_code)
        return status, resp.get_json()

    def _stored(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_get_returns_revision_and_current_revision_saves(self):
        rev = self._get()["revision"]
        self.assertTrue(rev)
        status, body = self._post({"preferences": {"preferred_formats": ["mp3"]}, "expected_revision": rev})
        self.assertEqual(status, 200)
        self.assertEqual(self._stored()["preferred_formats"], ["mp3"])
        self.assertNotEqual(body["revision"], rev)
        self.assertEqual(body["revision"], self._get()["revision"])

    def test_stale_revision_gets_409_and_stored_prefs_unchanged(self):
        stale = self._get()["revision"]
        status, body = self._post({"preferences": {"preferred_formats": ["aac"]}, "expected_revision": stale})
        self.assertEqual(status, 200)
        stored_before = self._stored()
        status, body = self._post({"preferences": {"preferred_formats": ["opus"]}, "expected_revision": stale})
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "music_format_revision_conflict")
        self.assertEqual(self._stored(), stored_before)

    def test_missing_revision_is_refused_with_428_and_nothing_written(self):
        # Documented behaviour: like /api/setup/settings and /api/config, a save
        # without expected_revision (old client) is refused, never last-write-wins.
        status, body = self._post({"preferences": {"preferred_formats": ["opus"]}})
        self.assertEqual(status, 428)
        self.assertEqual(body["code"], "music_format_missing_revision")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
