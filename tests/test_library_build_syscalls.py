"""ARCH-031: the cold library build lists each folder once instead of
stat-ing every Beets item, every walked entry and six cover-art names per
album. Synthetic library on a temp folder; Beets is faked."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.library_service as ls
from backend.beets_adapter import RemoteAlbum, RemoteItem


class LibraryBuildSyscallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.items, self.albums = [], []
        iid = aid = 0
        for a in range(3):
            artist = f"Artist {a}"
            for b in range(2):
                aid += 1
                folder = self.root / artist / f"Album {b} (200{b})"
                folder.mkdir(parents=True)
                (folder / "cover.jpg").touch()
                self.albums.append({"id": aid, "albumartist": artist, "album": f"Album {b}",
                                    "mb_albumid": f"m{aid}"})
                for t in range(3):
                    iid += 1
                    rel = f"{artist}/Album {b} (200{b})/0{t + 1}.flac"
                    (self.root / rel).touch()
                    self.items.append({"id": iid, "album_id": aid, "path": rel, "title": f"T{t}", "track": t + 1,
                                       "disc": 1, "tracktotal": 3, "albumartist": artist, "artist": artist,
                                       "album": f"Album {b}", "year": 2000 + b})
        # Two items whose files are gone: one in a present folder, one with no folder.
        self.items.append({"id": 900, "album_id": 1, "path": "Artist 0/Album 0 (2000)/09.flac", "title": "Lost",
                           "track": 9, "disc": 1, "tracktotal": 3, "albumartist": "Artist 0", "artist": "Artist 0",
                           "album": "Album 0", "year": 2000})
        self.items.append({"id": 901, "album_id": 99, "path": "Artist 1/Gone/01.flac", "title": "Gone", "track": 1,
                           "disc": 1, "tracktotal": 1, "albumartist": "Artist 1", "artist": "Artist 1",
                           "album": "Gone", "year": 2001})
        fake = mock.Mock()
        fake.items.side_effect = lambda q=None: [RemoteItem(dict(r)) for r in self.items]
        fake.albums.side_effect = lambda q=None: [RemoteAlbum(dict(r), adapter=object()) for r in self.albums]
        for patcher in (mock.patch.object(ls, "MUSIC_ROOT", self.root), mock.patch.object(ls, "lib", fake),
                        mock.patch.object(ls, "_load_pending_reviews", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)

    @unittest.skipIf(os.name != "posix", "counts os.stat, which Windows pathlib.exists() does not call")
    def test_stats_only_the_missing_items(self):
        real_stat = os.stat
        with mock.patch("os.stat", side_effect=real_stat) as stat:
            payload = ls._build_library_payload()
        # MUSIC_ROOT.exists() plus one check per missing item -- not one per file.
        self.assertLessEqual(stat.call_count, 3, [c.args[0] for c in stat.call_args_list])
        albums = {(a["name"], al["album"]): al for a in payload["artists"] for al in a["albums"]}
        first = albums[("Artist 0", "Album 0")]
        self.assertEqual((first["track_count"], first["missing"]), (4, 1))
        self.assertTrue(first["disk_art"].endswith("cover.jpg"))
        self.assertEqual(albums[("Artist 1", "Gone")]["missing"], 1)
        self.assertEqual(payload["stats"]["tracks"], 20)


if __name__ == "__main__":
    unittest.main()
