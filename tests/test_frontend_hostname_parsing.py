"""CodeQL closure follow-up: strict hostname parsing for playlist-platform
URLs, checked against the LIVE frontend source.

The legacy static fallback UI (root index.html) was deleted, together with the
CodeQL repowide-closure coverage that statically inspected it:

- Alerts #2/#3 (js/xss-through-dom: folder-browse inline ``onclick`` string
  interpolation and the manual-MBID ``href`` assignment) were defined only in
  that file; the file is gone, so no protection remains to cover and these
  tests are not reproduced.
- Alerts #9-#17 (js/incomplete-url-substring-sanitization) pointed at the
  playlist-platform label logic, which lives on in
  ``frontend/src/views/Playlists.tsx``. That protection must stay covered
  against the real, live surface.

Matching this repo's established pattern (see tests/test_musicbrainz_tracklist_cache.py
and the companion codecs suites), the shape is pinned by source inspection plus
a plain-Python proof of the matcher semantics: exact host or a "``.domain``"
suffix only — never a bare substring anywhere in the URL.
"""

import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PLAYLISTS_TSX = (ROOT / "frontend" / "src" / "views" / "Playlists.tsx").read_text(encoding="utf-8")


class PlaylistsPlatformHostnameParsingTests(unittest.TestCase):
    def test_playlists_tsx_uses_real_hostname_parsing(self):
        self.assertIn("function platformHost(value: string): string", PLAYLISTS_TSX)
        self.assertIn("function hostMatches(host: string, domain: string): boolean", PLAYLISTS_TSX)
        self.assertNotIn("url.includes('music.youtube')", PLAYLISTS_TSX)
        self.assertNotIn("url.toLowerCase().includes('spotify.com')", PLAYLISTS_TSX)
        self.assertIn("hostMatches(platformHost(url), 'spotify.com')", PLAYLISTS_TSX)

    def test_hostname_matcher_rejects_substring_lookalikes(self):
        # Playlists.tsx's hostMatches and the semantics proven here share the
        # same algorithm: exact host, or a ".<domain>" suffix — never a bare
        # substring anywhere in the URL.
        def host_matches(host: str, domain: str) -> bool:
            return bool(host) and (host == domain or host.endswith("." + domain))

        def url_host(value: str) -> str:
            try:
                return (urlsplit(value).hostname or "").lower()
            except Exception:
                return ""

        self.assertTrue(host_matches(url_host("https://open.spotify.com/playlist/abc"), "spotify.com"))
        self.assertTrue(host_matches(url_host("https://spotify.com/playlist/abc"), "spotify.com"))
        self.assertFalse(host_matches(url_host("https://evil.example/spotify.com/phish"), "spotify.com"))
        self.assertFalse(host_matches(url_host("https://spotify.com.evil.example/"), "spotify.com"))


if __name__ == "__main__":
    unittest.main()
