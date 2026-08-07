"""Tests verifying authentication token file path defaults and atomic persistence."""
import contextlib
import io
import logging
import os
import stat
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

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not enforced on Windows")
    def test_persist_generated_auth_token_sets_0600_permissions(self):
        import app as app_module

        with tempfile.TemporaryDirectory() as tmpdir:
            token_file = Path(tmpdir) / ".auth_token"
            with mock.patch.object(app_module, "_GENERATED_AUTH_TOKEN_FILE", token_file):
                app_module._persist_generated_auth_token("a_test_token_with_32_characters_minimum_entropy")
                mode = stat.S_IMODE(token_file.stat().st_mode)
                self.assertEqual(mode, 0o600)

    def test_persist_generated_auth_token_cleans_up_temp_file_on_failure(self):
        """A failed fsync must not leave an orphaned or partially written temp file."""
        import app as app_module

        with tempfile.TemporaryDirectory() as tmpdir:
            token_file = Path(tmpdir) / ".auth_token"
            with mock.patch.object(app_module, "_GENERATED_AUTH_TOKEN_FILE", token_file), \
                 mock.patch("os.fsync", side_effect=OSError("disk full")):
                app_module._persist_generated_auth_token("a_test_token_with_32_characters_minimum_entropy")

            self.assertFalse(token_file.exists())
            leftovers = list(Path(tmpdir).glob(".auth_token.tmp.*"))
            self.assertEqual(leftovers, [], f"orphaned temp token file(s) left behind: {leftovers}")

    def test_persist_generated_auth_token_does_not_corrupt_existing_token_on_failure(self):
        """An interrupted persist attempt must not touch a previously persisted token."""
        import app as app_module

        with tempfile.TemporaryDirectory() as tmpdir:
            token_file = Path(tmpdir) / ".auth_token"
            original_token = "original_token_value_32_chars_long_entropy_test_string"
            token_file.write_text(original_token, encoding="utf-8")

            with mock.patch.object(app_module, "_GENERATED_AUTH_TOKEN_FILE", token_file), \
                 mock.patch("os.fsync", side_effect=OSError("disk full")):
                app_module._persist_generated_auth_token("a_new_token_that_should_never_land_on_disk")

            self.assertEqual(token_file.read_text(encoding="utf-8"), original_token)

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


_MARKER_TOKEN = "MARKER-TOKEN-do-not-leak-1234567890123456789012345"


class _CapturingLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def _capture_everything():
    """Captures stdout, stderr, and every log record emitted through the
    root logger (app.logger propagates to it) during the block."""
    out, err = io.StringIO(), io.StringIO()
    handler = _CapturingLogHandler()
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield out, err, handler.messages
    finally:
        root_logger.removeHandler(handler)


class BootstrapTokenNeverLeaksTests(unittest.TestCase):
    """Covers the no-leak / fail-closed contract for _bootstrap_auth_token_if_missing():
    the generated token must never appear in stdout, stderr, or logs, under
    either a successful or a failed persistence attempt."""

    def setUp(self):
        self.env_patcher = mock.patch.dict(
            os.environ, {"BEETS_WEB_AUTH_TOKEN": "", "BEETS_WEB_PASSWORD": ""}, clear=False,
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

    def test_generated_token_absent_from_stdout_stderr_and_logs_on_success(self):
        import app as app_module

        with tempfile.TemporaryDirectory() as tmpdir:
            token_file = Path(tmpdir) / ".auth_token"
            with mock.patch.object(app_module, "_GENERATED_AUTH_TOKEN_FILE", token_file), \
                 mock.patch.object(app_module, "generate_secure_auth_token", return_value=_MARKER_TOKEN), \
                 _capture_everything() as (out, err, log_messages):
                app_module._bootstrap_auth_token_if_missing()

            self.assertEqual(os.environ.get("BEETS_WEB_AUTH_TOKEN"), _MARKER_TOKEN)
            self.assertNotIn(_MARKER_TOKEN, out.getvalue())
            self.assertNotIn(_MARKER_TOKEN, err.getvalue())
            self.assertTrue(all(_MARKER_TOKEN not in m for m in log_messages))
            # The persisted file legitimately contains it -- that's the
            # intended retrieval mechanism, not a leak.
            self.assertEqual(token_file.read_text(encoding="utf-8"), _MARKER_TOKEN)

    def test_generated_token_absent_everywhere_when_persistence_fails_and_startup_fails_closed(self):
        import app as app_module

        with tempfile.TemporaryDirectory() as tmpdir:
            token_file = Path(tmpdir) / ".auth_token"
            with mock.patch.object(app_module, "_GENERATED_AUTH_TOKEN_FILE", token_file), \
                 mock.patch.object(app_module, "generate_secure_auth_token", return_value=_MARKER_TOKEN), \
                 mock.patch("os.fsync", side_effect=OSError("disk full")), \
                 _capture_everything() as (out, err, log_messages):
                with self.assertRaises(RuntimeError) as ctx:
                    app_module._bootstrap_auth_token_if_missing()

            self.assertNotIn(_MARKER_TOKEN, str(ctx.exception))
            self.assertNotIn(_MARKER_TOKEN, out.getvalue())
            self.assertNotIn(_MARKER_TOKEN, err.getvalue())
            self.assertTrue(all(_MARKER_TOKEN not in m for m in log_messages))
            self.assertFalse(token_file.exists())
            leftovers = list(Path(tmpdir).glob(".auth_token.tmp.*"))
            self.assertEqual(leftovers, [], f"orphaned temp token file(s) left behind: {leftovers}")

    def test_startup_is_deterministic_when_token_directory_is_unwritable(self):
        """Simulates an unwritable /web-manager-data mount: the same failure
        (RuntimeError, no leaked token) must occur every time, not just on
        first attempt."""
        import app as app_module

        with tempfile.TemporaryDirectory() as tmpdir:
            unwritable_target = Path(tmpdir) / "not-a-real-dir" / ".auth_token"
            with mock.patch.object(app_module, "_GENERATED_AUTH_TOKEN_FILE", unwritable_target), \
                 mock.patch.object(app_module, "generate_secure_auth_token", return_value=_MARKER_TOKEN), \
                 mock.patch.object(Path, "mkdir", side_effect=PermissionError("read-only mount")):
                for _ in range(2):
                    with _capture_everything() as (out, err, log_messages):
                        with self.assertRaises(RuntimeError):
                            app_module._bootstrap_auth_token_if_missing()
                    self.assertNotIn(_MARKER_TOKEN, out.getvalue())
                    self.assertNotIn(_MARKER_TOKEN, err.getvalue())
                    self.assertTrue(all(_MARKER_TOKEN not in m for m in log_messages))


if __name__ == "__main__":
    unittest.main()
