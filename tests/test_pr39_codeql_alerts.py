"""Regression tests for PR #39 CodeQL alert families."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
from io import BytesIO
from pathlib import Path
from unittest import mock

import app as app_module
import routes_submissions
import scripts.validate_compose_security as compose_security
from backend import beets_control_agent as bca
from backend.beets_client import BeetsClient, BeetsError
from backend.beets_control_agent import ControlAgentHandler, UnsafePathError, resolve_safe_path


def _post_agent(path: str, payload: dict):
    handler = ControlAgentHandler.__new__(ControlAgentHandler)
    body = json.dumps(payload).encode("utf-8")
    handler.path = path
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    handler._authenticate = lambda: True
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))
    handler.do_POST()
    return responses[0]


def _patch_agent(path: str, payload: dict):
    handler = ControlAgentHandler.__new__(ControlAgentHandler)
    body = json.dumps(payload).encode("utf-8")
    handler.path = path
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    handler._authenticate = lambda: True
    responses = []
    handler._send_json = lambda code, data: responses.append((code, data))
    handler.do_PATCH()
    return responses[0]


class ControlAgentPathBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        base_name = Path(self.tmp.name).name
        self.base = f"/tmp/{base_name}"
        self.music = f"{self.base}/music"
        self.staging = f"{self.base}/staging"
        self.outside = f"{self.base}/outside"
        Path(self.music).mkdir(parents=True, exist_ok=True)
        Path(self.staging).mkdir(parents=True, exist_ok=True)
        Path(self.outside).mkdir(parents=True, exist_ok=True)
        self.patches = [
            mock.patch.object(bca, "MUSIC_LIBRARY_PATH", self.music),
            mock.patch.object(bca, "DOWNLOAD_PATH", self.staging),
            mock.patch.object(bca, "LOCK_PATH", f"{self.base}/agent.lock"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def test_resolve_safe_path_rejects_traversal_and_prefix_collision(self):
        good = f"{self.music}/Artist/Track.flac"
        Path(good).parent.mkdir(parents=True, exist_ok=True)
        Path(good).write_text("audio", encoding="utf-8")
        resolved = resolve_safe_path(good, ["music"])
        self.assertIsInstance(resolved, Path)
        self.assertEqual(resolved, Path(good).resolve())

        bad_paths = [
            f"{self.music}/../outside/secret.flac",
            f"{self.music}/%2e%2e/outside/secret.flac",
            f"{self.music}/%252e%252e/outside/secret.flac",
            f"{self.base}/music-other/secret.flac",
            f"{self.outside}/secret.flac",
            "C:\\data\\media\\music\\track.flac",
            "\\\\server\\share\\track.flac",
            f"{self.music}\\Artist\\Track.flac",
        ]
        for candidate in bad_paths:
            with self.subTest(candidate=candidate):
                with self.assertRaises(UnsafePathError):
                    resolve_safe_path(candidate, ["music"])

    def test_symlink_escape_is_rejected_where_supported(self):
        outside_secret = f"{self.outside}/secret.flac"
        Path(outside_secret).write_text("secret", encoding="utf-8")
        link_path = f"{self.music}/escape.flac"
        try:
            Path(link_path).symlink_to(Path(outside_secret))
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable")
        with self.assertRaises(UnsafePathError):
            resolve_safe_path(link_path, ["music"])

    def test_resolve_safe_path_rejects_empty_and_non_string_input(self):
        for bad in (None, "", 42, ["/tmp/x"], {"path": "/tmp/x"}):
            with self.subTest(bad=bad):
                with self.assertRaises(UnsafePathError):
                    resolve_safe_path(bad, ["music"])

    def test_resolve_safe_path_rejects_relative_path(self):
        with self.assertRaises(UnsafePathError):
            resolve_safe_path("Artist/Track.flac", ["music"])

    def test_resolve_safe_path_require_exists(self):
        missing = f"{self.music}/Artist/DoesNotExist.flac"
        # Not required: resolves fine even though nothing is on disk yet.
        resolved = resolve_safe_path(missing, ["music"])
        self.assertEqual(resolved, Path(missing).resolve())
        with self.assertRaises(UnsafePathError):
            resolve_safe_path(missing, ["music"], require_exists=True)

    def test_resolve_safe_path_expected_type_file_vs_directory(self):
        file_path = f"{self.music}/Artist/Track.flac"
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        Path(file_path).write_text("audio", encoding="utf-8")
        dir_path = f"{self.music}/Artist"

        # Correct expectations pass.
        resolve_safe_path(file_path, ["music"], expected_type="file")
        resolve_safe_path(dir_path, ["music"], expected_type="dir")

        # Wrong expectations are rejected.
        with self.assertRaises(UnsafePathError):
            resolve_safe_path(file_path, ["music"], expected_type="dir")
        with self.assertRaises(UnsafePathError):
            resolve_safe_path(dir_path, ["music"], expected_type="file")

    def test_resolve_safe_path_supports_multiple_allowed_roots(self):
        music_file = f"{self.music}/Artist/Track.flac"
        staging_file = f"{self.staging}/incoming/Track.flac"
        Path(music_file).parent.mkdir(parents=True, exist_ok=True)
        Path(staging_file).parent.mkdir(parents=True, exist_ok=True)
        Path(music_file).write_text("a", encoding="utf-8")
        Path(staging_file).write_text("a", encoding="utf-8")
        resolve_safe_path(music_file, ["music", "staging"])
        resolve_safe_path(staging_file, ["music", "staging"])
        with self.assertRaises(UnsafePathError):
            resolve_safe_path(f"{self.outside}/Track.flac", ["music", "staging"])

    def test_resolve_safe_path_accepts_unicode_and_spaces(self):
        fancy = f"{self.music}/Café Artist/Track (Live).flac"
        Path(fancy).parent.mkdir(parents=True, exist_ok=True)
        Path(fancy).write_text("audio", encoding="utf-8")
        resolved = resolve_safe_path(fancy, ["music"])
        self.assertEqual(resolved, Path(fancy).resolve())

    def test_delete_refuses_to_remove_allowed_root_itself(self):
        code, data = _post_agent("/files/delete", {"path": self.music})
        self.assertEqual(code, 403, data)
        self.assertTrue(Path(self.music).is_dir())

    def test_delete_endpoint_rejects_encoded_escape_before_mutation(self):
        outside_secret = f"{self.outside}/secret.flac"
        Path(outside_secret).write_text("secret", encoding="utf-8")
        code, data = _post_agent("/files/delete", {"path": f"{self.music}/%252e%252e/outside/secret.flac"})
        self.assertEqual(code, 403)
        self.assertIn("allowed roots", data["error"])
        self.assertTrue(Path(outside_secret).exists())

    def test_mkdir_endpoint_rejects_prefix_collision(self):
        code, _ = _post_agent("/files/mkdir", {"path": f"{self.base}/music-other/new"})
        self.assertEqual(code, 403)
        self.assertFalse(Path(f"{self.base}/music-other/new").exists())

    def test_move_endpoint_rejects_source_escape(self):
        outside_secret = f"{self.outside}/secret.flac"
        Path(outside_secret).write_text("secret", encoding="utf-8")
        code, data = _post_agent("/files/move", {
            "source_path": f"{self.music}/%252e%252e/outside/secret.flac",
            "target_path": f"{self.staging}/stolen.flac",
        })
        self.assertEqual(code, 403, data)
        self.assertTrue(Path(outside_secret).exists())
        self.assertFalse(Path(f"{self.staging}/stolen.flac").exists())

    def test_move_endpoint_rejects_destination_escape(self):
        source = f"{self.music}/Artist/Track.flac"
        Path(source).parent.mkdir(parents=True, exist_ok=True)
        Path(source).write_text("audio", encoding="utf-8")
        code, data = _post_agent("/files/move", {
            "source_path": source,
            "target_path": f"{self.music}/%252e%252e/outside/stolen.flac",
        })
        self.assertEqual(code, 403, data)
        self.assertTrue(Path(source).exists())

    def test_move_endpoint_accepts_valid_paths_and_uses_canonical_destination(self):
        source = f"{self.music}/Artist/Track.flac"
        Path(source).parent.mkdir(parents=True, exist_ok=True)
        Path(source).write_text("audio", encoding="utf-8")
        dest = f"{self.staging}/moved/Track.flac"
        code, data = _post_agent("/files/move", {"source_path": source, "target_path": dest})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["target_path"], str(Path(dest).resolve()))
        self.assertTrue(Path(dest).exists())
        self.assertFalse(Path(source).exists())

    def test_resolve_safe_path_preserves_literal_percent_filenames(self):
        """Regression test: resolve_safe_path() must canonicalize from the
        original raw path, not its decoded form. Decoding is used only to
        detect a hidden traversal/separator/null-byte attack; a legitimate
        literal filename that happens to contain a percent-sign byte
        sequence must resolve to itself, not be silently rewritten to a
        different string before it reaches the filesystem sink. Every path
        endpoint here receives values in a JSON body, never URL-encoded by
        any real caller, so decoding must never change what gets operated
        on."""
        literal_names = [
            "convention%20album.flac",
            "100%25 legit.flac",
            "literal%2520name.flac",
        ]
        for name in literal_names:
            with self.subTest(name=name):
                literal_path = f"{self.music}/{name}"
                Path(literal_path).write_text("audio", encoding="utf-8")
                resolved = resolve_safe_path(literal_path, ["music"])
                self.assertEqual(resolved, Path(literal_path).resolve())

        # Encoded traversal, separators, nulls, and nested encoding must
        # still be rejected -- the fix must not weaken detection.
        attack_paths = [
            f"{self.music}/../outside/secret.flac",
            f"{self.music}/%2e%2e/outside/secret.flac",
            f"{self.music}/%252e%252e/outside/secret.flac",
            f"{self.music}/foo%00.flac",
            f"{self.music}/..%2f..%2fetc/passwd",
        ]
        for candidate in attack_paths:
            with self.subTest(candidate=candidate):
                with self.assertRaises(UnsafePathError):
                    resolve_safe_path(candidate, ["music"])

        # Nested encoding of a genuine "../" must be rejected at every
        # depth, including beyond the resolver's decode budget.
        import urllib.parse as _up
        s = "../"
        for depth in range(1, 7):
            s = _up.quote(s, safe="")
            raw = f"{self.music}/{s}etc/passwd"
            with self.subTest(depth=depth):
                with self.assertRaises(UnsafePathError):
                    resolve_safe_path(raw, ["music"])

    def test_resolve_safe_path_requires_raw_absolute_not_just_decoded(self):
        """Regression test: resolve_safe_path() canonicalizes from the raw
        input string, so the absolute-path requirement must be enforced on
        that same raw string -- not only on its decoded copy. A value whose
        raw form is relative but whose fully-decoded form happens to be
        absolute (e.g. a fully percent-encoded leading separator) must be
        rejected outright rather than falling through to os.path.abspath(),
        which would silently anchor it to the process's cwd instead."""
        candidates = [
            "%2Fdata%2Fmedia%2Fmusic%2Ffile.flac",
            "%252Fdata%252Fmedia%252Fmusic%252Ffile.flac",
            "relative%2Fencoded",
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaises(UnsafePathError):
                    resolve_safe_path(candidate, ["music"])

    def test_literal_percent_filename_survives_delete_endpoint(self):
        literal_path = f"{self.music}/convention%20album.flac"
        Path(literal_path).write_text("audio", encoding="utf-8")
        decoded_sibling = f"{self.music}/convention album.flac"
        self.assertFalse(Path(decoded_sibling).exists())

        code, data = _post_agent("/files/delete", {"path": literal_path})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["path"], str(Path(literal_path).resolve()))
        self.assertFalse(Path(literal_path).exists())
        self.assertFalse(Path(decoded_sibling).exists())

    def test_command_args_allow_beets_range_query_syntax(self):
        """Regression test: a blanket '".." in arg' substring check rejected
        legitimate Beets range-query syntax (e.g. `year:2020..2023`), which
        is never filesystem-joined -- only an actual relative path segment
        is a real traversal-relevant shape and must still be rejected."""
        legitimate_args = ["year:2020..2023", "added:2020-01-01..2020-02-01", "title:Mr...Wonderful"]
        fake_result = mock.MagicMock(returncode=0, stdout="", stderr="")
        with mock.patch.object(bca.subprocess, "run", return_value=fake_result) as run_mock:
            code, data = _post_agent("/commands/execute", {"command": "ls", "args": legitimate_args})
        self.assertEqual(code, 200, data)
        full_cmd = run_mock.call_args.args[0]
        self.assertEqual(full_cmd[-len(legitimate_args):], legitimate_args)

        for traversal_arg in ["..", "../etc/passwd", "foo/../bar", "./relative"]:
            with self.subTest(arg=traversal_arg):
                with mock.patch.object(bca.subprocess, "run") as run_mock:
                    code, data = _post_agent("/commands/execute", {"command": "ls", "args": [traversal_arg]})
                self.assertEqual(code, 403, data)
                run_mock.assert_not_called()

    def test_commands_execute_and_jobs_create_use_canonical_source_path(self):
        """source_path and absolute args must be replaced by their canonical
        resolved values in the actual subprocess/job command array -- never
        the original request string."""
        source = f"{self.staging}/import-source"
        Path(source).mkdir()
        arg_file = f"{self.music}/Artist/Track.flac"
        Path(arg_file).parent.mkdir(parents=True, exist_ok=True)
        Path(arg_file).write_text("audio", encoding="utf-8")

        fake_result = mock.MagicMock(returncode=0, stdout="", stderr="")
        with mock.patch.object(bca.subprocess, "run", return_value=fake_result) as run_mock:
            code, data = _post_agent("/commands/execute", {
                "command": "import",
                "source_path": source,
                "args": ["--quiet", arg_file],
            })
        self.assertEqual(code, 200, data)
        full_cmd = run_mock.call_args.args[0]
        self.assertEqual(
            full_cmd[-4:],
            ["import", str(Path(source).resolve()), "--quiet", str(Path(arg_file).resolve())],
        )

        created = {}

        class FakeJob:
            def __init__(self, job_id, command, label="", config_override=""):
                created["job_id"] = job_id
                created["command"] = command

        with mock.patch.object(bca, "AgentJob", FakeJob):
            code, data = _post_agent("/jobs/create", {
                "command": "import",
                "source_path": source,
                "args": ["--quiet", arg_file],
            })
        try:
            self.assertEqual(code, 200, data)
            self.assertEqual(
                created["command"],
                ["import", str(Path(source).resolve()), "--quiet", str(Path(arg_file).resolve())],
            )
        finally:
            bca.JOBS.pop(created.get("job_id"), None)

    def test_command_and_job_reject_traversal_and_option_injection(self):
        with mock.patch.object(bca.subprocess, "run") as run_mock:
            code, data = _post_agent("/commands/execute", {
                "command": "import",
                "source_path": self.staging,
                "args": ["--config=/etc/passwd"],
            })
        self.assertEqual(code, 403, data)
        run_mock.assert_not_called()
        self.assertNotIn("/etc/passwd", json.dumps(data))

        before_jobs = set(bca.JOBS)
        code, data = _post_agent("/jobs/create", {
            "command": "import",
            "source_path": f"{self.staging}/%252e%252e/outside",
            "args": [],
        })
        self.assertEqual(code, 403, data)
        self.assertEqual(set(bca.JOBS), before_jobs)
        self.assertNotIn("%252e%252e", json.dumps(data))

    def test_commands_execute_ignores_unused_target_path(self):
        """target_path is not part of /commands/execute's actual command
        contract -- it is never appended to the constructed command array by
        any allowed subcommand, and no current caller sends it. It must be
        accepted (as inert, unvalidated JSON) rather than silently
        influencing command construction or being treated as a path sink."""
        fake_result = mock.MagicMock(returncode=0, stdout="", stderr="")
        with mock.patch.object(bca.subprocess, "run", return_value=fake_result) as run_mock:
            code, data = _post_agent("/commands/execute", {
                "command": "ls",
                "args": ["title:x"],
                "target_path": f"{self.outside}/anywhere-outside-every-root",
            })
        self.assertEqual(code, 200, data)
        full_cmd = run_mock.call_args.args[0]
        self.assertNotIn(f"{self.outside}/anywhere-outside-every-root", full_cmd)
        self.assertEqual(full_cmd[-1:], ["title:x"])


class AcoustidLookupProviderRouteTests(unittest.TestCase):
    """Regression tests for POST /audio/acoustid-lookup's configured-provider
    path. This exercises the real handler far enough to reach
    urllib.request.Request/urlopen and urllib.error.HTTPError/URLError --
    the exact code path that crashed with
    'AttributeError: module 'urllib' has no attribute 'request'' when only
    `import urllib.parse` was present. Mocking only the external HTTP
    boundary (urllib.request.urlopen) proves the fix reaches real provider
    request construction, not just that the import line exists."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        base_name = Path(self.tmp.name).name
        self.base = f"/tmp/{base_name}"
        self.music = f"{self.base}/music"
        Path(self.music).mkdir(parents=True, exist_ok=True)
        self.valid_file = f"{self.music}/track.flac"
        Path(self.valid_file).write_bytes(b"not-real-audio-bytes")
        self.patches = [
            mock.patch.object(bca, "MUSIC_LIBRARY_PATH", self.music),
            mock.patch.object(bca, "DOWNLOAD_PATH", f"{self.base}/staging"),
            mock.patch.object(bca, "LOCK_PATH", f"{self.base}/agent.lock"),
            mock.patch.dict(os.environ, {"ACOUSTID_API_KEY": "synthetic-test-key-not-real"}),
            mock.patch.object(bca.shutil, "which", return_value="/fake/fpcalc"),
            mock.patch.object(bca.os.path, "exists", return_value=True),
        ]
        for patch in self.patches:
            patch.start()
        self._fake_fpcalc = mock.patch.object(
            bca.subprocess, "run",
            return_value=mock.MagicMock(
                returncode=0,
                stdout=json.dumps({"duration": 180.5, "fingerprint": "AQAB-fake-fingerprint"}),
            ),
        )
        self._fake_fpcalc.start()

    def tearDown(self):
        self._fake_fpcalc.stop()
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def _fake_response(self, payload: dict):
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def test_matched_response_reaches_real_provider_request(self):
        provider_payload = {
            "status": "ok",
            "results": [{
                "id": "acoustid-id-1",
                "score": 0.95,
                "recordings": [{
                    "id": "mb-recording-id-1",
                    "title": "Test Track",
                    "artists": [{"name": "Test Artist"}],
                    "releases": [{"id": "mb-release-1", "title": "Test Album", "date": {"year": 2020}}],
                }],
            }],
        }
        with mock.patch.object(bca.urllib.request, "urlopen", return_value=self._fake_response(provider_payload)) as urlopen_mock:
            code, data = _post_agent("/audio/acoustid-lookup", {"path": self.valid_file})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["status"], "matched")
        self.assertTrue(data["candidates"])
        self.assertEqual(data["candidates"][0]["mb_trackid"], "mb-recording-id-1")
        urlopen_mock.assert_called_once()
        request_obj = urlopen_mock.call_args.args[0]
        self.assertIn("client=synthetic-test-key-not-real", request_obj.full_url)
        self.assertNotIn("synthetic-test-key-not-real", json.dumps(data))
        self.assertNotIn(self.valid_file, json.dumps(data))

    def test_no_match_response(self):
        with mock.patch.object(bca.urllib.request, "urlopen", return_value=self._fake_response({"status": "ok", "results": []})):
            code, data = _post_agent("/audio/acoustid-lookup", {"path": self.valid_file})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["status"], "no_match")
        self.assertEqual(data["candidates"], [])

    def test_http_error_maps_to_provider_error(self):
        def raise_http_error(*a, **k):
            raise urllib.error.HTTPError(url="https://api.acoustid.org/v2/lookup", code=503, msg="Service Unavailable", hdrs=None, fp=None)
        with mock.patch.object(bca.urllib.request, "urlopen", side_effect=raise_http_error):
            code, data = _post_agent("/audio/acoustid-lookup", {"path": self.valid_file})
        self.assertEqual(code, 502, data)
        self.assertEqual(data["status"], "provider_error")
        self.assertNotIn("503", json.dumps(data))

    def test_url_error_maps_to_provider_error(self):
        with mock.patch.object(bca.urllib.request, "urlopen", side_effect=urllib.error.URLError("connection refused")):
            code, data = _post_agent("/audio/acoustid-lookup", {"path": self.valid_file})
        self.assertEqual(code, 502, data)
        self.assertEqual(data["status"], "provider_error")
        self.assertNotIn("connection refused", json.dumps(data))

    def test_timeout_after_retry(self):
        with mock.patch.object(bca.urllib.request, "urlopen", side_effect=TimeoutError("timed out")), \
             mock.patch.object(bca.time, "sleep"):
            code, data = _post_agent("/audio/acoustid-lookup", {"path": self.valid_file})
        self.assertEqual(code, 504, data)
        self.assertEqual(data["status"], "timeout")

    def test_malformed_provider_payload(self):
        resp = mock.MagicMock()
        resp.read.return_value = b"not valid json{{{"
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with mock.patch.object(bca.urllib.request, "urlopen", return_value=resp):
            code, data = _post_agent("/audio/acoustid-lookup", {"path": self.valid_file})
        self.assertEqual(code, 500, data)
        self.assertEqual(data["status"], "analysis_error")
        self.assertNotIn("not valid json", json.dumps(data))

    def test_urllib_request_and_error_are_importable_from_the_module(self):
        """A future removal of either import must fail this test."""
        self.assertTrue(hasattr(bca.urllib, "request"))
        self.assertTrue(hasattr(bca.urllib, "error"))
        self.assertTrue(hasattr(bca.urllib.request, "Request"))
        self.assertTrue(hasattr(bca.urllib.request, "urlopen"))
        self.assertTrue(hasattr(bca.urllib.error, "HTTPError"))
        self.assertTrue(hasattr(bca.urllib.error, "URLError"))


class AcoustidLookupIsolatedImportTests(unittest.TestCase):
    """No mocking of any kind here (deliberately -- mock.patch.object on
    bca.subprocess.run patches the process-wide subprocess module, which
    would otherwise intercept this test's own attempt to spawn a real
    isolated interpreter). Regression test for the exact bug Codex
    reported: importing only `urllib.parse` does NOT make
    `urllib.request`/`urllib.error` accessible as attributes of the shared
    `urllib` package object -- that only appeared to work when other test
    modules in the same process had already imported urllib.request first.
    A genuinely fresh subprocess that imports ONLY
    backend.beets_control_agent has no such contamination, so this is the
    check that would actually have failed with 'AttributeError: module
    'urllib' has no attribute 'request'' against the previously reviewed
    f1b44ea1... head."""

    def test_urllib_submodules_load_in_a_fresh_isolated_process(self):
        script = (
            "import backend.beets_control_agent as bca\n"
            "bca.urllib.request.Request('https://example.com')\n"
            "assert bca.urllib.error.HTTPError\n"
            "assert bca.urllib.error.URLError\n"
            "print('URLLIB_OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("URLLIB_OK", result.stdout)


class AlbumDeleteCanonicalPathTests(unittest.TestCase):
    """Regression tests for _handle_delete_album() treating database-stored
    paths as untrusted, matching the same canonical-path/root-refusal
    contract as the /files/delete endpoint."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        base_name = Path(self.tmp.name).name
        self.base = f"/tmp/{base_name}"
        self.music = f"{self.base}/music"
        self.staging = f"{self.base}/staging"
        self.outside = f"{self.base}/outside"
        Path(self.music).mkdir(parents=True, exist_ok=True)
        Path(self.staging).mkdir(parents=True, exist_ok=True)
        Path(self.outside).mkdir(parents=True, exist_ok=True)
        self.db_path = Path(self.base) / "musiclibrary.blb"
        self.patches = [
            mock.patch.object(bca, "MUSIC_LIBRARY_PATH", self.music),
            mock.patch.object(bca, "DOWNLOAD_PATH", self.staging),
            mock.patch.object(bca, "LOCK_PATH", f"{self.base}/agent.lock"),
            mock.patch.object(bca, "LIB_PATH", str(self.db_path)),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def _seed_album(self, album_path: str, item_paths: list[str]):
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, path TEXT)")
        con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path TEXT)")
        con.execute("INSERT INTO albums (id, album, path) VALUES (1, 'Album', ?)", (album_path,))
        for idx, p in enumerate(item_paths, start=10):
            con.execute("INSERT INTO items (id, album_id, path) VALUES (?, 1, ?)", (idx, p))
        con.commit()
        con.close()

    def test_hostile_stored_paths_do_not_escape_approved_roots(self):
        real_item = f"{self.music}/Artist/Album/Track.flac"
        Path(real_item).parent.mkdir(parents=True, exist_ok=True)
        Path(real_item).write_text("audio", encoding="utf-8")

        outside_item = f"{self.outside}/secret.flac"
        Path(outside_item).write_text("secret", encoding="utf-8")

        percent_item = f"{self.music}/Artist/Album/convention%20album.flac"
        Path(percent_item).write_text("audio", encoding="utf-8")
        decoded_sibling = f"{self.music}/Artist/Album/convention album.flac"

        traversal_item = f"{self.music}/Artist/Album/%2e%2e/%2e%2e/outside/escape.flac"

        self._seed_album(
            self.music,  # hostile: album path itself is the approved root
            [real_item, outside_item, percent_item, traversal_item],
        )

        code, data = bca._handle_delete_album(1, delete_files=True)
        self.assertEqual(code, 200, data)

        # The real, legitimate item is gone.
        self.assertFalse(Path(real_item).exists())
        # The literal-percent file was deleted exactly, not a decoded sibling.
        self.assertFalse(Path(percent_item).exists())
        self.assertFalse(Path(decoded_sibling).exists())
        # The outside-root item was never touched.
        self.assertTrue(Path(outside_item).exists())
        # The approved root itself (used as the "album path") was not removed.
        self.assertTrue(Path(self.music).is_dir())

        self.assertEqual(data["files_deleted"], 2)
        self.assertGreaterEqual(data["files_failed"], 1)
        self.assertNotIn(self.outside, json.dumps(data))
        self.assertNotIn("secret.flac", json.dumps(data))

    def test_database_row_deletion_is_unconditional_even_with_hostile_paths(self):
        """A malicious stored path must not prevent safe database cleanup:
        the album/item rows are removed regardless of whether any stored
        file path is safe to delete."""
        self._seed_album(f"{self.outside}/evil-album", [f"{self.outside}/evil.flac"])
        code, data = bca._handle_delete_album(1, delete_files=True)
        self.assertEqual(code, 200, data)
        self.assertTrue(data["database_deleted"])
        self.assertEqual(data["items_deleted"], 1)
        self.assertEqual(data["albums_deleted"], 1)

    def test_filesystem_exception_after_commit_does_not_misreport_database_deleted(self):
        """Regression test: the album/item DB rows are committed before any
        filesystem cleanup runs. An unexpected filesystem exception during
        that best-effort cleanup (e.g. os.listdir() raising on the album
        directory) must not be allowed to propagate to the outer handler and
        misreport an already-successful database deletion as a total
        failure."""
        real_item = f"{self.music}/Artist/Album/Track.flac"
        Path(real_item).parent.mkdir(parents=True, exist_ok=True)
        Path(real_item).write_text("audio", encoding="utf-8")
        album_dir = f"{self.music}/Artist/Album"

        self._seed_album(album_dir, [real_item])

        with mock.patch.object(bca.os, "listdir", side_effect=PermissionError("denied")):
            code, data = bca._handle_delete_album(1, delete_files=True)

        self.assertEqual(code, 200, data)
        self.assertTrue(data["database_deleted"])
        self.assertEqual(data["items_deleted"], 1)
        self.assertEqual(data["albums_deleted"], 1)
        self.assertNotEqual(data["status"], "failed")
        # The item file itself must still have been deleted -- only the
        # album-directory listdir() call was made to fail.
        self.assertFalse(Path(real_item).exists())


class ControlAgentSqlBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.db_path = Path(self.tmp.name) / "musiclibrary.blb"
        base_name = Path(self.tmp.name).name
        self.music = f"/tmp/{base_name}/music"
        self.outside = f"/tmp/{base_name}/outside"
        Path(self.music).mkdir(parents=True, exist_ok=True)
        Path(self.outside).mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT, artist TEXT)")
        con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, albumartist TEXT, artpath TEXT)")
        con.execute("INSERT INTO items (id, title, artist) VALUES (1, 'Safe title', 'Artist')")
        con.execute("INSERT INTO albums (id, album, albumartist) VALUES (1, 'Safe album', 'Artist')")
        con.commit()
        con.close()
        self.patches = [
            mock.patch.object(bca, "LIB_PATH", str(self.db_path)),
            mock.patch.object(bca, "LOCK_PATH", str(Path(self.tmp.name) / "agent.lock")),
            mock.patch.object(bca, "MUSIC_LIBRARY_PATH", self.music),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def test_raw_query_endpoint_rejects_all_sql(self):
        for query in (
            "SELECT '/* not a comment */' AS marker, title FROM items WHERE title = ?",
            "SELECT * FROM items; SELECT * FROM albums",
            "UPDATE items SET title='owned' WHERE id=1",
            "WITH changed AS (DELETE FROM items RETURNING *) SELECT * FROM changed",
            "PRAGMA database_list",
        ):
            with self.subTest(query=query):
                code, data = _post_agent("/library/raw_query", {"query": query, "params": ["Safe title"]})
                self.assertEqual(code, 403, data)
                self.assertEqual(data["error"], "Raw SQL queries are not permitted")

        con = sqlite3.connect(self.db_path)
        title = con.execute("SELECT title FROM items WHERE id=1").fetchone()[0]
        con.close()
        self.assertEqual(title, "Safe title")

    def test_raw_sqlite_client_helper_fails_closed_locally(self):
        client = BeetsClient(base_url="http://127.0.0.1:1", token="unused")
        with self.assertRaisesRegex(BeetsError, "Raw SQLite queries are not permitted"):
            client.raw_sqlite_query("SELECT * FROM items")

    def test_item_patch_rejects_injected_column_name(self):
        code, data = _patch_agent("/items/1", {"fields": {"title = 'owned', artist": "ignored"}})
        self.assertEqual(code, 400, data)
        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT title, artist FROM items WHERE id=1").fetchone()
        con.close()
        self.assertEqual(row, ("Safe title", "Artist"))

    def test_album_patch_updates_allowlisted_column(self):
        code, data = _patch_agent("/albums/1", {"fields": {"album": "Updated album"}})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["album"]["album"], "Updated album")

    def test_album_artpath_endpoint_persists_only_canonical_music_path(self):
        cover = f"{self.music}/Artist/cover.jpg"
        Path(cover).parent.mkdir(parents=True, exist_ok=True)
        Path(cover).write_text("cover", encoding="utf-8")
        code, data = _post_agent("/albums/1/artpath", {"artpath": cover})
        self.assertEqual(code, 200, data)
        self.assertEqual(data["artpath"], str(Path(cover).resolve()))

        escape = f"{self.music}/%252e%252e/outside/secret.jpg"
        code, data = _post_agent("/albums/1/artpath", {"artpath": escape})
        self.assertEqual(code, 403, data)
        self.assertNotIn("%252e%252e", json.dumps(data))

        con = sqlite3.connect(self.db_path)
        artpath = con.execute("SELECT artpath FROM albums WHERE id=1").fetchone()[0]
        con.close()
        self.assertEqual(artpath, str(Path(cover).resolve()))


class ComposeValidatorRegexTests(unittest.TestCase):
    def test_image_tag_check_handles_adversarial_path_without_regex_backtracking(self):
        image = "./" + "9/" * 2000 + "beets-engine"
        self.assertTrue(compose_security._image_lacks_tag_or_digest(image))

    def test_image_tag_check_distinguishes_registry_port_from_tag(self):
        self.assertTrue(compose_security._image_lacks_tag_or_digest("localhost:5000/beets-engine"))
        self.assertFalse(compose_security._image_lacks_tag_or_digest("localhost:5000/beets-engine:2.4.0"))
        self.assertFalse(compose_security._image_lacks_tag_or_digest("example.test/beets@sha256:" + "a" * 64))


class PublicExceptionResponseTests(unittest.TestCase):
    def test_submission_readiness_does_not_return_remote_exception_text(self):
        leak = "LEAK_MARKER Traceback /tmp/internal.py line 12 Authorization: Bearer abc"
        with mock.patch.object(routes_submissions.beets_client, "get_status", side_effect=RuntimeError(leak)), \
             mock.patch.object(routes_submissions, "_acoustid_key", return_value=""), \
             mock.patch.object(routes_submissions, "_config_has_acoustid_key", return_value=False):
            readiness = routes_submissions._submission_readiness()
        self.assertEqual(readiness["reason"], "Beets control agent status is unavailable")
        self.assertNotIn("LEAK_MARKER", json.dumps(readiness))
        self.assertNotIn("Traceback", json.dumps(readiness))
        self.assertNotIn("Bearer abc", json.dumps(readiness))

    def test_album_delete_art_does_not_return_remote_exception_text(self):
        leak = "LEAK_MARKER Traceback /tmp/internal.py line 12 token=abc"
        album_dir = Path(tempfile.gettempdir()) / f"pr39-art-{uuid.uuid4().hex}"
        album_dir.mkdir(parents=True, exist_ok=True)
        try:
            with app_module.app.test_request_context("/api/albums/42/art", method="DELETE"), \
                 mock.patch.object(app_module.lib, "get_album", return_value=object()), \
                 mock.patch.object(app_module, "_album_dir_for_art", return_value=album_dir), \
                 mock.patch.object(app_module, "_album_stored_art_path", return_value=""), \
                 mock.patch.object(app_module, "_path_is_under", return_value=True), \
                 mock.patch.object(app_module, "_ALBUM_ART_NAMES", ()), \
                 mock.patch.object(app_module.beets_client, "clear_album_artpath", side_effect=RuntimeError(leak)):
                response, status = app_module.album_delete_art(42)
            data = response.get_json()
            self.assertEqual(status, 500)
            self.assertEqual(data["error"], "Could not clear artpath.")
            self.assertNotIn("LEAK_MARKER", json.dumps(data))
            self.assertNotIn("Traceback", json.dumps(data))
            self.assertNotIn("token=abc", json.dumps(data))
        finally:
            try:
                album_dir.rmdir()
            except OSError:
                pass


class PlexClientIdentifierFileDefaultTests(unittest.TestCase):
    def test_defaults_to_persistent_data_dir(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLEX_CLIENT_IDENTIFIER_FILE", None)
            self.assertEqual(
                app_module._plex_client_identifier_file(),
                Path("/web-manager-data/.plex_client_identifier"),
            )

    def test_honors_override_env_var(self):
        with mock.patch.dict(os.environ, {"PLEX_CLIENT_IDENTIFIER_FILE": "/tmp/custom-id-file"}):
            self.assertEqual(app_module._plex_client_identifier_file(), Path("/tmp/custom-id-file"))


class PlexClientIdentityTests(unittest.TestCase):
    """Track B follow-up: the app previously reused a bare account token
    with no distinct device registration, making a future rotation
    impossible to isolate. These cover the fix: a stable, persisted,
    per-installation client identifier, and token delivery via header
    (not a URL query string, which is far more likely to end up in an
    access log)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.id_file = Path(self.tmp.name) / ".plex_client_identifier"
        self.patches = [
            mock.patch.object(app_module, "_plex_client_identifier_file", lambda: self.id_file),
            mock.patch.object(app_module, "_plex_client_identifier_cache", ""),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def test_identifier_is_generated_and_persisted(self):
        self.assertFalse(self.id_file.exists())
        first = app_module._plex_client_identifier()
        self.assertTrue(self.id_file.exists())
        self.assertEqual(self.id_file.read_text(encoding="utf-8").strip(), first)

    def test_identifier_is_stable_across_calls_and_process_restart(self):
        first = app_module._plex_client_identifier()
        second = app_module._plex_client_identifier()
        self.assertEqual(first, second)

        # Simulate a fresh process (cold in-memory cache) reading the same
        # persisted file -- must recover the same identifier, not mint a
        # new one, or every container recreation would register as a new
        # Plex "device".
        app_module._plex_client_identifier_cache = ""
        third = app_module._plex_client_identifier()
        self.assertEqual(first, third)

    def test_two_installations_get_different_identifiers(self):
        first = app_module._plex_client_identifier()
        other_dir = tempfile.TemporaryDirectory(dir="/tmp")
        try:
            other_file = Path(other_dir.name) / ".id"
            with mock.patch.object(app_module, "_plex_client_identifier_file", lambda: other_file), \
                 mock.patch.object(app_module, "_plex_client_identifier_cache", ""):
                second = app_module._plex_client_identifier()
        finally:
            other_dir.cleanup()
        self.assertNotEqual(first, second)

    def test_client_headers_include_stable_identifier_and_no_secrets(self):
        headers = app_module._plex_client_headers()
        self.assertEqual(headers["X-Plex-Client-Identifier"], app_module._plex_client_identifier())
        self.assertIn("X-Plex-Product", headers)
        self.assertIn("X-Plex-Device-Name", headers)
        self.assertNotIn("X-Plex-Token", headers)

    def test_plex_request_sends_token_as_header_not_in_url(self):
        captured = {}

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = {k: v for k, v in req.header_items()}
            return _FakeResponse()

        with mock.patch.object(app_module, "_plex_settings", return_value={
                "url": "http://plex.example.test:32400", "token": "test-secret-token-value",
                "section": "", "plex_music_roots": "", "beets_music_root": "/data/media/music",
                "plex_scan_timeout": "10", "plex_index_timeout": "10"}), \
             mock.patch.object(app_module.urllib.request, "urlopen", side_effect=_fake_urlopen):
            app_module._plex_request("/library/sections")

        self.assertNotIn("test-secret-token-value", captured["url"])
        self.assertNotIn("token", captured["url"].lower())
        self.assertEqual(captured["headers"].get("X-plex-token"), "test-secret-token-value")


if __name__ == "__main__":
    unittest.main()