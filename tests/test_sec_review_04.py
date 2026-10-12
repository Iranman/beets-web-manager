"""Security review of the 0.4.0 integration branch: regressions for the
CodeQL findings it fixed and the M3U line-injection hardening."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import composite_workflows
from backend.transaction_engine import SettingsError, TransactionStore, validated_settings


class TransactionSettingsValidation(unittest.TestCase):
    def test_refusal_text_is_fixed_and_nothing_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TransactionStore(Path(tmp))
            before = store.settings()
            for bad, needle in (({"dry_run_by_default": "false"}, "true or false"),
                                ({"backup_retention_days": float("inf")}, "whole number"),
                                ({"backup_retention_days": float("nan")}, "whole number"),
                                ({"automatic_approval_threshold": 2}, "from 0.9 to 1.0")):
                with self.subTest(bad):
                    _values, problem = validated_settings(bad)
                    self.assertIn(needle, problem)
                    with self.assertRaises(SettingsError):
                        store.save_settings(bad)
                    self.assertEqual(store.settings(), before)
            self.assertEqual(validated_settings({"backup_retention_days": 30.0, "x": 1}),
                             ({"backup_retention_days": 30}, ""))


class RouteErrorTexts(unittest.TestCase):
    """CodeQL py/stack-trace-exposure on PR #349: both routes now answer with
    validator/constant text, never str() of a caught exception."""

    def setUp(self):
        import app as app_module  # noqa: F401  (registers the routes)
        import routes_maintenance
        import routes_system
        self.app, self.maint, self.system = app_module.app, routes_maintenance, routes_system

    def test_transaction_settings_refusal(self):
        with self.app.test_request_context("/api/transactions/settings", method="POST",
                                           json={"backup_retention_days": 0}), \
                mock.patch.object(self.maint.transactions, "save_settings") as save:
            body, status = self.maint.api_transaction_settings_save()
        self.assertEqual(status, 400)
        self.assertEqual(body.get_json()["code"], "invalid_setting")
        self.assertIn("backup_retention_days must be a whole number", body.get_json()["error"])
        save.assert_not_called()

    def test_config_redaction_failure(self):
        from backend.config_service import CONFIG_REDACTION_FAILED_MESSAGE, ConfigRedactionError
        with self.app.test_request_context("/api/config"), \
                mock.patch.object(self.system.composite_workflows, "get_config", return_value={"content": "x: 1\n"}), \
                mock.patch.object(self.system, "_redact_config_content", side_effect=ConfigRedactionError()):
            body, status = self.system.get_config()
        self.assertEqual(status, 409)
        self.assertEqual(body.get_json(), {"ok": False, "error": CONFIG_REDACTION_FAILED_MESSAGE,
                                           "code": "config_redaction_failed"})


class PlaylistM3U(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"WEB_MANAGER_DATA_DIR": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_key_cannot_leave_the_playlist_folder(self):
        base = Path(self._tmp.name) / "playlists"
        for key in ("../../etc/passwd", "..", "/abs/x", "a/../../b"):
            p = composite_workflows._get_playlist_m3u_path(key)
            self.assertEqual(p.parent, Path(os.path.normpath(str(base))), key)

    def test_tag_newlines_cannot_add_entries(self):
        items = [{"path": "/music/A/01.flac", "artist": "A", "title": "x\n/music/Other/evil.flac\r\n#EXTINF:1,y"}]
        composite_workflows.export_playlist_m3u("k1", "k1", items)
        parsed = composite_workflows.read_playlist_m3u("k1")
        self.assertEqual([it["path"] for it in parsed["items"]], ["/music/A/01.flac"])


if __name__ == "__main__":
    unittest.main()
