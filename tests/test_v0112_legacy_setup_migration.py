"""Regression tests for BUG-2 (v0.1.12): a legacy-established install
(admin credential created before the two-step setup_first_run ->
setup_complete flow existed) never got /web-manager-data/.setup_complete
created, because nothing in its history ever called POST /api/setup/complete.
first_run_required correctly stayed False, but the frontend's route guard
(frontend/src/App.tsx) additionally gates on setup_complete specifically,
so a real logged-in administrator was shown the First-Run Setup wizard
instead of their dashboard. Confirmed live on the v0.1.11 TrueNAS
production rollout and corrected there by hand; these tests cover the
automatic migration (_migrate_legacy_setup_complete_if_established() in
routes_setup.py) that now does it on its own.
"""
import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


class LegacySetupCompleteMigrationTests(unittest.TestCase):
    """Exercises _migrate_legacy_setup_complete_if_established() directly
    against the real app module (not the routes_setup stub-app harness --
    this migration explicitly no-ops under that stub, so it needs the real
    one to test the actual fail-closed evidence check)."""

    def setUp(self):
        import app as app_module
        import routes_setup

        self.app_module = app_module
        self.routes_setup = routes_setup

        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)

        self.marker = root / ".setup_complete"
        self.initial_pwd_file = root / ".initial_admin_password"
        self.persisted_pwd_file = root / ".browser_password"
        self.persisted_user_file = root / ".browser_username"
        self.setup_state_file = root / ".browser_setup_state"

        self.patches = [
            mock.patch.object(app_module, "WEB_MANAGER_DATA_DIR", root),
            mock.patch.object(routes_setup, "_SETUP_COMPLETE_MARKER", self.marker),
            mock.patch.object(app_module, "_INITIAL_BROWSER_PASSWORD_FILE", self.initial_pwd_file),
            mock.patch.object(app_module, "_PERSISTED_BROWSER_PASSWORD_FILE", self.persisted_pwd_file),
            mock.patch.object(app_module, "_PERSISTED_BROWSER_USERNAME_FILE", self.persisted_user_file),
            mock.patch.object(app_module, "_BROWSER_SETUP_STATE_FILE", self.setup_state_file),
        ]
        self.env_patch = mock.patch.dict(
            __import__("os").environ,
            {
                "BEETS_WEB_PASSWORD": "",
                "BEETS_WEB_USERNAME": "",
                "BEETS_WEB_AUTH_DISABLED": "0",
                "WEB_MANAGER_DATA_DIR": str(root),
            },
            clear=False,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _run_migration(self):
        self.routes_setup._migrate_legacy_setup_complete_if_established()

    # -- valid established legacy install -----------------------------------

    def test_legacy_established_install_gets_marker_created(self):
        """legacy auth valid + first_run_required=false + marker missing
        -> setup_complete becomes True and the marker file exists."""
        self.setup_state_file.write_text("legacy_established", encoding="utf-8")
        self.initial_pwd_file.write_text("a-real-legacy-password-value-1234", encoding="utf-8")

        self.assertFalse(self.app_module._first_run_setup_required())
        self._run_migration()

        self.assertTrue(self.marker.exists())
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "1")

    def test_claimed_install_gets_marker_created(self):
        from werkzeug.security import generate_password_hash
        self.setup_state_file.write_text("claimed", encoding="utf-8")
        self.persisted_user_file.write_text("admin", encoding="utf-8")
        self.persisted_pwd_file.write_text(generate_password_hash("correct horse battery staple"), encoding="utf-8")

        self._run_migration()
        self.assertTrue(self.marker.exists())

    def test_environment_password_install_gets_marker_created(self):
        with mock.patch.dict(os.environ, {"BEETS_WEB_PASSWORD": "EnvConfiguredPassword123!Aa4567890"}, clear=False):
            self.assertFalse(self.app_module._first_run_setup_required())
            self._run_migration()
        self.assertTrue(self.marker.exists())

    def test_token_only_claimed_state_does_not_create_marker(self):
        self.setup_state_file.write_text("claimed", encoding="utf-8")
        with mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_TOKEN": "token-only-upgrade-value-000000000000"}, clear=False):
            self._run_migration()
        self.assertFalse(self.marker.exists())

    # -- already migrated ------------------------------------------------

    def test_already_migrated_install_is_untouched(self):
        self.setup_state_file.write_text("legacy_established", encoding="utf-8")
        self.initial_pwd_file.write_text("a-real-legacy-password-value-1234", encoding="utf-8")
        self.marker.write_text("1", encoding="utf-8")
        before_mtime = self.marker.stat().st_mtime

        self._run_migration()

        self.assertEqual(self.marker.stat().st_mtime, before_mtime, "must not rewrite an existing marker")

    # -- missing auth ------------------------------------------------------

    def test_no_credential_at_all_does_not_create_marker(self):
        """Fresh-looking state (no state file, no credential file) must
        never produce a marker even if somehow called."""
        self._run_migration()
        self.assertFalse(self.marker.exists())

    # -- corrupt auth state (state file says claimed, but credential is gone) --

    def test_corrupt_state_fails_closed(self):
        """.browser_setup_state claims "claimed" but the actual persisted
        credential files are missing/empty -- _has_explicit_browser_password()
        independently re-verifies and must return False, so the marker must
        NOT be created even though the cached state string says otherwise."""
        self.setup_state_file.write_text("claimed", encoding="utf-8")
        # No password file written at all -- corrupt/tampered state.
        self._run_migration()
        self.assertFalse(self.marker.exists())

    # -- fresh installation --------------------------------------------------

    def test_fresh_install_first_run_still_required(self):
        self.setup_state_file.write_text("fresh", encoding="utf-8")
        self.assertTrue(self.app_module._first_run_setup_required())
        self._run_migration()
        self.assertFalse(self.marker.exists())

    # -- concurrent migration -------------------------------------------------

    def test_concurrent_migration_calls_do_not_corrupt_state(self):
        import threading
        self.setup_state_file.write_text("legacy_established", encoding="utf-8")
        self.initial_pwd_file.write_text("a-real-legacy-password-value-1234", encoding="utf-8")

        errors = []

        def worker():
            try:
                self._run_migration()
            except Exception as exc:  # pragma: no cover - failure path only
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "1")

    # -- stub-app guard ---------------------------------------------------

    def test_migration_noops_under_stub_app_harness(self):
        """Sanity check on the guard itself: importing routes_setup against
        the stub app (as test_routes_setup.py does) must not attempt real
        migration evidence checks that would crash against a bare Flask
        app with none of app.py's helpers."""
        stub = types.ModuleType("app")
        stub.__routes_setup_test_stub__ = True
        stub.app = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"app": stub}):
            # Must not raise even though the stub has none of the real
            # credential-check helpers.
            self.routes_setup._migrate_legacy_setup_complete_if_established()
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
