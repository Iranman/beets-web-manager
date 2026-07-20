"""Tests for the Library page caching fix (2026-07-20).

Root cause of "library takes forever to load every time": /api/library
already had a working request-scoped cache (_lib_cache, 90s TTL), but
_auto_scan_loop's background "quick tick" ran every _QUICK_SCAN_INTERVAL
(120s) and called _invalidate_lib_cache() -- just nuking the cache, never
rebuilding it. Since 120s > the old 90s TTL, and nothing else kept the
cache warm (no frontend polling of /api/library exists despite an old
comment claiming otherwise), the cache was cold far more often than it was
warm: almost every real page visit landed after the last invalidation and
had to pay for a full synchronous rebuild (confirmed ~25s+ for a large
library elsewhere in this codebase's history).

Fixed by extracting the actual library-tree builder out of the
library_full() route into a pure _build_library_payload() function, adding
_refresh_library_cache() (build + store, callable with no Flask request
context), and having both the quick tick and the post-full-scan callback
call _refresh_library_cache() instead of _invalidate_lib_cache() -- so the
cache is proactively kept warm in the background instead of requiring a
user's page load to pay for the rebuild. _LIB_CACHE_TTL was also raised to
180s (comfortably above the 120s background cadence) so a slightly-delayed
tick doesn't force an ad-hoc rebuild in the request path either.

Also covers a second, separate finding from live-verifying the cache fix:
server-side response time (time_starttransfer) dropped to ~0.6s as
expected, but total transfer time was still ~9s, because the ~28.7MB JSON
response was being sent completely uncompressed -- confirmed via curl that
`Accept-Encoding: gzip` from the client was never honored, no
Content-Encoding header at all. _compress_response (a new @app.after_request
hook, general-purpose across the whole app, not just /api/library) fixes
that.

A third, larger finding surfaced only by live browser testing (curl alone
never would have caught it): navigating to /library fired 328 concurrent
`/api/artist-image-url` requests -- one per artist with no cached image,
via Library.tsx's `useArtistArtUrl` hook mounting once per ArtistCard with
no shared concurrency limit. That flooded both the browser's per-origin
connection limit and the backend's Waitress thread pool (WEBCONTROL_THREADS,
default 8), badly enough that /api/library itself -- already fast and
already cached by this point -- got stuck queued for 20+ seconds behind
hundreds of rivals before its request even started. Confirmed live:
repeat "tab back and forth" navigations measured 22184ms and 15268ms
click-to-rendered-content. Fixed with `runArtistImageFetchThrottled`, a
small module-level queue in Library.tsx capping concurrent
/api/artist-image-url calls to `ARTIST_IMAGE_FETCH_CONCURRENCY` (4); the
same three navigations re-measured at 1283ms/1435ms/1114ms after the fix.
"""
import gzip
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
LIBRARY_TSX_SOURCE = (ROOT / "frontend" / "src" / "views" / "Library.tsx").read_text(encoding="utf-8")


def _function_source(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


class LibraryPayloadExtractionTests(unittest.TestCase):
    def test_build_library_payload_is_a_separate_pure_function(self):
        self.assertIn("def _build_library_payload() -> dict:", APP_SOURCE)

    def test_refresh_library_cache_builds_and_stores(self):
        fn = _function_source(
            APP_SOURCE, "def _refresh_library_cache() -> dict:", "\n\ndef _build_library_payload"
        )
        self.assertIn("payload = _build_library_payload()", fn)
        self.assertIn("_lib_cache    = payload", fn)
        self.assertIn("_lib_cache_ts = time.time()", fn)
        self.assertIn("return payload", fn)

    def test_route_delegates_to_refresh_helper_instead_of_building_inline(self):
        fn = _function_source(APP_SOURCE, "def library_full():", "\n\ndef _refresh_library_cache")
        self.assertIn("payload = _refresh_library_cache()", fn)
        # The route itself must not still contain the disk-walk build logic
        # inline -- that would mean the extraction duplicated instead of
        # moved it.
        self.assertNotIn("path_to_id:   Dict[str, int] = {}", fn)

    def test_builder_does_not_reference_flask_request_object(self):
        # Must be safe to call with no Flask request context (from the
        # background auto-scan thread).
        fn = _function_source(
            APP_SOURCE, "def _build_library_payload() -> dict:", "\n\n# ── Acquisition queue"
        )
        self.assertNotIn("request.", fn)


class AutoScanLoopRebuildsInsteadOfJustInvalidatingTests(unittest.TestCase):
    def setUp(self):
        self.fn = _function_source(APP_SOURCE, "def _auto_scan_loop():", "\n\nthreading.Thread(target=_auto_scan_loop")

    def test_quick_tick_calls_refresh_not_bare_invalidate(self):
        self.assertIn("_refresh_library_cache()", self.fn)

    def test_quick_tick_no_longer_only_invalidates(self):
        # The old bug: the quick-tick branch's only cache action was a bare
        # _invalidate_lib_cache() call with no rebuild anywhere nearby.
        tick_start = self.fn.index("Quick tick (every 2 min)")
        tick_block = self.fn[tick_start:tick_start + 1000]
        self.assertIn("_refresh_library_cache()", tick_block)

    def test_loop_still_runs_as_a_background_daemon_thread(self):
        self.assertIn(
            "threading.Thread(target=_auto_scan_loop, daemon=True).start()", APP_SOURCE
        )


class RecordScanRebuildsTests(unittest.TestCase):
    def test_record_scan_rebuilds_with_fallback_to_invalidate(self):
        fn = _function_source(APP_SOURCE, "def _record_scan():", "\n\ndef _do_scan_job")
        self.assertIn("_refresh_library_cache()", fn)
        self.assertIn("except Exception:", fn)
        self.assertIn("_invalidate_lib_cache()", fn)


class CacheTtlTests(unittest.TestCase):
    def test_ttl_exceeds_the_background_rebuild_cadence(self):
        ttl_line = next(line for line in APP_SOURCE.splitlines() if line.startswith("_LIB_CACHE_TTL ="))
        ttl_value = float(ttl_line.split("=", 1)[1].split("#", 1)[0].strip())
        interval_line = next(
            line for line in APP_SOURCE.splitlines() if line.strip().startswith("_QUICK_SCAN_INTERVAL =")
        )
        interval_value = float(interval_line.split("=", 1)[1].split("#", 1)[0].strip())
        self.assertGreater(
            ttl_value, interval_value,
            "TTL must exceed the background rebuild cadence, or a page visit can still "
            "land in the gap and pay for a synchronous rebuild.",
        )


class ResponseCompressionTests(unittest.TestCase):
    def setUp(self):
        self.fn = _function_source(
            APP_SOURCE, "def _compress_response(response):", "\n\n@app.after_request\ndef _set_security_headers"
        )

    def test_registered_before_set_security_headers_so_it_runs_after(self):
        # Flask executes after_request hooks in reverse registration order,
        # so _compress_response must be REGISTERED first (appear earlier in
        # the file) to RUN last -- after CSP header computation has already
        # read the plaintext HTML body via get_data(as_text=True). Gzipping
        # first would hand that hook undecodable bytes.
        compress_pos = APP_SOURCE.index("def _compress_response(response):")
        security_headers_pos = APP_SOURCE.index("def _set_security_headers(response):")
        self.assertLess(compress_pos, security_headers_pos)

    def test_skips_direct_passthrough_responses(self):
        self.assertIn("if response.direct_passthrough:", self.fn)

    def test_skips_already_encoded_responses(self):
        self.assertIn('if response.headers.get("Content-Encoding"):', self.fn)

    def test_requires_client_to_accept_gzip(self):
        self.assertIn('accept_encoding = request.headers.get("Accept-Encoding", "")', self.fn)
        self.assertIn('"gzip" not in accept_encoding.lower()', self.fn)

    def test_only_compresses_json_and_text_mimetypes(self):
        self.assertIn("_COMPRESSIBLE_MIMETYPE_PREFIXES", APP_SOURCE)
        self.assertIn('_COMPRESSIBLE_MIMETYPE_PREFIXES = ("application/json", "text/")', APP_SOURCE)

    def test_skips_tiny_responses(self):
        self.assertIn("_COMPRESS_MIN_BYTES", self.fn)

    def test_falls_back_to_uncompressed_if_compression_did_not_help(self):
        self.assertIn("if len(compressed) >= len(data):", self.fn)
        self.assertIn("return response", self.fn)

    def test_sets_content_encoding_and_vary_headers(self):
        self.assertIn('response.headers["Content-Encoding"] = "gzip"', self.fn)
        self.assertIn('response.headers["Content-Length"] = str(len(compressed))', self.fn)
        self.assertIn("Accept-Encoding", self.fn)

    def test_never_raises_on_unexpected_response_shape(self):
        self.assertIn("except Exception:", self.fn)
        self.assertIn("pass", self.fn)

    def test_gzip_round_trips_losslessly(self):
        # Real behavioral check of the actual stdlib call used, not just a
        # source-pattern match.
        original = (b'{"artists": [{"name": "Test Artist", "albums": []}]}' * 50)
        compressed = gzip.compress(original, compresslevel=6)
        self.assertLess(len(compressed), len(original))
        self.assertEqual(gzip.decompress(compressed), original)


class ArtistImageFetchThrottlingTests(unittest.TestCase):
    def test_throttled_queue_helper_exists(self):
        self.assertIn("function runArtistImageFetchThrottled", LIBRARY_TSX_SOURCE)

    def test_concurrency_cap_is_small_not_unlimited(self):
        match = re.search(r"ARTIST_IMAGE_FETCH_CONCURRENCY = (\d+)", LIBRARY_TSX_SOURCE)
        self.assertIsNotNone(match, "ARTIST_IMAGE_FETCH_CONCURRENCY constant not found")
        cap = int(match.group(1))
        # Must be small enough to actually protect the backend thread pool
        # (WEBCONTROL_THREADS defaults to 8) and the browser's per-origin
        # connection limit, but > 0 so images still load.
        self.assertGreater(cap, 0)
        self.assertLessEqual(cap, 6)

    def test_use_artist_art_url_goes_through_the_throttle_not_direct(self):
        fn = _function_source(
            LIBRARY_TSX_SOURCE,
            "function useArtistArtUrl(artist: LibraryArtist, libraryVersion?: number) {",
            "\n\nfunction initials(",
        )
        self.assertIn("runArtistImageFetchThrottled(() => getArtistImageUrl(artistName))", fn)
        # The historical bug: calling getArtistImageUrl directly, once per
        # ArtistCard, with no shared cap -- confirmed live as 328 simultaneous
        # requests for a 773-artist library.
        self.assertNotIn("getArtistImageUrl(artistName)\n      .then(", fn)

    def test_queue_defers_work_past_the_concurrency_cap_instead_of_dropping_it(self):
        fn = _function_source(
            LIBRARY_TSX_SOURCE,
            "function runArtistImageFetchThrottled<T>(task: () => Promise<T>): Promise<T> {",
            "\n\n// Falls back to an album cover immediately",
        )
        self.assertIn("artistImageFetchQueue.push(run)", fn)
        self.assertIn("artistImageFetchQueue.shift()", fn)
        self.assertIn("activeArtistImageFetches < ARTIST_IMAGE_FETCH_CONCURRENCY", fn)
        # A finished fetch must free its slot for the next queued one.
        self.assertIn("activeArtistImageFetches -= 1", fn)


if __name__ == "__main__":
    unittest.main()
