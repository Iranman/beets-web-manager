"""Regression tests for the SEC-002 non-app-backend CodeQL wave: alerts in
routes_submissions.py, routes_setup.py, routes_lidarr.py, and
backend/transaction_engine.py. See docs/TECHNICAL_DEBT.md SEC-002 for the
full alert inventory and per-alert classification.
"""

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import app as app_module
import routes_lidarr
import routes_setup
import routes_submissions
from backend.transaction_engine import TransactionStore


class TransactionStoreIdContainmentTests(unittest.TestCase):
    """Alerts #129/#130/#131/#132/#133 (py/path-injection, TransactionStore's
    _read/_write os.replace/write_text/read_text sinks): TransactionStore._path()
    requires a 'txn_' prefix and rejects '/', '\\', and NUL before any path is
    built, so no transaction_id can escape TransactionStore.root."""

    def test_adversarial_transaction_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TransactionStore(tmp)
            for bad_id in [
                "../../etc/passwd",
                "/etc/passwd",
                "txn_../../etc/passwd",
                "txn_\\..\\..\\windows",
                "txn_\x00hidden",
                "not_prefixed_txn_1",
                "",
            ]:
                with self.subTest(bad_id=bad_id):
                    with self.assertRaises(KeyError):
                        store._read(bad_id)

    def test_legitimate_generated_ids_stay_inside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TransactionStore(tmp)
            tx = store.create(operation_type="Rename", initiating_user="tester")
            path = store._path(tx["id"])
            self.assertEqual(path.parent.resolve(), Path(tmp).resolve())
            self.assertTrue(path.name.startswith("txn_"))


class SubmissionFolderPathContainmentTests(unittest.TestCase):
    """Alert #128 (py/path-injection, routes_submissions._abs_resolved): the
    helper performed no containment check at all, and its caller chain
    (GET /api/submissions/target?path=...) is reachable with an arbitrary
    query-string path. Fixed by requiring the resolved path fall under
    MUSIC_ROOT or DOWNLOADS_ROOT (reusing the existing _path_is_under
    helper and _SUBMISSION_ALLOWED_ROOTS tuple, both already present but
    previously unused by this function)."""

    def test_outside_root_path_is_rejected(self):
        with self.assertRaises(ValueError):
            routes_submissions._abs_resolved("/etc")

    def test_windows_style_outside_root_path_is_rejected(self):
        with self.assertRaises(ValueError):
            routes_submissions._abs_resolved("C:/Windows/System32")

    def test_path_under_music_root_is_accepted(self):
        with mock.patch.object(routes_submissions, "_SUBMISSION_ALLOWED_ROOTS",
                                (Path(tempfile.gettempdir()),)):
            target = Path(tempfile.gettempdir()) / "some-album"
            resolved = routes_submissions._abs_resolved(str(target))
            self.assertEqual(resolved, target.expanduser().resolve(strict=False))

    def test_submission_target_endpoint_rejects_outside_root_path_with_400(self):
        with app_module.app.test_request_context("/api/submissions/target?path=/etc"):
            response = routes_submissions.submission_target()
        status = response[1] if isinstance(response, tuple) else response.status_code
        self.assertEqual(status, 400)


class SubmissionExceptionSanitizationTests(unittest.TestCase):
    """Alerts #323 (submission_target broad except), #34 (reference-url
    metadata-fetch broad except), #36 (validate_musicbrainz_release broad
    except): each previously returned str(ex)/f"...{ex}" from an
    unbounded except Exception clause. ValueError/KeyError paths (which
    only ever carry deliberately-crafted, safe messages from this
    codebase's own helpers) are preserved; anything else is now a fixed
    safe message, with the real exception logged server-side only."""

    def test_submission_target_unexpected_exception_is_sanitized(self):
        leak = "LEAK_MARKER /internal/path token=abc123"
        with app_module.app.test_request_context("/api/submissions/target?album_id=1"), \
             mock.patch.object(routes_submissions, "_resolve_submission_target", side_effect=RuntimeError(leak)):
            response = routes_submissions.submission_target()
        status = response[1]
        data = response[0].get_json()
        self.assertEqual(status, 400)
        self.assertNotIn("LEAK_MARKER", json.dumps(data))
        self.assertNotIn("token=abc123", json.dumps(data))

    def test_submission_target_deliberate_valueerror_is_preserved(self):
        with app_module.app.test_request_context("/api/submissions/target"), \
             mock.patch.object(routes_submissions, "_resolve_submission_target",
                                side_effect=ValueError("Select a review item with a resolvable path, album, or item first.")):
            response = routes_submissions.submission_target()
        status = response[1]
        data = response[0].get_json()
        self.assertEqual(status, 400)
        self.assertIn("Select a review item", data["error"])

    def test_reference_url_unexpected_metadata_exception_is_sanitized(self):
        leak = "LEAK_MARKER /internal/path token=abc123"
        payload = {"url": "https://musicbrainz.org/release/" + "a" * 8 + "-0000-0000-0000-" + "b" * 12, "album_id": 1}
        with app_module.app.test_request_context(
            "/api/submissions/reference-url", method="POST",
            data=json.dumps(payload), content_type="application/json",
        ), mock.patch.object(routes_submissions, "_validate_reference_url", return_value=payload["url"]), \
           mock.patch.object(routes_submissions, "_extract_release_input", side_effect=RuntimeError(leak)), \
           mock.patch.object(routes_submissions, "_reference_url_source", return_value="musicbrainz"), \
           mock.patch.object(routes_submissions, "_submission_draft", return_value={}), \
           mock.patch.object(routes_submissions, "_save_submission_draft", return_value={}):
            response = routes_submissions.submission_reference_url()
        data = response.get_json() if hasattr(response, "get_json") else response[0].get_json()
        self.assertNotIn("LEAK_MARKER", json.dumps(data))
        self.assertNotIn("token=abc123", json.dumps(data))
        self.assertEqual(data["reference"]["status"], "error")

    def test_validate_musicbrainz_release_unexpected_exception_is_sanitized(self):
        leak = "LEAK_MARKER /internal/path token=abc123"
        payload = {"input": "a" * 8 + "-0000-0000-0000-" + "b" * 12}
        with app_module.app.test_request_context(
            "/api/submissions/musicbrainz-release/validate", method="POST",
            data=json.dumps(payload), content_type="application/json",
        ), mock.patch.object(routes_submissions, "_fetch_mb_release_tracklist", side_effect=RuntimeError(leak)):
            response = routes_submissions.validate_musicbrainz_release()
        status = response[1]
        data = response[0].get_json()
        self.assertEqual(status, 400)
        self.assertNotIn("LEAK_MARKER", json.dumps(data))
        self.assertNotIn("token=abc123", json.dumps(data))


class SetupExceptionSanitizationTests(unittest.TestCase):
    """Alerts #24/#322 (env file write broad except, two call sites),
    #25/#26/#27 (AI/MusicBrainz/Plex connectivity test broad excepts),
    plus the unreported fpcalc and AcoustID-lookup broad excepts in the
    same file: all previously returned f"...: {ex}"; now sanitized with
    the real exception logged server-side only. #23/#321 (ValueError from
    _write_env_file, deliberately-crafted key-name-only messages) are
    unchanged and remain safe to echo directly."""

    def test_write_env_file_unexpected_exception_is_sanitized(self):
        leak = "LEAK_MARKER /internal/path token=abc123"
        with app_module.app.test_request_context(
            "/api/setup/env", method="POST",
            data=json.dumps({"variables": {"AI_MODEL": "gpt-4o"}}), content_type="application/json",
        ), mock.patch.object(routes_setup, "_write_env_file", side_effect=OSError(leak)):
            response = routes_setup.setup_save_env()
        status = response[1]
        data = response[0].get_json()
        self.assertEqual(status, 500)
        self.assertNotIn("LEAK_MARKER", json.dumps(data))
        self.assertNotIn("token=abc123", json.dumps(data))

    def test_ai_connectivity_test_unexpected_exception_is_sanitized(self):
        leak = "LEAK_MARKER /internal/path token=abc123"
        with app_module.app.test_request_context(
            "/api/setup/test/ai", method="POST",
            data=json.dumps({"api_key": "sk-test"}), content_type="application/json",
        ), mock.patch("urllib.request.urlopen", side_effect=OSError(leak)):
            response = routes_setup.setup_test_ai()
        data = response.get_json() if hasattr(response, "get_json") else response[0].get_json()
        self.assertNotIn("LEAK_MARKER", json.dumps(data))
        self.assertNotIn("token=abc123", json.dumps(data))
        self.assertEqual(data["status"], "failed")

    def test_musicbrainz_connectivity_test_unexpected_exception_is_sanitized(self):
        leak = "LEAK_MARKER /internal/path token=abc123"
        with app_module.app.test_request_context("/api/setup/test/musicbrainz", method="POST"), \
             mock.patch("urllib.request.urlopen", side_effect=OSError(leak)):
            response = routes_setup.setup_test_musicbrainz()
        data = response.get_json() if hasattr(response, "get_json") else response[0].get_json()
        self.assertNotIn("LEAK_MARKER", json.dumps(data))

    def test_plex_connectivity_test_does_not_leak_url_or_exception(self):
        leak = "LEAK_MARKER /internal/path token=abc123"
        with app_module.app.test_request_context(
            "/api/setup/test/plex", method="POST",
            data=json.dumps({"url": "http://plex.internal.example:32400", "token": "plex-secret"}),
            content_type="application/json",
        ), mock.patch("urllib.request.urlopen", side_effect=OSError(leak)):
            response = routes_setup.setup_test_plex()
        data = response.get_json() if hasattr(response, "get_json") else response[0].get_json()
        self.assertNotIn("LEAK_MARKER", json.dumps(data))
        self.assertNotIn("plex.internal.example", json.dumps(data))

    def test_acoustid_fpcalc_unexpected_exception_is_sanitized(self):
        leak = "LEAK_MARKER /internal/path token=abc123"
        with app_module.app.test_request_context("/api/setup/test/acoustid", method="POST"), \
             mock.patch.object(routes_setup.shutil, "which", return_value="/usr/bin/fpcalc"), \
             mock.patch.object(routes_setup.subprocess, "run", side_effect=OSError(leak)):
            response = routes_setup.setup_test_acoustid()
        data = response.get_json() if hasattr(response, "get_json") else response[0].get_json()
        self.assertNotIn("LEAK_MARKER", json.dumps(data))

    def test_ai_provider_host_check_uses_exact_hostname_not_substring(self):
        # CodeQL #102 (py/incomplete-url-substring-sanitization): "openai.com"
        # in base_url matched an attacker-chosen host like
        # "https://evil.example/openai.com". Confirm the fix uses the real
        # parsed hostname instead.
        with app_module.app.test_request_context(
            "/api/setup/test/ai", method="POST",
            data=json.dumps({"api_key": "sk-test", "base_url": "https://evil.example.test/openai.com", "model": "gpt-4o"}),
            content_type="application/json",
        ), mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("unreachable")
            routes_setup.setup_test_ai()
        requested_url = mock_urlopen.call_args[0][0].full_url
        self.assertNotIn("/models/gpt-4o", requested_url)
        self.assertTrue(requested_url.endswith("/models"))


class LidarrErrorSanitizationTests(unittest.TestCase):
    """Alerts #20/#21 (py/stack-trace-exposure, routes_lidarr):
    _http_error_message() already redacted every exception except a
    RuntimeError, which only ever carries a fixed, safe configuration
    message from _lidarr_config_error() -- proven false positives.
    Alert #19 (routes_lidarr:134): the true unbounded str(exc) source is
    app.py's _acq_fetch_lidarr_wanted() broad except (out of scope for
    this wave); fixed at this file's boundary in wanted_lidarr() instead,
    which now only echoes the known-safe "not configured" message."""

    def test_http_error_message_never_echoes_an_arbitrary_exception(self):
        safe_runtime_messages = {"LIDARR_API_KEY not configured", "LIDARR_URL not configured"}
        message, status = routes_lidarr._http_error_message(RuntimeError("LIDARR_API_KEY not configured"))
        self.assertIn(message, safe_runtime_messages)
        message, status = routes_lidarr._http_error_message(OSError("LEAK_MARKER /internal/path"))
        self.assertNotIn("LEAK_MARKER", message)
        message, status = routes_lidarr._http_error_message(urllib.error.HTTPError("url", 500, "msg", {}, None))
        self.assertEqual(message, "Lidarr returned HTTP 500")

    def test_wanted_lidarr_sanitizes_non_config_errors(self):
        # Configuration is checked via this file's own type-safe
        # _lidarr_config_error(), not by pattern-matching
        # _acq_fetch_lidarr_wanted()'s error text (which could, in a
        # coincidental case, contain "not configured" itself despite
        # coming from its own unrelated broad except in app.py).
        leak = "LEAK_MARKER /internal/path token=abc123"
        with app_module.app.test_request_context("/api/wanted/lidarr"), \
             mock.patch.object(routes_lidarr, "_lidarr_config_error", return_value=""), \
             mock.patch.object(routes_lidarr, "_acq_fetch_lidarr_wanted", return_value=([], leak)):
            response = routes_lidarr.wanted_lidarr()
        data = response[0].get_json()
        self.assertEqual(response[1], 502)
        self.assertNotIn("LEAK_MARKER", json.dumps(data))
        self.assertNotIn("token=abc123", json.dumps(data))

    def test_wanted_lidarr_preserves_not_configured_message(self):
        with app_module.app.test_request_context("/api/wanted/lidarr"), \
             mock.patch.object(routes_lidarr, "_lidarr_config_error", return_value="LIDARR_API_KEY not configured"):
            response = routes_lidarr.wanted_lidarr()
        data = response[0].get_json()
        self.assertEqual(response[1], 503)
        self.assertIn("not configured", data["error"])

    def test_wanted_lidarr_never_echoes_error_text_even_if_it_says_not_configured(self):
        # Regression for the substring-matching gap CodeQL alert #407
        # flagged: an _acq_fetch_lidarr_wanted() failure that happens to
        # mention "not configured" for an unrelated reason must still be
        # sanitized, since only _lidarr_config_error() -- not this
        # string's content -- is trusted to mean "safe to echo".
        leak = "Name or service not configured LEAK_MARKER"
        with app_module.app.test_request_context("/api/wanted/lidarr"), \
             mock.patch.object(routes_lidarr, "_lidarr_config_error", return_value=""), \
             mock.patch.object(routes_lidarr, "_acq_fetch_lidarr_wanted", return_value=([], leak)):
            response = routes_lidarr.wanted_lidarr()
        data = response[0].get_json()
        self.assertEqual(response[1], 502)
        self.assertNotIn("LEAK_MARKER", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
