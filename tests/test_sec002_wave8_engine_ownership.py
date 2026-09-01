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

    def test_reimport_disk_import_step_routes_through_beets_client_reimport_source(self):
        # SEC-002 Wave 8 final mutation binding: the mutation itself moved
        # from a bare _beet_run(... "import" ...) call to the reviewed,
        # signature-and-identity-bound atomic engine endpoint. The old
        # pattern reappearing here would silently reopen the "final
        # reimport mutation is not source-bound" gap.
        source = function_source("reimport_disk")
        self.assertNotIn('_beet_run(\n            base_tmp + ["import"', source)
        self.assertIn("beets_client.reimport_source(", source)
        self.assertIn("expected_source_signature=import_source_evidence.get(\"source_signature\")", source)
        self.assertIn("expected_deterministic_identity=expected_identity", source)
        self.assertIn('"config_override": temp_cfg_content', source)

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


@unittest.skipUnless(os.name == "posix", "resolve_safe_path() requires POSIX-absolute paths; the control agent only ever runs inside the Linux engine container")
class DiscoverImportSourcesPaginationTests(unittest.TestCase):
    """Real, unmocked tests of discover_import_sources() -- specifically its
    pagination correctness, which Claude's independent review proved
    unsound in the original implementation.

    The original cursor was a lexicographic "resume after this relative
    path" string marker. That looks equivalent to DFS traversal order but
    is not: "/" (0x2F) sorts *above* space, "-", and "." (0x20/0x2D/0x2E),
    all extremely common in real album/artist folder names ("Artist -
    Album"), so a sibling directory named "Parent-suffix" can sort
    *before* "Parent/child" as a string while still being visited *after*
    it in DFS order. Resuming by skipping every rel_dir <= the
    lexicographic marker then silently drops that sibling's entire
    subtree -- a real candidate-loss bug for common real-world names, not
    a theoretical one. The fix encodes the actual remaining DFS stack in
    the cursor instead of a lexicographic marker."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="beets_test_discover_pagination_", dir=".")
        self.tmp_root = Path(self.tmp_dir).resolve()
        self.staging_root = self.tmp_root / "staging"
        self.staging_root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self._staging_patcher = mock.patch.object(CONTROL_AGENT, "DOWNLOAD_PATH", str(self.staging_root))
        self._staging_patcher.start()
        self.addCleanup(self._staging_patcher.stop)

    def _make_album(self, rel_dir: str) -> None:
        album = self.staging_root / rel_dir
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"not-real-audio")

    def _collect_all_pages(self, root: str, *, max_candidates: int = 1, max_dirs: int = 1000, max_files: int = 1000):
        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            pages += 1
            self.assertLess(pages, 50, "pagination did not converge -- possible infinite loop")
            res = CONTROL_AGENT.discover_import_sources(
                root, "ai_batch_discovery", cursor,
                {"max_candidates": max_candidates, "max_dirs": max_dirs, "max_files": max_files},
            )
            self.assertTrue(res["ok"], res)
            for cand in res["candidates"]:
                seen.append(cand["relative_path"])
            if res["complete"]:
                self.assertIsNone(res["continuation"])
                break
            cursor = res["continuation"]
            self.assertIsNotNone(cursor)
        return seen

    def test_pagination_finds_every_candidate_exactly_once_with_adversarial_names(self):
        # "A-suffix" is the adversarial sibling: it sorts lexicographically
        # *before* "A/Z" (hyphen 0x2D < slash 0x2F) but is only visited
        # *after* "A/Z" in DFS order, since "A/Z" is A's own child.
        for rel in ("A/Z", "A-suffix", "AA", "B/A", "Z/A"):
            self._make_album(rel)

        seen = self._collect_all_pages(str(self.staging_root), max_candidates=1)

        expected = {"A/Z", "A-suffix", "AA", "B/A", "Z/A"}
        self.assertEqual(set(seen), expected, seen)
        self.assertEqual(len(seen), len(expected), f"duplicate candidates: {seen}")

    def test_pagination_survives_directory_limit_boundary(self):
        for i in range(6):
            self._make_album(f"Album{i:02d}")
        seen = self._collect_all_pages(str(self.staging_root), max_candidates=100, max_dirs=2)
        self.assertEqual(len(seen), 6, seen)
        self.assertEqual(len(set(seen)), 6, f"duplicate candidates: {seen}")

    def test_pagination_survives_file_limit_boundary_mid_directory(self):
        # Force the per-page file budget to be smaller than a single
        # directory's own child count, so the "defer whole directory"
        # all-or-nothing logic is actually exercised.
        album = self.staging_root / "BigAlbum"
        album.mkdir()
        for i in range(5):
            (album / f"{i:02d}.flac").write_bytes(b"x")
        self._make_album("OtherAlbum")
        seen = self._collect_all_pages(str(self.staging_root), max_candidates=100, max_dirs=1000, max_files=3)
        self.assertEqual(set(seen), {"BigAlbum", "OtherAlbum"}, seen)
        self.assertEqual(len(seen), 2, f"duplicate candidates: {seen}")

    def test_tampered_cursor_root_mismatch_rejected(self):
        self._make_album("Album1")
        self._make_album("Album2")
        res1 = CONTROL_AGENT.discover_import_sources(
            str(self.staging_root), "ai_batch_discovery", None, {"max_candidates": 1},
        )
        self.assertTrue(res1["ok"], res1)
        self.assertIsNotNone(res1["continuation"])

        other_root = self.staging_root / "Album1"
        res2 = CONTROL_AGENT.discover_import_sources(
            str(other_root), "ai_batch_discovery", res1["continuation"], {"max_candidates": 1},
        )
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["error_code"], "invalid_cursor")

    def test_malformed_cursor_rejected_not_crashed(self):
        self._make_album("Album1")
        for bad_cursor in ("not-base64-json!!!", "", "e30=", "null"):
            res = CONTROL_AGENT.discover_import_sources(
                str(self.staging_root), "ai_batch_discovery", bad_cursor, None,
            )
            if bad_cursor:
                self.assertFalse(res["ok"], (bad_cursor, res))
                self.assertEqual(res["error_code"], "invalid_cursor")

    def test_symlink_directory_never_traversed_by_discovery(self):
        outside = self.tmp_root / "outside"
        outside.mkdir()
        (outside / "secret.flac").write_bytes(b"should never be seen")
        self._make_album("RealAlbum")
        link = self.staging_root / "escape_link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("platform/user cannot create symlinks")
        seen = self._collect_all_pages(str(self.staging_root), max_candidates=100)
        self.assertEqual(seen, ["RealAlbum"])


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


class ReimportDiskSinglePreservationTests(unittest.TestCase):
    """reimport_source_atomic() (called by reimport_disk() at the mutation
    boundary) detects a torrent-staged source and preserves it internally,
    immediately before the same Beets import it protects. reimport_disk()
    must not also preserve up front -- doing both would silently double-copy
    the source (see docs/TECHNICAL_DEBT.md, "SEC-002 Wave 8: production
    reimport binding")."""

    def test_stage_preserved_torrent_source_helper_removed(self):
        self.assertFalse(
            hasattr(APP, "_stage_preserved_torrent_source"),
            "a separate up-front preservation helper reappeared; "
            "reimport_source_atomic() must be the only thing that preserves "
            "a torrent-staged source before import",
        )

    def test_reimport_disk_does_not_call_preserve_import_source_directly(self):
        source = function_source("reimport_disk")
        self.assertNotIn("beets_client.preserve_import_source", source)
        self.assertNotIn("_preserve_torrent_source_path(aldir)", source)


class VerifyDeterministicIdentityFailClosedTests(unittest.TestCase):
    """verify_deterministic_identity() must never return ok: True on the
    strength of absent evidence -- Claude's independent review found the
    original implementation did exactly that (empty expected_identity,
    empty source MBID sets, and a swallowed DB exception all produced
    ok: True). None of these tests touch a real database (existing_album_id
    is omitted throughout), so they need no POSIX/engine-container
    environment."""

    def test_no_expected_identity_requires_review_not_success(self):
        result = CONTROL_AGENT.verify_deterministic_identity({"audio_files": []}, None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "review_required")

    def test_empty_expected_identity_dict_requires_review(self):
        result = CONTROL_AGENT.verify_deterministic_identity({"audio_files": []}, {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "review_required")

    def test_target_mb_albumid_with_no_source_tags_requires_review_not_success(self):
        # This is the exact fail-open case Claude's review found: expecting
        # mb_albumid but the source has zero embedded MusicBrainz tags to
        # check it against previously produced ok: True.
        source_inspect = {"audio_files": [{"properties": {}}, {"properties": {}}]}
        result = CONTROL_AGENT.verify_deterministic_identity(
            source_inspect, {"mb_albumid": "11111111-1111-1111-1111-111111111111"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "review_required")

    def test_target_mb_rgid_with_no_source_tags_requires_review(self):
        source_inspect = {"audio_files": [{"properties": {}}]}
        result = CONTROL_AGENT.verify_deterministic_identity(
            source_inspect, {"mb_releasegroupid": "22222222-2222-2222-2222-222222222222"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "review_required")

    def test_matching_embedded_mb_albumid_succeeds(self):
        mbid = "11111111-1111-1111-1111-111111111111"
        source_inspect = {"audio_files": [{"properties": {"mb_albumid": mbid}}]}
        result = CONTROL_AGENT.verify_deterministic_identity(source_inspect, {"mb_albumid": mbid})
        self.assertTrue(result["ok"], result)

    def test_conflicting_embedded_mb_albumid_is_a_hard_mismatch(self):
        source_inspect = {"audio_files": [{"properties": {"mb_albumid": "33333333-3333-3333-3333-333333333333"}}]}
        result = CONTROL_AGENT.verify_deterministic_identity(
            source_inspect, {"mb_albumid": "11111111-1111-1111-1111-111111111111"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "identity_mismatch")

    def test_track_count_mismatch_is_a_hard_mismatch(self):
        source_inspect = {"audio_files": [{"properties": {}}] * 3}
        result = CONTROL_AGENT.verify_deterministic_identity(source_inspect, {"track_count": 12})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "identity_mismatch")

    def test_existing_album_id_db_error_fails_closed_not_open(self):
        source_inspect = {"audio_files": [{"properties": {"mb_albumid": "11111111-1111-1111-1111-111111111111"}}]}
        with mock.patch("sqlite3.connect", side_effect=OSError("db unavailable")):
            result = CONTROL_AGENT.verify_deterministic_identity(
                source_inspect, {"existing_album_id": 42},
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "identity_verification_failed")

    def test_existing_album_id_alone_with_no_corroborating_evidence_requires_review(self):
        # existing_album_id alone only proves that DB row exists -- it says
        # nothing about whether THIS source folder's content is that
        # album's content. An earlier version of this check returned
        # ok: True here on the strength of the DB row alone, which Claude's
        # adversarial testing during final review proved lets an
        # authenticated caller attach any trusted-but-unrelated, untagged
        # staging folder to any existing album ID and have it imported with
        # zero content verification. See
        # test_unrelated_source_with_existing_album_id_is_never_authorized
        # for the full end-to-end proof.
        source_inspect = {
            "audio_files": [{"properties": {}}, {"properties": {}}],
            "canonical_path": "/data/torrents/unrelated_folder",
        }
        with mock.patch("sqlite3.connect") as mock_connect:
            mock_con = mock_connect.return_value.__enter__.return_value
            mock_con.row_factory = None
            mock_cur = mock_con.cursor.return_value
            mock_cur.execute.return_value.fetchone.return_value = {"id": 42, "mb_albumid": ""}
            mock_cur.execute.return_value.fetchall.return_value = [
                {"path": "/data/media/music/Some/Other/Album/01.flac"},
            ]
            result = CONTROL_AGENT.verify_deterministic_identity(
                source_inspect, {"existing_album_id": 42},
            )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error_code"], "review_required")

    def test_existing_album_id_with_source_matching_its_own_library_path_succeeds(self):
        # The legitimate case existing_album_id-alone evidence exists for:
        # repairing an album's own already-organized library folder, which
        # requires no embedded tags because the source path itself, cross-
        # checked against that album's own current item paths in the
        # library DB, is real, deterministic, content-independent proof.
        source_inspect = {
            "audio_files": [{"properties": {}}, {"properties": {}}],
            "canonical_path": "/data/media/music/Real Artist/Real Album",
        }
        with mock.patch("sqlite3.connect") as mock_connect:
            mock_con = mock_connect.return_value.__enter__.return_value
            mock_con.row_factory = None
            mock_cur = mock_con.cursor.return_value
            mock_cur.execute.return_value.fetchone.return_value = {"id": 42, "mb_albumid": ""}
            mock_cur.execute.return_value.fetchall.return_value = [
                {"path": "/data/media/music/Real Artist/Real Album/01.flac"},
            ]
            result = CONTROL_AGENT.verify_deterministic_identity(
                source_inspect, {"existing_album_id": 42},
            )
        self.assertTrue(result["ok"], result)

    def test_existing_album_id_conflicting_with_embedded_source_tags_is_a_mismatch(self):
        source_inspect = {"audio_files": [{"properties": {"mb_albumid": "33333333-3333-3333-3333-333333333333"}}]}
        with mock.patch("sqlite3.connect") as mock_connect:
            mock_con = mock_connect.return_value.__enter__.return_value
            mock_con.row_factory = None
            mock_cur = mock_con.cursor.return_value
            mock_cur.execute.return_value.fetchone.return_value = {
                "id": 42, "mb_albumid": "11111111-1111-1111-1111-111111111111",
            }
            result = CONTROL_AGENT.verify_deterministic_identity(
                source_inspect, {"existing_album_id": 42},
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "identity_mismatch")

    def test_error_messages_never_leak_raw_exception_text(self):
        source_inspect = {"audio_files": []}
        with mock.patch("sqlite3.connect", side_effect=OSError("/config/musiclibrary.blb: permission denied")):
            result = CONTROL_AGENT.verify_deterministic_identity(source_inspect, {"existing_album_id": 1})
        self.assertNotIn("musiclibrary.blb", result.get("message", ""))
        self.assertNotIn("permission denied", result.get("message", ""))


@unittest.skipUnless(os.name == "posix", "resolve_safe_path() requires POSIX-absolute paths; the control agent only ever runs inside the Linux engine container")
class PreserveImportSourceRootPolicyTests(unittest.TestCase):
    """preserve_import_source() must be staging-only (it exists specifically
    to protect torrent/download sources during import) and must never
    silently overwrite a differently-sourced preserved copy."""

    def setUp(self):
        # Deliberately the OS temp dir here, NOT dir="." (repo checkout):
        # preserve_import_source()'s post-copy verification compares
        # shutil.copy2()-preserved mtime_ns between source and destination,
        # and a Windows-host bind mount (as used elsewhere in this session
        # specifically to avoid "/tmp is already a trusted root" false
        # positives on the *web-manager* side) truncates that precision to
        # whole seconds through its file-sharing translation layer, causing
        # a false post-copy mismatch that never happens on a real Linux
        # filesystem. That web-manager concern doesn't apply here:
        # _allowed_root_paths()'s "tmp" category is never requested by
        # these tests (only "staging"/"music" are), so using the OS temp
        # dir cannot make an "outside" fixture accidentally trusted.
        self.tmp_dir = tempfile.mkdtemp(prefix="beets_test_preserve_policy_")
        self.tmp_root = Path(self.tmp_dir).resolve()
        self.staging_root = self.tmp_root / "staging"
        self.music_root = self.tmp_root / "music"
        self.staging_root.mkdir(parents=True)
        self.music_root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self._staging_patcher = mock.patch.object(CONTROL_AGENT, "DOWNLOAD_PATH", str(self.staging_root))
        self._music_patcher = mock.patch.object(CONTROL_AGENT, "MUSIC_LIBRARY_PATH", str(self.music_root))
        self._staging_patcher.start()
        self._music_patcher.start()
        self.addCleanup(self._staging_patcher.stop)
        self.addCleanup(self._music_patcher.stop)

    def test_music_library_source_rejected(self):
        album = self.music_root / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"not-real-audio")
        result = CONTROL_AGENT.preserve_import_source(str(album))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_path")

    def test_staging_source_preserved_successfully(self):
        album = self.staging_root / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"not-real-audio")
        result = CONTROL_AGENT.preserve_import_source(str(album))
        self.assertTrue(result["ok"], result)
        preserved = Path(result["preserved_path"])
        self.assertTrue(preserved.is_dir())
        self.assertTrue((preserved / "01.flac").exists())
        # Original source must be untouched.
        self.assertTrue((album / "01.flac").exists())

    def test_stale_source_rejected_before_copy(self):
        album = self.staging_root / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"not-real-audio")
        result = CONTROL_AGENT.preserve_import_source(str(album), expected_source_signature="stale-signature-that-never-matches")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "stale_source")

    def test_retry_with_unchanged_source_is_idempotent(self):
        album = self.staging_root / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"not-real-audio")
        first = CONTROL_AGENT.preserve_import_source(str(album), plan_id="plan1")
        self.assertTrue(first["ok"], first)
        second = CONTROL_AGENT.preserve_import_source(str(album), plan_id="plan1")
        self.assertTrue(second["ok"], second)
        self.assertTrue(second.get("already_preserved"))
        self.assertEqual(second["preserved_path"], first["preserved_path"])

    def test_error_message_never_leaks_raw_exception_text(self):
        # Wave 28 independent review: the byte-copy call moved from
        # shutil.copy2() to shutil.copyfile() (metadata preservation is now
        # a separate, best-effort step) -- this must patch the function that
        # actually performs the copy, or the mock silently never intercepts
        # anything and the test would pass for the wrong reason (the real
        # copy would just succeed).
        album = self.staging_root / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"not-real-audio")
        with mock.patch("shutil.copyfile", side_effect=OSError("/some/internal/host/path: disk quota exceeded")):
            result = CONTROL_AGENT.preserve_import_source(str(album))
        self.assertFalse(result["ok"])
        self.assertNotIn("/some/internal/host/path", result.get("message", ""))
        self.assertNotIn("disk quota exceeded", result.get("message", ""))

    # -- Wave 28 independent review, item 7: 12 new regression tests for
    # preservation-copy semantics (content-signature correctness, the
    # fail-closed collision policy, and the byte-copy/metadata-preservation
    # split). These use real temp directories and real file bytes throughout
    # -- no mocking of the signature/digest functions themselves -- so they
    # actually exercise _import_source_content_signature()'s real byte-digest loop.

    def test_unchanged_source_content_signature_is_stable_across_two_inspections(self):
        album = self.staging_root / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"identical-bytes")
        first = CONTROL_AGENT.inspect_import_source(str(album), "reimport", compute_content_signature=True)
        second = CONTROL_AGENT.inspect_import_source(str(album), "reimport", compute_content_signature=True)
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(first["content_signature"], second["content_signature"])
        self.assertNotEqual(first["content_signature"], "")

    def test_same_second_modification_is_still_detected_as_stale(self):
        # A same-source TOCTOU check (_import_source_signature, path+size+
        # mtime_ns) must catch a real content change even when the whole
        # thing happens inside one wall-clock second -- mtime_ns has
        # nanosecond resolution even though the filesystem's *displayed*
        # mtime may read as unchanged at second granularity.
        album = self.staging_root / "Artist" / "Album"
        album.mkdir(parents=True)
        f = album / "01.flac"
        f.write_bytes(b"original-bytes")
        inspect_res = CONTROL_AGENT.inspect_import_source(str(album), "reimport")
        sig = inspect_res["source_signature"]
        f.write_bytes(b"changed-bytes-different-size")
        result = CONTROL_AGENT.preserve_import_source(str(album), expected_source_signature=sig)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "stale_source")

    def test_same_path_and_size_different_bytes_is_not_content_equivalent(self):
        album_a = self.staging_root / "A"
        album_b = self.staging_root / "B"
        album_a.mkdir(parents=True)
        album_b.mkdir(parents=True)
        (album_a / "01.flac").write_bytes(b"AAAAAAAAAA")
        (album_b / "01.flac").write_bytes(b"BBBBBBBBBB")
        sig_a = CONTROL_AGENT.inspect_import_source(str(album_a), "reimport", compute_content_signature=True)["content_signature"]
        sig_b = CONTROL_AGENT.inspect_import_source(str(album_b), "reimport", compute_content_signature=True)["content_signature"]
        self.assertNotEqual(sig_a, sig_b)

    def test_different_size_is_not_content_equivalent(self):
        album_a = self.staging_root / "A"
        album_b = self.staging_root / "B"
        album_a.mkdir(parents=True)
        album_b.mkdir(parents=True)
        (album_a / "01.flac").write_bytes(b"short")
        (album_b / "01.flac").write_bytes(b"a-much-longer-payload-than-short")
        sig_a = CONTROL_AGENT.inspect_import_source(str(album_a), "reimport", compute_content_signature=True)["content_signature"]
        sig_b = CONTROL_AGENT.inspect_import_source(str(album_b), "reimport", compute_content_signature=True)["content_signature"]
        self.assertNotEqual(sig_a, sig_b)

    def test_added_file_is_not_content_equivalent(self):
        album_a = self.staging_root / "A"
        album_b = self.staging_root / "B"
        album_a.mkdir(parents=True)
        album_b.mkdir(parents=True)
        (album_a / "01.flac").write_bytes(b"same")
        (album_b / "01.flac").write_bytes(b"same")
        (album_b / "02.flac").write_bytes(b"extra")
        sig_a = CONTROL_AGENT.inspect_import_source(str(album_a), "reimport", compute_content_signature=True)["content_signature"]
        sig_b = CONTROL_AGENT.inspect_import_source(str(album_b), "reimport", compute_content_signature=True)["content_signature"]
        self.assertNotEqual(sig_a, sig_b)

    def test_removed_file_is_not_content_equivalent(self):
        album_a = self.staging_root / "A"
        album_b = self.staging_root / "B"
        album_a.mkdir(parents=True)
        album_b.mkdir(parents=True)
        (album_a / "01.flac").write_bytes(b"same")
        (album_a / "02.flac").write_bytes(b"extra")
        (album_b / "01.flac").write_bytes(b"same")
        sig_a = CONTROL_AGENT.inspect_import_source(str(album_a), "reimport", compute_content_signature=True)["content_signature"]
        sig_b = CONTROL_AGENT.inspect_import_source(str(album_b), "reimport", compute_content_signature=True)["content_signature"]
        self.assertNotEqual(sig_a, sig_b)

    def test_renamed_file_is_not_content_equivalent(self):
        album_a = self.staging_root / "A"
        album_b = self.staging_root / "B"
        album_a.mkdir(parents=True)
        album_b.mkdir(parents=True)
        (album_a / "01.flac").write_bytes(b"same-bytes")
        (album_b / "01-renamed.flac").write_bytes(b"same-bytes")
        sig_a = CONTROL_AGENT.inspect_import_source(str(album_a), "reimport", compute_content_signature=True)["content_signature"]
        sig_b = CONTROL_AGENT.inspect_import_source(str(album_b), "reimport", compute_content_signature=True)["content_signature"]
        self.assertNotEqual(sig_a, sig_b)

    def test_reduced_timestamp_precision_after_real_copy_is_still_content_equivalent(self):
        # Simulates a filesystem/copy path that truncates mtime precision
        # (e.g. a coarser-precision target filesystem): the content
        # signature must be based on real bytes only, never mtime, so a
        # genuine byte-for-byte copy with degraded timestamp precision is
        # still recognized as content-equivalent.
        album = self.staging_root / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"payload-bytes")
        first = CONTROL_AGENT.inspect_import_source(str(album), "reimport", compute_content_signature=True)
        # Truncate this file's own mtime to whole-second precision in place,
        # exactly what a coarser filesystem's copy would produce, without
        # touching the bytes at all.
        st = (album / "01.flac").stat()
        os.utime(str(album / "01.flac"), ns=((st.st_atime_ns // 10**9) * 10**9, (st.st_mtime_ns // 10**9) * 10**9))
        second = CONTROL_AGENT.inspect_import_source(str(album), "reimport", compute_content_signature=True)
        self.assertEqual(first["content_signature"], second["content_signature"])

    def test_copymode_failure_does_not_fail_a_successful_byte_copy(self):
        # Metadata preservation (mode bits) is best-effort per the review
        # finding -- a real byte copy that fully succeeds must not be
        # reported as a failure just because shutil.copymode() couldn't set
        # permission bits (e.g. a filesystem that doesn't support them).
        album = self.staging_root / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"real-audio-bytes")
        with mock.patch("shutil.copymode", side_effect=OSError("operation not supported")):
            result = CONTROL_AGENT.preserve_import_source(str(album))
        self.assertTrue(result["ok"], result)
        preserved = Path(result["preserved_path"])
        self.assertEqual((preserved / "01.flac").read_bytes(), b"real-audio-bytes")

    def test_real_byte_copy_failure_fails_closed_not_masked_by_a_fallback(self):
        # The previous `except Exception: shutil.copy(...)` fallback could
        # convert a genuine byte-copy failure into an apparent success via a
        # second copy attempt. The rewritten code has no such fallback:
        # shutil.copyfile() failing must propagate as a real, reported
        # failure, and must never silently retry via a different copy
        # function.
        album = self.staging_root / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"real-audio-bytes")
        with mock.patch("shutil.copyfile", side_effect=OSError("disk full")), \
             mock.patch("shutil.copy", side_effect=AssertionError(
                 "preserve_import_source must never fall back to shutil.copy()")):
            result = CONTROL_AGENT.preserve_import_source(str(album))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "copy_failed")

    def test_destination_collision_with_unverified_content_does_not_destroy_existing_data(self):
        # Review finding: the previous behavior on a destination-name
        # collision was an unconditional shutil.rmtree() + recreate. A
        # collision with content that does NOT verifiably match the current
        # source must fail closed and leave whatever is actually at the
        # destination completely untouched.
        album = self.staging_root / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"original-bytes")
        first = CONTROL_AGENT.preserve_import_source(str(album), plan_id="collision-plan")
        self.assertTrue(first["ok"], first)
        preserved = Path(first["preserved_path"])
        sentinel = preserved / "unrelated_marker.txt"
        sentinel.write_bytes(b"do-not-delete-me")
        # Change the source's content (keeping the same relative path/size
        # shape irrelevant -- what matters is the content digest no longer
        # matches) so the destination folder name collides but the content
        # signature does not.
        (album / "01.flac").write_bytes(b"different-bytes!")
        second = CONTROL_AGENT.preserve_import_source(str(album), plan_id="collision-plan")
        self.assertFalse(second["ok"])
        self.assertEqual(second["error_code"], "collision")
        # The pre-existing destination (and the sentinel file proving it's
        # the SAME directory, not a fresh recreation) must still be there.
        self.assertTrue(preserved.is_dir())
        self.assertTrue(sentinel.exists())
        self.assertEqual(sentinel.read_bytes(), b"do-not-delete-me")

    def test_raw_exception_path_and_signature_values_never_reach_the_http_response(self):
        # Broader than the single copy2/copyfile-specific leak test above:
        # proves the *post-copy verification* failure path (a different
        # branch of preserve_import_source than the outer exception handler)
        # also never serializes raw content-signature hex or internal host
        # paths into the client-facing message.
        album = self.staging_root / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"real-audio-bytes")
        original_inspect = CONTROL_AGENT.inspect_import_source
        call_count = {"n": 0}

        def flaky_inspect(path, operation, **kwargs):
            call_count["n"] += 1
            res = original_inspect(path, operation, **kwargs)
            # Corrupt only the post-copy destination re-inspection (the
            # second call for this destination path) so verification fails
            # without touching the initial source inspection.
            if call_count["n"] > 1 and kwargs.get("compute_content_signature"):
                res = dict(res)
                res["content_signature"] = "deliberately-mismatched-" + str(path)
            return res

        with mock.patch.object(CONTROL_AGENT, "inspect_import_source", side_effect=flaky_inspect):
            result = CONTROL_AGENT.preserve_import_source(str(album))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "copy_failed")
        message = result.get("message", "")
        self.assertNotIn(str(self.staging_root), message)
        self.assertNotIn("deliberately-mismatched-", message)


class ReimportTimeoutScalingTests(unittest.TestCase):
    """The engine's own Beets-import subprocess timeout must scale with the
    freshly re-inspected audio_count instead of a flat 180s -- a flat
    timeout silently regressed large-album imports (SEC-002 Wave 8
    production reimport binding). Mirrors reimport_disk()'s own
    _beet_import_timeout_for_count() bounds exactly."""

    def test_small_count_uses_the_floor(self):
        self.assertEqual(CONTROL_AGENT._reimport_timeout_for_count(0), 300)
        self.assertEqual(CONTROL_AGENT._reimport_timeout_for_count(1), 300)

    def test_scales_linearly_between_bounds(self):
        self.assertEqual(CONTROL_AGENT._reimport_timeout_for_count(10), 380)

    def test_large_count_is_capped_at_the_ceiling(self):
        self.assertEqual(CONTROL_AGENT._reimport_timeout_for_count(1000), 1200)
        self.assertEqual(CONTROL_AGENT._reimport_timeout_for_count(10_000), 1200)

    def test_negative_or_missing_count_does_not_break_the_floor(self):
        self.assertEqual(CONTROL_AGENT._reimport_timeout_for_count(-5), 300)
        self.assertEqual(CONTROL_AGENT._reimport_timeout_for_count(None), 300)


@unittest.skipUnless(os.name == "posix", "resolve_safe_path() requires POSIX-absolute paths; the control agent only ever runs inside the Linux engine container")
class ReimportSourceAtomicProductionPathTests(unittest.TestCase):
    """Real (non-mocked filesystem) behavioral tests of reimport_source_atomic()
    for the two scenarios reimport_disk() now depends on: an already-in-library
    source (copy:no/move:no, no preservation) and a torrent-staged source
    (preserved exactly once, internally, as part of the same atomic call)."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="beets_test_reimport_atomic_")
        self.tmp_root = Path(self.tmp_dir).resolve()
        self.staging_root = self.tmp_root / "staging"
        self.music_root = self.tmp_root / "music"
        self.staging_root.mkdir(parents=True)
        self.music_root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self._staging_patcher = mock.patch.object(CONTROL_AGENT, "DOWNLOAD_PATH", str(self.staging_root))
        self._music_patcher = mock.patch.object(CONTROL_AGENT, "MUSIC_LIBRARY_PATH", str(self.music_root))
        self._staging_patcher.start()
        self._music_patcher.start()
        self.addCleanup(self._staging_patcher.stop)
        self.addCleanup(self._music_patcher.stop)

        # A real (not mocked) sqlite DB so verify_deterministic_identity()'s
        # existing_album_id lookup and reimport_source_atomic()'s own
        # post-import album lookup -- two different call sites with
        # different row-access shapes (dict-style vs. positional) -- both
        # behave exactly as they do against the real engine, without having
        # to reconcile two mocks' row shapes by hand.
        import sqlite3 as _sqlite3
        self._sqlite3 = _sqlite3
        self.lib_path = str(self.tmp_root / "musiclibrary.blb")
        con = _sqlite3.connect(self.lib_path)
        con.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, mb_albumid TEXT)")
        con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path TEXT)")
        con.execute("INSERT INTO albums (id, mb_albumid) VALUES (42, '')")
        con.commit()
        con.close()
        self._lib_patcher = mock.patch.object(CONTROL_AGENT, "LIB_PATH", self.lib_path)
        self._lib_patcher.start()
        self.addCleanup(self._lib_patcher.stop)

    def _add_item_for_album_42(self, item_path):
        # verify_deterministic_identity()'s existing_album_id check requires
        # this album's own item paths (when it has no embedded-tag evidence
        # to cross-check) to actually be located under the source path --
        # see the adversarial test in VerifyDeterministicIdentityFailClosedTests.
        con = self._sqlite3.connect(self.lib_path)
        con.execute("INSERT INTO items (album_id, path) VALUES (42, ?)", (str(item_path),))
        con.commit()
        con.close()

    def _fake_subprocess_run(self, returncode=0, stdout="", stderr=""):
        class _Res:
            pass
        res = _Res()
        res.returncode = returncode
        res.stdout = stdout
        res.stderr = stderr
        # inspect_import_source() also shells out to ffprobe per audio file
        # via subprocess.run -- patching the module-global subprocess.run
        # intercepts those calls too, so callers must pick the "beet
        # import" invocation out of call_args_list rather than assuming
        # it's the only (or last) call.
        return mock.patch.object(CONTROL_AGENT.subprocess, "run", return_value=res)

    @staticmethod
    def _find_beet_import_call(mock_run):
        for call in mock_run.call_args_list:
            cmd = call.args[0] if call.args else call.kwargs.get("args")
            if isinstance(cmd, list) and "import" in cmd:
                return call
        raise AssertionError(
            f"no 'beet import' subprocess.run call found among {mock_run.call_args_list}"
        )

    def test_already_in_library_source_uses_copy_no_move_no_and_no_preservation(self):
        album = self.music_root / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"not-real-audio")
        self._add_item_for_album_42(album / "01.flac")
        # The temp config file is written just before the beet subprocess
        # call and unlinked in reimport_source_atomic()'s own finally block
        # -- it no longer exists by the time this test function regains
        # control, so its content must be captured while it's still there.
        with self._fake_subprocess_run() as mock_run, \
             mock.patch.object(CONTROL_AGENT.os, "unlink") as mock_unlink:
            result = CONTROL_AGENT.reimport_source_atomic(
                str(album),
                expected_deterministic_identity={"existing_album_id": 42},
                beets_options={"mb_albumid": "11111111-1111-1111-1111-111111111111"},
            )
            cfg_path = mock_unlink.call_args.args[0]
            cfg_content = Path(cfg_path).read_text(encoding="utf-8")
        Path(cfg_path).unlink(missing_ok=True)
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["preserved_path"])
        cmd = self._find_beet_import_call(mock_run).args[0]
        self.assertNotIn("--copy", cmd)
        self.assertNotIn("--move", cmd)
        self.assertIn("copy: no", cfg_content)
        self.assertIn("move: no", cfg_content)
        # CRITICAL command-shape regression guard, found live during
        # Claude's final review: "asis" is only meaningful as
        # --quiet-fallback's VALUE. A bare trailing "asis" not immediately
        # preceded by --quiet-fallback is parsed by real beets as an extra
        # positional PATH argument that does not exist, aborting the whole
        # import with "error: no such file or directory: asis" before it
        # ever reaches the real target -- confirmed against a real `beet`
        # binary. "-D" is not a real beet import flag at all (confirmed
        # against `beet import --help`); duplicate_action must only be
        # conveyed via config_override's import.duplicate_action YAML key.
        self.assertIn("--quiet-fallback", cmd)
        self.assertEqual(cmd[cmd.index("--quiet-fallback") + 1], "asis")
        self.assertNotIn("-D", cmd)
        self.assertIn("duplicate_action:", cfg_content)

    @unittest.skipUnless(shutil.which("beet"), "requires a real beet binary on PATH")
    def test_beet_import_command_is_syntactically_valid_against_real_beets(self):
        # The mocked-subprocess tests above cannot catch a malformed
        # command line (they never invoke a real parser) -- this is the
        # test that actually would have caught the --quiet-fallback/-D bug.
        # Skipped when no real beet binary is available (e.g. the
        # web-manager's own CI job), but exercised whenever one is -- as it
        # was during this review, which is how the bug was actually found.
        album = self.music_root / "Artist" / "RealBeetsCheck"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"not-real-audio")
        self._add_item_for_album_42(album / "01.flac")
        beets_config_dir = self.tmp_root / "beets_config"
        beets_config_dir.mkdir()
        with mock.patch.dict(os.environ, {"BEETSDIR": str(beets_config_dir)}), \
             mock.patch.object(CONTROL_AGENT, "BEETSDIR", str(beets_config_dir)), \
             mock.patch.object(CONTROL_AGENT, "BEET_BIN", shutil.which("beet")):
            result = CONTROL_AGENT.reimport_source_atomic(
                str(album),
                expected_deterministic_identity={"existing_album_id": 42},
                beets_options={"mb_albumid": "11111111-1111-1111-1111-111111111111"},
            )
        # A real, syntactically-invalid command returns import_failed here.
        # A non-audio fake file correctly produces "No files imported" but
        # is still ok: True with returncode 0 -- the point of this test is
        # that the command PARSES, not that a fake file gets tagged.
        self.assertTrue(result["ok"], result)

    def test_torrent_staged_source_is_preserved_exactly_once(self):
        # This test exercises preservation mechanics (exactly-once,
        # --copy), not identity-verification policy -- the item row below
        # is deliberately artificial (a real production existing_album_id
        # would never have its own item paths under a staging root) purely
        # to get the identity gate out of the way so preservation can be
        # observed in isolation.
        album = self.staging_root / "Torrent Album"
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"not-real-audio")
        self._add_item_for_album_42(album / "01.flac")
        with self._fake_subprocess_run() as mock_run, \
             mock.patch.object(CONTROL_AGENT, "preserve_import_source",
                                wraps=CONTROL_AGENT.preserve_import_source) as mock_preserve:
            result = CONTROL_AGENT.reimport_source_atomic(
                str(album),
                expected_deterministic_identity={"existing_album_id": 42},
                beets_options={"mb_albumid": "11111111-1111-1111-1111-111111111111"},
            )
        self.assertTrue(result["ok"], result)
        mock_preserve.assert_called_once()
        self.assertIsNotNone(result["preserved_path"])
        cmd = self._find_beet_import_call(mock_run).args[0]
        self.assertIn("--copy", cmd)
        self.assertIn(result["preserved_path"], cmd)

    def test_subprocess_timeout_scales_with_reinspected_audio_count(self):
        album = self.music_root / "Artist" / "BigAlbum"
        album.mkdir(parents=True)
        for i in range(20):
            (album / f"{i:02d}.flac").write_bytes(b"not-real-audio")
        self._add_item_for_album_42(album / "00.flac")
        with self._fake_subprocess_run() as mock_run:
            result = CONTROL_AGENT.reimport_source_atomic(
                str(album),
                expected_deterministic_identity={"existing_album_id": 42},
                beets_options={"mb_albumid": "11111111-1111-1111-1111-111111111111"},
            )
        self.assertTrue(result["ok"], result)
        kwargs = self._find_beet_import_call(mock_run).kwargs
        self.assertEqual(kwargs["timeout"], CONTROL_AGENT._reimport_timeout_for_count(20))
        self.assertGreater(kwargs["timeout"], 180)

    def test_unrelated_source_with_existing_album_id_is_never_authorized(self):
        # CRITICAL regression test: found live during Claude's final review
        # of this exact production wiring. An authenticated caller can
        # supply ANY trusted source_path alongside ANY existing_album_id --
        # both are ordinary request parameters. existing_album_id alone
        # (an earlier version of verify_deterministic_identity()) only
        # proved the DB row existed, not that the supplied source belonged
        # to it: an unrelated, untagged staging folder was silently
        # authorized and a real `beet import` subprocess was actually
        # launched against it. This must never happen again.
        real_album_dir = self.music_root / "Real Artist" / "Real Album"
        real_album_dir.mkdir(parents=True)
        (real_album_dir / "01.flac").write_bytes(b"real-album-audio")
        self._add_item_for_album_42(real_album_dir / "01.flac")

        unrelated_folder = self.staging_root / "Totally Unrelated Folder"
        unrelated_folder.mkdir(parents=True)
        (unrelated_folder / "01.mp3").write_bytes(b"unrelated-audio-content")

        with self._fake_subprocess_run() as mock_run:
            result = CONTROL_AGENT.reimport_source_atomic(
                str(unrelated_folder),
                expected_deterministic_identity={"existing_album_id": 42},
                beets_options={"mb_albumid": "22222222-2222-2222-2222-222222222222"},
            )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error_code"], "review_required")
        beet_import_calls = [
            c for c in mock_run.call_args_list
            if c.args and isinstance(c.args[0], list) and "import" in c.args[0]
        ]
        self.assertEqual(
            beet_import_calls, [],
            "beet import must never execute for an unverified source/existing_album_id pairing",
        )


if __name__ == "__main__":
    unittest.main()
