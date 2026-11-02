"""Environment variable contract: legacy aliases, and code/layer/docs drift."""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import config_layers as cl  # noqa: E402

FORWARDERS = {"_env_flag", "_env_int", "_env_float", "_config_file_first_line",
              "_audio_probe_timeout", "container_path", "legacy_env_value"}


def _sources():
    return [ROOT / "app.py", ROOT / "helpers_mb.py", ROOT / "job_engine.py", *ROOT.glob("routes_*.py"),
            *(ROOT / "backend").rglob("*.py"), *(ROOT / "beetsplug").rglob("*.py")]


def _direct_reads(tree):
    """Names read straight from os.environ."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
            arg = node.args[0]
            if (isinstance(arg, ast.Constant) and node.func.attr in ("get", "pop", "setdefault", "getenv")
                    and ast.unparse(node.func.value).endswith(("environ", "os"))):
                found.add(arg.value)
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value).endswith("environ"):
            if isinstance(node.slice, ast.Constant):
                found.add(node.slice.value)
        elif isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant):
            if any(ast.unparse(c).endswith("environ") for c in node.comparators):
                found.add(node.left.value)
    return found


def _forwarded_reads(tree):
    """Names passed to a helper that reads them from os.environ."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name == "_first_config_secret":
            found |= {a.value for a in node.args if isinstance(a, ast.Constant)}
        elif name in FORWARDERS and isinstance(node.args[0], ast.Constant):
            found.add(node.args[0].value)
    return found


def _read_names():
    names = set()
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names |= _direct_reads(tree) | _forwarded_reads(tree)
    return {n for n in names if isinstance(n, str)}


def _doc_names(path):
    return set(re.findall(r"\b[A-Z][A-Z0-9_]+\b", (ROOT / path).read_text(encoding="utf-8")))


class LegacyAliasTests(unittest.TestCase):
    def setUp(self):
        cl._warned_aliases.clear()
        self.addCleanup(cl._warned_aliases.clear)

    def test_canonical_wins_over_alias_without_a_warning(self):
        env = {"QBITTORRENT_URL": "http://new", "QBIT_URL": "http://old"}
        with self.assertNoLogs("beets.config_layers", "WARNING"):
            self.assertEqual(cl.legacy_env_value("QBITTORRENT_URL", environ=env), "http://new")

    def test_alias_works_and_warns_once_without_the_value(self):
        env = {"BEETS_WEB_TOKEN": "not-a-real-token-value"}
        with self.assertLogs("beets.config_layers", "WARNING") as logs:
            self.assertEqual(cl.legacy_env_value("BEETS_WEB_AUTH_TOKEN", environ=env), "not-a-real-token-value")
            cl.legacy_env_value("BEETS_WEB_AUTH_TOKEN", environ=env)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("BEETS_WEB_TOKEN", logs.output[0])
        self.assertIn("BEETS_WEB_AUTH_TOKEN", logs.output[0])
        self.assertNotIn("not-a-real-token-value", logs.output[0])

    def test_alias_order_and_blank_values(self):
        self.assertEqual(cl.legacy_env_value("QBITTORRENT_USERNAME", environ={"QBIT_USER": "a", "QB_USER": "b"}), "a")
        self.assertEqual(cl.legacy_env_value("QBITTORRENT_USERNAME", environ={"QBIT_USER": " ", "QB_USER": "b"}), "b")
        self.assertEqual(cl.legacy_env_value("QBITTORRENT_USERNAME", environ={}), "")

    def test_boot_check_reports_only_aliases_in_use(self):
        env = {"ACOUSTID_KEY": "not-a-real-key", "DISCOGS_TOKEN": "x", "DISCOGS_USER_TOKEN": "y"}
        with self.assertLogs("beets.config_layers", "WARNING") as logs:
            cl.warn_legacy_env_aliases(env)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("ACOUSTID_KEY", logs.output[0])
        self.assertNotIn("not-a-real-key", logs.output[0])

    def test_env_only_names_stay_unsaveable_and_report_unknown(self):
        for name in ("QBIT_PASS", "BEETS_WEB_TOKEN", "WEBCONTROL_THREADS"):
            self.assertEqual(cl.classify(name), cl.LAYER_ENV_ONLY)
            self.assertEqual(cl.reported_layer(name), cl.LAYER_UNKNOWN)
            self.assertFalse(cl.is_loadable(name))
            self.assertFalse(cl.is_saveable(name))
            self.assertEqual(cl.keys_to_migrate([name]), [])


class ReaderAliasTests(unittest.TestCase):
    """Real readers, in a fresh interpreter: several read at import time."""

    def test_readers_honour_aliases_and_never_log_their_values(self):
        env = {k: v for k, v in os.environ.items() if k not in cl.LEGACY_ENV_ALIASES.values()}
        env.update(BEETS_WEB_TOKEN="fake-token-value", QBIT_PASS="fake-qbit-value",
                   DISCOGS_USER_TOKEN="fake-discogs-value", PYTHONPATH=str(ROOT))
        code = (
            "import json\n"
            "from backend import app_runtime, artwork_service, auth_service\n"
            "print(json.dumps([auth_service._security_auth_token(), app_runtime.QBIT_PASSWORD,\n"
            "                  artwork_service.DISCOGS_TOKEN]))\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            env["WEB_MANAGER_DATA_DIR"] = tmp
            done = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, text=True,
                                  capture_output=True, check=True)
        self.assertEqual(json.loads(done.stdout.strip().splitlines()[-1]),
                         ["fake-token-value", "fake-qbit-value", "fake-discogs-value"])
        for alias in ("BEETS_WEB_TOKEN", "QBIT_PASS", "DISCOGS_USER_TOKEN"):
            self.assertIn(f"{alias} is deprecated", done.stderr)
        for fake in ("fake-token-value", "fake-qbit-value", "fake-discogs-value"):
            self.assertNotIn(fake, done.stderr)


class SettingMetadataDefaultTests(unittest.TestCase):
    READERS = {
        "BEETS_LONG_OPERATION_MAX_SECONDS": "backend/import_reconciliation_service.py",
        "BEETS_LONG_OPERATION_POLL_SECONDS": "backend/import_reconciliation_service.py",
        "BEETS_OUTBOUND_TIMEOUT_SECONDS": "backend/security.py",
    }

    @staticmethod
    def _reader_default(path, name):
        for node in ast.walk(ast.parse((ROOT / path).read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                    and len(node.args) == 2 and getattr(node.args[0], "value", None) == name):
                return node.args[1].value
        raise AssertionError(f"{name} reader not found in {path}")

    @staticmethod
    def _metadata_defaults():
        tree = ast.parse((ROOT / "routes_setup.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_SETTING_METADATA":
                return {
                    k.value: next(v.value for f, v in zip(entry.keys, entry.values) if f.value == "default")
                    for k, entry in zip(node.value.keys, node.value.values)
                    if k.value in SettingMetadataDefaultTests.READERS
                }
        raise AssertionError("_SETTING_METADATA not found")

    def test_metadata_defaults_match_the_readers(self):
        defaults = self._metadata_defaults()
        for name, path in self.READERS.items():
            self.assertEqual(defaults[name], self._reader_default(path, name), name)


class SecretNameTests(unittest.TestCase):
    def test_every_alias_of_a_secret_is_secret(self):
        for alias, canonical in cl.LEGACY_ENV_ALIASES.items():
            self.assertEqual(cl.is_secret_name(alias), canonical in cl.SECRET_KEYS, alias)
        self.assertTrue(cl.is_secret_name("QB_PASS"))
        self.assertFalse(cl.is_saveable("QB_PASS"))
        self.assertFalse(cl.is_secret_name("QB_URL"))


class EnvDriftTests(unittest.TestCase):
    def test_every_name_read_is_known_or_internal(self):
        unknown = {n for n in _read_names() if cl.classify(n) == cl.LAYER_UNKNOWN} - cl.INTERNAL_ENV_KEYS
        self.assertEqual(sorted(unknown), [], "add it to a layer in backend/config_layers.py")

    def test_every_name_read_is_documented(self):
        missing = _read_names() - cl.INTERNAL_ENV_KEYS - _doc_names("docs/CONFIGURATION.md")
        self.assertEqual(sorted(missing), [], "document it in docs/CONFIGURATION.md")

    def test_env_only_and_internal_names_are_really_read(self):
        indirect = set(cl.LEGACY_ENV_ALIASES) | {"BEETS_WEBMANAGER_API_KEY_FILE"}  # read through a helper or a loop
        stale = (cl.ENV_ONLY_KEYS | cl.INTERNAL_ENV_KEYS) - _read_names() - indirect
        self.assertEqual(sorted(stale), [], "no code reads these; remove them")

    def test_aliases_are_known_documented_and_canonical_names_are_in_the_example(self):
        docs, example = _doc_names("docs/CONFIGURATION.md"), _doc_names(".env.example")
        for alias, canonical in cl.LEGACY_ENV_ALIASES.items():
            self.assertNotEqual(cl.classify(alias), cl.LAYER_UNKNOWN, alias)
            self.assertNotEqual(cl.classify(canonical), cl.LAYER_UNKNOWN, canonical)
            self.assertIn(alias, docs)
            self.assertIn(canonical, docs)
            self.assertIn(canonical, example)

    def test_aliases_are_only_read_through_the_helper(self):
        offenders = []
        for path in _sources():
            reads = _direct_reads(ast.parse(path.read_text(encoding="utf-8")))
            offenders += [(path.name, n) for n in reads & set(cl.LEGACY_ENV_ALIASES)]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
