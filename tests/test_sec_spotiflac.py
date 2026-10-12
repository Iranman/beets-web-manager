"""#340: SPOTIFLAC_SERVICES is validated on save and at argv build.
#274: SpotiFLAC output is only counted when it is a regular file inside the
download folder; escaping symlinks are removed (the link, never its target).
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.ytdlp_service as ys
from backend import config_layers as cl
from backend.security import SPOTIFLAC_SERVICES_INVALID_MESSAGE, parse_spotiflac_services

try:
    from test_routes_setup import _load_routes_setup_against_stub_app
except ImportError:  # pragma: no cover
    from tests.test_routes_setup import _load_routes_setup_against_stub_app

BAD_VALUES = ("tidal,--output,/etc", "-o", "tidal --config /tmp/x", "ext:", "ext:../x", "spotify",
              "tidal;rm", "/etc/passwd", "ext:-x", "TIDAL,--help")


class ParseTests(unittest.TestCase):
    def test_known_aliases_and_extensions_pass(self):
        self.assertEqual(parse_spotiflac_services("Tidal, qobuz deezer,tidal,ext:tidal-web"),
                         ("tidal", "qobuz", "deezer", "ext:tidal-web"))
        self.assertEqual(parse_spotiflac_services(""), ())

    def test_options_paths_and_unknown_names_are_refused_without_echo(self):
        for value in BAD_VALUES:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    parse_spotiflac_services(value)
                self.assertEqual(str(ctx.exception), SPOTIFLAC_SERVICES_INVALID_MESSAGE)


class ArgvTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dest = os.path.join(tmp.name, "dl")
        self.outside = os.path.join(tmp.name, "outside")
        os.makedirs(self.outside)
        for p in (mock.patch.object(ys, "_spotiflac_command", return_value=["spotiflac"]),):
            p.start()
            self.addCleanup(p.stop)

    def run_with(self, services, writer=lambda dest: None):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            writer(cmd[2])
            return subprocess.CompletedProcess(cmd, 0, "", "")

        log = []
        with mock.patch.object(ys, "_SPOTIFLAC_SERVICES", services), \
                mock.patch.object(ys.subprocess, "run", side_effect=fake_run):
            n = ys._spotiflac_download_url("https://soundcloud.com/a/b", self.dest, log)
        return n, calls, log

    def test_injected_option_never_reaches_argv(self):
        for value in BAD_VALUES:
            with self.subTest(value=value), mock.patch.object(ys.subprocess, "run") as run:
                with mock.patch.object(ys, "_SPOTIFLAC_SERVICES", value):
                    with self.assertRaises(RuntimeError) as ctx:
                        ys._spotiflac_download_url("https://soundcloud.com/a/b", self.dest, [])
                run.assert_not_called()
                self.assertEqual(str(ctx.exception), SPOTIFLAC_SERVICES_INVALID_MESSAGE)

    def test_valid_services_follow_service_flag(self):
        _, calls, _ = self.run_with("qobuz,ext:tidal-web")
        cmd = calls[0]
        self.assertEqual(cmd[:3], ["spotiflac", "https://soundcloud.com/a/b", self.dest])
        i = cmd.index("--service")
        self.assertEqual(cmd[i + 1:i + 3], ["qobuz", "ext:tidal-web"])
        self.assertEqual(cmd[i + 3], "--filename-format")

    def test_status_reports_invalid_value_instead_of_raising(self):
        with mock.patch.object(ys, "_SPOTIFLAC_SERVICES", "--output"):
            self.assertEqual(ys._spotiflac_services_status(),
                             {"services": [], "services_error": SPOTIFLAC_SERVICES_INVALID_MESSAGE})

    def test_regular_output_inside_the_folder_counts(self):
        n, _, _ = self.run_with("tidal", lambda d: Path(d, "Song - Artist.flac").write_bytes(b"x"))
        self.assertEqual(n, 1)

    @unittest.skipIf(os.name == "nt", "symlinks")
    def test_symlink_out_of_the_folder_is_removed_and_not_counted(self):
        target = Path(self.outside, "secret.flac")
        target.write_bytes(b"keep")
        n, _, log = self.run_with("tidal", lambda d: os.symlink(target, os.path.join(d, "x.flac")))
        self.assertEqual(n, 0)
        self.assertFalse(os.path.lexists(os.path.join(self.dest, "x.flac")))
        self.assertEqual(target.read_bytes(), b"keep")
        self.assertTrue(any("Removed a symlink" in line for line in log))

    @unittest.skipIf(os.name == "nt", "symlinks")
    def test_file_through_symlinked_folder_removes_link_not_target(self):
        def writer(d):
            os.symlink(self.outside, os.path.join(d, "sub"))
            Path(self.outside, "t.flac").write_bytes(b"keep")

        n, _, _ = self.run_with("tidal", writer)
        self.assertEqual(n, 0)
        self.assertFalse(os.path.lexists(os.path.join(self.dest, "sub")))
        self.assertEqual(Path(self.outside, "t.flac").read_bytes(), b"keep")


class SaveValidationTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ.pop("SPOTIFLAC_SERVICES", None)
        for p in (mock.patch.object(self.module, "_setup_csrf_failure", return_value=None),
                  mock.patch.object(cl, "_BOOT_ENV_PINNED", frozenset())):
            p.start()
            self.addCleanup(p.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env_file = Path(tmp.name) / ".env"
        self.env_file.write_text("", encoding="utf-8")
        self.module._SETUP_ENV_FILE = self.env_file
        self.module._ENV_EXAMPLE_FILE = Path(tmp.name) / ".env.example"

    def save(self, value):
        return self.client.post("/api/setup/env", json={"variables": {"SPOTIFLAC_SERVICES": value}})

    def test_injection_is_refused_and_not_written(self):
        for value in BAD_VALUES:
            with self.subTest(value=value):
                resp = self.save(value)
                self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True))
                self.assertEqual(resp.get_json()["error"], SPOTIFLAC_SERVICES_INVALID_MESSAGE)
                self.assertNotIn("SPOTIFLAC_SERVICES", self.env_file.read_text(encoding="utf-8"))
                self.assertNotEqual(os.environ.get("SPOTIFLAC_SERVICES"), value)

    def test_valid_value_and_the_documented_default_save(self):
        default = self.module._SETTING_METADATA["SPOTIFLAC_SERVICES"]["default"]
        parse_spotiflac_services(default)
        self.assertEqual(self.save("qobuz,ext:tidal-web").status_code, 200)
        self.assertIn("SPOTIFLAC_SERVICES=qobuz,ext:tidal-web", self.env_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
