"""Real behavioral test around the Import Review "Needs MB ID"
(library_no_mb) album query in import_review_queue(): /api/library counted
620 albums missing a MusicBrainz release ID, but the review queue showed
far fewer. The original hypothesis here was a UnicodeDecodeError from
missing text_factory=bytes on a non-UTF-8 albumartist/album/path value,
silently swallowed by a blanket `except: pass`. That hypothesis was
disproven (see test_blob_bound_bytes_bypass_text_factory_regardless_of_setting
below): a byte string bound via sqlite3.Binary() into a TEXT-affinity
column is stored as BLOB storage class and bypasses text_factory
entirely, on any Python/sqlite3 setting. The real root cause was that
every one of those 620 rows was a *singleton item* (a track with no
albums-table row, album_id NULL) -- structurally invisible to a query
that only ever scans the `albums` table. See
tests/test_review_queue_singleton_items.py for the actual fix.

text_factory=bytes was still applied to this query as a legitimate
defensive match for ~48 other path/text-sensitive queries elsewhere in
this file; this test file now documents and verifies that decision
without asserting the disproven crash theory.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")

_SCHEMA = """
CREATE TABLE albums (id INTEGER PRIMARY KEY, albumartist TEXT, album TEXT, year TEXT, mb_albumid TEXT);
CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, path TEXT, added REAL);
"""

_QUERY = (
    "SELECT albums.id, albums.albumartist, albums.album, albums.year, "
    "COUNT(items.id) AS tracks, MAX(items.added) AS added, "
    "MIN(items.id) AS first_item_id, MIN(items.path) AS first_item_path "
    "FROM albums LEFT JOIN items ON items.album_id = albums.id "
    "WHERE COALESCE(albums.mb_albumid, '') = '' "
    "GROUP BY albums.id "
    "ORDER BY COALESCE(MAX(items.added), 0) DESC "
    "LIMIT ?"
)


def _make_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    # A clean row that should always show up.
    con.execute("INSERT INTO albums (id, albumartist, album, year, mb_albumid) VALUES (1, 'Clean Artist', 'Clean Album', '2020', '')")
    con.execute("INSERT INTO items (id, album_id, path, added) VALUES (1, 1, 'Clean Artist/Clean Album/01.mp3', 100)")
    con.commit()
    # A row with a non-UTF-8 byte sequence in albumartist, written via a raw
    # BLOB bind so sqlite3 stores exactly those bytes in the TEXT column
    # (real beets libraries can end up with these from scene-release/torrent
    # metadata in mixed encodings).
    bad_bytes = b"Sc\xe8ne Group"  # \xe8 is not valid standalone UTF-8
    con.execute(
        "INSERT INTO albums (id, albumartist, album, year, mb_albumid) VALUES (2, ?, 'Messy Album', '2019', '')",
        (sqlite3.Binary(bad_bytes),),
    )
    con.execute("INSERT INTO items (id, album_id, path, added) VALUES (2, 2, 'Messy/Messy Album/01.mp3', 50)")
    con.commit()
    con.close()


class LibraryNoMbQueryTextFactoryTests(unittest.TestCase):
    def test_blob_bound_bytes_bypass_text_factory_regardless_of_setting(self):
        # A byte string bound via sqlite3.Binary() into a TEXT-affinity
        # column is stored as SQLite's BLOB storage class (TEXT affinity
        # only converts INTEGER/REAL values, never BLOB), so it bypasses
        # text_factory entirely -- the default str text_factory does NOT
        # raise here, on either setting. This disproves the original
        # UnicodeDecodeError hypothesis for the "library_no_mb rows go
        # missing" bug (real root cause: singleton items with no albums
        # row at all, see tests/test_review_queue_singleton_items.py).
        # text_factory=bytes remains a legitimate, intentional match for
        # ~48 other path/text-sensitive queries in this codebase and is
        # still applied to this query for that consistency, just not
        # because it fixes a crash that never actually happens here.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "lib.blb")
            _make_db(db_path)
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(_QUERY, (5000,)).fetchall()
                self.assertEqual({int(r["id"]) for r in rows}, {1, 2})
            finally:
                con.close()

    def test_text_factory_bytes_returns_every_row_including_the_messy_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "lib.blb")
            _make_db(db_path)
            con = sqlite3.connect(db_path)
            con.text_factory = bytes
            con.row_factory = sqlite3.Row
            rows = con.execute(_QUERY, (5000,)).fetchall()
            con.close()
            self.assertEqual(len(rows), 2)
            ids = {int(r["id"]) for r in rows}
            self.assertEqual(ids, {1, 2})
            # The messy row's albumartist must still be usable via _s()'s
            # errors="replace" decoding, not raise a second time.
            messy = next(r for r in rows if int(r["id"]) == 2)
            decoded = messy["albumartist"].decode("utf-8", errors="replace")
            self.assertIn("Sc", decoded)


class LibraryNoMbQuerySourceFixTests(unittest.TestCase):
    def test_query_now_uses_text_factory_bytes(self):
        start = APP_SOURCE.index("MIN(items.path) AS first_item_path")
        block = APP_SOURCE[max(0, start - 400):start]
        self.assertIn("with _db(text_factory=bytes, row_factory=sqlite3.Row) as con:", block)


if __name__ == "__main__":
    unittest.main()
