"""Config hardening: #324 (F2 redaction forms, F4 write lock, F5 keyed
revision), #239 (SEC-237-3..7) and #183 S-4 (private config backups)."""
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from backend import beets_plugins, config_layers, config_manager
from backend.config_service import (
    ConfigRedactionError, ConfigSecretMergeError, _redact_config_content, _restore_redacted_config_secrets,
)

S = "s3cr3t-Value-91c2"
POSIX = os.name != "nt"


class RedactionFormsTests(unittest.TestCase):
    """#324 F2 / SEC-237-5/6: no secret survives GET /api/config redaction."""

    CASES = {
        "flow mapping": f"plex: {{url: 'http://p:32400', token: {S}}}\n",
        "anchor alias": f"defaults: &k {S}\nlistenbrainz:\n  token: *k\n",
        "explicit key": f"plex:\n  ? token\n  : {S}\n",
        "json one-liner": json.dumps({"plex": {"token": S}, "directory": "/music"}) + "\n",
        "url query": f"plex:\n  url: http://p:32400/?X-Plex-Token={S}\n",
        "url userinfo": f"lidarr:\n  url: http://admin:{S}@lidarr:8686\n",
        "key suffix": f"discogs:\n  user_key: {S}\n",
        "list item": f"accounts:\n  - name: a\n    password: {S}\n",
        "quoted key": f"'apikey': \"{S}\"\n",
        "block scalar": f"spotify:\n  client_secret: |\n    {S}\n",
    }

    def test_no_form_leaks_the_secret(self):
        for name, text in self.CASES.items():
            with self.subTest(form=name):
                shown = _redact_config_content(text)
                self.assertNotIn(S, shown)

    def test_unlocatable_secret_fails_closed(self):
        escaped = '{"plex": {"token": "' + "".join(f"\\u{ord(c):04x}" for c in S) + '"}}\n'
        with self.assertRaises(ConfigRedactionError) as ctx:
            _redact_config_content(escaped)
        self.assertNotIn(S, str(ctx.exception))

    def test_alias_bomb_is_walked_once(self):
        lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
        lines += [f"a{i}: &a{i} [*a{i-1}, *a{i-1}, *a{i-1}, *a{i-1}, *a{i-1}, *a{i-1}, *a{i-1}, *a{i-1}, *a{i-1}]"
                  for i in range(1, 12)]
        started = time.monotonic()
        _redact_config_content("\n".join(lines) + "\n")
        self.assertLess(time.monotonic() - started, 5)

    def test_plain_secret_still_round_trips(self):
        stored = f"plex:\n  url: http://p:32400\n  token: {S}\ndirectory: /music\n"
        shown = _redact_config_content(stored)
        self.assertNotIn(S, shown)
        self.assertEqual(_restore_redacted_config_secrets(shown, stored), stored)

    def test_placeholder_outside_a_secret_line_is_refused_not_written(self):
        for name in ("flow mapping", "anchor alias", "json one-liner"):
            stored = self.CASES[name]
            with self.subTest(form=name), self.assertRaises(ConfigSecretMergeError):
                _restore_redacted_config_secrets(_redact_config_content(stored), stored)


class RevisionAndLockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.yaml"
        self.path.write_text(f"plex:\n  token: {S}\n", encoding="utf-8")
        if POSIX:
            os.chmod(self.path, 0o644)

    def test_revision_is_keyed_not_a_plain_sha256(self):  # F5
        text = self.path.read_text(encoding="utf-8")
        rev = config_manager.get_config(self.path)["revision"]
        self.assertNotEqual(rev, hashlib.sha256(text.encode()).hexdigest())
        self.assertTrue(config_manager.revision_matches(rev, text))
        self.assertFalse(config_manager.revision_matches(hashlib.sha256(text.encode()).hexdigest(), text))

    def test_save_waits_for_the_write_lock(self):  # F4
        rev = config_manager.get_config(self.path)["revision"]
        done = threading.Event()
        errors = []

        def save():
            try:
                config_manager.save_config("directory: /music\n", expected_revision=rev, config_path=self.path)
                done.set()
            except config_manager.ConfigConflictError as exc:
                errors.append(exc)

        config_manager.CONFIG_WRITE_LOCK.acquire()
        try:
            t = threading.Thread(target=save)
            t.start()
            self.assertFalse(done.wait(0.3))
            self.path.write_text("directory: /elsewhere\n", encoding="utf-8")  # concurrent writer
        finally:
            config_manager.CONFIG_WRITE_LOCK.release()
        t.join(5)
        self.assertFalse(done.is_set(), "a stale revision must not overwrite the concurrent change")
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "directory: /elsewhere\n")

    @unittest.skipUnless(POSIX, "POSIX modes and symlinks")
    def test_backups_are_private_and_never_follow_a_symlink(self):  # #183 S-4
        outside = Path(self.path.parent, "outside.txt")
        outside.write_text("keep", encoding="utf-8")
        os.symlink(outside, config_manager.get_config_backup_path(self.path))
        rev = config_manager.get_config(self.path)["revision"]
        config_manager.save_config(self.path.read_text(encoding="utf-8") + "va_name: VA\n",
                                   expected_revision=rev, config_path=self.path)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
        backups = [config_manager.get_config_backup_path(self.path)] + list(self.path.parent.glob("config.yaml.bak.*"))
        self.assertEqual(len(backups), 2)
        for b in backups:
            self.assertFalse(b.is_symlink())
            self.assertEqual(stat.S_IMODE(os.lstat(b).st_mode), 0o600, b.name)
            self.assertIn(S, b.read_text(encoding="utf-8"))

    def test_plugin_edit_refuses_a_file_changed_since_its_snapshot(self):  # F4 for the other writer
        original = beets_plugins._read_config_snapshot(self.path)
        self.path.write_text("directory: /changed\n", encoding="utf-8")
        with self.assertRaises(beets_plugins.BeetsConfigEditError):
            beets_plugins._write_config_text(self.path, "plugins: x\n", backup_prefix="config.yaml.bak-t",
                                             original=original)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "directory: /changed\n")


class BeetsConfigFollowupTests(unittest.TestCase):
    def test_plugins_line_trailing_comment_is_not_kept_after_names(self):  # SEC-237-7
        new, _added, _changed = beets_plugins._plan_config_yaml_plugins(
            "plugins: fetchart # note\n", ["webmanager"], ["/config/beetsplug"])
        import yaml
        self.assertEqual(yaml.safe_load(new)["plugins"].split(), ["fetchart", "webmanager"])

    def test_preview_diff_masks_alias_and_query_secrets(self):  # SEC-237-6
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.yaml"
            path.write_text(f"base: &k {S}\nplugins: fetchart\nplex:\n  token: *k\n"
                            f"  url: http://p/?token={S}xyz\n", encoding="utf-8")
            diff = beets_plugins.preview_recommended_plugins(path)["diff"]
        self.assertNotIn(S, diff)

    def test_refused_config_path_is_never_verified_or_read(self):  # SEC-237-3
        try:
            from test_routes_setup import _load_routes_setup_against_stub_app
        except ImportError:  # pragma: no cover
            from tests.test_routes_setup import _load_routes_setup_against_stub_app
        _app, module = _load_routes_setup_against_stub_app(self)
        status = {"protocol_version": "1.0", "loaded_plugins": ["fetchart"], "capabilities": []}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.yaml"
            path.write_text("replaygain:\n  backend: command\n", encoding="utf-8")
            with mock.patch.object(module, "_checked_beets_config_path", return_value=(path, "refused")), \
                    mock.patch("backend.beets_adapter.beets_adapter.get_plugin_status", return_value=status), \
                    mock.patch("backend.beets_adapter.beets_adapter.get_stats", return_value={}), \
                    mock.patch("backend.beets_plugins.verify_all_plugins") as verify:
                diag = module._beets_plugin_diagnostics(path)
        verify.assert_not_called()
        self.assertNotEqual(diag.get("replaygain_backend"), "command")


class SecretKeyPredicateTests(unittest.TestCase):
    def test_predicate_is_shared(self):
        self.assertIs(beets_plugins._is_secret_key, config_layers.is_secret_config_key)
        for key in ("token", "user_key", "Api-Key", "'password'", "authorization", "bearer_value", "smtp_pass"):
            self.assertTrue(config_layers.is_secret_config_key(key), key)
        for key in ("directory", "plugins", "url", "user"):
            self.assertFalse(config_layers.is_secret_config_key(key), key)


if __name__ == "__main__":
    unittest.main()
