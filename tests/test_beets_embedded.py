"""In-process Beets engine (docs/adr/0006-embedded-beets.md).

Each scenario runs in a fresh interpreter: Beets' config, plugin registry and
the stock web app are process-global, so a shared test process would leak
state between scenarios. Everything runs against a temporary BEETSDIR,
library and music folder; no live data, no network (the happy path runs
with ``socket.socket`` disabled).
"""

import ast
import json
import os
import shutil
import site
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DRIVER = r'''
import json, os, socket, sys
scenario, src = sys.argv[1], sys.argv[2]
if scenario in ("version_mismatch", "readiness_broken"):
    import beets
    beets.__version__ = "0.0.0"
if scenario == "shadowed":  # a stale copy that resolved before the image's own
    import types
    stale = types.ModuleType("beetsplug.discpath")
    stale.__file__ = os.path.join(os.environ["HOME"], "stale", "beetsplug", "discpath.py")
    sys.modules["beetsplug.discpath"] = stale
from backend import beets_embedded
from backend.beets_adapter import BeetsAdapter, BeetsAdapterError
# The live second-writer probes see the whole test container (other tests run
# `beet` and listeners in parallel); each scenario sets what they find.
beets_embedded._separate_beets_address = lambda: "beets:8337" if scenario == "writer_service" else ""
beets_embedded._beet_commands = lambda: [4242] if scenario == "writer_beet" else []

def no_network(*a, **k):
    raise AssertionError("the embedded engine opened a socket")
if scenario not in ("readiness", "readiness_broken"):
    socket.socket.connect = socket.socket.connect_ex = no_network

a = BeetsAdapter()

# Readiness probe end to end through the real app and the real embedded
# engine (no mocking of _beets_plugin_diagnostics): a fresh config-less
# install must be ready; a genuinely broken engine must not.
if scenario in ("readiness", "readiness_broken"):
    from app import app as flask_app
    client = flask_app.test_client()
    r = client.get("/health/ready")
    print(json.dumps({"status_code": r.status_code, "body": r.get_json()}, default=str))
    sys.exit(0)

out = {"status": beets_embedded.status(), "public": beets_embedded.public_status()}
if scenario != "happy":
    try:
        a.get_stats()
        out["stats_error"] = None
    except BeetsAdapterError as ex:
        out["stats_error"] = [type(ex).__name__, ex.error_code, str(ex)]
    try:
        a.modify({"title": "x"}, item_ids=[1])
        out["mutate_error"] = None
    except BeetsAdapterError as ex:
        out["mutate_error"] = [type(ex).__name__, ex.error_code, str(ex), ex.status_code]
    print(json.dumps(out, default=str))
    sys.exit(0)

out["stats0"] = a.get_stats()
out["import"] = a.run_import(paths=[src], autotag=False, copy=True, move=False)
out["albums"] = a.get_albums()
items = a.get_items()
out["items"] = items
out["fields"] = a.get_fields("item", ["title", "path"])
iid = items[0]["id"]
out["modify"] = a.modify({"title": "Renamed"}, item_ids=[iid], write=True)
out["item_after"] = a.get_item(iid)
fh = a.open_item_file(iid)
data = fh.read(1000) + fh.read()
fh.close()
with open(items[0]["path"], "rb") as f:
    out["file_matches"] = data == f.read()
try:
    a.modify({"no_such_field": "x"}, item_ids=[iid])
    out["bad_modify"] = None
except BeetsAdapterError as ex:
    out["bad_modify"] = [type(ex).__name__, ex.status_code, ex.error_code]
out["missing_item"] = a.get_item(999999)
out["plugin_status"] = a.get_plugin_status()
import beetsplug.discpath
out["discpath_file"] = beetsplug.discpath.__file__

# A transaction-engine mutation end to end: album rename (plan -> approve ->
# apply through Beets' Album.move()) and its rollback.
from backend import composite_workflows as cw
from backend.transaction_engine import TransactionStore
store = TransactionStore(os.environ["BEETS_TRANSACTION_DIR"])
aid = out["albums"][0]["id"]
a.modify({"album": "Renamed Album"}, album_ids=[aid], write=False, move=False)
before = sorted(i["path"] for i in a.get_items())
reloc = cw.relocate_album(aid, "rename", adapter=a, store=store)
out["relocate"] = reloc
out["paths_moved"] = sorted(i["path"] for i in a.get_items())
out["tx_status"] = store.get(reloc["operation_id"])["status"] if reloc.get("operation_id") else None
out["rollback"] = cw.rollback_album_relocation(reloc["operation_id"], adapter=a, store=store)
out["paths_before"] = before
out["paths_restored"] = sorted(i["path"] for i in a.get_items())
print(json.dumps(out, default=str))
'''


def _wav(path, artist, album, title, n):
    import mutagen.id3
    import mutagen.wave
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<h", 0) * 8000)
    mw = mutagen.wave.WAVE(path)
    mw.add_tags()
    for frame in (mutagen.id3.TIT2(encoding=3, text=[title]), mutagen.id3.TPE1(encoding=3, text=[artist]),
                  mutagen.id3.TPE2(encoding=3, text=[artist]), mutagen.id3.TALB(encoding=3, text=[album]),
                  mutagen.id3.TRCK(encoding=3, text=[str(n)])):
        mw.tags.add(frame)
    mw.save()


class EmbeddedBeetsScenarios(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.config = os.path.join(self.td, "config")
        self.music = os.path.join(self.td, "music")
        self.downloads = os.path.join(self.td, "downloads")
        for d in (self.config, self.music, self.downloads):
            os.makedirs(d)
        self.src = os.path.join(self.downloads, "Artist - Album")
        os.makedirs(self.src)
        for n in (1, 2):
            _wav(os.path.join(self.src, f"0{n}.wav"), "Artist", "Album", f"Track {n}", n)
        self.dbpath = os.path.join(self.config, "musiclibrary.blb")

    def tearDown(self):
        os.chmod(self.music, 0o755)
        if os.path.exists(self.dbpath):
            os.chmod(self.dbpath, 0o644)
        shutil.rmtree(self.td, ignore_errors=True)

    def _write_config(self, plugins="[]", extra="", library=None):
        with open(os.path.join(self.config, "config.yaml"), "w", encoding="utf-8") as f:
            f.write(
                f"directory: {self.music}\n"
                f"library: {library or self.dbpath}\n"
                f"plugins: {plugins}\n"
                "webmanager:\n"
                f"  import_roots: [{self.downloads}]\n"
                f"  allowed_roots: [{self.music}, {self.downloads}]\n"
                + extra
            )

    def _precreate_library(self):
        import sqlite3
        sqlite3.connect(self.dbpath).close()  # an existing (empty) file; Beets creates the schema

    def _run(self, scenario, path_env=None, extra_env=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BEETS_", "BEETSDIR"))}
        env.update({
            "BEETSDIR": self.config, "HOME": self.td,
            "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
            "WEB_MANAGER_DATA_DIR": os.path.join(self.td, "wm"),
            "BEETS_TRANSACTION_DIR": os.path.join(self.td, "wm", "transactions"),
            "MUSIC_ROOT": self.music, "DOWNLOADS_ROOT": self.downloads,
            # HOME is replaced, so keep the user site-packages (pip --user) visible.
            "PYTHONUSERBASE": os.environ.get("PYTHONUSERBASE") or site.getuserbase(),
        })
        if path_env is not None:
            env["PATH"] = path_env
        env.update(extra_env or {})
        proc = subprocess.run([sys.executable, "-c", DRIVER, scenario, self.src], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-4000:])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    @staticmethod
    def _failed(status):
        return {c["code"]: c["severity"] for c in status["checks"] if not c["ok"]}

    def test_reads_import_modify_and_stream_in_process_without_network(self):
        stale = os.path.join(self.td, "stale")
        os.makedirs(os.path.join(stale, "webmanager"))
        for rel in ("discpath.py", os.path.join("webmanager", "__init__.py")):
            with open(os.path.join(stale, rel), "w", encoding="utf-8") as f:
                f.write("raise RuntimeError('stale copy from pluginpath was imported')\n")
        self._write_config(plugins="[discpath]", extra=f"pluginpath: [{stale}]\n")
        self._precreate_library()
        out = self._run("happy")
        status = out["status"]
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["engine"], "embedded")
        self.assertIn("webmanager", status["loaded_plugins"])
        self.assertNotIn("library_created", self._failed(status))
        self.assertEqual(out["stats0"]["items"], 0)

        self.assertEqual(len(out["albums"]), 1, out["import"])
        self.assertEqual(out["albums"][0]["album"], "Album")
        self.assertEqual(len(out["items"]), 2)
        for item in out["items"]:
            self.assertTrue(item["path"].startswith(self.music + os.sep), item["path"])
            self.assertTrue(os.path.isfile(item["path"]))
        self.assertEqual({r["title"] for r in out["fields"]}, {"Track 1", "Track 2"})
        self.assertEqual(out["item_after"]["title"], "Renamed")
        self.assertTrue(out["file_matches"])
        self.assertEqual(out["bad_modify"][:2], ["BeetsAdapterBadRequestError", 400])
        self.assertIsNone(out["missing_item"])
        self.assertEqual(out["plugin_status"]["library_directory"], self.music)
        # The image's own discpath (and webmanager) won over the stale pluginpath copies.
        self.assertIn("discpath", status["loaded_plugins"])
        self.assertEqual(os.path.realpath(out["discpath_file"]),
                         os.path.realpath(ROOT / "beetsplug" / "discpath.py"))
        self.assertTrue(out["plugin_status"]["web_include_paths"])
        # Transaction-engine relocation and rollback through the in-process engine.
        self.assertTrue(out["relocate"]["ok"], out["relocate"])
        self.assertEqual(out["tx_status"], "Completed")
        self.assertNotEqual(out["paths_moved"], out["paths_before"])
        self.assertTrue(all("Renamed Album" in p for p in out["paths_moved"]), out["paths_moved"])
        self.assertTrue(out["rollback"].get("ok"), out["rollback"])
        self.assertEqual(out["paths_restored"], out["paths_before"])
        # Copy import: the source stays in place.
        self.assertEqual(sorted(os.listdir(self.src)), ["01.wav", "02.wav"])
        # The user's config.yaml is never rewritten by the engine.
        with open(os.path.join(self.config, "config.yaml"), encoding="utf-8") as f:
            self.assertIn("plugins: [discpath]", f.read())

    def test_new_library_is_reported(self):
        self._write_config()
        out = self._run("new_library")
        self.assertTrue(out["status"]["ok"])
        self.assertEqual(self._failed(out["status"]).get("library_created"), "warning")
        self.assertIsNone(out["stats_error"])

    def test_invalid_config_fails_with_code_and_adapter_reports_it(self):
        with open(os.path.join(self.config, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("plugins: [unclosed\n")
        out = self._run("invalid_config")
        self.assertFalse(out["status"]["ok"])
        self.assertIn("config_invalid", self._failed(out["status"]))
        self.assertEqual(out["stats_error"][:2], ["BeetsAdapterConnectionError", "BEETS_EMBEDDED_UNAVAILABLE"])
        self.assertIn("config", out["stats_error"][2])

    def test_unloadable_plugin_is_named(self):
        self._write_config(plugins="[doesnotexist]")
        self._precreate_library()
        out = self._run("bad_plugin")
        self.assertFalse(out["status"]["ok"])
        self.assertEqual({k for k, v in self._failed(out["status"]).items() if v == "error"}, {"plugins_failed"})
        msg = next(c["message"] for c in out["status"]["checks"] if c["code"] == "plugins_failed")
        self.assertIn("doesnotexist", msg)
        # Degraded: reads continue, mutations are refused with the self-test message.
        self.assertIsNone(out["stats_error"])
        self.assertEqual(out["mutate_error"][1], "BEETS_EMBEDDED_DEGRADED")
        self.assertEqual(out["mutate_error"][3], 503)
        self.assertIn("doesnotexist", out["mutate_error"][2])

    def test_healthy_engine_does_not_refuse_mutations(self):
        self._write_config()
        self._precreate_library()
        out = self._run("new_library")
        self.assertNotEqual((out["mutate_error"] or [None, None])[1], "BEETS_EMBEDDED_DEGRADED")

    def test_shadowed_bundled_plugin_fails_closed(self):
        self._write_config()
        self._precreate_library()
        out = self._run("shadowed")
        self.assertFalse(out["status"]["ok"])
        self.assertEqual(self._failed(out["status"]).get("operations_shadowed"), "error")
        self.assertEqual(out["stats_error"][1], "BEETS_EMBEDDED_UNAVAILABLE")
        self.assertIn("discpath", out["stats_error"][2])

    def test_shipped_example_config_loads_every_plugin(self):
        # config.yaml.example is what the install docs tell users to start
        # from; every plugin it lists must load inside Web Manager.
        text = (ROOT / "config.yaml.example").read_text(encoding="utf-8")
        for container, local in (("/config", self.config), ("/music", self.music),
                                 ("/downloads", self.downloads), ("/web-manager-data", self.td)):
            text = text.replace(container + "/", local + "/").replace(container + "\n", local + "\n")
        with open(os.path.join(self.config, "config.yaml"), "w", encoding="utf-8") as f:
            f.write(text)
        out = self._run("new_library")
        failed = self._failed(out["status"])
        self.assertNotIn("plugins_failed", failed, out["status"])
        self.assertNotIn("config_invalid", failed)
        self.assertNotIn("library_dir_mismatch", failed)
        import yaml
        listed = set(yaml.safe_load(text)["plugins"].split())
        self.assertTrue(listed <= set(out["status"]["loaded_plugins"]), listed - set(out["status"]["loaded_plugins"]))

    def test_fresh_install_without_config_uses_the_stock_layout(self):
        # No config.yaml: library beside it, MUSIC_ROOT as `directory:`, as
        # the stock Beets container laid it out; nothing is written.
        out = self._run("no_config")
        failed = self._failed(out["status"])
        self.assertEqual(failed.get("config_missing"), "warning")
        self.assertEqual(failed.get("library_created"), "warning")
        self.assertTrue(out["status"]["ok"], out["status"])
        self.assertEqual(out["status"]["library_path"], self.dbpath)
        self.assertEqual(out["status"]["library_directory"], self.music)
        self.assertFalse(os.path.exists(os.path.join(self.config, "config.yaml")))
        self.assertNotEqual((out["mutate_error"] or [None, None])[1], "BEETS_EMBEDDED_DEGRADED")

    def test_fresh_install_without_config_is_ready(self):
        # Regression (PR #466): a fresh config-less install must answer
        # /health/ready with 200 and no blocking reasons. Driven end to end
        # through the real app and the real embedded engine -- NOT a mocked
        # _beets_plugin_diagnostics (the mock is exactly what hid this).
        # BEETS_CONFIG points inside BEETSDIR (as the shipped compose does:
        # /config/config.yaml in /config); the file itself does not exist.
        cfg = os.path.join(self.config, "config.yaml")
        out = self._run("readiness", extra_env={"BEETS_CONFIG": cfg})
        self.assertEqual(out["status_code"], 200, out["body"])
        self.assertEqual(out["body"]["blocking_reasons"], [], out["body"])
        self.assertEqual(out["body"]["status"], "ready")
        self.assertTrue(out["body"]["beets"]["plugin_loader_ok"], out["body"])

    def test_genuinely_broken_engine_is_not_ready(self):
        # D3 intent preserved: a genuinely broken Beets (version mismatch, so
        # the engine never starts) must FAIL readiness with a 503.
        out = self._run("readiness_broken", extra_env={"BEETS_CONFIG": os.path.join(self.config, "config.yaml")})
        self.assertEqual(out["status_code"], 503, out["body"])
        self.assertIn("Beets engine unavailable", out["body"]["blocking_reasons"], out["body"])

    def test_missing_library_dir_is_an_error(self):
        missing = os.path.join(self.td, "nowhere")
        out = self._run("no_config", extra_env={"MUSIC_ROOT": missing})
        self.assertEqual(self._failed(out["status"]).get("library_dir_missing"), "error")
        self.assertFalse(out["status"]["ok"])

    def test_unopenable_library_fails_with_code(self):
        self._write_config(library=self.music)  # a directory is not a database
        out = self._run("bad_library")
        self.assertIn("library_open_failed", self._failed(out["status"]))
        self.assertEqual(out["stats_error"][1], "BEETS_EMBEDDED_UNAVAILABLE")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file permissions")
    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_read_only_library_db_is_an_error(self):
        self._write_config()
        self._run("new_library")  # Beets creates and migrates the schema first
        os.chmod(self.dbpath, 0o444)
        out = self._run("ro_db")
        self.assertEqual(self._failed(out["status"]).get("library_db_not_writable"), "error")

    def test_missing_fpcalc_is_an_error_when_chroma_is_configured(self):
        self._write_config(plugins="[chroma]")
        self._precreate_library()
        empty_bin = os.path.join(self.td, "bin")
        os.makedirs(empty_bin)
        out = self._run("no_fpcalc", path_env=empty_bin)
        failed = self._failed(out["status"])
        self.assertEqual(failed.get("fpcalc_missing"), "error", out["status"])
        self.assertFalse(out["status"]["ok"])

    def test_missing_fpcalc_is_a_warning_without_chroma(self):
        self._write_config()
        self._precreate_library()
        empty_bin = os.path.join(self.td, "bin")
        os.makedirs(empty_bin)
        out = self._run("no_fpcalc", path_env=empty_bin)
        self.assertEqual(self._failed(out["status"]).get("fpcalc_missing"), "warning")
        self.assertEqual(self._failed(out["status"]).get("ffmpeg_missing"), "warning")
        self.assertTrue(out["status"]["ok"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_read_only_music_dir_is_an_error(self):
        self._write_config()
        self._precreate_library()
        os.chmod(self.music, 0o555)
        out = self._run("ro_music")
        self.assertFalse(out["status"]["ok"])
        self.assertEqual(self._failed(out["status"]).get("library_dir_not_writable"), "error")

    def test_version_mismatch_stops_before_the_library_is_opened(self):
        # #459 N-a: another Beets version could migrate the schema on open.
        self._write_config()
        out = self._run("version_mismatch")
        self.assertEqual(self._failed(out["status"]), {"beets_version_mismatch": "error"})
        self.assertFalse(out["status"]["ok"])
        self.assertEqual(out["stats_error"][1], "BEETS_EMBEDDED_UNAVAILABLE")
        self.assertFalse(os.path.exists(self.dbpath))

    def test_disabled_plugins_cannot_remove_the_required_plugins(self):
        # #459 N-b
        self._write_config(extra="disabled_plugins: [web, webmanager]\n")
        self._precreate_library()
        out = self._run("new_library")
        self.assertTrue(out["status"]["ok"], out["status"])
        self.assertTrue({"web", "webmanager"} <= set(out["status"]["loaded_plugins"]))
        self.assertIsNone(out["stats_error"])

    def test_leftover_two_container_settings_are_a_warning_not_a_stop(self):
        self._write_config()
        self._precreate_library()
        out = self._run("new_library", extra_env={
            "BEETS_ENGINE": "http", "BEETS_WEB_URL": "http://beets:8337",
            "BEETS_WEBMANAGER_API_KEY": "0" * 64})
        self.assertTrue(out["status"]["ok"], out["status"])
        self.assertEqual(self._failed(out["status"]).get("legacy_settings_ignored"), "warning")
        msg = next(c["message"] for c in out["status"]["checks"] if c["code"] == "legacy_settings_ignored")
        self.assertIn("BEETS_ENGINE, BEETS_WEB_URL, BEETS_WEBMANAGER_API_KEY", msg)
        self.assertNotIn("0" * 64, json.dumps(out))
        self.assertIsNone(out["stats_error"])

    def test_music_root_that_is_not_beets_directory_refuses_changes(self):
        self._write_config()
        self._precreate_library()
        out = self._run("dir_mismatch", extra_env={"MUSIC_ROOT": self.downloads})
        self.assertEqual(self._failed(out["status"]).get("library_dir_mismatch"), "error")
        self.assertIsNone(out["stats_error"])
        self.assertEqual(out["mutate_error"][1], "BEETS_EMBEDDED_DEGRADED")

    @unittest.skipIf(os.name == "nt", "flock")
    def test_second_web_manager_on_one_library_refuses_changes(self):
        import fcntl
        self._write_config()
        self._precreate_library()
        fd = os.open(self.dbpath + ".webmanager.lock", os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, b"host other-host, pid 1")
        out = self._run("locked")
        self.assertEqual(self._failed(out["status"]).get("library_locked"), "error")
        msg = next(c["message"] for c in out["status"]["checks"] if c["code"] == "library_locked")
        self.assertIn("host other-host, pid 1", msg)
        self.assertIsNone(out["stats_error"])
        self.assertEqual(out["mutate_error"][1], "BEETS_EMBEDDED_DEGRADED")

    @unittest.skipIf(os.name == "nt", "flock")
    def test_the_lock_is_released_when_the_process_ends(self):
        self._write_config()
        self._precreate_library()
        self.assertNotIn("library_locked", self._failed(self._run("new_library")["status"]))
        self.assertNotIn("library_locked", self._failed(self._run("new_library")["status"]))

    def test_old_beets_service_still_running_refuses_changes(self):
        self._write_config()
        self._precreate_library()
        out = self._run("writer_service")
        self.assertEqual(self._failed(out["status"]).get("separate_beets_running"), "error")
        self.assertIn("separate_beets_running", out["public"]["codes"])
        self.assertIsNone(out["stats_error"])
        self.assertEqual(out["mutate_error"][1:4:2], ["BEETS_EMBEDDED_DEGRADED", 503])
        self.assertIn("beets:8337", out["mutate_error"][2])

    def test_writing_beet_command_refuses_changes(self):
        self._write_config()
        self._precreate_library()
        out = self._run("writer_beet")
        self.assertEqual(self._failed(out["status"]).get("beet_command_running"), "error")
        self.assertEqual(out["mutate_error"][1], "BEETS_EMBEDDED_DEGRADED")
        self.assertIn("4242", out["mutate_error"][2])

    def test_public_status_carries_codes_but_no_paths_or_messages(self):
        self._write_config(plugins="[doesnotexist]")
        out = self._run("bad_plugin")
        public = out["public"]
        self.assertEqual(set(public), {"engine", "ok", "codes"})
        self.assertIn("plugins_failed", public["codes"])
        self.assertNotIn(self.td, json.dumps(public))


class EmbeddedBeetsStructure(unittest.TestCase):
    def test_only_the_engine_imports_beets_library_under_backend(self):
        offenders = []
        for path in sorted((ROOT / "backend").rglob("*.py")):
            if path.name == "beets_embedded.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
                if any(n == "beets.library" or n.startswith("beets.library.") for n in names):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_beet_subcommand_parsing(self):
        from backend.beets_embedded import _beet_subcommand
        cases = {
            ("/usr/bin/beet", "ls", "x"): "ls",
            ("beet", "-c", "/config/x.yaml", "import", "/downloads"): "import",
            ("beet", "-v", "--library", "l.blb", "stats"): "stats",
            ("/usr/bin/python3", "-m", "beets", "modify", "x"): "modify",
            ("/usr/bin/python3", "/usr/bin/beet", "web"): "web",
            ("beet",): "",
            ("python", "app.py"): None,
            ("/usr/bin/beetsplug-thing", "import"): None,
        }
        for argv, want in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(_beet_subcommand(list(argv)), want)

    def test_only_writing_beet_commands_count(self):
        from unittest import mock
        from backend.beets_embedded import _beet_commands
        proc = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, proc, True)
        for pid, argv in {"11": [b"beet", b"ls"], "12": [b"beet", b"import", b"/d"],
                          "13": [b"python3", b"-m", b"beets", b"web"], "14": [b"python3", b"app.py"],
                          "self": [b"beet", b"import"]}.items():
            os.makedirs(os.path.join(proc, pid))
            with open(os.path.join(proc, pid, "cmdline"), "wb") as f:
                f.write(b"\0".join(argv) + b"\0")
        # Pin getpid outside the fake set: in a low-PID container a test
        # worker's real PID can collide with a fake pid dir and be skipped by
        # the (correct) self-skip in _beet_commands.
        with mock.patch("backend.beets_embedded.os.getpid", return_value=999999):
            self.assertEqual(_beet_commands(proc), [12, 13])
            self.assertEqual(_beet_commands(os.path.join(proc, "missing")), [])

    @unittest.skipIf(os.name == "nt", "flock/symlink")
    def test_lock_refuses_a_symlinked_lock_path_without_following_it(self):
        from backend import beets_embedded as be
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        dbpath = os.path.join(d, "musiclibrary.blb")
        target = os.path.join(d, "victim")
        with open(target, "w", encoding="utf-8") as f:
            f.write("precious")
        os.symlink(target, dbpath + be.LOCK_SUFFIX)  # planted symlink at the lock path
        self.addCleanup(setattr, be, "_lock_fd", be._lock_fd)
        be._lock_fd = None
        holder = be._acquire_library_lock(dbpath)
        self.assertIsNotNone(holder)  # fail safe: refuses instead of following
        self.assertEqual(open(target, encoding="utf-8").read(), "precious")  # target untouched
        self.assertIsNone(be._lock_fd)

    def test_old_service_probe_finds_a_listener_and_skips_this_container(self):
        import socket
        from unittest import mock
        from backend import beets_embedded as be
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        port = srv.getsockname()[1]
        with mock.patch.dict(os.environ, {"BEETS_WEB_URL": f"http://u:pw@127.0.0.1:{port}"}):
            with mock.patch.object(be, "_own_addresses", return_value=set()):
                self.assertEqual(be._separate_beets_address(), f"127.0.0.1:{port}")
            self.assertEqual(be._separate_beets_address(), "")  # 127.0.0.1 is this container
        srv.close()
        with mock.patch.dict(os.environ, {"BEETS_WEB_URL": f"http://127.0.0.1:{port}"}), \
                mock.patch.object(be, "_own_addresses", return_value=set()):
            self.assertEqual(be._separate_beets_address(), "")  # nothing listening
        with mock.patch.dict(os.environ, {"BEETS_WEB_URL": "http://no-such-host.invalid:8337"}):
            self.assertEqual(be._separate_beets_address(), "")
        with mock.patch.dict(os.environ, {"BEETS_WEB_URL": "not a url"}):
            self.assertEqual(be._separate_beets_address(), "")

    def test_pin_matches_requirements(self):
        from backend.beets_embedded import PINNED_BEETS_VERSION
        for req in ("requirements.txt", "requirements-dev.txt"):
            text = (ROOT / req).read_text(encoding="utf-8")
            if req == "requirements-dev.txt" and "beets==" not in text:
                continue
            self.assertIn(f"beets=={PINNED_BEETS_VERSION}\n", text, req)


if __name__ == "__main__":
    unittest.main()
