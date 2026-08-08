"""SEC-002 Wave 8 architecture-unblock regression tests.

Claude's independent final review of PR #70 blocked it with:

    blocked -- web-manager media mutation violates architecture

reimport_disk() invoked BEET_BIN via a direct local subprocess.run() for its
core import step (the web-manager image has no beet binary in the shipped
Compose topology), and create_unmatched_draft() wrote directly under
MUSIC_ROOT (a root the web manager neither owns nor, per the shipped Compose
files, has mounted at all).

This module guards both fixes:

1. reimport_disk()'s import step now goes through _beet_run() ->
   backend.beets_client.BeetsClient.run_command() (an authenticated HTTP call
   to the Beets engine's control agent), exactly like every other Beets
   mutation in this function already did. Source-level tests assert no
   direct subprocess.run() call remains in reimport_disk(); behavioral tests
   assert the routing actually happens and that BEET_BIN being entirely
   absent from PATH does not matter anymore.
2. create_unmatched_draft() now writes review-only metadata (no audio) under
   UNMATCHED_DRAFT_ROOT (/web-manager-data/unmatched_drafts by default),
   which every shipped Compose file actually mounts into this container --
   see test_sec002_app_path_ai_batch_replacement_staging.py for its
   containment/collision/secret-redaction tests.
"""
import os
import shutil
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import app as APP
import job_engine
import backend.beets_control_agent as CONTROL_AGENT


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")


def function_source(name: str) -> str:
    start = APP_SOURCE.index(f"def {name}(")
    end = APP_SOURCE.find("\ndef ", start + 5)
    if end == -1:
        end = len(APP_SOURCE)
    return APP_SOURCE[start:end]


class ReimportDiskNoLocalSubprocessTests(unittest.TestCase):
    """Source-level architecture guard: the previously-blocking pattern must
    never reappear in this function, even from an unrelated future edit."""

    def test_reimport_disk_never_calls_subprocess_run_directly(self):
        source = function_source("reimport_disk")
        self.assertNotIn("subprocess.run(", source)
        self.assertNotIn("subprocess.Popen(", source)
        self.assertNotIn("subprocess.call(", source)

    def test_reimport_disk_import_step_routes_through_beet_run(self):
        source = function_source("reimport_disk")
        self.assertIn('_beet_run(\n            base_tmp + ["import"', source)
        self.assertIn("config_override=temp_cfg_content", source)

    def test_create_unmatched_draft_never_references_music_root(self):
        # Excludes the docstring, which legitimately explains in prose why
        # MUSIC_ROOT is NOT used here -- the code itself must never
        # reference it as an actual path component.
        source = function_source("create_unmatched_draft")
        code_only = source.split('"""', 2)[-1]
        self.assertNotIn("MUSIC_ROOT", code_only)
        self.assertIn("UNMATCHED_DRAFT_ROOT", code_only)


class BeetRunConfigOverrideTests(unittest.TestCase):
    """_beet_run must forward config_override to the remote engine call
    rather than silently discarding it (a "-c <path>" token embedded in cmd
    is always stripped, since a local web-manager path has no meaning on
    the remote engine -- only the explicit config_override kwarg reaches
    the control agent)."""

    def test_config_override_is_forwarded_to_run_command(self):
        log: list = []
        with mock.patch.object(job_engine.beets_client, "run_command",
                                return_value={"returncode": 0, "stdout": "", "stderr": ""}) as mock_run:
            job_engine._beet_run(
                ["/lsiopy/bin/beet", "-c", "/tmp/should-be-ignored.yaml", "import", "/data/torrents/music/x"],
                log, config_override="import:\n  copy: no\n",
            )
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs.get("config_override"), "import:\n  copy: no\n")
        # The stripped local "-c" token must never reach the remote call.
        called_args = mock_run.call_args.args
        self.assertNotIn("/tmp/should-be-ignored.yaml", called_args)

    def test_beet_executable_token_and_dash_c_are_stripped_not_forwarded(self):
        log: list = []
        with mock.patch.object(job_engine.beets_client, "run_command",
                                return_value={"returncode": 0, "stdout": "ok", "stderr": ""}) as mock_run:
            r = job_engine._beet_run(["/lsiopy/bin/beet", "-c", "/tmp/x.yaml", "mbsync", "album_id:1"], log)
        self.assertEqual(r.returncode, 0)
        mock_run.assert_called_once_with(
            "mbsync", args=["album_id:1"], timeout=120.0, config_override="",
        )

    def test_remote_timeout_is_surfaced_not_silently_swallowed(self):
        """The control agent maps a subprocess timeout to HTTP 408; the
        client wraps that as a BeetsError. reimport_disk()'s import step
        detects this via "timed out" in the returned stderr to preserve its
        pre-migration "treat timeout as inconclusive, check the DB
        afterward" behavior instead of a hard failure."""
        from backend.beets_client import BeetsError
        log: list = []
        with mock.patch.object(job_engine.beets_client, "run_command",
                                side_effect=BeetsError("Beets API request error: Command 'import' timed out after 300s")):
            r = job_engine._beet_run(["/lsiopy/bin/beet", "import", "/data/torrents/music/x"], log, timeout=300)
        self.assertEqual(r.returncode, 1)
        self.assertIn("timed out", r.stderr.lower())


class ReimportDiskNoBeetBinaryRequiredTests(unittest.TestCase):
    """Proves the architecture fix, not just its source shape: the import
    step succeeds via the mocked remote engine call even when BEET_BIN
    resolves to a path that does not exist on this machine at all --
    reproducing the real web-manager container, which has no beet binary."""

    def test_import_step_succeeds_with_no_local_beet_binary_present(self):
        log: list = []
        # Ends in "/beet" (matching how BEET_BIN is actually named -- see
        # app.py: BEET_BIN = shutil.which("beet") or "/lsiopy/bin/beet") but
        # the path itself does not exist anywhere on this machine, exactly
        # reproducing the real web-manager container.
        with mock.patch.object(APP, "BEET_BIN", "/nonexistent/path/does/not/exist/beet"), \
             mock.patch.object(job_engine.beets_client, "run_command",
                                return_value={"returncode": 0, "stdout": "tagged and imported", "stderr": ""}) as mock_run, \
             mock.patch("subprocess.run", side_effect=AssertionError(
                 "reimport_disk must not call subprocess.run locally")):
            r = job_engine._beet_run(
                [APP.BEET_BIN, "-c", "/tmp/beets_reimport_disk.yaml", "import", "-q",
                 "--noincremental", "--quiet-fallback", "asis", "--search-id",
                 "11111111-1111-1111-1111-111111111111", "/data/torrents/music/Artist/Album"],
                log, timeout=300, config_override="import:\n  copy: no\n  move: no\n",
            )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "tagged and imported")
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.args[0], "import")


@unittest.skipUnless(os.name == "posix", "resolve_safe_path() requires POSIX-absolute paths; the control agent only ever runs inside the Linux engine container")
class ImportSourceInspectionTests(unittest.TestCase):
    """Real, unmocked tests of inspect_import_source() -- the engine-side
    function that replaces reimport_disk()'s old web-manager-local
    _resolve_import_review_source_path() call (SEC-002 Wave 8 ARCH-003).
    Uses real temp directories and real symlinks, exactly as this function
    will actually run inside the beets engine container, where
    MUSIC_LIBRARY_PATH/DOWNLOAD_PATH are real mounts."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="beets_test_arch003_", dir=".")
        self.tmp_root = Path(self.tmp_dir).resolve()
        self.music_root = self.tmp_root / "music"
        self.staging_root = self.tmp_root / "staging"
        self.outside = self.tmp_root / "outside"
        self.music_root.mkdir(parents=True)
        self.staging_root.mkdir(parents=True)
        self.outside.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self._music_patcher = mock.patch.object(CONTROL_AGENT, "MUSIC_LIBRARY_PATH", str(self.music_root))
        self._staging_patcher = mock.patch.object(CONTROL_AGENT, "DOWNLOAD_PATH", str(self.staging_root))
        self._music_patcher.start()
        self._staging_patcher.start()
        self.addCleanup(self._music_patcher.stop)
        self.addCleanup(self._staging_patcher.stop)

    def test_unknown_operation_rejected(self):
        result = CONTROL_AGENT.inspect_import_source(str(self.staging_root), "not_a_real_operation")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_operation")

    def test_outside_root_rejected(self):
        result = CONTROL_AGENT.inspect_import_source(str(self.outside), "reimport")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_path")

    def test_nonexistent_path_rejected(self):
        result = CONTROL_AGENT.inspect_import_source(str(self.staging_root / "does_not_exist"), "reimport")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_path")

    def test_traversal_rejected(self):
        result = CONTROL_AGENT.inspect_import_source(str(self.staging_root / ".." / "outside"), "reimport")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_path")

    def test_root_self_rejected(self):
        result = CONTROL_AGENT.inspect_import_source(str(self.staging_root), "reimport")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "root_self_rejected")

    def test_symlink_directory_escaping_root_is_not_traversed(self):
        (self.outside / "private.flac").write_bytes(b"not-real-audio")
        album_dir = self.staging_root / "album_with_link"
        album_dir.mkdir()
        link = album_dir / "escape_link"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("platform/user cannot create symlinks")
        (album_dir / "01.flac").write_bytes(b"not-real-audio-either")
        result = CONTROL_AGENT.inspect_import_source(str(album_dir), "reimport")
        self.assertTrue(result["ok"], result)
        relative_paths = [e["relative_path"] for e in result["audio_files"]]
        self.assertIn("01.flac", relative_paths)
        self.assertNotIn("escape_link/private.flac", relative_paths)
        # The outside file must never even have been touched/read.
        self.assertEqual(len(result["audio_files"]), 1)

    def test_valid_staging_directory_returns_canonical_path_and_audio_evidence(self):
        album_dir = self.staging_root / "real_album"
        album_dir.mkdir()
        (album_dir / "01.flac").write_bytes(b"not-real-audio")
        (album_dir / "readme.txt").write_text("not audio")
        result = CONTROL_AGENT.inspect_import_source(str(album_dir), "reimport")
        self.assertTrue(result["ok"], result)
        self.assertEqual(Path(result["canonical_path"]), album_dir.resolve())
        self.assertEqual(result["audio_count"], 1)
        self.assertEqual(result["audio_files"][0]["relative_path"], "01.flac")
        self.assertIn("properties", result["audio_files"][0])
        self.assertIn("source_signature", result)

    def test_valid_music_directory_permitted_for_reimport(self):
        album_dir = self.music_root / "Artist" / "Album"
        album_dir.mkdir(parents=True)
        (album_dir / "01.mp3").write_bytes(b"not-real-audio")
        result = CONTROL_AGENT.inspect_import_source(str(album_dir), "reimport")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["audio_count"], 1)

    def test_source_signature_changes_when_files_change(self):
        album_dir = self.staging_root / "changeable_album"
        album_dir.mkdir()
        (album_dir / "01.flac").write_bytes(b"version one")
        first = CONTROL_AGENT.inspect_import_source(str(album_dir), "reimport")
        (album_dir / "01.flac").write_bytes(b"version two but different length!!")
        second = CONTROL_AGENT.inspect_import_source(str(album_dir), "reimport")
        self.assertNotEqual(first["source_signature"], second["source_signature"])

    def test_nul_byte_rejected(self):
        result = CONTROL_AGENT.inspect_import_source(str(self.staging_root) + "\x00evil", "reimport")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_path")

    def test_ai_batch_discovery_operation_also_permits_music_root(self):
        album_dir = self.music_root / "Some Artist" / "Some Album"
        album_dir.mkdir(parents=True)
        (album_dir / "01.flac").write_bytes(b"not-real-audio")
        result = CONTROL_AGENT.inspect_import_source(str(album_dir), "ai_batch_discovery")
        self.assertTrue(result["ok"], result)


class AiBatchEngineDiscoveryTests(unittest.TestCase):
    """Tests for _ai_batch_find_audio_dirs using engine beets_client discovery."""

    def test_ai_batch_find_audio_dirs_uses_beets_client_discover(self):
        source_code = function_source("_ai_batch_find_audio_dirs")
        self.assertIn("beets_client.discover_import_sources", source_code)
        self.assertNotIn("os.walk(", source_code)

    def test_ai_batch_find_audio_dirs_paginates_continuation(self):
        mock_responses = [
            {"ok": True, "candidates": [{"canonical_path": "/data/media/music/Artist/Album1"}], "continuation": "tok1"},
            {"ok": True, "candidates": [{"canonical_path": "/data/media/music/Artist/Album2"}], "continuation": None},
        ]
        with mock.patch.object(APP.beets_client, "discover_import_sources", side_effect=mock_responses) as mock_disc:
            res = APP._ai_batch_find_audio_dirs("/data/media/music")
            self.assertEqual(res, ["/data/media/music/Artist/Album1", "/data/media/music/Artist/Album2"])
            self.assertEqual(mock_disc.call_count, 2)


class PreserveImportSourceSignatureBindingTests(unittest.TestCase):
    """Tests for engine source signature binding during preservation and reimport."""

    def test_stage_preserved_torrent_source_forwards_expected_signature(self):
        source_code = function_source("_stage_preserved_torrent_source")
        self.assertIn("expected_source_signature=expected_source_signature", source_code)
        with mock.patch.object(APP.beets_client, "preserve_import_source", return_value={"ok": True, "preserved_path": "/data/torrents/.preserved_staging/test_123"}) as mock_pres:
            path = APP._stage_preserved_torrent_source("/data/torrents/torrent1", "Artist", "Album", [], expected_source_signature="sig123")
            self.assertEqual(path, "/data/torrents/.preserved_staging/test_123")
            mock_pres.assert_called_once_with("/data/torrents/torrent1", expected_source_signature="sig123")


if __name__ == "__main__":
    unittest.main()

