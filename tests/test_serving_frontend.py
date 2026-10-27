"""Serving behavior for the modern-frontend-only UI.

The legacy single-file fallback UI (root index.html, served whenever the built
frontend dist was absent) is gone: app.py serves only the Next static export
in frontend/dist. These tests lock the behavior at both ends:

- with a (fabricated) dist present: ``/``, deep links, ``/assets/<file>`` and
  ``/_next/static/<file>`` serve the SPA entrypoint and its chunks;
- with the build missing or empty: index and the SPA fallback answer 503 with
  a small inline text/html page, an unknown ``/api/*`` path that falls through
  to the SPA fallback answers 503 ``{"error": "frontend_missing"}``, and asset
  prefixes keep their plain 404s with no fallback at all.

Real ``/api/*`` endpoints are registered before the SPA catch-all and are not
affected by a missing frontend build — ``/api/health`` stays a plain 200
liveness answer here.

app.py source-shape assertions for these routes live in
tests/test_security_hardening.py; tests/arch001_route_baseline.json keeps the
route set itself in place.
"""

import mimetypes
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

SPA_ENTRY = (
    '<!DOCTYPE html><html><head><title>SPA-TEST-ENTRYPOINT</title></head>'
    '<body><div id="app"></div></body></html>'
)
ASSET_JS = "console.log('spa-asset');"
NEXT_JS = "console.log('spa-next-chunk');"

MISSING_MESSAGE = "The web frontend build is missing. Rebuild or reinstall the application."


class _AppTestEnv(unittest.TestCase):
    """app imported with WEB_MANAGER_DATA_DIR in a temp dir (once per module)."""

    @classmethod
    def setUpClass(cls):
        cls._import_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        with mock.patch.dict(os.environ, {"WEB_MANAGER_DATA_DIR": cls._import_tmp.name}):
            import app  # noqa: F401

    @classmethod
    def tearDownClass(cls):
        cls._import_tmp.cleanup()


class ServingTests(_AppTestEnv):
    def setUp(self):
        import app as app_module
        self.app_module = app_module
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.client = app_module.app.test_client()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_root = Path(tmp.name)

    def _install_dist(self, *, index_html=SPA_ENTRY, favicon=True):
        """Fabricate a frontend/dist and point REACT_DIST_DIR at it."""
        dist = self.tmp_root / "frontend" / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "assets" / "app.js").write_text(ASSET_JS, encoding="utf-8")
        (dist / "_next" / "static").mkdir(parents=True)
        (dist / "_next" / "static" / "bundle.js").write_text(NEXT_JS, encoding="utf-8")
        if index_html is not None:
            (dist / "index.html").write_text(index_html, encoding="utf-8")
        if favicon:
            (dist / "favicon.ico").write_bytes(b"ico")
        return mock.patch.object(self.app_module, "REACT_DIST_DIR", dist)

    def _serve_no_dist(self):
        """Point REACT_DIST_DIR at a path that does not exist."""
        target = self.tmp_root / "no-such" / "dist"
        return mock.patch.object(self.app_module, "REACT_DIST_DIR", target)

    # ── build present ─────────────────────────────────────────────────────

    def test_index_serves_the_spa_entrypoint(self):
        with self._install_dist():
            resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/html")
        self.assertIn("SPA-TEST-ENTRYPOINT", resp.get_data(as_text=True))
        self.assertIn("no-store", resp.headers.get("Cache-Control", ""))

    def test_deep_link_fallback_serves_the_spa_entrypoint(self):
        with self._install_dist():
            for path in ("/library", "/settings/system", "/library/playlists/mix"):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 200, path)
                self.assertEqual(resp.mimetype, "text/html", path)
                self.assertIn("SPA-TEST-ENTRYPOINT", resp.get_data(as_text=True))

    def test_assets_and_next_chunks_serve_from_the_dist(self):
        with self._install_dist():
            for path, body, name in (
                ("/assets/app.js", ASSET_JS, "app.js"),
                ("/_next/static/bundle.js", NEXT_JS, "bundle.js"),
            ):
                with self.client.get(path) as resp:
                    self.assertEqual(resp.status_code, 200, path)
                    # send_file guesses the type exactly like this, so asserting
                    # the same guess keeps the check correct on every platform.
                    expected = mimetypes.guess_type(name)[0] or "application/octet-stream"
                    self.assertEqual(resp.mimetype, expected, path)
                    self.assertEqual(resp.get_data(as_text=True), body)

    def test_favicon_serves_from_the_dist(self):
        with self._install_dist():
            with self.client.get("/favicon.ico") as resp:
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.get_data(), b"ico")

    def test_favicon_without_one_in_the_dist_is_204(self):
        with self._install_dist(favicon=False):
            resp = self.client.get("/favicon.ico")
        self.assertEqual(resp.status_code, 204)

    def test_unknown_asset_file_is_a_plain_404_with_no_fallback(self):
        with self._install_dist():
            resp = self.client.get("/assets/nope.js")
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("SPA-TEST-ENTRYPOINT", resp.get_data(as_text=True))

    def test_unknown_api_path_stays_an_ordinary_404_with_a_build(self):
        with self._install_dist():
            resp = self.client.get("/api/definitely-not-an-endpoint")
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(resp.is_json)
        self.assertNotEqual((resp.get_json() or {}).get("error"), "frontend_missing")

    # ── build missing / empty ─────────────────────────────────────────────

    def test_missing_build_index_is_503_inline_html(self):
        with self._serve_no_dist():
            resp = self.client.get("/")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.mimetype, "text/html")
        body = resp.get_data(as_text=True)
        self.assertIn(MISSING_MESSAGE, body)
        # No chrome, no external assets: nothing to load, nothing to run.
        self.assertNotIn("<script", body)
        self.assertNotIn("<img", body)
        self.assertNotIn("src=", body)
        self.assertNotIn("href=", body)
        self.assertIn("no-store", resp.headers.get("Cache-Control", ""))

    def test_empty_entrypoint_counts_as_a_missing_build(self):
        with self._install_dist(index_html=""):
            resp = self.client.get("/")
            self.assertEqual(resp.status_code, 503)
            self.assertEqual(resp.mimetype, "text/html")
            self.assertIn(MISSING_MESSAGE, resp.get_data(as_text=True))
            # The dist's own assets still serve: the entrypoint gate only
            # decides whether ANY UI exists to serve.
            with self.client.get("/assets/app.js") as resp:
                self.assertEqual(resp.status_code, 200)

    def test_missing_build_deep_link_is_503_inline_html(self):
        with self._serve_no_dist():
            resp = self.client.get("/library")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.mimetype, "text/html")
        self.assertIn(MISSING_MESSAGE, resp.get_data(as_text=True))

    def test_missing_build_unknown_api_path_is_503_json(self):
        with self._serve_no_dist():
            resp = self.client.get("/api/definitely-not-an-endpoint")
        self.assertEqual(resp.status_code, 503)
        self.assertTrue(resp.is_json)
        self.assertEqual(
            resp.get_json(),
            {"ok": False, "error": "frontend_missing", "message": MISSING_MESSAGE},
        )

    def test_missing_build_asset_prefixes_stay_404(self):
        with self._serve_no_dist():
            for path in ("/assets/nope.js", "/_next/static/nope.js"):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 404, path)

    def test_health_stays_up_when_the_build_is_missing(self):
        with self._serve_no_dist():
            resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.is_json)
        self.assertTrue(resp.get_json().get("ok"))


if __name__ == "__main__":
    unittest.main()
