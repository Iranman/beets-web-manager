import re
import unittest
from pathlib import Path


class YtdlpCookieMessageTests(unittest.TestCase):
    def test_bot_check_failures_use_rejected_cookie_message(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8")

        self.assertIn("def _ytdlp_cookie_rejected_help", source)
        self.assertIn("def _ytdlp_cookie_rejection_seen", source)
        self.assertIn("_ytdlp_cookie_rejected_help(cookie_file)", source)

        stale_patterns = [
            r'if bot_check_seen\["count"\]:\s*\n\s*raise RuntimeError\(_ytdlp_cookie_help\(\)\)',
            (
                r'if _yt_bot_check_message\(str\(ex\)\) or bot_check_seen\["count"\]:'
                r'\s*\n\s*raise RuntimeError\(_ytdlp_cookie_help\(\)\) from ex'
            ),
        ]
        for pattern in stale_patterns:
            self.assertIsNone(re.search(pattern, source))

    def test_acquire_batch_uses_source_fallback_not_youtube_fallback(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8")

        self.assertIn("batch_source_fallback_enabled = bool(try_source_fallback)", source)
        self.assertIn('"try_source_fallback": try_source_fallback', source)
        self.assertIn("ytdlp_fallback_disabled", source)
        self.assertNotIn("Disabled yt-dlp fallback for the remaining", source)

    def test_rejected_cookie_state_does_not_disable_anonymous_youtube(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8")

        self.assertIn("_YTDLP_COOKIE_REJECTED_FILE", source)
        self.assertIn("def _mark_ytdlp_cookie_rejected", source)
        self.assertIn("def _ytdlp_cookie_rejection_state", source)
        self.assertIn("cookie_rejected", source)
        self.assertIn("YTDLP_REQUIRE_YOUTUBE_AUTH", source)
        self.assertIn("Anonymous YouTube remains available", source)
        self.assertNotIn("yt-dlp is disabled until YouTube authentication cookies are configured", source)

    def test_cookie_file_is_optional_and_browser_cookies_are_opt_in(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8")

        self.assertIn("YTDLP_ALLOW_BROWSER_COOKIES", source)
        self.assertIn("YTDLP_COOKIES_FROM_BROWSER", source)
        self.assertIn("YTDLP_COOKIES_FROM_BROWSER_FALLBACK", source)
        self.assertIn('YTDLP_COOKIES_FROM_BROWSER_FALLBACK",\n    "",', source)
        self.assertIn("def _configured_ytdlp_cookie_auth", source)
        self.assertIn('ydl_opts["cookiefile"]', source)
        self.assertIn("Using --cookies", source)
        self.assertIn('ydl_opts["cookiesfrombrowser"]', source)
        self.assertIn("Browser cookies disabled; using --cookies FILE only", source)
        self.assertIn('"cookies_from_browser"', source)
        self.assertIn('"browser_cookies_enabled"', source)
        self.assertIn('"cookie_auth_mode"', source)
        self.assertIn("YouTube uses anonymous yt-dlp by default", source)

    def test_ytdlp_netrc_is_scoped_without_exposing_credentials(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8")

        self.assertIn("YTDLP_NETRC_FILE", source)
        self.assertIn("def _configured_ytdlp_netrc_file", source)
        self.assertIn("def _apply_ytdlp_netrc", source)
        self.assertIn("def _ytdlp_apply_source_auth", source)
        self.assertIn('if _normalise_download_method(source) == "soundcloud":', source)
        self.assertIn('ydl_opts["usenetrc"] = True', source)
        self.assertIn('ydl_opts["netrc_location"] = netrc_file', source)
        self.assertIn("Using --netrc --netrc-location", source)
        self.assertIn("def _ytdlp_netrc_status", source)
        self.assertIn('"netrc": _ytdlp_netrc_status()', source)
        self.assertIn('"machines": _ytdlp_netrc_machines(netrc_file)', source)
        self.assertNotIn("sk8ter23", source)

    def test_cookie_file_rejection_can_fall_back_to_browser_auth_when_enabled(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8")

        self.assertIn("def _configured_ytdlp_cookie_auths", source)
        self.assertIn("def _usable_ytdlp_cookie_auths", source)
        self.assertIn("_fallback_ytdlp_browser_cookie_spec", source)
        self.assertIn("last_auth_error = _mark_ytdlp_auth_rejected(cookie_auth", source)
        self.assertIn("trying next auth source", source)

    def test_ytdlp_status_is_cookie_independent_for_youtube(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8")

        self.assertIn("def _ytdlp_auth_smoke_check", source)
        self.assertIn("def _usable_ytdlp_cookie_auths_with_smoke", source)
        self.assertIn('"cookie_auth_smoke"', source)
        self.assertIn('enabled = bool(ready and js_runtime.get("available"))', source)
        self.assertIn('"youtube": _ytdlp_youtube_status(js_runtime)', source)
        self.assertIn("yt-dlp ready; YouTube anonymous", source)
        self.assertNotIn('enabled = bool(ready and usable_auths', source)

    def test_ytdlp_bootstrap_probes_without_runtime_installers(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8")

        self.assertIn('os.environ.get("YTDLP_PIP_PACKAGE", "yt-dlp[default,curl-cffi]")', source)
        self.assertIn('os.environ.get("YTDLP_BGUTIL_PIP_PACKAGE", "bgutil-ytdlp-pot-provider==1.3.1")', source)
        self.assertIn('raw = os.environ.get("YTDLP_JS_RUNTIMES", "deno,node,quickjs")', source)
        self.assertIn('raw = os.environ.get("YTDLP_REMOTE_COMPONENTS", "")', source)
        self.assertIn("runtime package installation disabled", source)
        self.assertIn("yt-dlp and helper runtimes must be installed in the image build", source)
        self.assertIn('"runtime_install_enabled": False', source)
        self.assertIn("def _yt_dlp_install_status", source)
        self.assertIn("def _ffmpeg_status", source)
        self.assertIn('"install": _yt_dlp_install_status()', source)
        self.assertIn('"ffmpeg": _ffmpeg_status()', source)
        self.assertNotIn('_PIP_CMD = [sys.executable, "-m", "pip"]', source)
        self.assertNotIn('YTDLP_PIP_PRE', source)
        self.assertNotIn('[try] installing deno via apk', source)
        self.assertNotIn('latest/download', source)
        self.assertNotIn('apt-get", "install"', source)
        self.assertNotIn('apk", "add"', source)

    def test_spotiflac_cli_does_not_install_at_runtime(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8")

        self.assertIn("_SPOTIFLAC_AUTO_INSTALL = False", source)
        self.assertIn("runtime CLI installation is disabled", source)
        self.assertIn("Install the pinned SpotiFLAC CLI during the container image build", source)
        self.assertNotIn("Installing {_SPOTIFLAC_PIP_PACKAGE} with pip", source)
        self.assertNotIn("SPOTIFLAC_AUTO_INSTALL=1 so the app can install it on first use", source)
        self.assertNotIn("_pip_install(_SPOTIFLAC_PIP_PACKAGE", source)
    def test_youtube_uses_flac_po_provider_and_anonymous_test_endpoint(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8")

        self.assertIn('YTDLP_PO_PROVIDER_URL = os.environ.get(', source)
        self.assertIn('"youtubepot-bgutilhttp": {"base_url": [YTDLP_PO_PROVIDER_URL]}', source)
        self.assertIn('return "flac" if _normalise_download_method(source) == "ytdlp" else "mp3"', source)
        self.assertIn("'format': _ytdlp_audio_format_for_source(source)", source)
        self.assertIn("'postprocessors': _ytdlp_postprocessors_for_source(source)", source)
        self.assertIn('@app.post("/api/ytdlp/test-youtube")', source)
        self.assertIn("Inspecting public YouTube URL without cookies", source)
        self.assertNotIn("yt-dlp-YTAgeGateBypass", source)

    def test_parent_child_wait_includes_actionable_failure_detail(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        source = app_path.read_text(encoding="utf-8")

        self.assertIn("def _child_failure_detail", source)
        self.assertIn('f"{prefix} job failed: {detail}"', source)

    def test_download_sources_are_exposed_across_backend_frontend_and_compose(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "app.py").read_text(encoding="utf-8")
        type_source = (root / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")
        helper_source = (root / "frontend" / "src" / "lib" / "downloadMethods.ts").read_text(encoding="utf-8")
        playlist_source = (root / "frontend" / "src" / "views" / "Playlists.tsx").read_text(encoding="utf-8")
        compose_source = (root / "docker-compose.arrs.yml").read_text(encoding="utf-8")

        self.assertIn('_DOWNLOAD_METHODS = {"slskd", "spotiflac", "ytdlp", "soundcloud"}', app_source)
        self.assertIn('_SLSKD_FALLBACK_METHODS = os.environ.get("SLSKD_FALLBACK_METHODS", "spotiflac,ytdlp,soundcloud")', app_source)
        self.assertIn('if methods and "ytdlp" not in methods:', app_source)
        self.assertIn('insert_at = methods.index("soundcloud") if "soundcloud" in methods else len(methods)', app_source)
        self.assertIn('methods.insert(insert_at, "ytdlp")', app_source)
        self.assertIn('PLAYLIST_DOWNLOAD_METHODS = os.environ.get("PLAYLIST_DOWNLOAD_METHODS", "slskd,spotiflac,ytdlp,soundcloud")', app_source)
        self.assertIn('f"scsearch{max_tracks}:{artist} {album}"', app_source)
        self.assertIn('prefix = "scsearch1" if source == "soundcloud" else "ytsearch1"', app_source)
        self.assertIn('elif method == "ytdlp":', app_source)
        self.assertIn("def _spotiflac_missing_tracks_download", app_source)
        self.assertIn('"spotiflac": _spotiflac_status()', app_source)
        self.assertIn("export type DownloadMethod = 'slskd' | 'spotiflac' | 'ytdlp' | 'soundcloud';", type_source)
        self.assertIn('youtube?: Record<string, unknown>;', type_source)
        self.assertIn("method: 'spotiflac'", helper_source)
        self.assertIn("method: 'ytdlp'", helper_source)
        self.assertIn("method: 'soundcloud'", helper_source)
        self.assertIn("['slskd', 'spotiflac', 'ytdlp', 'soundcloud']", playlist_source)
        self.assertIn("{ value: 'ytdlp', label: 'YouTube' }", playlist_source)
        self.assertIn("bgutil-provider:", compose_source)
        self.assertIn("image: brainicism/bgutil-ytdlp-pot-provider:1.3.1-deno", compose_source)
        self.assertIn("YTDLP_PO_PROVIDER_URL: http://bgutil-provider:4416", compose_source)
        self.assertNotIn("127.0.0.1:4416", compose_source)


if __name__ == "__main__":
    unittest.main()
