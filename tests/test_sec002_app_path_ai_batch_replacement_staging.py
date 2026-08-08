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

Claude's independent final review (docs/TECHNICAL_DEBT.md, "Claude
independent final review of Wave 8") additionally found that
create_unmatched_draft() writing under MUSIC_ROOT was itself an
architecture violation: the web manager neither owns nor (in the shipped
Compose topology) has MUSIC_ROOT mounted at all. The route now writes
review-only metadata (no audio) under UNMATCHED_DRAFT_ROOT
(/web-manager-data/unmatched_drafts by default), which every shipped
Compose file actually mounts into this container. The tests below assert
containment under UNMATCHED_DRAFT_ROOT and, separately, that MUSIC_ROOT is
never touched at all.
"""
import json
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
        self.draft_root = self.tmp_root / "web-manager-data" / "unmatched_drafts"
        self.music_root = self.tmp_root / "music"
        self.outside = self.tmp_root / "outside"
        self.draft_root.mkdir(parents=True)
        self.music_root.mkdir(parents=True)
        self.outside.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self._draft_root_patcher = mock.patch.object(APP, "UNMATCHED_DRAFT_ROOT", self.draft_root)
        self._draft_root_patcher.start()
        self.addCleanup(self._draft_root_patcher.stop)
        # MUSIC_ROOT is deliberately ALSO patched to an isolated, empty temp
        # directory (never used as UNMATCHED_DRAFT_ROOT) so that
        # test_music_root_is_never_touched below is a genuine architecture
        # assertion, not just "the test happens not to look there."
        self._music_root_patcher = mock.patch.object(APP, "MUSIC_ROOT", self.music_root)
        self._music_root_patcher.start()
        self.addCleanup(self._music_root_patcher.stop)
        self.env_patcher = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}, clear=False)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        self.client = APP.app.test_client()

    def post_draft(self, **payload):
        return self.client.post("/api/library/unmatched-draft", json=payload)

    def test_normal_creation_succeeds_under_draft_root(self):
        res = self.post_draft(artist="Real Artist", album="Real Album", year="2024", tracks=[])
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()
        self.assertTrue(data.get("ok"))
        # Whatever path was reported (or not), the only files actually
        # written must live under draft_root.
        found = list(self.draft_root.rglob("draft.json"))
        self.assertEqual(len(found), 1)
        self.assertTrue(APP._path_is_under(found[0].resolve(), self.draft_root.resolve()))

    def test_music_root_is_never_touched(self):
        # The architecture point of this fix: create_unmatched_draft() must
        # not write into MUSIC_ROOT at all, not merely "not outside of it."
        self.post_draft(artist="Real Artist", album="Real Album", year="2024", tracks=[])
        self.post_draft(artist="../../../outside", album="Escape Album", year="", tracks=[])
        self.assertEqual(list(self.music_root.rglob("*")), [])

    def test_traversal_in_artist_does_not_escape_draft_root(self):
        res = self.post_draft(artist="../../../outside", album="Escape Album", year="", tracks=[])
        # Whether this returns ok=True (sanitized into a safe literal
        # component) or an error, the critical invariant is: nothing is
        # ever written outside draft_root.
        self.assertEqual(list(self.outside.rglob("*")), [])
        for p in self.tmp_root.rglob("draft.json"):
            self.assertTrue(APP._path_is_under(p.resolve(), self.draft_root.resolve()), p)

    def test_traversal_in_album_does_not_escape_draft_root(self):
        res = self.post_draft(artist="Real Artist", album="../../../outside/pwned", year="", tracks=[])
        self.assertEqual(list(self.outside.rglob("*")), [])
        for p in self.tmp_root.rglob("draft.json"):
            self.assertTrue(APP._path_is_under(p.resolve(), self.draft_root.resolve()), p)

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
        found = list(self.draft_root.rglob("musicbrainz_submission.txt"))
        self.assertEqual(len(found), 1)
        self.assertIn("Café Müsic", found[0].read_text(encoding="utf-8"))

    def test_traversal_in_year_does_not_escape_draft_root(self):
        res = self.post_draft(artist="Real Artist", album="Album", year="../../outside", tracks=[])
        self.assertEqual(list(self.outside.rglob("*")), [])

    def test_bidi_override_and_zero_width_chars_stripped_from_artist(self):
        # U+202E (RIGHT-TO-LEFT OVERRIDE) and U+200B (ZERO WIDTH SPACE) are
        # Unicode format characters, not ASCII control characters -- Claude's
        # independent review (SEC-002 Wave 8 final review) found the
        # pre-existing ASCII-only control-char strip in _safe_path_component
        # let these through unchanged. They can't escape draft_root (still a
        # single safe path component either way), but can make the resulting
        # folder name render deceptively, so they must not survive
        # sanitization either.
        res = self.post_draft(
            artist="Real​Artist‮evil", album="Album", year="2024", tracks=[],
        )
        self.assertEqual(res.status_code, 200, res.get_json())
        found = list(self.draft_root.rglob("draft.json"))
        self.assertEqual(len(found), 1)
        artist_dir_name = found[0].parent.parent.name
        self.assertNotIn("​", artist_dir_name)
        self.assertNotIn("‮", artist_dir_name)

    def test_different_draft_mapping_to_same_folder_is_a_collision(self):
        res1 = self.post_draft(artist="A/B", album="Album", year="2024", tracks=[])
        self.assertEqual(res1.status_code, 200, res1.get_json())
        res2 = self.post_draft(artist=r"A\B", album="Album", year="2024", tracks=[])
        # "A/B" and "A\B" both sanitize to the same folder component but are
        # different raw identities -- must not silently overwrite.
        self.assertEqual(res2.status_code, 409, res2.get_json())

    def test_same_draft_resubmitted_is_idempotent(self):
        res1 = self.post_draft(artist="Real Artist", album="Real Album", year="2024", tracks=[])
        self.assertEqual(res1.status_code, 200, res1.get_json())
        res2 = self.post_draft(artist="Real Artist", album="Real Album", year="2024", tracks=[])
        self.assertEqual(res2.status_code, 200, res2.get_json())

    def test_secret_like_track_fields_are_not_persisted(self):
        res = self.post_draft(
            artist="Real Artist", album="Real Album", year="2024",
            source_url="https://user:hunter2@example.test/release/1",
            tracks=[{
                "title": "Track One",
                "duration": "3:45",
                "api_key": "sk-should-not-be-here",
                "authorization": "Bearer super-secret-token",
            }],
        )
        self.assertEqual(res.status_code, 200, res.get_json())
        draft_json_path = next(self.draft_root.rglob("draft.json"))
        submission_path = next(self.draft_root.rglob("musicbrainz_submission.txt"))
        draft_text = draft_json_path.read_text(encoding="utf-8")
        submission_text = submission_path.read_text(encoding="utf-8")
        for secret in ("hunter2", "sk-should-not-be-here", "super-secret-token"):
            self.assertNotIn(secret, draft_text)
            self.assertNotIn(secret, submission_text)
        data = json.loads(draft_text)
        self.assertEqual(data["tracks"], [{"title": "Track One", "duration": "3:45"}])


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
        # Named allowed_root, not "trusted*": an earlier revision named this
        # fixture self.trusted, and CodeQL's py/clear-text-storage-sensitive-
        # data heuristic source detector matched that identifier's "trust"
        # substring as a sensitive-data name pattern, producing two false
        # positives (PR #70 alerts #530/#531) whose flagged dataflow traced
        # straight back to this benign test path variable -- confirmed via
        # the SARIF codeFlow's relatedLocations, not guessed. Renaming avoids
        # re-triggering the same heuristic on every future analysis of this
        # file without needing a standing dismissal.
        self.allowed_root = self.tmp_root / "allowed_root"
        self.outside = self.tmp_root / "outside"
        self.allowed_root.mkdir(parents=True)
        self.outside.mkdir(parents=True)
        (self.outside / "sentinel.flac").write_bytes(b"SHOULD_NOT_BE_SCANNED")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self._downloads_roots_patcher = mock.patch.object(
            APP, "_DOWNLOADS_ROOTS", APP._DOWNLOADS_ROOTS + [str(self.allowed_root)]
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
        traversal = str(self.allowed_root / ".." / ".." / "etc")
        with mock.patch.object(APP, "_ai_batch_find_audio_dirs") as mock_walk:
            result = APP._start_ai_batch_job(traversal)
        mock_walk.assert_not_called()
        body, status = result
        self.assertEqual(status, 400)

    def test_symlink_scan_path_rejected(self):
        link = self.allowed_root / "link_to_outside"
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
        valid_subdir = self.allowed_root / "torrent_album"
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
            result = APP._start_ai_batch_job(str(self.allowed_root) + "\x00evil")
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

    # SEC-002 Wave 8 ARCH-003: reimport_disk() no longer validates aldir
    # against its own filesystem at all -- it has no local view of
    # MUSIC_ROOT/DOWNLOADS_ROOT in the shipped Compose topology. Path
    # containment, existence, and symlink-safety are now engine-authoritative
    # (backend.beets_control_agent.inspect_import_source(), reusing
    # resolve_safe_path()'s full-realpath-then-containment check). These
    # tests now verify reimport_disk() correctly maps the engine's rejection
    # to a safe, generic web-manager-facing error, not a local check.
    # inspect_import_source() itself is tested directly (with real temp
    # directories, no mocking) in
    # tests/test_sec002_wave8_engine_ownership.py.

    def test_outside_root_aldir_rejected_with_generic_error_not_existence_leak(self):
        missing_outside = self.outside / "does_not_exist_either"
        with mock.patch.object(APP.beets_client, "inspect_import_source",
                                side_effect=APP.BeetsError("Beets API request error: invalid_path")):
            res = self.client.post("/api/albums/reimport-disk", json={
                "aldir": str(missing_outside), "mb_albumid": "11111111-1111-1111-1111-111111111111",
            })
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertNotIn(str(self.outside), str(data.get("error", "")))

    def test_existing_outside_root_aldir_rejected(self):
        with mock.patch.object(APP.beets_client, "inspect_import_source",
                                side_effect=APP.BeetsError("Beets API request error: invalid_path")):
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
        with mock.patch.object(APP.beets_client, "inspect_import_source",
                                side_effect=APP.BeetsError("Beets API request error: invalid_path")):
            res = self.client.post("/api/albums/reimport-disk", json={
                "aldir": str(link), "mb_albumid": "11111111-1111-1111-1111-111111111111",
            })
        self.assertEqual(res.status_code, 400)

    def test_engine_unavailable_returns_502_not_500(self):
        with mock.patch.object(APP.beets_client, "inspect_import_source",
                                side_effect=APP.BeetsUnavailableError("Beets Control Agent is unavailable")):
            res = self.client.post("/api/albums/reimport-disk", json={
                "aldir": "/data/torrents/music/some_album", "mb_albumid": "11111111-1111-1111-1111-111111111111",
            })
        self.assertEqual(res.status_code, 502)

    def test_valid_engine_inspection_reaches_past_validation(self):
        # A successful inspection response must be accepted and its
        # canonical_path used -- proving the route reaches past the
        # (now engine-side) validation gate for a legitimate source.
        with mock.patch.object(APP.beets_client, "inspect_import_source", return_value={
                    "ok": True, "canonical_path": "/data/torrents/music/real_album",
                    "audio_count": 3, "audio_files": [], "source_signature": "abc",
                }), \
             mock.patch.object(APP, "_library_album_ids_for_folder", return_value=[]):
            res = self.client.post("/api/albums/reimport-disk", json={
                "aldir": "/data/torrents/music/real_album", "mb_albumid": "11111111-1111-1111-1111-111111111111",
            })
        # Fails further downstream (no real job/DB/engine wired up in this
        # unit test) -- the point is it is NOT rejected at the validation
        # gate (400) the way the outside-root/symlink cases above are.
        self.assertNotEqual(res.status_code, 400)

    def test_unrecognized_engine_error_never_leaks_raw_response_text(self):
        # BeetsClient._request() falls back to embedding up to 200 raw
        # response-body characters in its exception message for any
        # non-JSON/unrecognized error response (e.g. an unexpected proxy or
        # framework error page) -- which could carry stack-trace-shaped
        # text. reimport_disk() must never forward that text verbatim.
        fake_traceback = (
            "Traceback (most recent call last):\n"
            '  File "/opt/beets-web-manager-agent/beets_control_agent.py", line 999\n'
            "  ZeroDivisionError: division by zero"
        )
        with mock.patch.object(APP.beets_client, "inspect_import_source",
                                side_effect=APP.BeetsError(f"Beets API request error: HTTP 500: {fake_traceback}")):
            res = self.client.post("/api/albums/reimport-disk", json={
                "aldir": "/data/torrents/music/some_album", "mb_albumid": "11111111-1111-1111-1111-111111111111",
            })
        self.assertEqual(res.status_code, 400)
        error_text = str(res.get_json().get("error", ""))
        self.assertNotIn("Traceback", error_text)
        self.assertNotIn("ZeroDivisionError", error_text)
        self.assertNotIn("beets_control_agent.py", error_text)


if __name__ == "__main__":
    unittest.main()
