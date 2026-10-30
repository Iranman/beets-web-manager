"""Contract checks for the M2b consolidation: shared Beets error responses and
the metadata-driven settings item (redaction above all)."""

import os
import unittest
from unittest import mock

from flask import Flask

import app as _app  # noqa: F401  (route modules load through app)
import routes_setup
from backend.beets_adapter import BeetsAdapterAuthError, BeetsAdapterError, BeetsAdapterTimeoutError
from backend.serializers import beets_auth_failed_response, beets_engine_error_response, beets_offline_response


class BeetsErrorResponses(unittest.TestCase):
    def _call(self, fn, *args):
        with Flask(__name__).app_context():
            resp, status = fn(*args)
            return resp.get_json(), status

    def test_engine_error_mapping_is_fixed_text(self):
        for ex, status, code in (
            (BeetsAdapterAuthError("secret detail"), 502, "ENGINE_AUTH_ERROR"),
            (BeetsAdapterTimeoutError("secret detail"), 503, "ENGINE_UNAVAILABLE"),
            (BeetsAdapterError("secret detail"), 502, "ENGINE_ERROR"),
        ):
            body, got = self._call(beets_engine_error_response, ex)
            self.assertEqual((got, body["error_code"], body["ok"]), (status, code, False))
            self.assertNotIn("secret", body["error"])

    def test_offline_and_auth(self):
        self.assertEqual(self._call(beets_offline_response), (
            {"ok": False, "error": "Beets library unavailable", "error_code": "ENGINE_OFFLINE"}, 503))
        self.assertEqual(self._call(beets_auth_failed_response), (
            {"ok": False, "error": "Beets engine authentication failed.", "error_code": "ENGINE_AUTH_FAILED"}, 503))


class SettingItem(unittest.TestCase):
    KEYS = {
        "name", "section", "secret", "revealable", "configured", "editable", "restart_required", "type", "value",
        "effective_value", "saved_value", "has_saved_value", "is_overridden", "default", "container_path",
        "host_path", "description", "source", "status_message", "has_value", "runtime_has_value", "runtime_value",
    }

    def _item(self, name, env=None, persisted=None, meta=None):
        with mock.patch.dict(os.environ, {name: env} if env else {}):
            if not env:
                os.environ.pop(name, None)
            return routes_setup._resolve_setting_item(name, meta or {}, persisted or {})

    def test_every_branch_has_every_field(self):
        for name in ("BEETS_WEB_PASSWORD", "BEETS_WEB_AUTH_TOKEN", "BEETS_WEB_USERNAME", "PUID", "PGID", "TZ",
                     "AI_BASE_URL", "AI_MODEL", "OPENAI_API_KEY", "SOME_PLAIN_SETTING"):
            self.assertEqual(set(self._item(name)), self.KEYS, name)

    def test_secrets_never_returned_in_clear(self):
        raw = "sup3r-s3cret-value-1234567890"
        for name in ("BEETS_WEB_PASSWORD", "BEETS_WEB_AUTH_TOKEN", "OPENAI_API_KEY"):
            for item in (self._item(name, env=raw), self._item(name, persisted={name: raw})):
                self.assertNotIn(raw, repr(item), name)
                self.assertTrue(item["secret"])
                self.assertIsNone(item["effective_value"])

    def test_override_message_keeps_its_noun(self):
        item = self._item("TZ", env="Europe/Paris", persisted={"TZ": "UTC"})
        self.assertEqual(item["status_message"], "Docker environment overrides saved timezone (UTC)")
        self.assertEqual(self._item("TZ", persisted={"TZ": "UTC"})["status_message"], "Saved — restart required")

    def test_ai_defaults_follow_the_configured_provider(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "x"}):
            os.environ.pop("OPENAI_API_KEY", None)
            item = self._item("AI_BASE_URL")
        self.assertEqual(item["value"], "https://openrouter.ai/api/v1")
        self.assertEqual(item["default"], "https://api.openai.com/v1")


if __name__ == "__main__":
    unittest.main()
