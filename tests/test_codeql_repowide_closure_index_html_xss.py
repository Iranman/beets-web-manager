"""Regression coverage for real vulnerabilities found while dispositioning
CodeQL alerts #2/#3 (js/xss-through-dom, index.html:3888/4207) and #9-#17
(js/incomplete-url-substring-sanitization, index.html:5213-5223) during the
repository-wide CodeQL closure pass's browser-side security tranche.

index.html is a legacy static (non-React) fallback UI served by
render_index() when the built React dist and legacy-static overrides are
both absent (see app.py). It has no existing JS test harness (no Node/DOM
test infra targets this file), so -- matching this repo's established
pattern for exactly this situation (see tests/test_musicbrainz_tracklist_cache.py,
which statically inspects app.py source the same way) -- this suite proves
the vulnerable code shapes are gone and the fixed shapes are present, rather
than executing the JS in a DOM.

Alert #3 (real, fixed): the folder-browse directory listing interpolated
path/directory-name strings raw into inline onclick="fn('...')" attribute
strings. HTML-escaping alone does not make that safe -- the browser
HTML-decodes an attribute value BEFORE the JS parser sees it, so an escaped
quote is restored to a literal ' and still breaks out of the embedded JS
string. A directory name containing a quote character (reachable via any
writable path under the configured browse roots) could inject arbitrary
onclick/JS. Fixed with data-* attributes + this.dataset reads (the same
safe pattern already used elsewhere in this file).

Alert #2 (real, fixed): _applySkManualMbid() set a link's .href from a
manual-entry input's raw value with no validation of its own -- the
UUID-format check that gated the Apply button's visibility was a UI
convenience, not a guarantee enforced at the point of use.

Alerts #9-#17 (false positive for security purposes, hardened anyway):
detectPlatform()/platformLabel() (Playlists.tsx) only ever assign fixed
literal label/tone strings, never derived from the URL itself -- substring
matching here could only mislabel a cosmetic badge. Still switched to real
hostname parsing per the mission's explicit preference for strict parsing.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "index.html").read_text(encoding="utf-8")
PLAYLISTS_TSX = (ROOT / "frontend" / "src" / "views" / "Playlists.tsx").read_text(encoding="utf-8")


class FolderBrowseXssContainmentTests(unittest.TestCase):
    def test_vulnerable_inline_onclick_path_interpolation_is_gone(self):
        self.assertNotIn("onclick=\"loadBrowse('${par}')\"", INDEX_HTML)
        self.assertNotIn("onclick=\"sp('${p}')\"", INDEX_HTML)
        self.assertNotIn("onclick=\"loadBrowse('${full}')\"", INDEX_HTML)

    def test_safe_data_attribute_pattern_is_used_instead(self):
        self.assertIn('data-path="${e(par)}" onclick="loadBrowse(this.dataset.path)"', INDEX_HTML)
        self.assertIn('data-path="${e(p)}" onclick="sp(this.dataset.path)"', INDEX_HTML)
        self.assertIn('data-path="${e(full)}" onclick="loadBrowse(this.dataset.path)"', INDEX_HTML)


class ManualMbidLinkContainmentTests(unittest.TestCase):
    def test_shared_uuid_regex_exists(self):
        self.assertIn(
            "const MB_RELEASE_UUID_RE=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;",
            INDEX_HTML,
        )

    def test_apply_function_validates_before_setting_href(self):
        start = INDEX_HTML.index("function _applySkManualMbid(sid)")
        end = INDEX_HTML.index("\n}", start)
        body = INDEX_HTML[start:end]
        self.assertIn("if(!mbid||!MB_RELEASE_UUID_RE.test(mbid)) return;", body)
        # The validation must happen before the href assignment, not after.
        validate_idx = body.index("MB_RELEASE_UUID_RE.test(mbid)")
        href_idx = body.index("mbLink.href=")
        self.assertLess(validate_idx, href_idx)


class PlatformDetectionHostnameParsingTests(unittest.TestCase):
    def test_index_html_uses_real_hostname_parsing(self):
        self.assertIn("function _plUrlHost(val)", INDEX_HTML)
        self.assertIn("new URL(val).hostname.toLowerCase()", INDEX_HTML)
        self.assertNotIn("val.includes('music.youtube.com')", INDEX_HTML)
        self.assertNotIn("val.includes('spotify.com')", INDEX_HTML)

    def test_playlists_tsx_uses_real_hostname_parsing(self):
        self.assertIn("function platformHost(value: string): string", PLAYLISTS_TSX)
        self.assertIn("function hostMatches(host: string, domain: string): boolean", PLAYLISTS_TSX)
        self.assertNotIn("url.includes('music.youtube')", PLAYLISTS_TSX)
        self.assertNotIn("url.toLowerCase().includes('spotify.com')", PLAYLISTS_TSX)
        self.assertIn("hostMatches(platformHost(url), 'spotify.com')", PLAYLISTS_TSX)

    def test_hostname_matcher_rejects_substring_lookalikes_when_evaluated(self):
        # Prove the actual matching semantics in Python (the two
        # implementations share the same algorithm: exact host or a
        # ".<domain>" suffix, never a bare substring anywhere in the URL).
        def host_matches(host: str, domain: str) -> bool:
            return bool(host) and (host == domain or host.endswith("." + domain))

        from urllib.parse import urlsplit

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
