import unittest
from pathlib import Path
try:  # ARCH-001: app.py module family (works under discovery and tests.<module> runs)
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402


class LibraryLazyTracksTests(unittest.TestCase):
    def test_library_summary_defers_tracks_but_album_tracks_include_disk_only_rows(self):
        root = Path(__file__).resolve().parents[1]
        app_source = app_family_source()
        types_source = (root / "frontend" / "src" / "types" / "api.ts").read_text(encoding="utf-8")
        client_source = (root / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        library_page = (root / "frontend" / "src" / "views" / "Library.tsx").read_text(encoding="utf-8")

        self.assertIn("def _library_payload_for_response", app_source)
        self.assertIn('next_album["tracks_deferred"] = True', app_source)
        self.assertIn("def _library_album_is_disk_only", app_source)
        self.assertIn("include_disk_only", app_source)
        self.assertIn("def _library_stats_for_artists", app_source)
        self.assertIn('include_tracks=request.args.get("include_tracks", "0") == "1"', app_source)
        self.assertIn('@app.get("/api/albums/<int:aid>/tracks")', app_source)
        self.assertIn('"ok": False,', app_source)
        self.assertIn('"missing": False,', app_source)
        self.assertIn('p.suffix.lower() in AUDIO_EXT', app_source)

        self.assertIn("tracks?: LibraryTrack[];", types_source)
        self.assertIn("tracks_deferred?: boolean;", types_source)
        self.assertIn("getAlbumTracks", client_source)
        self.assertIn("await getAlbumTracks(albumId)", library_page)
        # The DiscographyPanel assertions that used to live here are gone with
        # the panel itself: it was never imported or rendered, and its backend
        # album-completeness endpoints survive untouched for future clients.
        # The Library is a browse-only overview: no repair focal block, issue filters, review notes
        # or fix links. The album back cover prints the lazily loaded tracklist instead.
        self.assertIn("Side A", library_page)
        for removed in ("function NextRepairPanel", "albumSuggestedStatus", "compareRepairRows",
                        "Show issue group", "No passive issue evidence", "Manage on Import",
                        "Missing MusicBrainz IDs"):
            self.assertNotIn(removed, library_page)


if __name__ == "__main__":
    unittest.main()
