"""Library page showed the same artist several times.

Both tree builders produced one artist entry per raw grouping string: the full
builder one per artist *folder* ("Foo" and a stamped "Foo (<mbid>)" folder both
display as "Foo"), the fast builder one per exact albumartist string ("Foo",
"foo", "Foo  ", NFC/NFD variants). Entries with the same display name must now
collapse into one, unless their albums carry different mb_albumartistids.
"""
import unicodedata
import unittest
from unittest import mock

import backend.library_service as ls
from backend.beets_adapter import RemoteAlbum, RemoteItem

_MBID_X = "11111111-1111-1111-1111-111111111111"
_MBID_Y = "22222222-2222-2222-2222-222222222222"
_MBID_Z = "33333333-3333-3333-3333-333333333333"


def _lib(albums):
    items = [
        {"id": a["id"] * 10, "album_id": a["id"], "path": f"x/{a['id']}/01.flac",
         "title": "t", "track": 1, "disc": 1, "tracktotal": 1,
         "albumartist": a["albumartist"], "artist": a["albumartist"],
         "album": a["album"], "year": 2000, "mb_trackid": ""}
        for a in albums
    ]
    fake = mock.Mock()
    fake.items.side_effect = lambda q=None, fields=None: [RemoteItem(dict(r)) for r in items]
    fake.albums.side_effect = lambda q=None, fields=None: [
        RemoteAlbum(dict(r), adapter=object()) for r in albums]
    return fake


def _album(aid, artist, mbid=""):
    return {"id": aid, "albumartist": artist, "album": f"Album {aid}",
            "mb_albumartistid": mbid, "albumtype": "album"}


class DuplicateArtistTests(unittest.TestCase):
    def _names(self, albums):
        with mock.patch.object(ls, "lib", _lib(albums)), \
             mock.patch.object(ls, "_attach_artist_image_cache_urls", lambda a: None):
            payload = ls._build_library_payload_fast()
        return sorted(a["name"] for a in payload["artists"]), payload

    def test_case_whitespace_and_unicode_variants_are_one_artist(self):
        names, payload = self._names([
            _album(1, "Artist A", _MBID_X),
            _album(2, "artist a"),
            _album(3, "ARTIST  A", _MBID_X),
            _album(4, unicodedata.normalize("NFC", "Beyoncé")),
            _album(5, unicodedata.normalize("NFD", "Beyoncé")),
        ])
        self.assertEqual(len(names), 2, names)
        artist_a = next(a for a in payload["artists"] if a["name"].casefold() == "artist a")
        self.assertEqual(len(artist_a["albums"]), 3)
        self.assertEqual(artist_a["total"], 3)

    def test_different_mbids_are_never_merged(self):
        names, _ = self._names([
            _album(1, "Same Name", _MBID_Y),
            _album(2, "same name", _MBID_Z),
        ])
        self.assertEqual(len(names), 2, names)

    def test_stamped_and_plain_artist_folders_are_one_artist(self):
        # Full-builder shape: one entry per disk folder, name already stamp-stripped.
        result = [
            {"name": "Foo", "path": "/m/Foo", "albums": [
                {"album": "A", "album_id": 1, "track_count": 2, "mb_albumartistid": _MBID_X}],
             "total": 2, "imported": 2, "not_imported": 0, "missing": 0},
            {"name": "Foo", "path": f"/m/Foo ({_MBID_X})", "albums": [
                {"album": "B", "album_id": 2, "track_count": 5, "mb_albumartistid": _MBID_X}],
             "total": 5, "imported": 5, "not_imported": 0, "missing": 0},
            {"name": "Bar", "path": "/m/Bar", "albums": [], "total": 0,
             "imported": 0, "not_imported": 0, "missing": 0},
        ]
        ls._merge_same_name_artists(result)
        self.assertEqual([a["name"] for a in result], ["Foo", "Bar"])
        foo = result[0]
        self.assertEqual(foo["path"], f"/m/Foo ({_MBID_X})")  # spelling with most tracks
        self.assertEqual(sorted(a["album"] for a in foo["albums"]), ["A", "B"])
        self.assertEqual(foo["total"], 7)


if __name__ == "__main__":
    unittest.main()
