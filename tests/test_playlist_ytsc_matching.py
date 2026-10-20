"""YouTube/SoundCloud playlist-entry matching against a present library.

YT/SC entries routinely carry the real "Artist - Title" (spaced) or compact
"Title-ARTIST" inside the video title while the uploader field is a
VEVO/Topic/aggregator channel that is not the artist. Before the query-side
title split, the plain (uploader, title) pair never reached a confident
score, so a library track that was actually present stayed "missing".

These tests exercise the real import match path (`_match_playlist_tracks`,
which canonicalizes then matches) with the real normalization helpers, plus
a false-positive guard proving a distinct track is never cross-mapped.
"""

import difflib
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional

try:  # ARCH-001: works under discovery and tests.<module> runs
    from _app_ast_cache import load_app_closure  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import load_app_closure  # noqa: E402


def _item(title, artist):
    return SimpleNamespace(
        title=title,
        artist=artist,
        albumartist=artist,
        artist_credit=artist,
        album="Some Album",
        path=f"/tmp/music/{artist}/{title}.mp3",
        length=210.0,
        bitrate=256,
        format="MP3",
        id=abs(hash((title, artist))) % 100000,
        year=2015,
        mb_albumid="",
        mb_releasegroupid="",
        mb_albumartistid="",
        mb_albumartistids="",
        mb_trackid="",
    )


class PlaylistYtScMatchingTests(unittest.TestCase):
    def _match(self, items):
        namespace: Dict[str, Any] = {
            "Any": Any,
            "Dict": Dict,
            "Iterable": Iterable,
            "List": List,
            "Optional": Optional,
            "difflib": difflib,
            "re": re,
            "Path": Path,
            "PLAYLIST_MIN_DOWNLOAD_SECONDS": 45,
            "MUSIC_ROOT": Path("/tmp/music"),
            "lib": SimpleNamespace(items=lambda _query: items),
            # Deterministic path only: MusicBrainz "unavailable" (empty) here,
            # exactly as when the provider is down -- matching must still work.
            "provider_boundary": SimpleNamespace(ProviderError=Exception),
            "_mb_recording_search": lambda *a, **k: [],
            # Force the fallback library scan (no prebuilt index in tests).
            "_playlist_library_index": lambda: (_ for _ in ()).throw(RuntimeError("no index")),
            # Isolate matching from on-disk file quality checks.
            "_playlist_quality_for_item": lambda it, path: {
                "quality": "ok",
                "quality_flags": [],
                "length": 210.0,
                "format": "MP3",
                "bitrate": 256,
                "albumartist": getattr(it, "albumartist", ""),
                "source": "Library",
            },
        }
        helpers = load_app_closure(["_match_playlist_tracks"], namespace)
        return helpers["_match_playlist_tracks"]

    def assertMatches(self, items, artist, title, expected_title):
        match_tracks = self._match(items)
        matched, missing = match_tracks([{"artist": artist, "title": title}])
        self.assertTrue(matched, f"expected a match for ({artist!r}, {title!r}); missing={missing}")
        self.assertEqual(expected_title, matched[0].get("title"))
        self.assertGreaterEqual(float(matched[0].get("score") or 0), 0.82)

    def assertMissing(self, items, artist, title):
        match_tracks = self._match(items)
        matched, missing = match_tracks([{"artist": artist, "title": title}])
        self.assertFalse(
            matched,
            f"({artist!r}, {title!r}) must not match; got {matched}",
        )

    # ── recall: present library tracks now match ──────────────────────────
    def test_youtube_vevo_uploader_with_artist_title_in_video_title(self):
        lib = [_item("Hotline Bling", "Drake")]
        # "<Artist>VEVO" uploader is not the artist; real pair lives in the title.
        self.assertMatches(lib, "DrakeVEVO", "Drake - Hotline Bling (Official Video)", "Hotline Bling")

    def test_youtube_official_music_video_suffix(self):
        lib = [_item("Redbone", "Childish Gambino")]
        self.assertMatches(
            lib, "ChildishGambinoVEVO",
            "Childish Gambino - Redbone (Official Music Video)", "Redbone",
        )

    def test_youtube_aggregator_channel_uploader(self):
        lib = [_item("Sicko Mode", "Travis Scott")]
        # Promo/aggregator uploader ("TrapNation") is not the artist.
        self.assertMatches(lib, "TrapNation", "Travis Scott - SICKO MODE (Audio)", "Sicko Mode")

    def test_soundcloud_spaced_artist_title(self):
        lib = [_item("Jocelyn Flores", "XXXTentacion")]
        self.assertMatches(lib, "coolvibes", "XXXTentacion - Jocelyn Flores", "Jocelyn Flores")

    def test_soundcloud_compact_title_dash_artist(self):
        lib = [_item("Passionfruit", "Drake")]
        # SoundCloud/SpotiFLAC "Title-ARTIST" compact order.
        self.assertMatches(lib, "SoundCloud User", "Passionfruit-DRAKE", "Passionfruit")

    def test_plain_entry_without_channel_noise_still_matches(self):
        lib = [_item("One Dance", "Drake")]
        self.assertMatches(lib, "Drake", "One Dance (feat. Wizkid & Kyla)", "One Dance")

    # ── precision: distinct tracks must never be cross-mapped ─────────────
    def test_dash_title_wrong_artist_stays_missing(self):
        # "Hello" is in the library by Adele; a "Hello - Goodbye" entry by a
        # different artist must NOT be mapped onto it via the title split.
        lib = [_item("Hello", "Adele"), _item("Hotline Bling", "Drake")]
        self.assertMissing(lib, "Beatles", "Hello - Goodbye")

    def test_split_does_not_map_absent_track(self):
        lib = [_item("Hello", "Adele")]
        self.assertMissing(lib, "SomeChannel", "Adele - Someone Like You")

    def test_swap_does_not_match_on_title_alone(self):
        lib = [_item("Hotline Bling", "Drake")]
        self.assertMissing(lib, "RandomUploader", "Hotline Bling - Ringtone Remix")


if __name__ == "__main__":
    unittest.main()
