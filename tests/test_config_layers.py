"""Configuration layer model (backend/config_layers.py) -- BI-1/2/3/17/18.

Host-side Compose values (DOWNLOADS_PATH, BEETS_CONFIG_PATH, ...) must never
reach the application, saved settings hold only application keys, and the
one-time migration cleans files earlier releases seeded from .env.example.
"""

import ast
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import config_layers as cl  # noqa: E402
from backend import config_manager  # noqa: E402

SEEDED_0149_FILE = (
    "# 1. Standard System & Volume Configuration (Optional)\n"
    "PUID=1000\nPGID=1000\nTZ=UTC\nWEBCONTROL_PORT=8337\n"
    "BEETS_CONFIG_PATH=./beets\nMUSIC_PATH=./music\nDOWNLOADS_PATH=./downloads\n"
    "WEB_MANAGER_DATA_PATH=./web-manager\n"
    "BEETS_WEB_USERNAME=admin\nAI_MODEL=gpt-4o-mini\nOPENAI_API_KEY=sk-test-not-real\n"
    "SLSKD_URL=http://slskd:5030\nPLAYLIST_DIR=/music/playlists\n"
)


def _routes_setup_tree():
    return ast.parse((ROOT / "routes_setup.py").read_text(encoding="utf-8"))


def _setting_metadata_keys():
    for node in ast.walk(_routes_setup_tree()):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_SETTING_METADATA":
            return [k.value for k in node.value.keys]
    raise AssertionError("_SETTING_METADATA not found")


def _fallback_template_keys():
    for node in ast.walk(_routes_setup_tree()):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_FALLBACK_ENV_TEMPLATE" for t in node.targets):
            text = node.value.value
            return re.findall(r"(?m)^([A-Z_][A-Z0-9_]*)=", text)
    raise AssertionError("_FALLBACK_ENV_TEMPLATE not found")


def _env_example_keys():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return re.findall(r"(?m)^#?\s*([A-Z_][A-Z0-9_]*)=", text)


class ClassificationTests(unittest.TestCase):
    def test_every_known_key_is_classified(self):
        names = set(_setting_metadata_keys()) | set(_fallback_template_keys()) | set(_env_example_keys())
        unknown = sorted(n for n in names if cl.classify(n) == cl.LAYER_UNKNOWN)
        self.assertEqual(unknown, [], "every catalog/template key needs a layer in backend/config_layers.py")

    def test_layers_are_disjoint(self):
        sets = [cl.HOST_KEYS, cl.DEPLOYMENT_KEYS, cl.CONTAINER_KEYS, cl.APP_KEYS, cl.SECRET_KEYS]
        for i, a in enumerate(sets):
            for b in sets[i + 1:]:
                self.assertFalse(a & b, f"overlap: {a & b}")

    def test_host_and_pinned_keys_are_never_loadable_or_saveable(self):
        for name in ("DOWNLOADS_PATH", "BEETS_CONFIG_PATH", "MUSIC_PATH", "WEB_MANAGER_DATA_PATH",
                     "PUID", "PGID", "TZ", "WEBCONTROL_PORT", "MUSIC_ROOT", "DOWNLOADS_ROOT",
                     "BEETS_CONFIG", "PLAYLIST_DIR", "BEETS_SQLITE_TIMEOUT", "WEB_MANAGER_PATH"):
            self.assertFalse(cl.is_loadable(name), name)
            self.assertFalse(cl.is_saveable(name), name)
            self.assertEqual(cl.apply_mode(name) if cl.classify(name) != cl.LAYER_DEAD else "deploy", "deploy", name)
        for name in ("AI_MODEL", "OPENAI_API_KEY", "PLEX_URL", "BEETS_WEB_URL", "BEETS_WEBMANAGER_API_KEY"):
            self.assertTrue(cl.is_loadable(name), name)


class ContainerPathTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(cl.music_root({}), "/music")
        self.assertEqual(cl.downloads_root({}), "/downloads")
        self.assertEqual(cl.beets_config_file({}), "/config/config.yaml")
        self.assertEqual(cl.beets_web_url({}), "http://beets:8337")

    def test_relative_container_value_is_ignored(self):
        self.assertEqual(cl.downloads_root({"DOWNLOADS_ROOT": "./downloads"}), "/downloads")
        self.assertEqual(cl.music_root({"MUSIC_ROOT": "music"}), "/music")

    def test_canonical_beats_deprecated_alias_and_alias_is_honoured(self):
        self.assertEqual(cl.music_root({"MUSIC_ROOT": "/srv/a", "MUSIC_LIBRARY_PATH": "/srv/b"}), "/srv/a")
        self.assertEqual(cl.music_root({"MUSIC_LIBRARY_PATH": "/srv/b"}), "/srv/b")
        self.assertEqual(cl.downloads_root({"DOWNLOAD_PATH": "/srv/dl"}), "/srv/dl")

    def test_host_side_downloads_path_never_feeds_the_container_root(self):
        self.assertEqual(cl.downloads_root({"DOWNLOADS_PATH": "./downloads"}), "/downloads")
        self.assertEqual(cl.downloads_root({"DOWNLOADS_PATH": "/mnt/pool/dl"}), "/downloads")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_file = Path(self.tmp.name) / ".env"

    def test_seeded_049_file_is_migrated_with_backup_and_names_only_report(self):
        self.env_file.write_text(SEEDED_0149_FILE, encoding="utf-8")
        report = cl.migrate_saved_env_file(self.env_file)
        self.assertEqual(
            report["removed"],
            sorted(["PUID", "PGID", "TZ", "WEBCONTROL_PORT", "BEETS_CONFIG_PATH", "MUSIC_PATH",
                    "DOWNLOADS_PATH", "WEB_MANAGER_DATA_PATH", "PLAYLIST_DIR"]),
        )
        text = self.env_file.read_text(encoding="utf-8")
        for gone in ("DOWNLOADS_PATH", "BEETS_CONFIG_PATH", "PUID=", "PLAYLIST_DIR"):
            self.assertNotIn(gone, text)
        for kept in ("AI_MODEL=gpt-4o-mini", "OPENAI_API_KEY=sk-test-not-real", "SLSKD_URL=", "BEETS_WEB_USERNAME=admin"):
            self.assertIn(kept, text)
        backup = self.env_file.with_name(report["backup"])
        self.assertTrue(backup.name.startswith(".env.bak-migration-"))
        self.assertEqual(backup.read_text(encoding="utf-8"), SEEDED_0149_FILE)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        saved = json.loads(cl.migration_report_path(self.env_file).read_text(encoding="utf-8"))
        self.assertNotIn("sk-test-not-real", json.dumps(saved))
        surfaced = cl.read_migration_report(self.env_file)
        self.assertEqual(surfaced["removed_count"], 9)
        self.assertEqual(surfaced["backup"], backup.name)

    def test_migration_is_idempotent(self):
        self.env_file.write_text(SEEDED_0149_FILE, encoding="utf-8")
        cl.migrate_saved_env_file(self.env_file)
        before = sorted(p.name for p in Path(self.tmp.name).iterdir())
        again = cl.migrate_saved_env_file(self.env_file)
        self.assertEqual(again["removed"], [])
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()), before)

    def test_clean_or_missing_file_is_untouched(self):
        self.assertEqual(cl.migrate_saved_env_file(self.env_file)["removed"], [])
        self.env_file.write_text("AI_MODEL=x\n", encoding="utf-8")
        self.assertEqual(cl.migrate_saved_env_file(self.env_file)["removed"], [])
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), "AI_MODEL=x\n")
        self.assertFalse(any(p.name.startswith(".env.bak-migration-") for p in Path(self.tmp.name).iterdir()))

    def test_loader_loads_only_application_keys_and_never_overrides_docker(self):
        self.env_file.write_text(SEEDED_0149_FILE + "SOMETHING_UNKNOWN=1\n", encoding="utf-8")
        env = {"OPENAI_API_KEY": "from-docker"}
        loaded, ignored = cl.load_saved_env_into_environ(self.env_file, environ=env)
        self.assertNotIn("DOWNLOADS_PATH", env)
        self.assertNotIn("BEETS_CONFIG_PATH", env)
        self.assertNotIn("PUID", env)
        self.assertEqual(env["AI_MODEL"], "gpt-4o-mini")
        self.assertEqual(env["OPENAI_API_KEY"], "from-docker")
        self.assertIn("DOWNLOADS_PATH", ignored)
        self.assertIn("SOMETHING_UNKNOWN", ignored)
        self.assertNotIn("OPENAI_API_KEY", loaded)


class BootUpgradeTests(unittest.TestCase):
    """End to end in a fresh interpreter: a 0.1.49-seeded saved settings file
    is migrated at boot, host keys never reach os.environ, and the config
    editor resolves the real Beets config file."""

    def test_boot_migrates_and_config_editor_resolves_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(SEEDED_0149_FILE, encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if k not in cl.HOST_KEYS | cl.CONTAINER_KEYS | cl.DEPLOYMENT_KEYS | {"AI_MODEL", "SETUP_ENV_FILE"}}
            env["WEB_MANAGER_DATA_DIR"] = tmp
            code = (
                "import os, json\n"
                "from backend import app_runtime\n"
                "from backend import config_manager\n"
                "print(json.dumps({k: os.environ.get(k) for k in ('DOWNLOADS_PATH','BEETS_CONFIG_PATH','PUID','AI_MODEL')}))\n"
                "print(config_manager.get_config_path().as_posix())\n"
            )
            out = subprocess.check_output([sys.executable, "-c", code], cwd=str(ROOT), env=env, text=True,
                                          stderr=subprocess.DEVNULL).strip().splitlines()
            values = json.loads(out[-2])
            self.assertIsNone(values["DOWNLOADS_PATH"])
            self.assertIsNone(values["BEETS_CONFIG_PATH"])
            self.assertIsNone(values["PUID"])
            self.assertEqual(values["AI_MODEL"], "gpt-4o-mini")
            self.assertTrue(out[-1].endswith("/config/config.yaml"))
            self.assertNotIn("DOWNLOADS_PATH", (Path(tmp) / ".env").read_text(encoding="utf-8"))
            self.assertTrue(any(p.name.startswith(".env.bak-migration-") for p in Path(tmp).iterdir()))


class ConfigManagerPathTests(unittest.TestCase):
    def test_beets_config_path_host_variable_is_ignored(self):
        with mock.patch.dict(os.environ, {"BEETS_CONFIG_PATH": "./beets"}, clear=False):
            os.environ.pop("BEETS_CONFIG", None)
            os.environ.pop("BEETSDIR", None)
            self.assertEqual(config_manager.get_config_path().as_posix().split(":")[-1], "/config/config.yaml")

    def test_relative_beets_config_is_refused(self):
        with mock.patch.dict(os.environ, {"BEETS_CONFIG": "beets/config.yaml"}):
            with self.assertRaises(config_manager.ConfigPathError):
                config_manager.get_config_path()

    def test_out_of_directory_target_is_refused(self):
        for bad in ("/app/beets", "/config/sub/config.yaml", "/config/../etc/passwd"):
            with mock.patch.dict(os.environ, {"BEETS_CONFIG": bad, "BEETSDIR": "/config"}):
                with self.assertRaises(config_manager.ConfigPathError, msg=bad):
                    config_manager.get_config_path()

    def test_missing_config_fails_closed_instead_of_empty_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp).as_posix()
            if not cfg_dir.startswith("/"):
                self.skipTest("container paths are POSIX; covered in the Docker acceptance run")
            with mock.patch.dict(os.environ, {"BEETS_CONFIG": f"{cfg_dir}/config.yaml", "BEETSDIR": cfg_dir}):
                with self.assertRaises(config_manager.ConfigNotFoundError):
                    config_manager.get_config()
                with self.assertRaises(config_manager.ConfigNotFoundError):
                    config_manager.save_config("directory: /music\n")
                self.assertFalse((Path(tmp) / "config.yaml").exists())
                (Path(tmp) / "config.yaml").write_text("directory: /music\n", encoding="utf-8")
                self.assertEqual(config_manager.get_config()["content"], "directory: /music\n")


class NoHostVariableReadsTests(unittest.TestCase):
    """Application code must never read host-side Compose variables (BI-18)."""

    def _sources(self):
        files = [ROOT / "app.py", ROOT / "helpers_mb.py", ROOT / "job_engine.py", *ROOT.glob("routes_*.py"),
                 *(ROOT / "backend").rglob("*.py")]
        return [f for f in files if f.name != "config_layers.py"]

    @staticmethod
    def _read_names(tree):
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in ("get", "getenv", "pop", "setdefault") and node.args and isinstance(node.args[0], ast.Constant):
                    owner = ast.unparse(func.value) if isinstance(func, ast.Attribute) else ""
                    if name == "getenv" or "environ" in owner:
                        found.append(node.args[0].value)
            elif isinstance(node, ast.Subscript) and "environ" in ast.unparse(node.value):
                if isinstance(node.slice, ast.Constant):
                    found.append(node.slice.value)
        return found

    def test_no_app_code_reads_host_side_compose_variables(self):
        offenders = []
        for path in self._sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name in self._read_names(tree):
                if name in cl.HOST_KEYS:
                    offenders.append(f"{path.relative_to(ROOT)}: {name}")
        self.assertEqual(offenders, [])

    def test_detector_catches_a_host_variable_read(self):
        tree = ast.parse("import os\nos.environ.get('DOWNLOADS_PATH')\nos.getenv('MUSIC_PATH')\nos.environ['BEETS_CONFIG_PATH']\n")
        self.assertEqual(sorted(self._read_names(tree)), ["BEETS_CONFIG_PATH", "DOWNLOADS_PATH", "MUSIC_PATH"])


if __name__ == "__main__":
    unittest.main()
