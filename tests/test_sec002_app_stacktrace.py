"""Regression tests for the SEC-002 app.py stack-trace-exposure wave: all 62
open py/stack-trace-exposure alerts in app.py. See docs/TECHNICAL_DEBT.md
SEC-002 for the full alert inventory and per-alert classification.
"""

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import app as app_module


class ClassifyOpenAiErrorSharedHelperTests(unittest.TestCase):
    """Alerts #324/#39 and 2 unreported call sites (14395/17337, already
    routed through this helper): the fallback branch of
    _classify_openai_error() used to embed raw exception text
    (f"AI provider error ({exc})"); it now returns only the exception's
    type name. HTTPError/TimeoutError/URLError branches were already
    safe and are unchanged."""

    def test_unclassified_exception_type_name_only(self):
        result = app_module._classify_openai_error(ValueError("token=secret-abc123 at /internal/path"))
        self.assertIn("ValueError", result)
        self.assertNotIn("secret-abc123", result)
        self.assertNotIn("/internal/path", result)

    def test_http_error_still_classified_normally(self):
        exc = urllib.error.HTTPError("https://api.openai.com/v1/chat/completions", 401, "Unauthorized", {}, None)
        self.assertIn("rejected the API key", app_module._classify_openai_error(exc))


class FolderCleanupPathContainmentTests(unittest.TestCase):
    """7 alerts (#72/#73/#74/#75/#76/#78/#80, all py/stack-trace-exposure):
    _folder_cleanup_path()'s except-branch embedded the raw resolve()
    exception (f"Invalid path: {exc}") into its returned error message,
    which flows into many /api/clean/* route responses. Now returns a
    fixed message; the real exception is logged server-side only."""

    def test_invalid_path_error_is_fixed_message(self):
        with mock.patch.object(Path, "resolve", side_effect=OSError("LEAK_MARKER /internal detail")):
            path, error = app_module._folder_cleanup_path("some/relative/path")
        self.assertIsNone(path)
        self.assertEqual(error, "Invalid path.")
        self.assertNotIn("LEAK_MARKER", error)

    def test_valid_path_under_music_root_still_resolves(self):
        with mock.patch.object(app_module, "MUSIC_ROOT", Path(tempfile.gettempdir())):
            target = Path(tempfile.gettempdir()) / "some-album"
            path, error = app_module._folder_cleanup_path(str(target))
        self.assertIsNone(error)
        self.assertEqual(path, target.resolve(strict=False))

    def test_outside_root_path_still_rejected_with_safe_message(self):
        with mock.patch.object(app_module, "MUSIC_ROOT", Path(tempfile.gettempdir()) / "music-only"):
            path, error = app_module._folder_cleanup_path("/etc")
        self.assertIsNone(path)
        self.assertEqual(error, "Path is outside the configured music library")


class FolderCleanRootFalsePositiveTests(unittest.TestCase):
    """Alerts #61/#62 (py/stack-trace-exposure): proven false positive.
    _folder_clean_root() raises RuntimeError only with fixed, deliberate
    messages (never wraps another exception); the previous "Path not
    found: {root}" variant embedded a filesystem path and has been
    tightened to a fixed string too."""

    def test_missing_path_raises_fixed_message(self):
        with self.assertRaises(RuntimeError) as ctx:
            app_module._folder_clean_root("/definitely/not/a/real/path/xyz")
        self.assertEqual(str(ctx.exception), "Path not found.")

    def test_outside_allowed_roots_raises_fixed_message(self):
        # FOLDER_CLEAN_ROOTS includes /tmp by design, so a bare
        # tempfile.TemporaryDirectory() is not reliably outside it (and
        # on Windows /tmp itself isn't meaningful) -- pin the allowed
        # roots to a disjoint sibling directory instead, so this test is
        # deterministic on every platform/CI environment.
        with tempfile.TemporaryDirectory() as tmp:
            allowed_root = Path(tmp) / "allowed"
            outside_dir = Path(tmp) / "outside"
            allowed_root.mkdir()
            outside_dir.mkdir()
            with mock.patch.object(app_module, "FOLDER_CLEAN_ROOTS", [allowed_root]):
                with self.assertRaises(RuntimeError) as ctx:
                    app_module._folder_clean_root(str(outside_dir))
        self.assertIn("must be under", str(ctx.exception))


class SpotifyFetchErrorSafeMessageTests(unittest.TestCase):
    """Alerts #85/#86 (py/stack-trace-exposure): _SpotifyFetchError's two
    raise sites used to wrap the underlying urllib/json exception
    (f"Spotify auth failed: {ex}"), which the strict policy treats as
    unsafe (never wrap another exception). Both now raise a fixed
    message; the real exception is logged server-side and chained via
    `from ex` for server-side traceback context only."""

    def test_auth_failure_message_is_fixed(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused to 10.0.0.5")):
            with self.assertRaises(app_module._SpotifyFetchError) as ctx:
                app_module._fetch_spotify_playlist_tracks("playlist123", "client-id", "client-secret")
        self.assertEqual(str(ctx.exception), "Spotify authentication failed.")
        self.assertNotIn("10.0.0.5", str(ctx.exception))


class ConfigYamlExceptionSanitizationTests(unittest.TestCase):
    """Alerts #96/#97/#98 (py/stack-trace-exposure): /api/config GET/POST
    and /api/config/revert returned raw filesystem exception text for
    the beets config.yaml file -- the most sensitive file in the
    container, since it can hold provider credentials. All three now
    proxy through beets_client to the control agent (BUG-1 -- config.yaml
    lives on the engine's own /config mount, not the web manager's, in the
    real two-service topology) and still return a fixed message, never the
    engine's raw detail text, to the browser."""

    def test_get_config_read_failure_is_sanitized(self):
        with app_module.app.test_request_context("/api/config"), \
             mock.patch.object(app_module.beets_client, "get_config",
                                side_effect=app_module.BeetsError("LEAK_MARKER /data/config.yaml", error_code="config_read_failed", status_code=500)):
            response = app_module.get_config()
        status = response[1]
        data = response[0].get_json()
        self.assertEqual(status, 502)
        self.assertNotIn("LEAK_MARKER", json.dumps(data))

    def test_save_config_write_failure_is_sanitized(self):
        with app_module.app.test_request_context(
            "/api/config", method="POST",
            data=json.dumps({"content": "beets:\n  library: /config/musiclibrary.blb\n"}),
            content_type="application/json",
        ), mock.patch.object(app_module.beets_client, "save_config",
                              side_effect=app_module.BeetsError("LEAK_MARKER /data/config.yaml", error_code="config_write_failed", status_code=500)):
            response = app_module.save_config()
        status = response[1]
        data = response[0].get_json()
        self.assertEqual(status, 502)
        self.assertNotIn("LEAK_MARKER", json.dumps(data))

    def test_revert_config_failure_is_sanitized(self):
        with app_module.app.test_request_context("/api/config/revert", method="POST"), \
             mock.patch.object(app_module.beets_client, "revert_config",
                                side_effect=app_module.BeetsError("LEAK_MARKER /data/config.yaml.bak", error_code="config_revert_failed", status_code=500)):
            response = app_module.revert_config()
        status = response[1]
        data = response[0].get_json()
        self.assertEqual(status, 502)
        self.assertNotIn("LEAK_MARKER", json.dumps(data))


class PlexStatusExceptionSanitizationTests(unittest.TestCase):
    """Alert #84 (py/stack-trace-exposure): _plex_status_payload()'s
    broad except embedded raw exception text (which for a Plex
    connectivity failure could include the configured Plex URL) into
    the public status payload's "error" field. Now uses the same
    HTTPError/generic classification pattern as the setup Plex test."""

    def test_unexpected_exception_is_sanitized(self):
        # The payload legitimately includes the *configured* Plex URL as
        # its own "url" status field (not a leak); only the exception
        # text itself -- which could carry different/unexpected detail --
        # must never reach the "error" field.
        with mock.patch.object(app_module, "_plex_settings", return_value={"url": "http://plex.internal.example:32400", "token": "secret"}), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("LEAK_MARKER token=secret-xyz")):
            payload = app_module._plex_status_payload(force=True)
        self.assertNotIn("LEAK_MARKER", payload.get("error", ""))
        self.assertNotIn("secret-xyz", payload.get("error", ""))


class StartMetadataApplyTransactionFalsePositiveTests(unittest.TestCase):
    """Alerts #38 and #100 (py/stack-trace-exposure, two separate call
    sites of the same function): proven false positive.
    _start_metadata_apply_transaction() raises ValueError only with
    fixed, deliberate messages."""

    def test_all_valueerror_messages_are_fixed_strings(self):
        with mock.patch.object(app_module.transactions, "get", return_value={"operation_type": "Rename", "status": "Approved"}):
            with self.assertRaises(ValueError) as ctx:
                app_module._start_metadata_apply_transaction("txn_test")
        self.assertEqual(str(ctx.exception), "Only metadata update transactions can be applied by this endpoint.")

    def test_not_approved_message_is_fixed_string(self):
        with mock.patch.object(app_module.transactions, "get", return_value={"operation_type": "Metadata Update", "status": "Pending"}):
            with self.assertRaises(ValueError) as ctx:
                app_module._start_metadata_apply_transaction("txn_test")
        self.assertEqual(str(ctx.exception), "Approve the transaction before applying it.")


class PlaylistDeleteExceptionSanitizationTests(unittest.TestCase):
    """Alerts #90/#91 (py/stack-trace-exposure): playlist file deletion
    and the subsequent best-effort Plex playlist delete both echoed raw
    exception text. Now sanitized, matching the same HTTPError
    classification pattern used elsewhere for Plex."""

    def test_engine_m3u_delete_failure_is_sanitized(self):
        with app_module.app.test_request_context("/api/playlists/Test%20Playlist", method="DELETE"), \
             mock.patch.object(app_module, "_playlist_ensure_state_dirs"), \
             mock.patch.object(app_module, "_playlist_resolve_stable_id", return_value="pl_11111111111111111111111111111111"), \
             mock.patch.object(app_module, "_playlist_key", return_value="pl_test_playlist"), \
             mock.patch.object(app_module.beets_client, "delete_playlist_m3u", side_effect=OSError("LEAK_MARKER /data/playlists")):
            response = app_module.playlist_delete("Test Playlist")
        data = response.get_json() if hasattr(response, "get_json") else response[0].get_json()
        status_code = response.status_code if hasattr(response, "status_code") else response[1]
        self.assertEqual(status_code, 503)
        self.assertEqual(data.get("error_code"), "engine_unavailable")
        self.assertNotIn("LEAK_MARKER", json.dumps(data))

    def test_plex_delete_failure_is_sanitized(self):
        with app_module.app.test_request_context(
            "/api/playlists/Test%20Playlist", method="DELETE",
            data=json.dumps({"delete_plex": True}), content_type="application/json",
        ), mock.patch.object(app_module, "_playlist_ensure_state_dirs"), \
           mock.patch.object(app_module, "_playlist_resolve_stable_id", return_value="pl_11111111111111111111111111111111"), \
           mock.patch.object(app_module, "_playlist_key", return_value="pl_test_playlist"), \
           mock.patch.object(app_module.beets_client, "delete_playlist_m3u", return_value={"ok": True, "deleted": True}), \
           mock.patch.object(app_module, "_plex_settings", return_value={"token": "secret"}), \
           mock.patch.object(app_module, "_plex_delete_playlist_by_title", side_effect=OSError("LEAK_MARKER plex.internal.example")):
            response = app_module.playlist_delete("Test Playlist")
        data = response.get_json() if hasattr(response, "get_json") else response[0].get_json()
        self.assertNotIn("LEAK_MARKER", json.dumps(data))
        self.assertIn("plex_error", data)


if __name__ == "__main__":
    unittest.main()
