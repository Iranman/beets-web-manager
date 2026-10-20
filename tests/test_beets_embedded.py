"""In-process Beets engine (docs/adr/0006-embedded-beets.md, Phase 1).

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
if scenario == "version_mismatch":
    import beets
    beets.__version__ = "0.0.0"
from backend import beets_embedded
from backend.beets_adapter import BeetsAdapter, BeetsAdapterError

def no_network(*a, **k):
    raise AssertionError("the embedded engine opened a socket")
socket.socket = no_network

a = BeetsAdapter()
out = {"status": beets_embedded.status(), "public": beets_embedded.public_status()}
if scenario != "happy":
    try:
        a.get_stats()
        out["stats_error"] = None
    except BeetsAdapterError as ex:
        out["stats_error"] = [type(ex).__name__, ex.error_code, str(ex)]
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
        shutil.rmtree(self.td, ignore_errors=True)

    def _write_config(self, plugins="[]", extra=""):
        with open(os.path.join(self.config, "config.yaml"), "w", encoding="utf-8") as f:
            f.write(
                f"directory: {self.music}\n"
                f"library: {self.dbpath}\n"
                f"plugins: {plugins}\n"
                "webmanager:\n"
                f"  import_roots: [{self.downloads}]\n"
                f"  allowed_roots: [{self.music}, {self.downloads}]\n"
                + extra
            )

    def _precreate_library(self):
        import sqlite3
        sqlite3.connect(self.dbpath).close()  # an existing (empty) file; Beets creates the schema

    def _run(self, scenario, path_env=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BEETS_", "BEETSDIR"))}
        env.update({
            "BEETSDIR": self.config, "BEETS_ENGINE": "embedded", "HOME": self.td,
            "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
            "WEB_MANAGER_DATA_DIR": os.path.join(self.td, "wm"),
            "BEETS_TRANSACTION_DIR": os.path.join(self.td, "wm", "transactions"),
            "MUSIC_ROOT": self.music, "DOWNLOADS_ROOT": self.downloads,
        })
        if path_env is not None:
            env["PATH"] = path_env
        proc = subprocess.run([sys.executable, "-c", DRIVER, scenario, self.src], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-4000:])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    @staticmethod
    def _failed(status):
        return {c["code"]: c["severity"] for c in status["checks"] if not c["ok"]}

    def test_reads_import_modify_and_stream_in_process_without_network(self):
        self._write_config()
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
            self.assertIn("plugins: []", f.read())

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

    def test_version_mismatch_is_reported(self):
        self._write_config()
        self._precreate_library()
        out = self._run("version_mismatch")
        self.assertEqual(self._failed(out["status"]).get("beets_version_mismatch"), "warning")

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

    def test_engine_is_opt_in_during_migration(self):
        from backend import beets_embedded
        old = os.environ.pop("BEETS_ENGINE", None)
        try:
            self.assertFalse(beets_embedded.enabled())
            self.assertEqual(beets_embedded.status(), {"engine": "http", "ok": True, "checks": []})
        finally:
            if old is not None:
                os.environ["BEETS_ENGINE"] = old

    def test_pin_matches_requirements(self):
        from backend.beets_embedded import PINNED_BEETS_VERSION
        for req in ("requirements.txt", "requirements-dev.txt"):
            text = (ROOT / req).read_text(encoding="utf-8")
            if req == "requirements-dev.txt" and "beets==" not in text:
                continue
            self.assertIn(f"beets=={PINNED_BEETS_VERSION}\n", text, req)


if __name__ == "__main__":
    unittest.main()
