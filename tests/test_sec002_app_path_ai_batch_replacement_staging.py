"""SEC-002 Wave 8 regression tests: AI batch import scan-path validation and
unmatched-draft folder-name path safety.

Reconstructed live cluster (see docs/TECHNICAL_DEBT.md for the full matrix):
two genuine vulnerabilities, not CodeQL sanitizer-recognition false
positives:

1. start_ai_batch_import() / ai_batch_import_recover() both fed a
   caller/job-state-supplied scan_path straight into an unbounded recursive
   os.walk() (_ai_batch_find_audio_dirs) with no root-containment check at
   all -- only an existence probe. Fixed by validating in the single shared
   entry point (_start_ai_batch_job) both routes call into, so stored job
   state is revalidated at execution time rather than trusted because it
   was "already validated when queued."
2. create_unmatched_draft() built MUSIC_ROOT / artist / album_folder from
   entirely unsanitized client-supplied artist/album/year text, then called
   mkdir()+write_text() on the result -- an embedded "/" or ".." in any of
   those fields escapes MUSIC_ROOT entirely (Path's / operator re-parses
   embedded separators as additional components). Fixed by sanitizing each
   field into a single safe path component before folder-name construction,
   plus a final containment re-check before any destructive filesystem call.
"""
import os
import shutil
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import app as APP


class UnmatchedDraftPathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="beets_test_wave8_draft_")
        self.tmp_root = Path(self.tmp_dir).resolve()
        self.music_root = self.tmp_root / "music"
        self.outside = self.tmp_root / "outside"
        self.music_root.mkdir(parents=True)
        self.outside.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self._music_root_patcher = mock.patch.object(APP, "MUSIC_ROOT", self.music_root)
        self._music_root_patcher.start()
        self.addCleanup(self._music_root_patcher.stop)
        self.env_patcher = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}, clear=False)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        self.client = APP.app.test_client()

    def post_draft(self, **payload):
        return self.client.post("/api/library/unmatched-draft", json=payload)

    def test_normal_creation_succeeds_under_music_root(self):
        res = self.post_draft(artist="Real Artist", album="Real Album", year="2024", tracks=[])
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        created = Path(data["path"]) if "path" in data else None
        # Whatever path was reported (or not), the only files actually
        # written must live under music_root.
        found = list(self.music_root.rglob("draft.json"))
        self.assertEqual(len(found), 1)
        self.assertTrue(APP._path_is_under(found[0].resolve(), self.music_root.resolve()))

    def test_traversal_in_artist_does_not_escape_music_root(self):
        res = self.post_draft(artist="../../../outside", album="Escape Album", year="", tracks=[])
        # Whether this returns ok=True (sanitized into a safe literal
        # component) or an error, the critical invariant is: nothing is
        # ever written outside music_root.
        self.assertEqual(list(self.outside.rglob("*")), [])
        for p in self.tmp_root.rglob("draft.json"):
            self.assertTrue(APP._path_is_under(p.resolve(), self.music_root.resolve()), p)

    def test_traversal_in_album_does_not_escape_music_root(self):
        res = self.post_draft(artist="Real Artist", album="../../../outside/pwned", year="", tracks=[])
        self.assertEqual(list(self.outside.rglob("*")), [])
        for p in self.tmp_root.rglob("draft.json"):
            self.assertTrue(APP._path_is_under(p.resolve(), self.music_root.resolve()), p)

    def test_embedded_slash_in_artist_does_not_create_nested_traversal(self):
        res = self.post_draft(artist="foo/../../outside", album="Album", year="", tracks=[])
        self.assertEqual(list(self.outside.rglob("*")), [])

    def test_embedded_backslash_in_artist_does_not_escape(self):
        res = self.post_draft(artist=r"..\..\..\outside", album="Album", year="", tracks=[])
        self.assertEqual(list(self.outside.rglob("*")), [])

    def test_nul_byte_in_artist_does_not_crash(self):
        res = self.post_draft(artist="Artist\x00Injected", album="Album", year="", tracks=[])
        self.assertIn(res.status_code, (200, 400, 500))
        self.assertEqual(list(self.outside.rglob("*")), [])

    def test_unicode_and_literal_names_create_real_draft(self):
        res = self.post_draft(artist="Café Müsic", album="Naïve (Deluxe)", year="2024", tracks=[])
        self.assertEqual(res.status_code, 200, res.get_json())
        found = list(self.music_root.rglob("musicbrainz_submission.txt"))
        self.assertEqual(len(found), 1)
        self.assertIn("Café Müsic", found[0].read_text(encoding="utf-8"))

    def test_traversal_in_year_does_not_escape_music_root(self):
        res = self.post_draft(artist="Real Artist", album="Album", year="../../outside", tracks=[])
        self.assertEqual(list(self.outside.rglob("*")), [])


class AiBatchScanPathValidationTests(unittest.TestCase):
    """_start_ai_batch_job is the single shared entry point for both
    /api/ai-batch-import and /api/ai-batch-import/recover; validating there
    (rather than per-route) closes the gap for both callers, including the
    recover route's job-state-sourced scan_path."""

    def setUp(self):
        # Deliberately NOT under the OS temp directory: "/tmp" is already a
        # hardcoded trusted root in production _DOWNLOADS_ROOTS, so nesting
        # fixture roots there would make the "outside" sentinel accidentally
        # valid regardless of the containment logic under test (reproduces
        # on Linux, where tempfile.mkdtemp() defaults under /tmp; masked on
        # Windows, where it doesn't -- same class of bug as SEC-002 Wave 6/7).
        self.tmp_dir = tempfile.mkdtemp(prefix="beets_test_wave8_scan_", dir=os.getcwd())
        self.tmp_root = Path(self.tmp_dir).resolve()
        self.trusted = self.tmp_root / "trusted"
        self.outside = self.tmp_root / "outside"
        self.trusted.mkdir(parents=True)
        self.outside.mkdir(parents=True)
        (self.outside / "sentinel.flac").write_bytes(b"SHOULD_NOT_BE_SCANNED")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self._downloads_roots_patcher = mock.patch.object(
            APP, "_DOWNLOADS_ROOTS", APP._DOWNLOADS_ROOTS + [str(self.trusted)]
        )
        self._downloads_roots_patcher.start()
        self.addCleanup(self._downloads_roots_patcher.stop)
        # _start_ai_batch_job's error paths call jsonify(), which requires
        # an active Flask app context when invoked directly (not through a
        # real request).
        self._app_context = APP.app.app_context()
        self._app_context.push()
        self.addCleanup(self._app_context.pop)

    def test_outside_root_scan_path_rejected_before_any_walk(self):
        with mock.patch.object(APP, "_ai_batch_find_audio_dirs") as mock_walk:
            result = APP._start_ai_batch_job(str(self.outside))
        mock_walk.assert_not_called()
        self.assertIsInstance(result, tuple)
        body, status = result
        self.assertEqual(status, 400)
        self.assertFalse(body.get_json().get("ok"))

    def test_traversal_scan_path_rejected(self):
        traversal = str(self.trusted / ".." / ".." / "etc")
        with mock.patch.object(APP, "_ai_batch_find_audio_dirs") as mock_walk:
            result = APP._start_ai_batch_job(traversal)
        mock_walk.assert_not_called()
        body, status = result
        self.assertEqual(status, 400)

    def test_symlink_scan_path_rejected(self):
        link = self.trusted / "link_to_outside"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("platform/user cannot create symlinks")
        with mock.patch.object(APP, "_ai_batch_find_audio_dirs") as mock_walk:
            result = APP._start_ai_batch_job(str(link))
        mock_walk.assert_not_called()
        body, status = result
        self.assertEqual(status, 400)

    def test_valid_scan_path_passes_validation_gate(self):
        # Only assert the validation gate itself is passed (reaches worker
        # reservation) without spinning up the real background worker
        # machinery -- that is covered by tests/test_ai_batch_retry_race.py.
        # A subdirectory, not the trusted root itself: resolve_import_review
        # correctly refuses root-self operations, which is the behavior
        # under test in test_outside_root_scan_path_rejected_before_any_walk's
        # sibling root-self coverage elsewhere -- this test wants the
        # ordinary "valid" path.
        valid_subdir = self.trusted / "torrent_album"
        valid_subdir.mkdir()
        with mock.patch.object(APP, "_ai_batch_reserve_worker", return_value=False) as mock_reserve, \
             mock.patch.object(APP, "_ai_batch_reconnect_response", return_value=("reconnect", 200)) as mock_reconnect:
            result = APP._start_ai_batch_job(str(valid_subdir))
        mock_reserve.assert_called_once()
        mock_reconnect.assert_called_once()
        self.assertEqual(result, ("reconnect", 200))

    def test_recover_route_revalidates_stale_job_state_path(self):
        """A batch state file with an outside-root source_path (simulating
        corrupted/tampered/stale state, or state written before this fix
        existed) must be rejected at recovery time, not trusted because it
        was already queued."""
        with mock.patch.object(APP, "_ai_batch_reserve_worker") as mock_reserve:
            result = APP._start_ai_batch_job(str(self.outside), recover_batch_job_id="stale-batch-id")
        mock_reserve.assert_not_called()
        body, status = result
        self.assertEqual(status, 400)

    def test_nul_byte_scan_path_rejected(self):
        with mock.patch.object(APP, "_ai_batch_find_audio_dirs") as mock_walk:
            result = APP._start_ai_batch_job(str(self.trusted) + "\x00evil")
        mock_walk.assert_not_called()
        body, status = result
        self.assertEqual(status, 400)


class ReimportDiskPathSafetyTests(unittest.TestCase):
    def setUp(self):
        # See AiBatchScanPathValidationTests.setUp for why this must not be
        # under the OS temp directory ("/tmp" is already a hardcoded
        # trusted root in production).
        self.tmp_dir = tempfile.mkdtemp(prefix="beets_test_wave8_reimport_", dir=os.getcwd())
        self.tmp_root = Path(self.tmp_dir).resolve()
        self.music_root = self.tmp_root / "music"
        self.outside = self.tmp_root / "outside_secret"
        self.music_root.mkdir(parents=True)
        self.outside.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self._music_root_patcher = mock.patch.object(APP, "MUSIC_ROOT", self.music_root)
        self._music_root_patcher.start()
        self.addCleanup(self._music_root_patcher.stop)
        self.env_patcher = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}, clear=False)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        self.client = APP.app.test_client()

    def test_outside_root_aldir_rejected_with_generic_error_not_existence_leak(self):
        # A nonexistent, outside-root path must fail with the generic
        # containment error, not an existence-specific message -- proving
        # containment is checked before any filesystem existence probe.
        missing_outside = self.outside / "does_not_exist_either"
        res = self.client.post("/api/albums/reimport-disk", json={
            "aldir": str(missing_outside), "mb_albumid": "11111111-1111-1111-1111-111111111111",
        })
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertNotIn(str(self.outside), str(data.get("error", "")))

    def test_existing_outside_root_aldir_rejected(self):
        res = self.client.post("/api/albums/reimport-disk", json={
            "aldir": str(self.outside), "mb_albumid": "11111111-1111-1111-1111-111111111111",
        })
        self.assertEqual(res.status_code, 400)

    def test_symlink_aldir_rejected(self):
        link = self.music_root / "link_to_outside"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("platform/user cannot create symlinks")
        res = self.client.post("/api/albums/reimport-disk", json={
            "aldir": str(link), "mb_albumid": "11111111-1111-1111-1111-111111111111",
        })
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
