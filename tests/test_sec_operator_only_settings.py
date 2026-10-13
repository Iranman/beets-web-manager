"""T1: settings that run commands, name files or control security are
operator-only (Compose/Docker environment), and a key Docker/Compose set at
boot is never saved from the Settings page (it would silently revert).

Before: BEETS_OUTBOUND_ALLOWLIST and SPOTIFLAC_CMD (run through shlex.split)
were editable from the web and applied live to os.environ (F1/F4), and a
Compose-pinned key was "saved" live, then reverted at the next restart.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import config_layers as cl

try:
    from test_routes_setup import _load_routes_setup_against_stub_app
except ImportError:  # pragma: no cover
    from tests.test_routes_setup import _load_routes_setup_against_stub_app

CANARY = "canary-value-must-not-be-logged-7f3a"


class OperatorOnlyClassificationTests(unittest.TestCase):
    def test_operator_keys_are_deployment_layer_and_never_saveable_or_loadable(self):
        for key in ("BEETS_OUTBOUND_ALLOWLIST", "SPOTIFLAC_CMD", "YTDLP_COOKIE_FILE", "YTDLP_NETRC_FILE",
                    "YTDLP_JS_RUNTIMES", "YTDLP_REMOTE_COMPONENTS", "SLSKD_API_KEY_FILE", "BEETS_WEB_AUTH_DISABLED", "BEETS_TRUSTED_PROXIES"):
            with self.subTest(key=key):
                self.assertIn(key, cl.OPERATOR_KEYS)
                self.assertEqual(cl.classify(key), cl.LAYER_DEPLOYMENT)
                self.assertFalse(cl.is_saveable(key))
                self.assertFalse(cl.is_loadable(key))
        self.assertFalse(cl.OPERATOR_KEYS & cl.APP_KEYS)

    def test_plex_url_and_token_apply_live(self):
        # plex_service._plex_settings reads os.environ on every call.
        for key in ("PLEX_URL", "PLEX_TOKEN"):
            self.assertEqual(cl.apply_mode(key), cl.APPLY_LIVE, key)
        for key in ("LIDARR_URL", "SLSKD_URL", "QBITTORRENT_URL", "YTDLP_PO_PROVIDER_URL"):  # import-time constants
            self.assertEqual(cl.apply_mode(key), cl.APPLY_RESTART, key)

    def test_service_urls_stay_web_editable(self):
        for key in ("PLEX_URL", "LIDARR_URL", "SLSKD_URL", "QBITTORRENT_URL", "AI_BASE_URL", "YTDLP_PO_PROVIDER_URL"):
            self.assertTrue(cl.is_saveable(key), key)


class MigrationDropsOperatorKeysTests(unittest.TestCase):
    def test_saved_operator_keys_are_removed_and_only_names_are_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                f"AI_MODEL=gpt-4o-mini\nBEETS_OUTBOUND_ALLOWLIST=0.0.0.0/0:80\nSPOTIFLAC_CMD=sh -c {CANARY}\n"
                f"YTDLP_COOKIE_FILE=/etc/{CANARY}\n", encoding="utf-8")
            with self.assertLogs("beets.config_layers", level="WARNING") as logs:
                report = cl.migrate_saved_env_file(env_file)
            self.assertEqual(report["removed"], ["BEETS_OUTBOUND_ALLOWLIST", "SPOTIFLAC_CMD", "YTDLP_COOKIE_FILE"])
            self.assertEqual(env_file.read_text(encoding="utf-8"), "AI_MODEL=gpt-4o-mini\n")
            self.assertNotIn(CANARY, "\n".join(logs.output))
            self.assertNotIn(CANARY, json.dumps(cl.read_migration_report(env_file)))
            env = {}
            env_file.write_text(f"SPOTIFLAC_CMD=sh -c {CANARY}\n", encoding="utf-8")
            cl.load_saved_env_into_environ(env_file, environ=env)
            self.assertNotIn("SPOTIFLAC_CMD", env)


class BootPinnedSnapshotTests(unittest.TestCase):
    def test_only_non_blank_boot_values_are_pinned(self):
        with mock.patch.object(cl, "_BOOT_ENV_PINNED", None):
            self.assertFalse(cl.pinned_by_boot_environment("PLEX_URL"))
            cl.record_boot_environment({"PLEX_URL": "http://plex:32400", "LIDARR_URL": "  "})
            self.assertTrue(cl.pinned_by_boot_environment("PLEX_URL"))
            self.assertFalse(cl.pinned_by_boot_environment("LIDARR_URL"))


class SettingsSaveRefusalTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env_file = Path(tmp.name) / ".env"
        self.env_file.write_text("AI_MODEL=gpt-4o-mini\n", encoding="utf-8")
        self.module._SETUP_ENV_FILE = self.env_file
        self.module._ENV_EXAMPLE_FILE = Path(tmp.name) / ".env.example"
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        pinned = mock.patch.object(cl, "_BOOT_ENV_PINNED", frozenset({"PLEX_URL"}))
        pinned.start()
        self.addCleanup(pinned.stop)

    def save(self, variables):
        return self.client.post("/api/setup/env", json={"variables": variables})

    def test_operator_only_keys_are_refused(self):
        for key, value in (("BEETS_OUTBOUND_ALLOWLIST", "0.0.0.0/0:80"), ("SPOTIFLAC_CMD", f"sh -c {CANARY}"),
                           ("YTDLP_NETRC_FILE", "/etc/shadow"), ("BEETS_WEB_AUTH_DISABLED", "1")):
            with self.subTest(key=key):
                before = os.environ.get(key)
                response = self.save({key: value})
                self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
                self.assertIn(key, response.get_json()["error"])
                self.assertNotIn(CANARY, response.get_data(as_text=True))
                self.assertEqual(os.environ.get(key), before)
                self.assertNotIn(key, self.env_file.read_text(encoding="utf-8"))

    def test_spotiflac_cmd_clear_is_refused_too(self):
        response = self.client.post("/api/setup/env", json={"variables": {}, "clear": ["SPOTIFLAC_CMD"]})
        self.assertEqual(response.status_code, 400)

    def test_compose_pinned_key_is_refused_and_not_applied(self):
        os.environ["PLEX_URL"] = "http://compose-plex:32400"
        response = self.save({"PLEX_URL": "http://other:32400"})
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        self.assertIn("Docker/Compose environment", response.get_json()["error"])
        self.assertEqual(os.environ["PLEX_URL"], "http://compose-plex:32400")
        self.assertNotIn("PLEX_URL", self.env_file.read_text(encoding="utf-8"))

    def test_unpinned_key_still_saves(self):
        response = self.save({"LIDARR_URL": "http://lidarr:8686"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIn("LIDARR_URL=http://lidarr:8686", self.env_file.read_text(encoding="utf-8"))

    def test_allowlist_is_shown_read_only(self):
        variables = {v["name"]: v for v in self.client.get("/api/setup/env").get_json()["variables"]}
        entry = variables.get("BEETS_OUTBOUND_ALLOWLIST")
        if entry is not None:  # listed when the catalog has it
            self.assertFalse(entry["editable"])
            self.assertEqual(entry["layer"], cl.LAYER_DEPLOYMENT)
        meta = self.module._SETTING_METADATA["BEETS_OUTBOUND_ALLOWLIST"]
        self.assertFalse(meta["editable"])
        self.assertNotIn("whitelist", meta["description"].lower())


if __name__ == "__main__":
    unittest.main()
