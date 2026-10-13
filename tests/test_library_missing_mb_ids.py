"""Review Queue split (audit F5, F6, F18).

GET /api/import/review-queue lists import decisions only (pending_ai and
skipped) and never reads Beets. Library albums/tracks missing MusicBrainz IDs
moved to GET /api/library/missing-mb-ids, which builds the full list from one
get_items + get_albums read, sorts it, then slices one page.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.beets_adapter import BeetsAuthError, BeetsUnavailableError

_UNMATCHED = "backend.composite_workflows.get_unmatched_review_items"


def _album(aid, added, first_path, **kw):
    return {"id": aid, "album": f"Album {aid}", "albumartist": f"Artist {aid}", "year": 2001,
            "added": added, "first_item_id": aid * 10, "first_item_path": first_path, "tracks": 3, **kw}


def _single(iid, added, path, **kw):
    return {"id": iid, "title": f"Song {iid}", "artist": f"Singer {iid}", "added": added,
            "path": path, "length": 61.0, **kw}


class _ClientCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module  # noqa: F401  (registers routes; never at module level)
        cls.client = app_module.app.test_client()

    def setUp(self):
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        env.start()
        self.addCleanup(env.stop)

    def patch(self, target, **kw):
        p = mock.patch(target, **kw)
        self.addCleanup(p.stop)
        return p.start()


class LibraryMissingMbIdsTests(_ClientCase):
    def setUp(self):
        super().setUp()
        from backend.app_runtime import MUSIC_ROOT
        self.music = MUSIC_ROOT
        self.data = {
            "albums": [
                _album(1, 100, "A1/Album 1/CD2/01.flac"),
                _album(2, 300, "A2/Album 2/01.flac"),
                _album(3, 50, "/elsewhere/not-music/01.flac"),  # outside MUSIC_ROOT
            ],
            "singletons": [_single(10 + i, 200 + i, f"Loose/Song {10 + i}.mp3") for i in range(5)],
        }
        self.unmatched = self.patch(_UNMATCHED, return_value=self.data)
        self.per_album = self.patch("backend.composite_workflows.find_all_items_by_album_id",
                                    side_effect=AssertionError("per-album Beets call (F6)"))
        self.patch("backend.library_service._load_pending_reviews",
                   return_value=[{"path": "/downloads/x", "suggestion": {"existing_album_ids": [2]}}])
        self.patch("backend.library_service._load_album_mb_suggestions",
                   return_value={"1": {"mb_albumid": "rel", "confidence": "high", "mb_valid": True}})

    def get(self, qs=""):
        res = self.client.get("/api/library/missing-mb-ids" + qs)
        return res.status_code, res.get_json()

    def test_full_list_sorted_newest_first_then_sliced(self):
        status, body = self.get("?limit=3")
        self.assertEqual(status, 200, body)
        self.assertEqual([r["id"] for r in body["items"]], ["album:2", "item:14", "item:13"])
        self.assertEqual((body["total"], body["offset"], body["limit"]), (7, 0, 3))
        self.assertEqual(body["counts"], {"all": 7, "album": 2, "item": 5})
        _, page2 = self.get("?limit=3&offset=3")
        self.assertEqual([r["id"] for r in page2["items"]], ["item:12", "item:11", "item:10"])
        _, last = self.get("?limit=3&offset=6")
        self.assertEqual([r["id"] for r in last["items"]], ["album:1"])
        # One full read, no paging inside the composite, no per-album calls.
        self.unmatched.assert_called_with(limit=0, offset=0, include_singletons=True)
        self.per_album.assert_not_called()

    def test_kind_search_sort_and_counts_only(self):
        _, body = self.get("?kind=album&sort=added_asc")
        self.assertEqual([r["id"] for r in body["items"]], ["album:1", "album:2"])
        self.assertEqual((body["total"], body["counts"]["all"]), (2, 7))
        _, body = self.get("?search=song%2012")
        self.assertEqual([r["id"] for r in body["items"]], ["item:12"])
        self.assertEqual(body["counts"], {"all": 1, "album": 0, "item": 1})
        _, body = self.get("?limit=0")
        self.assertEqual((body["items"], body["total"]), ([], 7))
        _, body = self.get("?sort=title&kind=item&limit=1")
        self.assertEqual(body["items"][0]["id"], "item:10")

    def test_row_shape(self):
        _, body = self.get("?limit=500")
        rows = {r["id"]: r for r in body["items"]}
        a1, a2, i10 = rows["album:1"], rows["album:2"], rows["item:10"]
        # Folder comes from the first item path, Disc folder collapsed (F6).
        self.assertEqual(a1["path"], str(self.music / "A1" / "Album 1"))
        self.assertEqual(a1["folder_name"], "Album 1")
        self.assertEqual((a1["type"], a1["target_kind"], a1["missing_id_type"], a1["status_key"]),
                         ("library_no_mb", "album", "Release ID", "needs_mb_id"))
        self.assertEqual((a1["mb_albumid"], a1["confidence"], a1["mb_valid"]), ("rel", "high", True))
        # A pending item claims album 2: shown, flagged.
        self.assertEqual((a1["in_import_review"], a2["in_import_review"]), (False, True))
        self.assertEqual((i10["target_kind"], i10["missing_id_type"], i10["item_id"], i10["album_id"]),
                         ("item", "Recording ID", 10, 0))
        self.assertEqual(i10["path"], str(self.music / "Loose" / "Song 10.mp3"))
        self.assertEqual(i10["evidence"]["missing_id_type"], "Recording ID")
        self.assertIn("origin_type", i10)
        self.assertNotIn("album:3", rows)

    def test_bad_params_are_400(self):
        for qs in ("?kind=bogus", "?sort=bogus", "?limit=501", "?limit=x", "?offset=-1"):
            with self.subTest(qs=qs):
                status, body = self.get(qs)
                self.assertEqual((status, body["error_code"]), (400, "INVALID_PARAM"))

    def test_beets_errors_map_to_codes(self):
        for exc, status, code in ((BeetsUnavailableError("down"), 503, "ENGINE_UNAVAILABLE"),
                                  (BeetsAuthError("auth"), 502, "ENGINE_AUTH_ERROR"),
                                  (RuntimeError("boom"), 500, "INTERNAL_ERROR")):
            with self.subTest(code=code):
                self.unmatched.side_effect = exc
                got, body = self.get()
                self.assertEqual((got, body["ok"], body["error_code"]), (status, False, code))
                self.assertNotIn("boom", body["error"])


class ImportReviewQueueSplitTests(_ClientCase):
    def setUp(self):
        super().setUp()
        import routes_import
        self.ri = routes_import
        # Any Beets read fails loudly: the queue must not need Beets.
        self.patch(_UNMATCHED, side_effect=BeetsUnavailableError("down"))
        self.pending = [{"path": f"/downloads/P{i}", "folder_name": f"P{i}", "added_at": 1000 + i,
                         "status": "Pending AI", "suggestion": {"album": f"Pending {i}"}} for i in range(4)]
        self.skipped = [{"path": "/downloads/S1", "filename": "S1", "folder": "/downloads", "reason": "x"},
                        {"path": "/downloads/P1", "filename": "P1", "folder": "/downloads", "reason": "dup"},
                        {"path": "/downloads/S0", "filename": "S0", "folder": "/downloads", "reason": ""}]
        self.patch("routes_import._load_pending_reviews", side_effect=lambda **kw: list(self.pending))
        self.skipped_loader = self.patch("routes_import._import_skipped_items",
                                         side_effect=lambda *a, **kw: list(self.skipped))

    def get(self, qs=""):
        res = self.client.get("/api/import/review-queue" + qs)
        return res.status_code, res.get_json()

    def test_import_decisions_only_while_beets_is_down(self):
        status, body = self.get()
        self.assertEqual(status, 200, body)
        self.assertEqual({r["type"] for r in body["items"]}, {"pending_ai", "skipped"})
        self.assertEqual(body["counts"], {"all": 6, "pending_ai": 4, "skipped": 2})
        self.assertNotIn("library_no_mb", body["counts"])

    def test_skipped_row_deduped_against_pending_path(self):
        _, body = self.get("?status=skipped")
        self.assertEqual([r["path"] for r in body["items"]], ["/downloads/S1", "/downloads/S0"])

    def test_newest_first_then_sliced_with_real_total(self):
        _, body = self.get("?limit=2")
        self.assertEqual([r["path"] for r in body["items"]], ["/downloads/P3", "/downloads/P2"])
        self.assertEqual((body["total"], body["offset"], body["limit"]), (6, 0, 2))
        _, body = self.get("?limit=2&offset=3")
        self.assertEqual([r["path"] for r in body["items"]], ["/downloads/P0", "/downloads/S1"])
        _, body = self.get("?limit=0")
        self.assertEqual((body["items"], body["total"]), ([], 6))
        # The skipped scan is not capped by the page size, so total is real.
        self.assertEqual(self.skipped_loader.call_args.args[0], self.ri._SKIPPED_SCAN_CAP)

    def test_library_no_mb_status_is_a_moved_stub(self):
        status, body = self.get("?status=library_no_mb")
        self.assertEqual(status, 200)
        self.assertEqual(body["moved_to"], "/api/library/missing-mb-ids")
        self.assertEqual((body["items"], body["total"]), ([], 0))
        self.assertEqual(body["counts"], {"all": 0, "pending_ai": 0, "skipped": 0})

    def test_bad_paging_params_are_400(self):
        for qs in ("?limit=x", "?offset=-2"):
            with self.subTest(qs=qs):
                status, body = self.get(qs)
                self.assertEqual((status, body["error_code"]), (400, "INVALID_PARAM"))


class ImportLogTailTests(unittest.TestCase):
    def test_tail_reads_only_the_last_bytes_and_drops_the_cut_line(self):
        from backend.import_service import _read_log_tail
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "beet.log"
            log.write_text("".join(f"skip /downloads/F{i:03d}\n" for i in range(100)))
            self.assertEqual(len(_read_log_tail(log)), 100)
            tail = _read_log_tail(log, max_bytes=60)
            self.assertTrue(all(line.startswith("skip /downloads/F") for line in tail), tail)
            self.assertEqual(tail[-1], "skip /downloads/F099")
            self.assertLess(len(tail), 4)
            self.assertEqual(_read_log_tail(Path(tmp) / "missing.log"), [])


if __name__ == "__main__":
    unittest.main()
