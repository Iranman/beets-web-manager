"""Tests verifying authentication token file path defaults and atomic persistence."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")


class AuthTokenPersistenceTests(unittest.TestCase):
    def test_auth_token_file_default_is_web_manager_data(self):
        """Default BEETS_WEB_AUTH_TOKEN_FILE must be /web-manager-data/.auth_token."""
        self.assertIn('/web-manager-data/.auth_token', APP_SOURCE)
        self.assertNotIn('/config/.auth_token', APP_SOURCE)

    def test_persist_generated_auth_token_writes_atomically_with_permissions(self):
        import app as app_module

        with tempfile.TemporaryDirectory() as tmpdir:
            token_file = Path(tmpdir) / "subfolder" / ".auth_token"
            with mock.patch.object(app_module, "_GENERATED_AUTH_TOKEN_FILE", token_file):
                test_token = "a_test_token_with_32_characters_minimum_entropy_12345"
                app_module._persist_generated_auth_token(test_token)

                self.assertTrue(token_file.exists())
                self.assertEqual(token_file.read_text(encoding="utf-8").strip(), test_token)

    def test_bootstrap_token_reuses_existing_file_on_restart(self):
        import app as app_module

        with tempfile.TemporaryDirectory() as tmpdir:
            token_file = Path(tmpdir) / ".auth_token"
            existing_token = "existing_token_value_32_chars_long_entropy_test_string"
            token_file.write_text(existing_token, encoding="utf-8")

            with mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_TOKEN": "", "BEETS_WEB_PASSWORD": ""}, clear=False), \
                 mock.patch.object(app_module, "_GENERATED_AUTH_TOKEN_FILE", token_file):
                app_module._bootstrap_auth_token_if_missing()
                self.assertEqual(os.environ.get("BEETS_WEB_AUTH_TOKEN"), existing_token)


if __name__ == "__main__":
    unittest.main()
