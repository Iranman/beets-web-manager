"""D4 / #302: Beets 2.14 stores item paths relative to its library directory
and expands them through a ContextVar that only the thread which built
Library() holds. Werkzeug serves every request on a new thread, so stock
routes (/item/<id>/file, item rows, path: queries) saw relative paths."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import tests.test_stock_beets_acceptance as acc
from backend.beets_adapter import BeetsAdapter, BeetsAdapterConnectionError


def _on_new_thread(fn):
    """Run fn on a fresh thread (fresh contextvars context), like Werkzeug."""
    out = {}
    t = threading.Thread(target=lambda: out.update(result=fn()))
    t.start()
    t.join(30)
    return out["result"]


class StockRoutesOnRequestThreadTests(unittest.TestCase):
    setUp = acc.StockBeetsInProcessAcceptanceTests.setUp
    tearDown = acc.StockBeetsInProcessAcceptanceTests.tearDown

    def test_item_file_streams_on_a_request_thread(self):
        res = _on_new_thread(lambda: self.client.get(f"/item/{self.item1.id}/file"))
        self.assertEqual(res.status_code, 200)
        with open(self.sample_file, "rb") as f:
            self.assertEqual(res.data, f.read())

    def test_item_rows_and_path_queries_are_absolute_on_a_request_thread(self):
        row = _on_new_thread(lambda: self.client.get(f"/item/{self.item1.id}").get_json())
        self.assertEqual(row["path"], self.sample_file)
        found = _on_new_thread(lambda: self.client.get("/item/query/" + "path:" + self.sample_file.replace("/", "\\")))
        self.assertEqual(found.status_code, 200)
        self.assertEqual([r["id"] for r in found.get_json()["results"]], [self.item1.id])


class AdapterAbsolutePathTests(unittest.TestCase):
    """A webmanager plugin that predates the app-wide binding (Beets not yet
    restarted) still serves relative paths; the adapter joins them to the
    library directory Beets reports."""

    def _adapter(self, responses, libdir="/music"):
        ad = BeetsAdapter(base_url="http://beets:8337", api_key="k")
        calls = []

        def fake(method, path, **_):
            calls.append(path)
            if path == "/webmanager/status":
                if isinstance(libdir, Exception):
                    raise libdir
                return {"library_directory": libdir}
            return responses[path]

        ad._request = fake
        return ad, calls

    def test_relative_paths_become_absolute_once_per_call(self):
        ad, calls = self._adapter({
            "/item/": {"items": [{"id": 1, "path": "A/B/01.flac"}, {"id": 2, "path": "/music/A/B/02.flac"},
                                 {"id": 3, "path": "A/B/03.flac"}]},
            "/album/7": {"id": 7, "artpath": "A/B/cover.jpg", "items": [{"id": 1, "path": "A/B/01.flac"}]},
        })
        self.assertEqual([r["path"] for r in ad.get_items()],
                         ["/music/A/B/01.flac", "/music/A/B/02.flac", "/music/A/B/03.flac"])
        album = ad.get_album(7)
        self.assertEqual(album["artpath"], "/music/A/B/cover.jpg")
        self.assertEqual(album["items"][0]["path"], "/music/A/B/01.flac")
        self.assertEqual(calls.count("/webmanager/status"), 1)  # cached once known

    def test_absolute_paths_need_no_status_call(self):
        ad, calls = self._adapter({"/item/1": {"id": 1, "path": "/music/x.flac"}})
        self.assertEqual(ad.get_item(1)["path"], "/music/x.flac")
        self.assertNotIn("/webmanager/status", calls)

    def test_unreadable_status_leaves_paths_and_asks_once_per_call(self):
        ad, calls = self._adapter({"/item/": {"items": [{"id": 1, "path": "a.flac"}, {"id": 2, "path": "b.flac"}]}},
                                  libdir=BeetsAdapterConnectionError("down"))
        self.assertEqual([r["path"] for r in ad.get_items()], ["a.flac", "b.flac"])
        self.assertEqual(calls.count("/webmanager/status"), 1)

    def test_dotdot_relative_paths_fail_closed(self):
        """#351 N5: ".." is never collapsed lexically; the row loses its path."""
        ad, _ = self._adapter({
            "/item/": {"items": [{"id": 1, "path": "A/../../etc/passwd"}, {"id": 2, "path": "link/../x.flac"},
                                 {"id": 3, "path": "A/..B/01.flac"}]},
            "/album/7": {"id": 7, "artpath": "../cover.jpg", "items": [{"id": 1, "path": ".."}]},
        })
        self.assertEqual([r["path"] for r in ad.get_items()], [None, None, "/music/A/..B/01.flac"])
        album = ad.get_album(7)
        self.assertIsNone(album["artpath"])
        self.assertIsNone(album["items"][0]["path"])

    def test_relative_library_directory_is_never_joined(self):
        """#351 N5: a library_directory that is not absolute counts as unknown."""
        for bad in ("music", "./music", ""):
            ad, calls = self._adapter({"/item/": {"items": [{"id": 1, "path": "a.flac"}]}}, libdir=bad)
            self.assertEqual(ad.get_items()[0]["path"], "a.flac")
            self.assertEqual(ad.get_items()[0]["path"], "a.flac")
            self.assertEqual(calls.count("/webmanager/status"), 2)  # not cached


class FindItemByPathBackslashTests(unittest.TestCase):
    """#351 N6: Beets turns "\\" in a query term into os.sep, so a path with a
    backslash is queried as another path and Beets' match cannot be trusted."""

    def _adapter(self, rows):
        ad = BeetsAdapter(base_url="http://beets:8337", api_key="k")
        ad._request = lambda method, path, **_: {"results": [dict(r) for r in rows]}
        return ad

    def test_include_paths_off_match_is_not_trusted_for_a_backslash_path(self):
        ad = self._adapter([{"id": 9}])  # Beets matched /music/A/B/01.flac
        self.assertIsNone(ad.find_item_by_path("/music/A/B\\01.flac"))
        self.assertEqual(ad.find_item_by_path("/music/A/B/01.flac")["id"], 9)

    def test_include_paths_on_rejects_the_other_file(self):
        ad = self._adapter([{"id": 9, "path": "/music/A/B/01.flac"}])
        self.assertIsNone(ad.find_item_by_path("/music/A/B\\01.flac"))
        self.assertEqual(ad.find_item_by_path("/music/A/B/01.flac")["id"], 9)


class ArtworkPlacementRelativePathTests(unittest.TestCase):
    """#302: artwork placement found no album folder for a relative item path."""

    def test_relative_item_path_locates_the_album_folder(self):
        import backend.artwork_service as art
        import backend.composite_workflows as cw

        with tempfile.TemporaryDirectory() as td:
            album_dir = Path(td, "Artist", "Album")
            album_dir.mkdir(parents=True)
            src = Path(td, "src")
            src.mkdir()
            ad = BeetsAdapter(base_url="http://beets:8337", api_key="k")
            libdir = Path(td).as_posix()

            def fake(method, path, **_):
                if path == "/webmanager/status":
                    return {"library_directory": libdir}
                return {"id": 5, "items": [{"id": 1, "album_id": 5, "path": "Artist/Album/01.flac"}]}

            ad._request = fake
            with mock.patch.object(cw, "beets_adapter", ad):
                log = []
                target = art._move_artwork_to_target(src, [5], log)
            self.assertEqual(Path(target), Path(libdir, "Artist", "Album"))
            self.assertFalse(any("Cannot find canonical album folder" in line for line in log))


if __name__ == "__main__":
    unittest.main()
