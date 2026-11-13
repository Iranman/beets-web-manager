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
              "_audio_probe_timeout", "container_path", "legacy_env_value", "_env_limit"}


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
    """Every row with a default names the reader it must match, or says why not.
    A row with default None reports nothing as effective, so needs neither."""

    READERS = {
        "WEBCONTROL_PORT": "backend/app_runtime.py",
        "DEMO_MODE": "routes_setup.py",
        "BEETS_LOG": "backend/app_runtime.py",
        "BEETS_LONG_OPERATION_MAX_SECONDS": "backend/import_reconciliation_service.py",
        "BEETS_LONG_OPERATION_POLL_SECONDS": "backend/import_reconciliation_service.py",
        "BEETS_TRUSTED_PROXIES": "backend/auth_service.py",
        "BEETS_WEB_SESSION_COOKIE_SECURE": "app.py",
        "BEETS_OUTBOUND_TIMEOUT_SECONDS": "backend/security.py",
        "BEETS_OUTBOUND_MAX_REDIRECTS": "backend/security.py",
        "BEETS_OUTBOUND_MAX_RESPONSE_BYTES": "backend/security.py",
        "BEETS_OUTBOUND_ALLOWLIST": "backend/security.py",
        "BEETS_CONFIG": "backend/config_layers.py",
        "MUSIC_FORMAT_QUARANTINE_DIR": "backend/audio_preferences.py",
        "SPOTIFY_CLIENT_ID": "backend/playlist_service.py",
        "PLEX_URL": "backend/app_runtime.py",
        "PLEX_MUSIC_SECTION": "backend/plex_service.py",
        "LIDARR_URL": "backend/app_runtime.py",
        "SLSKD_URL": "backend/app_runtime.py",
        "QBITTORRENT_URL": "backend/app_runtime.py",
        "QBITTORRENT_USERNAME": "backend/app_runtime.py",
        "QBITTORRENT_CATEGORY": "backend/app_runtime.py",
        "PLAYLIST_AUTO_SYNC": "backend/app_runtime.py",
        "PLAYLIST_AUTO_SYNC_INTERVAL": "backend/app_runtime.py",
        "PLAYLIST_DOWNLOAD_METHODS": "backend/app_runtime.py",
        "PLAYLIST_MIN_DOWNLOAD_SECONDS": "backend/app_runtime.py",
        "YTDLP_JS_RUNTIMES": "backend/ytdlp_service.py",
        "SPOTIFLAC_SERVICES": "backend/ytdlp_service.py",
    }
    EXEMPT = {
        "PUID": "_BUILTIN_SETTINGS: reported from the running process, not this default",
        "PGID": "_BUILTIN_SETTINGS: reported from the running process, not this default",
        "TZ": "_BUILTIN_SETTINGS: reported from the system timezone, not this default",
        "BEETS_WEB_USERNAME": "auth_service returns the literal admin in a later statement, not in the read",
        "BEETS_WEB_AUTH_DISABLED": "the reader's blank means the same as 0: auth stays on",
        "AI_MODEL": "matching_service takes the fallback from each call site; the row matches the setup probe",
        "AI_BASE_URL": "_BUILTIN_SETTINGS: matching_service._ai_provider picks OpenAI or OpenRouter by the key set (#523)",
    }
    _READ_CALLS = {"get", "getenv", "legacy_env_value", "container_path", "_env_limit"}

    @classmethod
    def _reader_default(cls, path, name):
        """The value the first read of ``name`` yields when it is unset."""
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        constants = {n.targets[0].id: n.value for n in tree.body
                     if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
        calls = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Call)), key=lambda n: n.lineno)
        for node in calls:
            called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if called not in cls._READ_CALLS or not node.args or getattr(node.args[0], "value", None) != name:
                continue
            found = node.args[1] if len(node.args) > 1 else ast.Constant("")
            up = parents[node]
            while isinstance(up, ast.Attribute) or (isinstance(up, ast.Call) and up.func in parents and up.args == []):
                up = parents[up]
            if isinstance(up, ast.BoolOp) and isinstance(up.values[-1], ast.Constant):
                found = up.values[-1]
            return str(cls._literal(found, constants))
        raise AssertionError(f"{name} reader not found in {path}")

    @classmethod
    def _literal(cls, node, constants):
        """A reader's default: a constant, a module constant, a product, or str() of one."""
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return cls._literal(constants[node.id], constants)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            return cls._literal(node.left, constants) * cls._literal(node.right, constants)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "str" and len(node.args) == 1:
            return str(cls._literal(node.args[0], constants))
        raise AssertionError(f"not a static default: {ast.unparse(node)}")

    @staticmethod
    def _metadata_defaults():
        tree = ast.parse((ROOT / "routes_setup.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_SETTING_METADATA":
                rows = {}
                for key, entry in zip(node.value.keys, node.value.values):
                    fields = {f.value: v for f, v in zip(entry.keys, entry.values)}
                    rows[key.value] = fields["default"].value if "default" in fields else None
                return rows
        raise AssertionError("_SETTING_METADATA not found")

    @staticmethod
    def _same(left, right):
        try:
            return float(left) == float(right)
        except ValueError:
            return left == right

    def test_every_row_with_a_default_matches_its_reader_or_is_exempt(self):
        with_default = {k for k, v in self._metadata_defaults().items() if v is not None}
        self.assertEqual(sorted(with_default - set(self.READERS) - set(self.EXEMPT)), [],
                         "name this row's reader in READERS, or justify it in EXEMPT")
        self.assertEqual(sorted((set(self.READERS) | set(self.EXEMPT)) - with_default), [], "stale entries")

    def test_metadata_defaults_match_the_readers(self):
        defaults = self._metadata_defaults()
        for name, path in self.READERS.items():
            reader = self._reader_default(path, name)
            self.assertTrue(self._same(defaults[name], reader), f"{name}: row {defaults[name]!r}, reader {reader!r}")


class BootOrderTests(unittest.TestCase):
    """A real ``import app``: saved settings must reach modules that read at import."""

    def _boot(self, saved, code):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BEETS_OUTBOUND_", "BEETS_LONG_OPERATION_"))}
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".env").write_text(saved, encoding="utf-8")
            env.update(WEB_MANAGER_DATA_DIR=tmp, SETUP_ENV_FILE=str(Path(tmp, ".env")), PYTHONPATH=str(ROOT),
                       BEETS_CONFIG=str(Path(tmp, "config.yaml")), BEETSDIR=tmp, BEETS_LOG=str(Path(tmp, "b.log")),
                       MUSIC_ROOT=tmp, DOWNLOADS_ROOT=tmp)
            done = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, text=True,
                                  capture_output=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr[-1500:])
        return json.loads(done.stdout.strip().splitlines()[-1]), done.stderr

    CODE = (
        "import json, app\n"
        "from backend import security as s, import_reconciliation_service as i\n"
        "print(json.dumps([s._DEFAULT_TIMEOUT, s._DEFAULT_MAX_BYTES, s._DEFAULT_MAX_REDIRECTS,\n"
        "                  i.BEETS_LONG_OPERATION_MAX_SECONDS, i.BEETS_LONG_OPERATION_POLL_SECONDS]))\n"
    )

    def test_saved_limits_take_effect_at_boot(self):
        saved = ("BEETS_OUTBOUND_TIMEOUT_SECONDS=7\nBEETS_OUTBOUND_MAX_RESPONSE_BYTES=1234\n"
                 "BEETS_OUTBOUND_MAX_REDIRECTS=1\nBEETS_LONG_OPERATION_MAX_SECONDS=77\n"
                 "BEETS_LONG_OPERATION_POLL_SECONDS=3\n")
        self.assertEqual(self._boot(saved, self.CODE)[0], [7.0, 1234, 1, 77.0, 3.0])

    def test_bad_saved_limits_warn_and_do_not_stop_the_app(self):
        saved = ("BEETS_OUTBOUND_TIMEOUT_SECONDS=soon\nBEETS_OUTBOUND_MAX_RESPONSE_BYTES=-1\n"
                 "BEETS_OUTBOUND_MAX_REDIRECTS=1.5\n")
        limits, stderr = self._boot(saved, self.CODE)
        self.assertEqual(limits[:3], [20.0, 20 * 1024 * 1024, 5])
        self.assertIn("BEETS_OUTBOUND_TIMEOUT_SECONDS is invalid", stderr)

    def test_total_timeout_below_its_minimum_warns_and_uses_the_default(self):
        code = "import json, app\nfrom backend import security as s\nprint(json.dumps(s._DEFAULT_TOTAL_TIMEOUT))\n"
        limit, stderr = self._boot("BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS=0.05\n", code)
        self.assertEqual(limit, 60.0)
        self.assertIn("BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS is invalid", stderr)

    def test_limits_above_their_ceiling_warn_and_are_capped(self):
        saved = ("BEETS_OUTBOUND_TIMEOUT_SECONDS=301\nBEETS_OUTBOUND_MAX_RESPONSE_BYTES=209715201\n"
                 "BEETS_OUTBOUND_MAX_REDIRECTS=21\nBEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS=601\n")
        code = ("import json, app\nfrom backend import security as s\n"
                "print(json.dumps([s._DEFAULT_TIMEOUT, s._DEFAULT_MAX_BYTES, s._DEFAULT_MAX_REDIRECTS, s._DEFAULT_TOTAL_TIMEOUT]))\n")
        limits, stderr = self._boot(saved, code)
        self.assertEqual(limits, [300.0, 200 * 1024 * 1024, 20, 600.0])
        for name in ("TIMEOUT_SECONDS", "MAX_RESPONSE_BYTES", "MAX_REDIRECTS", "TOTAL_TIMEOUT_SECONDS"):
            self.assertIn(f"BEETS_OUTBOUND_{name} is above its ceiling", stderr)
        self.assertIn("using the ceiling", stderr)

    def test_limits_at_their_ceiling_apply(self):
        saved = ("BEETS_OUTBOUND_TIMEOUT_SECONDS=300\nBEETS_OUTBOUND_MAX_RESPONSE_BYTES=209715200\n"
                 "BEETS_OUTBOUND_MAX_REDIRECTS=20\nBEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS=600\n")
        code = ("import json, app\nfrom backend import security as s\n"
                "print(json.dumps([s._DEFAULT_TIMEOUT, s._DEFAULT_MAX_BYTES, s._DEFAULT_MAX_REDIRECTS, s._DEFAULT_TOTAL_TIMEOUT]))\n")
        self.assertEqual(self._boot(saved, code)[0], [300.0, 209715200, 20, 600.0])

    def test_import_time_settings_report_restart(self):
        for name in ("BEETS_OUTBOUND_TIMEOUT_SECONDS", "BEETS_OUTBOUND_MAX_REDIRECTS", "BEETS_OUTBOUND_MAX_RESPONSE_BYTES",
                     "BEETS_OUTBOUND_TOTAL_TIMEOUT_SECONDS", "BEETS_LONG_OPERATION_MAX_SECONDS",
                     "BEETS_LONG_OPERATION_POLL_SECONDS", "SPOTIFLAC_SERVICES", "BEETS_ARTIST_RECONCILE_TIMEOUT_SECONDS",
                     "DISCOGS_TOKEN", "SLSKD_API_KEY"):
            self.assertEqual(cl.apply_mode(name), cl.APPLY_RESTART, name)

    def test_retired_key_is_removed_from_the_saved_file_at_boot(self):
        code = ("import json, os, app\n"
                "print(json.dumps(open(os.environ['SETUP_ENV_FILE'], encoding='utf-8').read()))\n")
        text, _ = self._boot("BEETS_VERSION_PROBE_TIMEOUT_SECONDS=45\nAI_MODEL=fake-model\n", code)
        self.assertNotIn("BEETS_VERSION_PROBE_TIMEOUT_SECONDS", text)
        self.assertIn("AI_MODEL=fake-model", text)

    def test_no_connection_before_the_urllib_patch_and_no_captured_urlopen(self):
        code = (
            "import json, sys, types, urllib.request\n"
            "original = urllib.request.urlopen\n"
            "early = []\n"
            "def hook(event, args):\n"
            "    if event in ('socket.connect', 'socket.getaddrinfo') and urllib.request.urlopen is original:\n"
            "        early.append(event)\n"
            "sys.addaudithook(hook)\n"
            "import app\n"
            "patched = urllib.request.urlopen is not original\n"
            "holders = sorted(\n"
            "    f'{name}.{attr}' for name, mod in list(sys.modules.items())\n"
            "    if isinstance(mod, types.ModuleType) and name != '__main__'\n"  # this probe's own reference
            "    for attr, value in list(vars(mod).items())\n"
            "    if value is original and not (name == 'urllib.request' and attr.startswith('_beets_'))\n"
            ")\n"
            "print(json.dumps([patched, early, holders]))\n"
        )
        (patched, early, holders), _ = self._boot("", code)
        self.assertTrue(patched)
        self.assertEqual(early, [], "connected before install_secure_urllib")
        self.assertEqual(holders, [], "these keep the unpatched urlopen")


class ExampleKeyDefaultTests(unittest.TestCase):
    """A key in .env.example with no metadata row reports the example's value
    as its default (routes_setup._env_catalog), so that value must be the reader's."""

    READERS = {}
    EXEMPT = {}

    def test_keys_without_a_row_report_their_readers_default(self):
        rows = SettingMetadataDefaultTests._metadata_defaults()
        values = dict(re.findall(r"(?m)^([A-Z_][A-Z0-9_]*)=(.*)$", (ROOT / ".env.example").read_text(encoding="utf-8")))
        no_row = set(values) - set(rows)
        self.assertEqual(sorted(no_row - set(self.READERS) - set(self.EXEMPT)), [],
                         "add a metadata row, name the reader in READERS, or justify it in EXEMPT")
        self.assertEqual(sorted((set(self.READERS) | set(self.EXEMPT)) - no_row), [], "stale entries")
        for name, path in self.READERS.items():
            self.assertEqual(values[name], SettingMetadataDefaultTests._reader_default(path, name), name)


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
