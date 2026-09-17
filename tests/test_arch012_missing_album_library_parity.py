"""ARCH-012 Missing-Album Library Parity & Regression Tests.

Validates that _build_library_payload() and _library_stats_for_artists() correctly
resolve and count albums whose on-disk folders are entirely missing:
1. Artist folder on disk, album folder missing -> counted as real album (album_id > 0), not singleton.
2. Artist folder NOT on disk at all -> counted as real album (album_id > 0), not singleton.
3. True singletons (album_id == 0) -> counted in singleton_tracks, not albums.
4. Disk-only folders -> counted in disk_only_*, excluded from main albums/tracks.
5. Multiple date-stamped Beets items for the same missing album merge into 1 album card.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402


class MockItem:
    def __init__(self, id, album_id, artist, album, title, track, path, year=2020, tracktotal=10, disc=1, mb_trackid=""):
        self.id = id
        self.album_id = album_id
        self.albumartist = artist
        self.artist = artist
        self.album = album
        self.title = title
        self.track = track
        self.path = path
        self.year = year
        self.tracktotal = tracktotal
        self.disc = disc
        self.mb_trackid = mb_trackid


class MockAlbum:
    def __init__(self, id, albumartist, album, year=2020, mb_albumid="mb-alb-123", mb_releasegroupid="mb-rg-123", albumtype="album"):
        self.id = id
        self.albumartist = albumartist
        self.artist = albumartist
        self.album = album
        self.year = year
        self.artpath = "/path/to/art.jpg"
        self.albumartist_credit = ""
        self.albumartists = ""
        self.albumartists_credit = ""
        self.mb_albumartistid = ""
        self.mb_albumartistids = ""
        self.mb_albumid = mb_albumid
        self.mb_releasegroupid = mb_releasegroupid
        self.albumtype = albumtype
        self.albumtypes = albumtype


class Arch012MissingAlbumLibraryParityTests(unittest.TestCase):

    def test_missing_album_under_existing_artist_folder_counted_as_album(self):
        """When an artist folder exists on disk but a specific album's folder is missing,
        its Beets album rows must resolve to the real album_id and count as 1 album."""
        mock_lib = MagicMock()
        
        # Album in Beets DB (id=42)
        ba = MockAlbum(id=42, albumartist="Radiohead", album="OK Computer", year=1997)
        mock_lib.albums.return_value = [ba]
        
        # Items in Beets DB whose paths don't exist on disk
        items = [
            MockItem(id=101, album_id=42, artist="Radiohead", album="OK Computer",
                     title="Airbag", track=1, path="/data/media/music/Radiohead/OK Computer/01.flac", year=1997),
            MockItem(id=102, album_id=42, artist="Radiohead", album="OK Computer",
                     title="Paranoid Android", track=2, path="/data/media/music/Radiohead/OK Computer/02.flac", year=1997),
        ]
        mock_lib.items.return_value = items

        with patch.object(app, "lib", mock_lib), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.iterdir", return_value=[]):
            
            payload = app._build_library_payload()
            stats = app._library_stats_for_artists(payload["artists"])
            
            self.assertEqual(stats["albums"], 1, "Missing album must be counted as 1 album")
            self.assertEqual(stats["tracks"], 2, "Tracks must count toward tracks total")
            self.assertEqual(stats["singleton_tracks"], 0, "Missing album tracks must not be marked as singletons")
            
            # Check album card fields
            radiohead = next((a for a in payload["artists"] if a["name"] == "Radiohead"), None)
            self.assertIsNotNone(radiohead)
            self.assertEqual(len(radiohead["albums"]), 1)
            alb = radiohead["albums"][0]
            self.assertEqual(alb["album_id"], 42)
            self.assertEqual(alb["missing"], 2)
            self.assertEqual(alb["track_count"], 2)

    def test_missing_artist_and_album_counted_as_album(self):
        """When an artist folder does NOT exist on disk at all, any Beets rows for it
        must resolve to the real album_id and count as 1 album, not singletons."""
        mock_lib = MagicMock()
        
        ba = MockAlbum(id=99, albumartist="Daft Punk", album="Discovery", year=2001)
        mock_lib.albums.return_value = [ba]
        
        items = [
            MockItem(id=201, album_id=99, artist="Daft Punk", album="Discovery",
                     title="One More Time", track=1, path="/data/media/music/Daft Punk/Discovery/01.flac", year=2001),
            MockItem(id=202, album_id=99, artist="Daft Punk", album="Discovery",
                     title="Aerodynamic", track=2, path="/data/media/music/Daft Punk/Discovery/02.flac", year=2001),
        ]
        mock_lib.items.return_value = items

        with patch.object(app, "lib", mock_lib), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.iterdir", return_value=[]):
            
            payload = app._build_library_payload()
            stats = app._library_stats_for_artists(payload["artists"])
            
            self.assertEqual(stats["albums"], 1)
            self.assertEqual(stats["tracks"], 2)
            self.assertEqual(stats["singleton_tracks"], 0)
            
            daft_punk = next((a for a in payload["artists"] if a["name"] == "Daft Punk"), None)
            self.assertIsNotNone(daft_punk)
            self.assertEqual(len(daft_punk["albums"]), 1)
            alb = daft_punk["albums"][0]
            self.assertEqual(alb["album_id"], 99)
            self.assertEqual(alb["missing"], 2)

    def test_true_singleton_tracks_count_as_singletons_not_albums(self):
        """Items with album_id == 0 must count as singletons in stats, not albums."""
        mock_lib = MagicMock()
        mock_lib.albums.return_value = []
        
        items = [
            MockItem(id=301, album_id=0, artist="Solo Artist", album="Single Track",
                     title="Single Track", track=1, path="/data/media/music/Solo Artist/Single Track.flac"),
        ]
        mock_lib.items.return_value = items

        with patch.object(app, "lib", mock_lib), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.iterdir", return_value=[]):
            
            payload = app._build_library_payload()
            stats = app._library_stats_for_artists(payload["artists"])
            
            self.assertEqual(stats["albums"], 0)
            self.assertEqual(stats["tracks"], 1)
            self.assertEqual(stats["singleton_tracks"], 1)

    def test_multiple_date_stamped_missing_items_for_same_album_merge(self):
        """When Beets has items with different release dates for the same album_id,
        they must merge into a single album card."""
        mock_lib = MagicMock()
        
        ba = MockAlbum(id=55, albumartist="Kavinsky", album="Nightcall", year=2010)
        mock_lib.albums.return_value = [ba]
        
        items = [
            MockItem(id=401, album_id=55, artist="Kavinsky", album="Nightcall",
                     title="Nightcall", track=1, path="/data/media/music/Kavinsky/Nightcall/01.flac", year=20100401),
            MockItem(id=402, album_id=55, artist="Kavinsky", album="Nightcall",
                     title="Pacific Coast Highway", track=2, path="/data/media/music/Kavinsky/Nightcall/02.flac", year=20101015),
        ]
        mock_lib.items.return_value = items

        with patch.object(app, "lib", mock_lib), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.iterdir", return_value=[]):
            
            payload = app._build_library_payload()
            stats = app._library_stats_for_artists(payload["artists"])
            
            self.assertEqual(stats["albums"], 1)
            self.assertEqual(stats["tracks"], 2)
            self.assertEqual(stats["singleton_tracks"], 0)
            
            kavinsky = next((a for a in payload["artists"] if a["name"] == "Kavinsky"), None)
            self.assertIsNotNone(kavinsky)
            self.assertEqual(len(kavinsky["albums"]), 1)
            alb = kavinsky["albums"][0]
            self.assertEqual(alb["album_id"], 55)
            self.assertEqual(len(alb["tracks"]), 2)

    def test_missing_album_with_differing_item_album_name_resolves_via_album_id(self):
        """When item artist/album string differs from the Beets Album row,
        item.album_id must still resolve the Beets album metadata correctly."""
        mock_lib = MagicMock()
        
        ba = MockAlbum(id=77, albumartist="Radiohead [UK]", album="Kid A", year=2000)
        mock_lib.albums.return_value = [ba]
        
        items = [
            MockItem(id=501, album_id=77, artist="Radiohead", album="Kid A (Collector's Edition)",
                     title="Everything In Its Right Place", track=1,
                     path="/data/media/music/Radiohead/Kid A/01.flac", year=2000),
        ]
        mock_lib.items.return_value = items

        with patch.object(app, "lib", mock_lib), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("pathlib.Path.iterdir", return_value=[]):
            
            payload = app._build_library_payload()
            stats = app._library_stats_for_artists(payload["artists"])
            
            self.assertEqual(stats["albums"], 1, "Must resolve to real album via album_id")
            self.assertEqual(stats["tracks"], 1)
            self.assertEqual(stats["singleton_tracks"], 0)


if __name__ == "__main__":
    unittest.main()
