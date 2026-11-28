"""#463 NB2: the Beets-config save/revert routes surface only the fixed,
curated validation reasons (shape/size), while raw YAML-parser excerpts --
which can quote secret config values -- stay in the server log and never
reach the client.

Fast and local: no beets subprocess, no chroma, no network.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.config_manager import (  # noqa: E402
    SAFE_CONFIG_REASONS,
    ConfigValidationError,
    save_config,
    validate_config_yaml,
)
import app as app_module  # noqa: E402

SECRET = "SUPERSECRET_TOKEN_abc123"

# (label, content, curated reason) for the four fixed shape/size problems.
CURATED_CASES = [
    ("alias", "match:\n  a: &x 1\n  b: *x\n",
     "YAML aliases (*name) are not supported; write the value out in full"),
    ("nontext_key", "? [1, 2]\n: value\n", "Config keys must be text"),
    ("too_large", "x: " + "a" * (1024 * 1024 + 10) + "\n", "Config is larger than 1 MiB"),
    ("too_deep", "[" * 6000 + "]" * 6000 + "\n", "Invalid YAML: nested too deeply"),
]
# A syntax error whose raw parser message quotes a line that holds a secret.
SECRET_SYNTAX = 'api_key: "' + SECRET + "\n"


class ValidationClassificationTests(unittest.TestCase):
    """save_config raises ConfigValidationError whose .safe_reason is the
    curated reason for fixed problems and None for raw parser detail."""

    def _raise(self, content):
        try:
            save_config(content, config_path=Path("/nonexistent/config.yaml"))
        except ConfigValidationError as exc:
            return exc
        self.fail("expected ConfigValidationError")

    def test_curated_reasons_are_surfaced_and_secret_free(self):
        for label, content, reason in CURATED_CASES:
            with self.subTest(label):
                exc = self._raise(content)
                self.assertEqual(exc.safe_reason, reason)
                self.assertIn(reason, SAFE_CONFIG_REASONS)

    def test_raw_parser_detail_is_not_surfaced_but_kept_for_the_log(self):
        exc = self._raise(SECRET_SYNTAX)
        self.assertIsNone(exc.safe_reason)   # nothing curated reaches the client
        self.assertIn(SECRET, str(exc))      # full detail retained server-side

    def test_validate_logs_raw_detail_server_side_only(self):
        with self.assertLogs("beets.config_manager", "WARNING") as logs:
            ok, msg = validate_config_yaml(SECRET_SYNTAX)
        self.assertFalse(ok)
        self.assertNotIn(msg, SAFE_CONFIG_REASONS)
        self.assertIn(SECRET, "\n".join(logs.output))

    def test_safe_reason_only_ever_holds_an_allowlisted_string(self):
        # Even a caller that passes raw text as safe_reason cannot leak it.
        exc = ConfigValidationError("whatever", safe_reason=f"leak {SECRET}")
        self.assertIsNone(exc.safe_reason)


class RouteSurfacesCuratedReasonTests(unittest.TestCase):
    """The /api/config and /api/config/revert handlers return the curated
    reason when present, and the generic message (no secret) otherwise."""

    def _post_save(self, exc):
        with app_module.app.test_request_context(
            "/api/config", method="POST",
            data=json.dumps({"content": "x: 1\n", "expected_revision": "rev1"}),
            content_type="application/json",
        ), mock.patch.object(app_module.composite_workflows, "save_config", side_effect=exc):
            resp = app_module.save_config()
        return resp[1], resp[0].get_json()

    def _post_revert(self, exc):
        with app_module.app.test_request_context(
            "/api/config/revert", method="POST",
            data=json.dumps({"expected_revision": "rev1"}),
            content_type="application/json",
        ), mock.patch.object(app_module.composite_workflows, "revert_config", side_effect=exc):
            resp = app_module.revert_config()
        return resp[1], resp[0].get_json()

    def test_save_returns_each_curated_reason(self):
        for label, _content, reason in CURATED_CASES:
            with self.subTest(label):
                status, data = self._post_save(ConfigValidationError(reason))
                self.assertEqual(status, 400)
                self.assertEqual(data["error"], reason)
                self.assertEqual(data["code"], "config_invalid_yaml")

    def test_save_suppresses_raw_secret_excerpt(self):
        exc = ConfigValidationError(f"Invalid YAML syntax: ...{SECRET}...")
        status, data = self._post_save(exc)
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "Invalid Beets configuration YAML.")
        self.assertNotIn(SECRET, json.dumps(data))

    def test_revert_returns_curated_reason(self):
        status, data = self._post_revert(ConfigValidationError("Config keys must be text"))
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "Config keys must be text")

    def test_revert_suppresses_raw_secret_excerpt(self):
        # Mirrors the backup-invalid raise site, where the composed message
        # holds raw detail but safe_reason is the (here unsafe) base reason.
        exc = ConfigValidationError(
            f"Backup configuration is invalid: Invalid YAML syntax: {SECRET}",
            safe_reason=f"Invalid YAML syntax: {SECRET}",
        )
        status, data = self._post_revert(exc)
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "Invalid Beets configuration YAML.")
        self.assertNotIn(SECRET, json.dumps(data))


if __name__ == "__main__":
    unittest.main()
