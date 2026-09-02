"""Regression coverage for a real vulnerability found while dispositioning
CodeQL alerts #103/#104/#105 (py/incomplete-url-substring-sanitization,
app.py:45292/45328/45332 at baseline commit 7b05844d58d657ce6f1ae6c5b1724f5ba70ee257)
during the repository-wide CodeQL closure pass's browser/URL-security
tranche.

Unlike the cosmetic frontend badge findings in the same rule class (Phase 4,
docs/security/codeql_repository_closure.md), playlist_parse()'s
"spotify.com"/"youtube.com"/"youtu.be"/"soundcloud.com" substring checks
were a genuine trust decision: the soundcloud.com branch calls
_apply_ytdlp_netrc(ydl_opts), which instructs yt-dlp to attach the
operator's stored netrc credentials to the request. A URL that merely
CONTAINS "soundcloud.com" as a substring without actually being hosted
there (e.g. "https://evil.example/soundcloud.com/x") would previously have
had those credentials attached and sent to the attacker-controlled host --
real credential exfiltration, not just a mislabeled UI badge.

Fixed with real hostname parsing (_playlist_url_host()/_playlist_url_host_is()),
hoisted to module level so this property is directly testable without
driving the full yt-dlp integration.
"""

import unittest

import app as app_module


class PlaylistUrlHostParsingTests(unittest.TestCase):
    def test_genuine_soundcloud_url_matches(self):
        host = app_module._playlist_url_host("https://soundcloud.com/artist/track")
        self.assertTrue(app_module._playlist_url_host_is(host, "soundcloud.com"))

    def test_genuine_soundcloud_subdomain_matches(self):
        host = app_module._playlist_url_host("https://m.soundcloud.com/artist/track")
        self.assertTrue(app_module._playlist_url_host_is(host, "soundcloud.com"))

    def test_substring_lookalike_does_not_match(self):
        # The exact spoof the original substring check was vulnerable to:
        # credentials would have been attached and sent to evil.example.
        host = app_module._playlist_url_host("https://evil.example/soundcloud.com/track")
        self.assertFalse(app_module._playlist_url_host_is(host, "soundcloud.com"))

    def test_domain_suffix_lookalike_does_not_match(self):
        host = app_module._playlist_url_host("https://soundcloud.com.evil.example/track")
        self.assertFalse(app_module._playlist_url_host_is(host, "soundcloud.com"))

    def test_spotify_lookalike_does_not_match(self):
        host = app_module._playlist_url_host("https://evil.example/redirect?u=spotify.com")
        self.assertFalse(app_module._playlist_url_host_is(host, "spotify.com"))

    def test_genuine_spotify_open_subdomain_matches(self):
        host = app_module._playlist_url_host("https://open.spotify.com/playlist/abc123")
        self.assertTrue(app_module._playlist_url_host_is(host, "spotify.com"))

    def test_genuine_youtube_and_short_domain_match(self):
        self.assertTrue(app_module._playlist_url_host_is(
            app_module._playlist_url_host("https://www.youtube.com/playlist?list=abc"), "youtube.com"))
        self.assertTrue(app_module._playlist_url_host_is(
            app_module._playlist_url_host("https://youtu.be/abc123"), "youtu.be"))

    def test_malformed_url_yields_empty_host_and_no_match(self):
        host = app_module._playlist_url_host("not a url at all :::")
        self.assertEqual(host, "")
        self.assertFalse(app_module._playlist_url_host_is(host, "soundcloud.com"))


class PlaylistParseCredentialAttachmentTests(unittest.TestCase):
    """Integration-level proof: playlist_parse()'s soundcloud branch only
    attaches netrc credentials for a URL whose real hostname is
    soundcloud.com, not a substring lookalike."""

    def _run(self, url):
        import sys
        import types
        from unittest import mock

        fake_ydl_instance = mock.MagicMock()
        fake_ydl_instance.extract_info.return_value = {"entries": []}
        fake_ydl_instance.__enter__ = mock.Mock(return_value=fake_ydl_instance)
        fake_ydl_instance.__exit__ = mock.Mock(return_value=False)
        fake_yt_dlp = types.ModuleType("yt_dlp")
        fake_yt_dlp.YoutubeDL = mock.Mock(return_value=fake_ydl_instance)

        with mock.patch.dict(sys.modules, {"yt_dlp": fake_yt_dlp}), \
             mock.patch.object(app_module._ytdlp_ready, "wait", return_value=True), \
             mock.patch.object(app_module, "_apply_ytdlp_netrc") as mock_netrc, \
             app_module.app.test_request_context(
                 "/api/playlist/parse", method="POST",
                 json={"source": "url", "content": url},
             ):
            try:
                app_module.playlist_parse()
            except Exception:
                # The provider-routing decision under test (whether
                # _apply_ytdlp_netrc is called) happens before track
                # matching; a downstream failure reaching the (unmocked,
                # unreachable in this test env) beets control agent for
                # library-match candidates is irrelevant to this test and
                # is intentionally swallowed here.
                pass
        return mock_netrc

    def test_lookalike_soundcloud_url_does_not_attach_credentials(self):
        mock_netrc = self._run("https://evil.example/soundcloud.com/track")
        mock_netrc.assert_not_called()

    def test_genuine_soundcloud_url_still_attaches_credentials(self):
        mock_netrc = self._run("https://soundcloud.com/artist/track")
        mock_netrc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
