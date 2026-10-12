"""0.4.1 security follow-ups: #350, #239 F-3, #351 N1/N2, #183 residuals,
#324 N-2/N-3. Each test fails on integration/0.4.1 @ dfff31b."""

import hashlib
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import job_engine
from backend import config_layers as cl
from backend import config_manager as cm
from backend.beets_plugins import _mask_url_userinfo
from backend.provider_boundary import redact
from backend.security import redact_url_for_log

SECRET = "S3CR3TVALUE"
POSIX = os.name == "posix"
_DATA_DIR = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def setUpModule():
    # The first import of app/app_runtime fixes the data dir; never the real default.
    with mock.patch.dict(os.environ, {"WEB_MANAGER_DATA_DIR": _DATA_DIR.name}):
        import app  # noqa: F401
        import routes_jobs  # noqa: F401


class UrlRedactionTests(unittest.TestCase):
    def test_log_url_never_keeps_the_path(self):
        """#350: a token in the path (webhook, signed URL) never reaches a log."""
        for url in (f"https://hooks.example.com/hooks/{SECRET}?q=1#f",
                    f"http://u:1/{SECRET}@host/x",  # #239 F-3: parsed as port 1 + path
                    f"https://user:pw@cdn.example.com:8443/sig/{SECRET}/a.jpg"):
            out = redact_url_for_log(url)
            self.assertNotIn(SECRET, out, url)
            self.assertNotIn("pw", out, url)
        self.assertEqual(redact_url_for_log("https://cdn.example.com:8443/a/b"),
                         "https://cdn.example.com:8443/[path " + hashlib.sha256(b"/a/b").hexdigest()[:8] + "]")
        self.assertEqual(redact_url_for_log("https://example.com/"), "https://example.com")

    def test_raw_slash_in_password_is_masked_everywhere(self):
        """#239 F-3: ``http://u:p/SECRET@host`` -- urlsplit ends userinfo at "/"."""
        url = f"http://u:p/{SECRET}@host/x"
        self.assertNotIn(SECRET, _mask_url_userinfo(f"url: {url}"))
        self.assertNotIn(SECRET, str(job_engine.redact_for_persistence(url)))
        self.assertIn(f"p/{SECRET}", cl.config_secret_values(f"plex:\n  url: {url}\n"))
        from backend.config_service import _redact_config_content
        self.assertNotIn(SECRET, _redact_config_content(f"plex:\n  url: {url}\n"))

    def test_persistence_redacts_user_only_and_at_sign_passwords(self):
        """#351 N2: ``https://TOKEN@host`` and ``user:p@ss@host``."""
        for text in (f"GET https://{SECRET}@api.example.com/x failed",
                     f"http://user:pa@{SECRET}@host:8080/x",
                     f"see 'https://u:{SECRET}@h/x' and http://other/y"):
            out = job_engine.redact_for_persistence({"log": [text]})
            self.assertNotIn(SECRET, str(out), text)
            self.assertIn("[REDACTED]@", out["log"][0])
        self.assertEqual(job_engine.redact_for_persistence("http://other/y"), "http://other/y")

    def test_persisted_result_keeps_non_secret_keys(self):
        """#351 N2: ``fingerprint``/``client`` are data, not secrets, as dict keys."""
        value = {"fingerprint": "AQADtEmUaEkS", "client": "web_safari", "api_key": "k3y"}
        self.assertEqual(redact(value), {"fingerprint": "AQADtEmUaEkS", "client": "web_safari",
                                         "api_key": "[REDACTED]"})
        self.assertNotIn(SECRET, redact(f"GET /v2/lookup?client={SECRET}&fingerprint=AQAD"))
        with tempfile.TemporaryDirectory() as d:
            store = job_engine.JobStore(d)
            job = store.start_python(lambda log: {"fingerprint": "AQADtEmUaEkS"}, label="fp")
            deadline = time.time() + 5
            while job.status == "running" and time.time() < deadline:
                time.sleep(0.01)
            store.close()
            self.assertIn("AQADtEmUaEkS", (Path(d) / f"{job.job_id}.json").read_text(encoding="utf-8"))


class JobFingerprintTests(unittest.TestCase):
    def test_fingerprint_is_computed_outside_the_store_lock(self):
        """#351 N1: hashing a large upload must not stall other job starts."""
        store = job_engine.JobStore()
        held = []

        def fingerprint():
            free = []
            t = threading.Thread(target=lambda: free.append(store._lock.acquire(timeout=0) and
                                                            (store._lock.release() or True)))
            t.start()
            t.join()
            held.append(not free[0])
            return "fp"
        token = job_engine.enter_start_scope(fingerprint)
        try:
            job = store.start_python(lambda log: None, label="x")
        finally:
            job_engine.exit_start_scope(token)
        self.assertEqual(held, [False])
        self.assertEqual(job.start_keys["fingerprint"], "fp")


class JobFingerprintHmacTests(unittest.TestCase):
    def test_fingerprint_is_keyed_per_install(self):
        """#351 N1: not a bare sha256 an offline guesser can recompute."""
        import routes_jobs
        from app import app
        body = b'{"password": "hunter2"}'

        def fp():
            with app.test_request_context("/api/x?a=1", method="POST", data=body,
                                          content_type="application/json"):
                return routes_jobs._request_fingerprint()
        plain = hashlib.sha256()
        for part in ("POST", "/api/x", "a=1"):
            plain.update(part.encode() + b"\0")
        plain.update(body)
        first = fp()
        self.assertNotEqual(first, plain.hexdigest())
        self.assertEqual(first, fp())  # stable for one install
        with mock.patch.dict(app.config, {"SECRET_KEY": "another-install-key-0123456789"}):
            self.assertNotEqual(fp(), first)


class PrivateFileReplaceTests(unittest.TestCase):
    def test_concurrent_replace_never_fails(self):
        """#183: a fixed ``<name>.tmp`` let one save unlink another's temp file."""
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("A=0\n", encoding="utf-8")
            real_replace = os.replace
            nested = []

            def interleaved(src, dst, *a, **k):
                if not nested:  # a second save runs between this one's write and rename
                    nested.append(1)
                    cl.replace_private_file(env, "A=2\n")
                return real_replace(src, dst, *a, **k)
            with mock.patch("os.replace", interleaved):
                cl.replace_private_file(env, "A=1\n")
            self.assertEqual(env.read_text(encoding="utf-8"), "A=1\n")
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), [".env"])
            if POSIX:
                self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)


class SetupEnvPathTests(unittest.TestCase):
    def test_setup_env_returns_the_file_name_only(self):
        """#183 S-8 residual: no container path in GET /api/setup/env."""
        try:
            from tests.test_routes_setup import _load_routes_setup_against_stub_app
        except ImportError:  # pragma: no cover
            from test_routes_setup import _load_routes_setup_against_stub_app
        flask_app, module = _load_routes_setup_against_stub_app(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "private-data-dir"
        root.mkdir()
        (Path(tmp.name) / ".env.example").write_text("PLEX_URL=\n", encoding="utf-8")
        module._SETUP_ENV_FILE = root / ".env"
        module._ENV_EXAMPLE_FILE = Path(tmp.name) / ".env.example"
        resp = flask_app.test_client().get("/api/setup/env")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["env_file"], ".env")
        self.assertNotIn(str(module._SETUP_ENV_FILE), resp.get_data(as_text=True))


@unittest.skipUnless(POSIX, "BEETSDIR containment uses absolute POSIX paths")
class DiscogsTokenConfigPathTests(unittest.TestCase):
    def test_token_is_read_only_from_a_contained_config(self):
        """#239 note / #183: the Discogs token read honours BEETSDIR containment."""
        from backend import artwork_service
        with tempfile.TemporaryDirectory() as beetsdir, tempfile.TemporaryDirectory() as outside:
            for folder in (beetsdir, outside):
                Path(folder, "config.yaml").write_text(f"discogs:\n  user_token: {SECRET}\n", encoding="utf-8")
            env = {"BEETSDIR": beetsdir, "BEETS_CONFIG": os.path.join(outside, "config.yaml")}
            with mock.patch.dict(os.environ, env):
                self.assertEqual(artwork_service._beets_config_discogs_token(), "")
            env["BEETS_CONFIG"] = os.path.join(beetsdir, "config.yaml")
            with mock.patch.dict(os.environ, env):
                self.assertEqual(artwork_service._beets_config_discogs_token(), SECRET)


class ConfigBackupPruneTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.cfg = self.dir / "config.yaml"
        self.cfg.write_text("library: /config/l.blb\n", encoding="utf-8")

    def test_timestamped_backups_are_pruned(self):
        """#324 N-2: every save left another copy of the secrets, forever."""
        for n in range(cm.KEEP_TIMESTAMPED_BACKUPS + 5):
            (self.dir / f"config.yaml.bak.{1000 + n}").write_text("old\n", encoding="utf-8")
        (self.dir / "config.yaml.bak.1000-1").write_text("old\n", encoding="utf-8")
        (self.dir / "config.yaml.bak.notes").write_text("keep\n", encoding="utf-8")
        cm.save_config("library: /config/new.blb\n", config_path=self.cfg)
        stamped = sorted(p.name for p in self.dir.iterdir() if p.name.startswith("config.yaml.bak.")
                         and p.name != "config.yaml.bak.notes")
        self.assertEqual(len(stamped), cm.KEEP_TIMESTAMPED_BACKUPS)
        self.assertNotIn("config.yaml.bak.1000", stamped)
        self.assertNotIn("config.yaml.bak.1000-1", stamped)
        self.assertTrue((self.dir / "config.yaml.bak.notes").exists())
        self.assertTrue((self.dir / "config.yaml.bak").exists())  # the revert source stays
        newest = max(stamped, key=lambda n: int(n.rsplit(".", 1)[1].split("-")[0]))
        self.assertEqual((self.dir / newest).read_text(encoding="utf-8"), "library: /config/l.blb\n")

    def test_crlf_file_keeps_crlf(self):
        """#324 N-3: saving no longer rewrites a CRLF file to LF."""
        self.cfg.write_bytes(b"library: /config/l.blb\r\ndirectory: /music\r\n")
        current = cm.get_config(self.cfg)
        self.assertEqual(current["content"], "library: /config/l.blb\ndirectory: /music\n")
        res = cm.save_config("library: /config/x.blb\ndirectory: /music\n",
                             expected_revision=current["revision"], config_path=self.cfg)
        self.assertEqual(self.cfg.read_bytes(), b"library: /config/x.blb\r\ndirectory: /music\r\n")
        self.assertEqual(res["revision"], cm.get_config(self.cfg)["revision"])


if __name__ == "__main__":
    unittest.main()
