"""YouTube/SoundCloud playlist-entry matching against a present library.

YT/SC entries routinely carry the real "Artist - Title" (spaced) or compact
"Title-ARTIST" inside the video title while the uploader field is a
VEVO/Topic/aggregator channel that is not the artist. Deriving a different
artist by re-splitting the title is DEFAULT-DENY: it happens only for a
genuinely artist-less entry, or a raw uploader field paired with the original
title (rows carry `uploader_artist`, as the producers set). Any established
artist -- upstream metadata, a parser split, a canonicalization promotion,
m3u EXTINF, or file tags -- leaves the title settled and never re-split.

These tests feed representative builder rows into the real match path
(`_match_playlist_tracks`) and prove both recall and that a distinct track is
never cross-mapped. `PlaylistDedupMatchTests` covers the dedup caller that
opts out entirely.
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


def _ns(items):
    return {
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


class PlaylistYtScMatchingTests(unittest.TestCase):
    def _match(self, items):
        return load_app_closure(["_match_playlist_tracks"], _ns(items))["_match_playlist_tracks"]

    def assertMatches(self, items, artist, title, expected_title, **flags):
        row = {"artist": artist, "title": title, **flags}
        matched, missing = self._match(items)([row])
        self.assertTrue(matched, f"expected a match for {row!r}; missing={missing}")
        self.assertEqual(expected_title, matched[0].get("title"))
        self.assertGreaterEqual(float(matched[0].get("score") or 0), 0.82)

    def assertMissing(self, items, artist, title, **flags):
        row = {"artist": artist, "title": title, **flags}
        matched, missing = self._match(items)([row])
        self.assertFalse(matched, f"{row!r} must not match; got {matched}")

    # ── recall: uploader-sourced entries (uploader_artist, as producers set) ─
    def test_youtube_vevo_uploader_with_artist_title_in_video_title(self):
        lib = [_item("Hotline Bling", "Drake")]
        self.assertMatches(lib, "DrakeVEVO", "Drake - Hotline Bling (Official Video)",
                           "Hotline Bling", uploader_artist=True)

    def test_youtube_official_music_video_suffix(self):
        lib = [_item("Redbone", "Childish Gambino")]
        self.assertMatches(lib, "ChildishGambinoVEVO",
                           "Childish Gambino - Redbone (Official Music Video)",
                           "Redbone", uploader_artist=True)

    def test_youtube_aggregator_channel_uploader(self):
        lib = [_item("Sicko Mode", "Travis Scott")]
        self.assertMatches(lib, "TrapNation", "Travis Scott - SICKO MODE (Audio)",
                           "Sicko Mode", uploader_artist=True)

    def test_soundcloud_spaced_artist_title(self):
        lib = [_item("Jocelyn Flores", "XXXTentacion")]
        self.assertMatches(lib, "coolvibes", "XXXTentacion - Jocelyn Flores",
                           "Jocelyn Flores", uploader_artist=True)

    def test_soundcloud_compact_title_dash_artist(self):
        lib = [_item("Passionfruit", "Drake")]
        # SoundCloud/SpotiFLAC "Title-ARTIST" compact order.
        self.assertMatches(lib, "SoundCloud User", "Passionfruit-DRAKE",
                           "Passionfruit", uploader_artist=True)

    def test_hyphenated_artist_spaced_still_matches(self):
        lib = [_item("Empire State of Mind", "Jay-Z")]
        self.assertMatches(lib, "SomeVEVO", "Jay-Z - Empire State of Mind",
                           "Empire State of Mind", uploader_artist=True)

    def test_genuinely_artist_less_entry_matches_by_title(self):
        lib = [_item("Hello", "Adele")]
        self.assertMatches(lib, "", "Hello", "Hello")

    def test_established_metadata_artist_matches_directly(self):
        # A correct metadata artist+title (no uploader flag) matches with no
        # re-split needed; feat. noise in the title is handled by scoring.
        lib = [_item("One Dance", "Drake")]
        self.assertMatches(lib, "Drake", "One Dance (feat. Wizkid & Kyla)", "One Dance")

    # ── precision: ABSENT track must not be invented ──────────────────────
    def test_dash_title_wrong_artist_stays_missing(self):
        lib = [_item("Hello", "Adele"), _item("Hotline Bling", "Drake")]
        self.assertMissing(lib, "Beatles", "Hello - Goodbye", uploader_artist=True)

    def test_split_does_not_map_absent_track(self):
        lib = [_item("Hello", "Adele")]
        self.assertMissing(lib, "SomeChannel", "Adele - Someone Like You", uploader_artist=True)

    def test_swap_does_not_match_on_title_alone(self):
        lib = [_item("Hotline Bling", "Drake")]
        self.assertMissing(lib, "RandomUploader", "Hotline Bling - Ringtone Remix", uploader_artist=True)

    # ── precision: a PRESENT-but-wrong row must not be cross-mapped ────────
    def test_punctuation_only_split_artist_does_not_title_only_match(self):
        lib = [_item("Hello", "Adele")]
        self.assertMissing(lib, "someuploader", "!!! - Hello", uploader_artist=True)

    def test_punctuation_only_artist_with_blank_uploader_stays_missing(self):
        # Artist-less entry: re-split is allowed, but the promoted "!!!"
        # normalizes away and must not title-only match Adele's "Hello".
        lib = [_item("Hello", "Adele")]
        self.assertMissing(lib, "", "!!! - Hello")

    def test_compact_version_label_is_not_treated_as_artist(self):
        lib = [_item("Lightning Crashes", "Live")]
        self.assertMissing(lib, "uploader", "Lightning Crashes-Live", uploader_artist=True)

    def test_compact_remix_label_is_not_treated_as_artist(self):
        lib = [_item("Humble", "Remix")]
        self.assertMissing(lib, "uploader", "Humble-Remix", uploader_artist=True)

    def test_spaced_artist_title_is_not_reversed(self):
        lib = [_item("Adele", "Hello")]
        self.assertMissing(lib, "uploader", "Adele - Hello", uploader_artist=True)

    def test_nested_spaced_then_compact_is_not_reversed(self):
        # Channel uploader -> canonicalization splits the SPACED dash first:
        # "Drake - Hello-ADELE" -> artist="Drake", title="Hello-ADELE" (now a
        # residual). The compact tail must not be reversed onto Adele.
        lib = [_item("Hello", "Adele")]
        self.assertMissing(lib, "Some Music", "Drake - Hello-ADELE", uploader_artist=True)

    def test_forward_split_of_residual_is_suppressed(self):
        lib = [_item("Hello", "Adele")]
        self.assertMissing(lib, "Some Music", "Drake - ADELE-Hello", uploader_artist=True)

    def test_gateless_fallback_channelish_promotion_is_suppressed(self):
        # Empty uploader -> "Roxy Music - Hello-ADELE" promotes artist="Roxy
        # Music"; the residual "Hello-ADELE" must not be reversed onto Adele.
        lib = [_item("Hello", "Adele")]
        self.assertMissing(lib, "", "Roxy Music - Hello-ADELE")

    def test_version_label_with_hyphen_and_double_space_is_caught(self):
        self.assertMissing([_item("Lightning Crashes", "Live")],
                           "uploader", "Lightning Crashes-Sped-Up!", uploader_artist=True)
        self.assertMissing([_item("Lightning Crashes", "Video")],
                           "uploader", "Lightning Crashes-Official  Video", uploader_artist=True)
        self.assertMissing([_item("Lightning Crashes", "Video")],
                           "uploader", "Lightning Crashes-Official.Video", uploader_artist=True)

    def test_punctuation_artist_does_not_title_only_match_punctuation_row(self):
        lib = [_item("Song", "???")]
        self.assertMissing(lib, "", "!!! - Song")

    # ── default-deny: an established artist row is never re-split ──────────
    def test_established_artist_compact_tail_not_resplit(self):
        # No uploader flag -> established artist (upstream metadata / EXTINF /
        # parser split). "Drake" + "ADELE-Hello" must not reinterpret to Adele.
        lib = [_item("Hello", "Adele")]
        self.assertMissing(lib, "Drake", "ADELE-Hello")
        self.assertMissing(lib, "Drake", "Hello-ADELE")

    def test_established_channelish_artist_not_resplit(self):
        # c2: a real artist that merely looks channelish ("Roxy Music") is
        # trusted, not discarded in favor of a title-derived one.
        lib = [_item("Hello", "Adele")]
        self.assertMissing(lib, "Roxy Music", "ADELE-Hello")
        self.assertMissing(lib, "Roxy Music", "Hello-ADELE")

    def test_matcher_honors_artist_from_split_provenance(self):
        # The exact track shape the text paste parser emits for a
        # "Drake - Hello-ADELE" line (artist pulled from the line).
        lib = [_item("Hello", "Adele")]
        self.assertMissing(lib, "Drake", "Hello-ADELE", artist_from_split=True)

    def test_artist_from_split_provenance_still_matches_correct_entry(self):
        lib = [_item("Hello", "Adele")]
        self.assertMatches(lib, "Adele", "Hello", "Hello", artist_from_split=True)

    # ── F1: NO punctuation/stylization spelling of a label may evade the
    #     guard -- it is recognized on the alnum-collapsed form ─────────────
    def test_stylized_version_labels_do_not_match_band(self):
        for tail in ("L.I.V.E.", "L.I.V.E._", "re.mix", "R.E.M.I.X!", "(Live)", "Live!!"):
            self.assertMissing([_item("Lightning Crashes", "Live")],
                               "uploader", f"Lightning Crashes-{tail}", uploader_artist=True)
        for tail in ("O.F.F.I.C.I.A.L", "Official_Video", "Official.Video"):
            self.assertMissing([_item("Lightning Crashes", "Official")],
                               "uploader", f"Lightning Crashes-{tail}", uploader_artist=True)

    # ── F3: a promoted artist is trusted; the title-only fallback must not
    #     adopt a different library artist for the residual ─────────────────
    def test_artist_less_promotion_does_not_title_only_adopt_other_artist(self):
        # "Roxy Music - Hello" (artist-less) promotes artist="Roxy Music";
        # the residual "Hello" must not be library-title-matched onto Adele.
        lib = [_item("Hello", "Adele")]
        self.assertMissing(lib, "", "Roxy Music - Hello")

    # ── F5: a canonical (alias) promotion clears the raw-uploader flag so the
    #     now-established artist's title is not later re-split ───────────────
    def test_channel_alias_promotion_is_not_resplit(self):
        # Uploader "StarBoy TV" (default alias -> Wizkid) with a compact-tail
        # title: after the alias establishes "Wizkid", the tail must not be
        # reversed onto Adele's "Hello".
        self.assertMissing([_item("Hello", "Adele")],
                           "StarBoy TV", "Hello-ADELE", uploader_artist=True)

    def test_channel_alias_recall_still_matches(self):
        # Recall: the alias still resolves and matches the present track.
        self.assertMatches([_item("Essence", "Wizkid")],
                           "StarBoy TV", "Essence", "Essence", uploader_artist=True)

    def test_channel_alias_applied_when_channel_also_in_title(self):
        # fix(4): the channel name is both the uploader and the title prefix
        # ("StarBoy TV" + "StarBoy TV - Essence"); the alias must still resolve
        # to Wizkid rather than returning the promoted channel name.
        self.assertMatches([_item("Essence", "Wizkid")],
                           "StarBoy TV", "StarBoy TV - Essence", "Essence", uploader_artist=True)


class PlaylistBuilderRowMatchTests(unittest.TestCase):
    """Representative producer rows into the matcher. The producer that sets
    an artist from upstream metadata (Spotify, yt-dlp `entry.artist`) or from
    a title split (m3u EXTINF) leaves no `uploader_artist` flag, so the title
    is settled and a compact tail is never reinterpreted onto a distinct row."""

    def _match(self, items):
        return load_app_closure(["_match_playlist_tracks"], _ns(items))["_match_playlist_tracks"]

    def _assert_missing(self, row):
        matched, _ = self._match([_item("Hello", "Adele")])([row])
        self.assertFalse(matched, f"{row!r} must not match; got {matched}")

    def test_spotify_metadata_row(self):
        # _fetch_spotify_playlist_tracks -> {"artist": <metadata>, "title": ...}
        self._assert_missing({"artist": "Drake", "title": "Hello-ADELE"})

    def test_ytdlp_metadata_artist_row(self):
        # yt-dlp entry.get("artist") present -> established, no uploader flag.
        self._assert_missing({"artist": "Drake", "title": "ADELE-Hello"})

    def test_m3u_extinf_split_row(self):
        # EXTINF "Artist - Title" -> {"artist": <split>, "title": ...}, established.
        self._assert_missing({"artist": "Drake", "title": "Hello-ADELE"})

    def test_ytdlp_uploader_row_still_matches(self):
        # yt-dlp uploader/channel fallback -> uploader_artist=True -> re-split ok.
        matched, _ = self._match([_item("Hotline Bling", "Drake")])(
            [{"artist": "DrakeVEVO", "title": "Drake - Hotline Bling (Official Video)",
              "uploader_artist": True}])
        self.assertTrue(matched)
        self.assertEqual("Hotline Bling", matched[0].get("title"))


class PlaylistProvenanceDropTests(unittest.TestCase):
    """Every promotion that establishes a canonical artist must drop the
    `uploader_artist` flag (F5), on both the live canonicalize path and the
    manifest serialization used for save->reload."""

    def _canon(self, track, items=(), mb=None):
        ns = _ns(list(items))
        if mb is not None:
            ns["_mb_recording_search"] = lambda *a, **k: mb
        return load_app_closure(["_playlist_canonicalize_track"], ns)["_playlist_canonicalize_track"](track)

    def _manifest(self, track, items=()):
        return load_app_closure(["_playlist_track_manifest_payload"], _ns(list(items)))[
            "_playlist_track_manifest_payload"](track)

    def test_video_title_promotion_drops_uploader_artist(self):
        row = self._canon({"artist": "Some Music", "title": "Drake - Song", "uploader_artist": True})
        self.assertEqual("Drake", row.get("artist"))
        self.assertNotIn("uploader_artist", row)

    def test_channel_alias_promotion_drops_uploader_artist(self):
        row = self._canon({"artist": "StarBoy TV", "title": "Essence", "uploader_artist": True})
        self.assertEqual("Wizkid", row.get("artist"))
        self.assertNotIn("uploader_artist", row)

    def test_library_title_promotion_drops_uploader_artist(self):
        # Channelish uploader, no promotion/alias; the library-title fallback
        # adopts Adele and must drop the flag.
        row = self._canon({"artist": "Random Music", "title": "Hello", "uploader_artist": True},
                          items=[_item("Hello", "Adele")])
        self.assertEqual("Adele", row.get("artist"))
        self.assertNotIn("uploader_artist", row)

    def test_musicbrainz_title_promotion_drops_uploader_artist(self):
        row = self._canon(
            {"artist": "Random Music", "title": "Hello", "uploader_artist": True},
            items=[], mb=[{"title": "Hello", "artist": "Adele", "score": 95}])
        self.assertEqual("Adele", row.get("artist"))
        self.assertNotIn("uploader_artist", row)

    def test_title_cleanup_keeps_uploader_artist(self):
        # Artist unchanged (only title noise stripped): still the raw uploader,
        # so the flag stays and the matcher can still re-split it.
        row = self._canon({"artist": "DrakeVEVO", "title": "Drake - Song (Official Video)",
                           "uploader_artist": True})
        self.assertEqual("DrakeVEVO", row.get("artist"))
        self.assertTrue(row.get("uploader_artist"))

    def test_manifest_round_trip_no_mismap(self):
        # save->reload: canonicalize, serialize via the manifest, then re-match.
        # An alias-resolved row must not carry uploader_artist into the reload
        # and must not re-split onto a distinct track.
        match_tracks = load_app_closure(["_match_playlist_tracks"], _ns([_item("Hello", "Adele")]))[
            "_match_playlist_tracks"]
        serialized = self._manifest({"artist": "StarBoy TV", "title": "Hello-ADELE",
                                     "uploader_artist": True})
        self.assertNotIn("uploader_artist", serialized)
        matched, _ = match_tracks([serialized])
        self.assertFalse(matched, f"reloaded row must not mis-map; got {matched}")

    def test_manifest_does_not_realias_established_artist(self):
        # QA(b): an already-established non-uploader artist must not be
        # overwritten by reapplying the uploader's channel alias.
        serialized = self._manifest({
            "artist": "Some Real Band", "title": "Song",
            "source_artist": "StarBoy TV", "canonicalized": True,
            "canonical_source": "library-title",
        })
        self.assertEqual("Some Real Band", serialized.get("artist"))


class PlaylistDedupMatchTests(unittest.TestCase):
    """The dedup caller (`backend/dedup_service.py`) calls
    `_playlist_match_payload_from_candidates` with no flag -- it is
    mutation-adjacent and must stay default-deny (no title re-split)."""

    def _helpers(self, items):
        return load_app_closure(
            ["_playlist_match_payload_from_candidates", "_playlist_library_match_candidates"],
            _ns(items))

    def test_dedup_file_tag_residual_not_resplit(self):
        h = self._helpers([_item("Hello", "Adele")])
        cands = h["_playlist_library_match_candidates"]()
        # File tags artist="Drake", title="ADELE-Hello": default-deny, no match.
        self.assertIsNone(h["_playlist_match_payload_from_candidates"]("Drake", "ADELE-Hello", cands))

    def test_dedup_direct_match_still_works(self):
        h = self._helpers([_item("Hello", "Adele")])
        cands = h["_playlist_library_match_candidates"]()
        payload = h["_playlist_match_payload_from_candidates"]("Adele", "Hello", cands)
        self.assertIsNotNone(payload)
        self.assertEqual("Hello", payload.get("title"))


if __name__ == "__main__":
    unittest.main()
