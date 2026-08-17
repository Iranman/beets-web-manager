"""SEC-002 Wave 9: Playlist, Plex Sync & Staging-Manifest Path Security.

Comprehensive regression tests for playlist staging, manifest generation,
M3U security, Plex path translation, and staged track deletion safety.
"""

from contextlib import contextmanager
import json
import os
import shutil
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

import app as app_module


def _record_staged_path(name, track, staged_path):
    """Establish server-owned manifest state the way a real download
    completion would, since requested_path is now only honored when it
    matches this recorded state."""
    app_module._playlist_store_track_state(
        name, track, "downloaded", staged_path=str(staged_path), path=str(staged_path))


@contextmanager
def _patched_playlist_state(base_dir):
    base = Path(base_dir)
    state_root = base / "state"
    manifests = state_root / "manifests"
    exports = state_root / "exports"
    jobs = state_root / "jobs"
    membership = state_root / "membership"
    for d in (base, state_root, manifests, exports, jobs, membership):
        d.mkdir(parents=True, exist_ok=True)
    patches = [
        mock.patch.object(app_module, "WEB_MANAGER_DATA_DIR", base),
        mock.patch.object(app_module, "PLAYLIST_STATE_ROOT", state_root),
        mock.patch.object(app_module, "PLAYLIST_MANIFESTS_DIR", manifests),
        mock.patch.object(app_module, "PLAYLIST_EXPORTS_DIR", exports),
        mock.patch.object(app_module, "PLAYLIST_JOB_STATE_DIR", jobs),
        mock.patch.object(app_module, "PLAYLIST_MEMBERSHIP_DIR", membership),
    ]
    for patcher in patches:
        patcher.start()
    try:
        yield {"base": base, "state": state_root, "manifests": manifests, "exports": exports, "jobs": jobs, "membership": membership}
    finally:
        for patcher in reversed(patches):
            patcher.stop()


class Wave9PlaylistPathSanitizationTests(unittest.TestCase):
    """Test suite for _clean_playlist_name and playlist staging root containment."""

    def test_clean_playlist_name_strips_traversal_and_separators(self):
        cases = [
            ("../../etc/passwd", "etc_passwd"),
            ("..\\..\\windows\\system32", "windows_system32"),
            ("normal-playlist", "normal-playlist"),
            ("My/Cool\\Playlist", "My_Cool_Playlist"),
            ("..", "Playlist"),
            (".", "Playlist"),
            ("  ...  ", "Playlist"),
            ("\x00\r\n\u200eBadName", "BadName"),
            ("<>:\"/\\|?*", "Playlist"),
        ]
        for raw, expected in cases:
            cleaned = app_module._clean_playlist_name(raw)
            self.assertNotIn("/", cleaned)
            self.assertNotIn("\\", cleaned)
            self.assertNotIn("..", cleaned)
            self.assertFalse(cleaned.startswith("."))
            self.assertEqual(cleaned, expected)

    def test_get_playlist_staging_root_containment(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patched_playlist_state(Path(tmp) / "state"):
                root = app_module.get_playlist_staging_root("../../../outside")
                staging_dir = app_module.PLAYLIST_DOWNLOAD_ROOT.resolve(strict=False)
                self.assertTrue(app_module._path_is_under(root.resolve(strict=False), staging_dir))
                self.assertNotIn("..", root.name)

    def test_playlist_ensure_staging_dirs_rejects_outside_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "staging"
            tmp_root.mkdir()
            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", tmp_root), \
                 _patched_playlist_state(Path(tmp) / "state"), \
                 mock.patch.object(app_module.beets_client, "ensure_playlist_staging",
                                    return_value={"ok": True}) as mock_ensure:
                app_module._playlist_ensure_staging_dirs("../../../outside")
                created_root = app_module.get_playlist_staging_root("../../../outside").resolve(strict=False)
                self.assertTrue(app_module._path_is_under(created_root, tmp_root.resolve(strict=False)))
                mock_ensure.assert_called_once()

    def test_playlist_ensure_staging_dirs_fails_truthfully_when_engine_unavailable(self):
        """No local mkdir/chmod fallback: if the engine call fails or the
        engine does not confirm success, the web manager must raise, not
        silently create directories on a filesystem it doesn't have in the
        supported topology and pretend staging succeeded."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "staging"
            tmp_root.mkdir()
            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", tmp_root), \
                 _patched_playlist_state(Path(tmp) / "state"), \
                 mock.patch.object(app_module.beets_client, "ensure_playlist_staging",
                                    side_effect=ConnectionError("engine unreachable")):
                with self.assertRaises(app_module.PlaylistStagingUnavailableError):
                    app_module._playlist_ensure_staging_dirs("Some Playlist")
                created_root = app_module.get_playlist_staging_root("Some Playlist")
                self.assertFalse(created_root.exists(), "no local directory may be created when the engine is unavailable")

    def test_playlist_ensure_staging_dirs_fails_truthfully_when_engine_says_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "staging"
            tmp_root.mkdir()
            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", tmp_root), \
                 _patched_playlist_state(Path(tmp) / "state"), \
                 mock.patch.object(app_module.beets_client, "ensure_playlist_staging",
                                    return_value={"ok": False, "error": "staging_unavailable"}):
                with self.assertRaises(app_module.PlaylistStagingUnavailableError):
                    app_module._playlist_ensure_staging_dirs("Some Playlist")
                created_root = app_module.get_playlist_staging_root("Some Playlist")
                self.assertFalse(created_root.exists())


class Wave9AtomicJsonReplaceSecurityTests(unittest.TestCase):
    """Test suite for _playlist_atomic_json_replace containment enforcement."""

    def test_atomic_json_replace_refuses_outside_target_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside_dir = Path(tmp) / "outside"
            outside_dir.mkdir()
            outside_file = outside_dir / "target.json"
            sentinel = outside_dir / "sentinel.txt"
            sentinel.write_text("protected content")

            with self.assertRaises(ValueError):
                app_module._playlist_atomic_json_replace(
                    outside_file,
                    {"data": "malicious"},
                    save_key="test",
                    label="test json",
                )

            self.assertFalse(outside_file.exists())
            self.assertEqual(sentinel.read_text(), "protected content")

    def test_atomic_json_replace_valid_inside_allowed_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "playlists"
            state_dir.mkdir()
            target_file = state_dir / "my_playlist.playlist.json"

            with _patched_playlist_state(state_dir):
                app_module._playlist_atomic_json_replace(
                    target_file,
                    {"name": "my_playlist", "version": 2},
                    save_key="test_key",
                    label="playlist manifest",
                )

            self.assertTrue(target_file.exists())
            data = json.loads(target_file.read_text(encoding="utf-8"))
            self.assertEqual(data["name"], "my_playlist")


class Wave9StagedTrackDeletionSecurityTests(unittest.TestCase):
    """Test suite for _playlist_delete_staged_track_file containment and symlink safety."""

    def test_delete_staged_track_refuses_deletion_outside_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging_root = Path(tmp) / "playlist-staging"
            playlist_state_dir = Path(tmp) / "playlists"
            staging_root.mkdir()
            playlist_state_dir.mkdir()
            outside_dir = Path(tmp) / "outside"
            outside_dir.mkdir()
            outside_mp3 = outside_dir / "secret.mp3"
            outside_mp3.write_bytes(b"audio data")

            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", staging_root), \
                 _patched_playlist_state(playlist_state_dir):
                track = {"id": "tr1"}
                _record_staged_path("MyPlaylist", track, outside_mp3)
                with self.assertRaises(RuntimeError) as cm:
                    app_module._playlist_delete_staged_track_file(
                        "MyPlaylist",
                        track,
                        requested_path=str(outside_mp3),
                    )
                self.assertIn("Refusing to delete a file outside this playlist's own staging directory", str(cm.exception))
                self.assertTrue(outside_mp3.exists())

    def test_delete_staged_track_refuses_non_audio_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging_root = Path(tmp) / "playlist-staging"
            playlist_state_dir = Path(tmp) / "playlists"
            staging_root.mkdir()
            playlist_state_dir.mkdir()
            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", staging_root), \
                 _patched_playlist_state(playlist_state_dir):
                # Nested under this playlist's own staging subfolder --
                # real staged files always live there, never directly
                # under the shared PLAYLIST_DOWNLOAD_ROOT (see the
                # cross-playlist containment narrowing above).
                playlist_staging = app_module.get_playlist_staging_root("MyPlaylist")
                playlist_staging.mkdir(parents=True)
                script_file = playlist_staging / "payload.py"
                script_file.write_text("import os; os.system('echo pwned')")
                track = {"id": "tr2"}
                _record_staged_path("MyPlaylist", track, script_file)
                with self.assertRaises(RuntimeError) as cm:
                    app_module._playlist_delete_staged_track_file(
                        "MyPlaylist",
                        track,
                        requested_path=str(script_file),
                    )
                self.assertIn("Refusing to delete a non-audio staging file", str(cm.exception))
                self.assertTrue(script_file.exists())

    def test_delete_staged_track_refuses_library_music_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging_root = Path(tmp) / "playlist-staging"
            playlist_state_dir = Path(tmp) / "playlists"
            staging_root.mkdir()
            playlist_state_dir.mkdir()
            music_root = Path(tmp) / "music"
            music_root.mkdir()
            library_track = music_root / "Artist" / "Album" / "01 - Track.mp3"
            library_track.parent.mkdir(parents=True)
            library_track.write_bytes(b"library track")

            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", staging_root), \
                 _patched_playlist_state(playlist_state_dir), \
                 mock.patch.object(app_module, "MUSIC_ROOT", music_root):
                track = {"id": "tr3"}
                _record_staged_path("MyPlaylist", track, library_track)
                with self.assertRaises(RuntimeError) as cm:
                    app_module._playlist_delete_staged_track_file(
                        "MyPlaylist",
                        track,
                        requested_path=str(library_track),
                    )
                self.assertIn("Refusing to delete a Beets library file", str(cm.exception))
                self.assertTrue(library_track.exists())

    def test_delete_staged_track_refuses_requested_path_mismatch(self):
        """requested_path must match the server-owned manifest state for
        this track; a caller-supplied path pointing anywhere else -- even a
        seemingly valid staged file -- must be rejected outright rather
        than silently substituted or silently honored."""
        with tempfile.TemporaryDirectory() as tmp:
            staging_root = Path(tmp) / "playlist-staging"
            playlist_state_dir = Path(tmp) / "playlists"
            staging_root.mkdir()
            playlist_state_dir.mkdir()

            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", staging_root), \
                 _patched_playlist_state(playlist_state_dir) as _patched:
                playlist_dir = app_module.get_playlist_staging_root("MyPlaylist")
                downloads = playlist_dir / "downloads"
                downloads.mkdir(parents=True)
                real_staged = downloads / "01 - Real Track.mp3"
                real_staged.write_bytes(b"real audio data")
                other_staged = downloads / "02 - Other Track.mp3"
                other_staged.write_bytes(b"other audio data")

                track = {"id": "tr-mismatch"}
                _record_staged_path("MyPlaylist", track, real_staged)

                with self.assertRaises(RuntimeError) as cm:
                    app_module._playlist_delete_staged_track_file(
                        "MyPlaylist",
                        track,
                        requested_path=str(other_staged),
                    )
                self.assertIn("does not match the server-recorded staged path", str(cm.exception))
                self.assertTrue(real_staged.exists())
                self.assertTrue(other_staged.exists())

    def test_delete_staged_track_deletes_valid_staged_mp3(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging_root = Path(tmp) / "playlist-staging"
            playlist_state_dir = Path(tmp) / "playlists"
            staging_root.mkdir()
            playlist_state_dir.mkdir()

            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", staging_root), \
                 _patched_playlist_state(playlist_state_dir) as _patched:
                playlist_dir = app_module.get_playlist_staging_root("MyPlaylist")
                playlist_downloads = playlist_dir / "downloads"
                playlist_downloads.mkdir(parents=True)
                staged_mp3 = playlist_downloads / "01 - Track One.mp3"
                staged_mp3.write_bytes(b"audio data")
                track = {"id": "tr4"}
                _record_staged_path("MyPlaylist", track, staged_mp3)

                def _fake_engine_delete(playlist_key, track_id, requested_path):
                    p = Path(requested_path)
                    existed = p.exists()
                    if existed:
                        p.unlink()
                    return {"ok": True, "deleted": existed, "already_absent": not existed, "path": requested_path}

                with mock.patch.object(app_module.beets_client, "delete_playlist_staged_track",
                                        side_effect=_fake_engine_delete) as mock_delete:
                    res = app_module._playlist_delete_staged_track_file(
                        "MyPlaylist",
                        track,
                        requested_path=str(staged_mp3),
                    )
                mock_delete.assert_called_once()
                self.assertTrue(res["deleted"])
                self.assertTrue(any((_patched["manifests"]).glob("*.playlist.json")))

    def test_delete_staged_track_fails_truthfully_when_engine_unavailable(self):
        """No local Path.unlink() fallback: if the engine call fails, the
        function must raise, not silently delete locally or pretend
        success."""
        with tempfile.TemporaryDirectory() as tmp:
            staging_root = Path(tmp) / "playlist-staging"
            playlist_state_dir = Path(tmp) / "playlists"
            staging_root.mkdir()
            playlist_state_dir.mkdir()

            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", staging_root), \
                 _patched_playlist_state(playlist_state_dir):
                playlist_dir = app_module.get_playlist_staging_root("MyPlaylist")
                playlist_downloads = playlist_dir / "downloads"
                playlist_downloads.mkdir(parents=True)
                staged_mp3 = playlist_downloads / "01 - Track One.mp3"
                staged_mp3.write_bytes(b"audio data")
                track = {"id": "tr5"}
                _record_staged_path("MyPlaylist", track, staged_mp3)

                with mock.patch.object(app_module.beets_client, "delete_playlist_staged_track",
                                        side_effect=ConnectionError("engine unreachable")):
                    with self.assertRaises(RuntimeError) as cm:
                        app_module._playlist_delete_staged_track_file(
                            "MyPlaylist",
                            track,
                            requested_path=str(staged_mp3),
                        )
                self.assertIn("Engine is unavailable", str(cm.exception))
                self.assertTrue(staged_mp3.exists(), "file must survive locally when the engine call fails")


class Wave9M3UAndPlexTranslationSecurityTests(unittest.TestCase):
    """Test suite for M3U generation and Plex path containment."""

    def test_create_playlist_outputs_sanitizes_m3u_newlines(self):
        with tempfile.TemporaryDirectory() as tmp:
            playlist_dir = Path(tmp) / "playlists"
            playlist_dir.mkdir()

            items = [
                {
                    "artist": "Artist\nInjected Header",
                    "title": "Title\r\nInjected Track",
                    "path": "/data/media/music/Artist/Album/01.mp3",
                }
            ]

            with _patched_playlist_state(Path(tmp) / "state"), \
                 mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", playlist_dir), \
                 mock.patch.object(app_module, "_plex_settings", return_value={"token": ""}), \
                 mock.patch.object(app_module.beets_client, "export_playlist_m3u", return_value={"ok": True, "playlist_key": "engine_key"}) as mock_export:
                result = app_module._create_playlist_outputs("CleanPlaylist", items, sync_plex=False)

            mock_export.assert_called_once()
            exported_items = mock_export.call_args.args[2]
            self.assertEqual(exported_items[0]["artist"], "Artist\nInjected Header")
            self.assertTrue(str(result.get("m3u", "")).startswith("engine:"))
            self.assertFalse((playlist_dir / "CleanPlaylist.m3u").exists())

    def test_plex_path_containment_rejects_prefix_collision(self):
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")):
            # /data/media/music2 must NOT be considered inside /data/media/music
            self.assertFalse(app_module._plex_path_is_under("/data/media/music2/song.mp3", "/data/media/music"))
            self.assertTrue(app_module._plex_path_is_under("/data/media/music/Artist/Album/01.mp3", "/data/media/music"))


class Wave9FinalReviewCorrectionTests(unittest.TestCase):
    """Regression tests for the corrections made during Claude's independent
    final review of PR #82 (SEC-002 Wave 9). Each test proves the specific
    vulnerable behavior the correction closes, not just the new code shape."""

    def test_unauthorized_absolute_path_does_not_gain_basename_authority(self):
        """An absolute path outside every allowed root (/etc/passwd, or a
        real file under an unrelated same-prefix root like .../music2) must
        not be silently reinterpreted as MUSIC_ROOT / <its basename> -- that
        invents a new, different, plausible-looking library path from
        attacker input instead of rejecting it."""
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")), \
             mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", Path("/data/torrents/music/Playlist Downloads")), \
             mock.patch.object(app_module, "DOWNLOADS_ROOT", Path("/data/torrents/music")), \
             mock.patch.object(app_module, "PLAYLIST_PATH_ROOT_ALIASES", []):
            for unauthorized in ("/etc/passwd", "/data/media/music2/song.mp3", "/secret/outside/song.mp3"):
                resolved = app_module._playlist_resolve_item_path(unauthorized)
                self.assertEqual(resolved, app_module._PLAYLIST_UNRESOLVED_PATH, unauthorized)
                # The dangerous old behavior: MUSIC_ROOT / Path(unauthorized).name
                self.assertNotEqual(resolved, Path("/data/media/music") / Path(unauthorized).name, unauthorized)
                self.assertFalse(resolved.exists())

    def test_empty_path_does_not_resolve_to_library_root(self):
        """An empty item path must never become authority for the library
        root itself -- root-self must not silently satisfy downstream
        containment/existence checks."""
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")):
            resolved = app_module._playlist_resolve_item_path("")
            self.assertNotEqual(resolved, app_module.MUSIC_ROOT)
            self.assertEqual(resolved, app_module._PLAYLIST_UNRESOLVED_PATH)
            self.assertFalse(resolved.exists())

    def test_relative_traversal_path_does_not_escape_music_root(self):
        """A relative item path containing traversal segments must not
        resolve outside MUSIC_ROOT once a caller calls .resolve()/.exists()
        on the returned path."""
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp) / "music"
            music_root.mkdir()
            outside_marker = Path(tmp) / "outside-secret.txt"
            outside_marker.write_text("do not expose")
            with mock.patch.object(app_module, "MUSIC_ROOT", music_root):
                resolved = app_module._playlist_resolve_item_path("../outside-secret.txt")
                self.assertEqual(resolved, app_module._PLAYLIST_UNRESOLVED_PATH)
                self.assertFalse(resolved.exists())

    def test_relative_path_within_music_root_still_resolves_normally(self):
        """The fix must not regress the legitimate case: a plain
        library-relative path still resolves under MUSIC_ROOT."""
        with mock.patch.object(app_module, "MUSIC_ROOT", Path("/data/media/music")):
            resolved = app_module._playlist_resolve_item_path("Artist/Album/01 - Track.mp3")
            self.assertEqual(resolved, Path("/data/media/music/Artist/Album/01 - Track.mp3"))

    def test_playlist_name_sanitization_collision_is_a_known_unresolved_defect(self):
        """_clean_playlist_name() maps distinct raw names to the same
        sanitized identity -- confirmed, real, NOT fixed by this review
        (would require a stable playlist-ID identity scheme, out of this
        narrow review's scope; see docs/TECHNICAL_DEBT.md). This test
        documents/locks the current (unsafe) behavior so it is not
        silently forgotten, rather than asserting it is safe."""
        collisions = ["A/B", "A\\B", "A:B", "A?B"]
        cleaned = {app_module._clean_playlist_name(name) for name in collisions}
        self.assertEqual(cleaned, {"A_B"}, "distinct playlist names collide to the same sanitized identity")

        truncation_a = "X" * 120 + "AAAA"
        truncation_b = "X" * 120 + "BBBB"
        self.assertEqual(
            app_module._clean_playlist_name(truncation_a),
            app_module._clean_playlist_name(truncation_b),
            "distinct long playlist names collide after 120-char truncation",
        )

    def test_cross_playlist_staged_deletion_is_refused(self):
        """A track-action delete_staged request for playlist A must not be
        able to delete a file staged for playlist B, even though both sit
        under the same shared PLAYLIST_DOWNLOAD_ROOT, and even if playlist
        A's own manifest state is stale/tampered to point at it -- this was
        reachable end-to-end from POST /api/playlists/<name>/tracks/action's
        raw, attacker-controlled payload["path"], which used to flow
        straight into requested_path with no server-side ownership check,
        and containment must still refuse it as defense-in-depth even now
        that requested_path is checked against manifest state first."""
        with tempfile.TemporaryDirectory() as tmp:
            shared_root = Path(tmp) / "Playlist Downloads"
            playlist_state_dir = Path(tmp) / "playlists"
            playlist_state_dir.mkdir()
            with mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", shared_root), \
                 _patched_playlist_state(playlist_state_dir):
                playlist_b_dir = app_module.get_playlist_staging_root("Playlist B") / "downloads"
                playlist_b_dir.mkdir(parents=True)
                victim_track = playlist_b_dir / "01 - Victim Track.mp3"
                victim_track.write_bytes(b"audio data belonging to playlist B")

                track = {"id": "cross-playlist-attack"}
                _record_staged_path("Playlist A", track, victim_track)

                with self.assertRaises(RuntimeError) as cm:
                    app_module._playlist_delete_staged_track_file(
                        "Playlist A",
                        track,
                        requested_path=str(victim_track),
                    )
                self.assertIn("outside this playlist's own staging directory", str(cm.exception))
                self.assertTrue(victim_track.exists(), "playlist B's staged track must survive playlist A's delete request")

class ControlAgentDeleteTrackEndpointTests(unittest.TestCase):
    """Real invocation of ControlAgentHandler.do_POST for
    /playlists/staging/delete-track (follows the established pattern in
    ControlAgentHardlinkEndpointTests / test_sec002_app_path_qbittorrent_hardlink.py:
    construct the handler via __new__, feed it a real JSON body, call
    do_POST() directly so the actual endpoint code runs end to end)."""

    def setUp(self):
        import backend.beets_control_agent as agent_module
        self.agent_module = agent_module
        self._scratch_base = Path(tempfile.mkdtemp(prefix="wave9_engine_delete_test_", dir=os.getcwd())).resolve()
        self.addCleanup(shutil.rmtree, self._scratch_base, ignore_errors=True)
        self.staging_root = self._scratch_base / "staging"
        self.music_root = self._scratch_base / "music"
        self.staging_root.mkdir(parents=True)
        self.music_root.mkdir(parents=True)

    def post_delete(self, payload):
        body = json.dumps(payload).encode("utf-8")
        handler = self.agent_module.ControlAgentHandler.__new__(self.agent_module.ControlAgentHandler)
        handler.path = "/playlists/staging/delete-track"
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = BytesIO(body)
        handler._authenticate = lambda: True
        responses = []
        handler._send_json = lambda code, data: responses.append((code, data))
        with mock.patch.object(self.agent_module, "PLAYLIST_DOWNLOAD_ROOT", self.staging_root), \
             mock.patch.object(self.agent_module, "MUSIC_ROOT", self.music_root), \
             mock.patch.object(self.agent_module, "DOWNLOAD_PATH", str(self.staging_root)), \
             mock.patch.object(self.agent_module, "MUSIC_LIBRARY_PATH", str(self.music_root)), \
             mock.patch.object(self.agent_module, "acquire_os_lock", return_value=None), \
             mock.patch.object(self.agent_module, "release_os_lock"):
            handler.do_POST()
        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_engine_refuses_cross_playlist_delete(self):
        """The playlist_staging OR staging_root containment bug: a delete
        request scoped to playlist-a must not be able to remove a file that
        actually belongs to playlist-b, even though both live under the
        same shared PLAYLIST_DOWNLOAD_ROOT."""
        if os.name == "nt":
            self.skipTest("resolve_safe_path requires POSIX-absolute input; covered by Docker/Linux runtime")
        playlist_b_downloads = self.staging_root / "playlist-b" / "downloads"
        playlist_b_downloads.mkdir(parents=True)
        victim = playlist_b_downloads / "victim.mp3"
        victim.write_bytes(b"audio data belonging to playlist-b")

        code, data = self.post_delete({
            "playlist_key": "playlist-a",
            "track_id": "track-1",
            "requested_path": str(victim),
        })

        self.assertEqual(code, 403, data)
        self.assertTrue(victim.exists(), "playlist-b's staged track must survive playlist-a's delete request")

    def test_engine_allows_own_playlist_delete(self):
        if os.name == "nt":
            self.skipTest("resolve_safe_path requires POSIX-absolute input; covered by Docker/Linux runtime")
        playlist_a_downloads = self.staging_root / "playlist-a" / "downloads"
        playlist_a_downloads.mkdir(parents=True)
        own_file = playlist_a_downloads / "own.mp3"
        own_file.write_bytes(b"audio data belonging to playlist-a")

        code, data = self.post_delete({
            "playlist_key": "playlist-a",
            "track_id": "track-1",
            "requested_path": str(own_file),
        })

        self.assertEqual(code, 200, data)
        self.assertTrue(data.get("deleted"))
        self.assertFalse(own_file.exists())

    def test_atomic_json_replace_no_longer_trusts_music_root_or_system_temp(self):
        """MUSIC_ROOT and the whole system temp directory were previously
        accepted allowed_roots for _playlist_atomic_json_replace() with no
        production caller ever targeting either -- both must now be
        refused."""
        with tempfile.TemporaryDirectory() as tmp:
            music_root = Path(tmp) / "music"
            music_root.mkdir()
            with mock.patch.object(app_module, "MUSIC_ROOT", music_root), \
                 mock.patch.object(app_module, "PLAYLIST_DIR", Path(tmp) / "playlists"), \
                 mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", Path(tmp) / "staging"), \
                 mock.patch.object(app_module, "PLAYLIST_JOB_STATE_DIR", Path(tmp) / "jobs"):
                with self.assertRaises(ValueError):
                    app_module._playlist_atomic_json_replace(
                        music_root / "not-state.json", {"x": 1}, save_key="t",
                    )
                system_tmp = Path(tempfile.gettempdir()) / "some-other-app-file.json"
                with self.assertRaises(ValueError):
                    app_module._playlist_atomic_json_replace(
                        system_tmp, {"x": 1}, save_key="t2",
                    )

    def test_atomic_json_replace_accepts_playlist_job_state_dir(self):
        """PLAYLIST_JOB_STATE_DIR (the real root _playlist_save_job_state()
        writes checkpoints under) was previously *absent* from
        allowed_roots entirely -- every real job-state save raised
        ValueError in every environment. Confirmed fixed here."""
        with tempfile.TemporaryDirectory() as tmp:
            job_state_dir = Path(tmp) / "playlist_jobs"
            job_state_dir.mkdir()
            with mock.patch.object(app_module, "PLAYLIST_JOB_STATE_DIR", job_state_dir), \
                 mock.patch.object(app_module, "PLAYLIST_DIR", Path(tmp) / "playlists"), \
                 mock.patch.object(app_module, "PLAYLIST_DOWNLOAD_ROOT", Path(tmp) / "staging"):
                target = job_state_dir / "pl-deadbeef.json"
                app_module._playlist_atomic_json_replace(target, {"job_id": "pl-deadbeef"}, save_key="job")
                self.assertTrue(target.exists())


class Wave9StableIdentityAndEngineOwnershipTests(unittest.TestCase):
    """Unit tests for Wave 9 stable playlist identity and engine IPC ownership."""

    def test_distinct_playlist_names_produce_distinct_keys_and_staging_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patched_playlist_state(Path(tmp) / "state"):
                names = ["A/B", "A\\B", "A:B", "A?B"]
                ids = [app_module._playlist_ensure_stable_id(n) for n in names]
                keys = [app_module._playlist_key(n, playlist_id=pid) for n, pid in zip(names, ids)]
                self.assertEqual(len(set(keys)), len(names), "Distinct raw names must yield distinct playlist keys")

                staging_roots = [str(app_module.get_playlist_staging_root({"name": n, "playlist_id": pid})) for n, pid in zip(names, ids)]
                self.assertEqual(len(set(staging_roots)), len(names), "Distinct raw names must yield distinct staging roots")

    def test_long_names_produce_distinct_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patched_playlist_state(Path(tmp) / "state"):
                long_a = "X" * 120 + "AAAA"
                long_b = "X" * 120 + "BBBB"
                id_a = app_module._playlist_ensure_stable_id(long_a)
                id_b = app_module._playlist_ensure_stable_id(long_b)
                key_a = app_module._playlist_key(long_a, playlist_id=id_a)
                key_b = app_module._playlist_key(long_b, playlist_id=id_b)
                self.assertNotEqual(key_a, key_b, "Long distinct names must yield distinct keys")

    def test_beets_client_engine_staging_ipc(self):
        client = app_module.beets_client
        with mock.patch.object(client, "_request", return_value={"ok": True, "staged": True}) as mock_req:
            res = client.ensure_playlist_staging("test--key123", "pl-123", "Test Playlist")
            self.assertTrue(res.get("ok"))
            mock_req.assert_called_once_with(
                "POST",
                "/playlists/staging/ensure",
                {"playlist_key": "test--key123", "playlist_id": "pl-123", "name": "Test Playlist"},
            )

    def test_beets_client_engine_delete_track_ipc(self):
        client = app_module.beets_client
        with mock.patch.object(client, "_request", return_value={"ok": True, "deleted": True}) as mock_req:
            res = client.delete_playlist_staged_track("test--key123", "track-456", "/data/staging/01.mp3")
            self.assertTrue(res.get("deleted"))
            mock_req.assert_called_once_with(
                "POST",
                "/playlists/staging/delete-track",
                {"playlist_key": "test--key123", "track_id": "track-456", "requested_path": "/data/staging/01.mp3"},
            )

    def test_beets_client_engine_export_m3u_ipc(self):
        client = app_module.beets_client
        items = [{"artist": "Art", "title": "Song", "path": "/data/music/01.mp3"}]
        with mock.patch.object(client, "_request", return_value={"ok": True, "exported": True}) as mock_req:
            res = client.export_playlist_m3u("test--key123", "Test Playlist", items)
            self.assertTrue(res.get("exported"))
            mock_req.assert_called_once_with(
                "POST",
                "/playlists/export_m3u",
                {"playlist_key": "test--key123", "display_name": "Test Playlist", "items": items},
            )


if __name__ == "__main__":
    unittest.main()
