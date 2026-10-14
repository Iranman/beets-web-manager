"""QA edge tests for the Review Queue split (PR #391, audit F5/F6/F18).

Complements tests/test_library_missing_mb_ids.py: runs the real composite
(not a mocked get_unmatched_review_items) against a counting fake adapter,
and probes paging edges, dedupe normalisation, the log tail boundary, and
that the new rows still satisfy the existing action gates.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.beets_adapter import BeetsUnavailableError


class _CountingAdapter:
    """Fake Beets adapter: records every method call by name."""

    def __init__(self, items, albums):
        self.calls = []
        self._items, self._albums = items, albums

    def __getattr__(self, name):
        def _call(*a, **kw):
            self.calls.append(name)
            if name == "get_items":
                return [dict(i) for i in self._items]
            if name == "get_albums":
                return [dict(a) for a in self._albums]
            raise AssertionError(f"unexpected adapter call {name}")
        return _call


class _ClientCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as app_module  # noqa: F401
        cls.client = app_module.app.test_client()

    def setUp(self):
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        env.start()
        self.addCleanup(env.stop)

    def patch(self, target, **kw):
        p = mock.patch(target, **kw)
        self.addCleanup(p.stop)
        return p.start()


class MissingMbIdsRealCompositeTests(_ClientCase):
    def setUp(self):
        super().setUp()
        # 3 albums without mb_albumid (one multi-disc whose disc-2 track 1 has
        # the lowest id), 1 matched album, 620 singletons without mb_trackid.
        items = [
            {"id": 1, "album_id": 1, "disc": 2, "track": 1, "path": "Art/Multi/Disc 2/01.flac"},
            {"id": 2, "album_id": 1, "disc": 1, "track": 1, "path": "Art/Multi/Disc 1/01.flac"},
            {"id": 3, "album_id": 2, "track": 1, "path": "Art/Plain/01.flac"},
            {"id": 4, "album_id": 3, "track": 1, "path": "Art/Old/01.flac"},
            {"id": 5, "album_id": 9, "track": 1, "path": "Art/Matched/01.flac"},
            {"id": 6, "album_id": None, "mb_trackid": "x", "path": "Loose/has-id.mp3", "added": 1},
        ]
        items += [{"id": 100 + i, "album_id": None, "mb_trackid": "", "title": f"S{i}",
                   "path": f"Loose/s{i}.mp3", "added": 1000 + i} for i in range(620)]
        albums = [
            {"id": 1, "album": "Multi", "albumartist": "Art", "added": 5000},
            {"id": 2, "album": "Plain", "albumartist": "Art", "added": 1500.5},
            {"id": 3, "album": "Old", "albumartist": "Art", "added": 10},
            {"id": 9, "album": "Matched", "albumartist": "Art", "mb_albumid": "abc",
             "mb_releasegroupid": "def", "added": 9999},  # fully identified (QA-N1)
        ]
        self.adapter = _CountingAdapter(items, albums)
        self.patch("backend.composite_workflows.beets_adapter", new=self.adapter)
        self.patch("backend.library_service._load_pending_reviews", return_value=[])
        self.patch("backend.library_service._load_album_mb_suggestions", return_value={})

    def get(self, qs=""):
        res = self.client.get("/api/library/missing-mb-ids" + qs)
        return res.status_code, res.get_json()

    def test_one_items_and_one_albums_read_per_request(self):
        status, body = self.get("?limit=500")
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(self.adapter.calls), ["get_albums", "get_items"])

    def test_pages_cover_every_row_once_newest_first_across_kinds(self):
        seen, ts = [], []
        offset = 0
        while True:
            _, body = self.get(f"?limit=500&offset={offset}")
            self.assertEqual(body["total"], 623)
            self.assertEqual(body["counts"], {"all": 623, "album": 3, "item": 620})
            if not body["items"]:
                break
            seen += [r["id"] for r in body["items"]]
            ts += [r["sort_ts"] for r in body["items"]]
            offset += 500
        self.assertEqual(len(seen), 623)
        self.assertEqual(len(set(seen)), 623)
        self.assertEqual(ts, sorted(ts, reverse=True))
        self.assertEqual(seen[0], "album:1")          # 5000: newest overall
        self.assertNotIn("album:9", seen)              # has mb_albumid
        self.assertNotIn("item:6", seen)               # has mb_trackid
        # album:2 (1500.5) sits between singletons 1500 and 1501.
        self.assertEqual(seen.index("album:2"), seen.index("item:601") + 1)
        self.assertEqual(seen.index("item:600"), seen.index("album:2") + 1)

    def test_offset_past_end_keeps_total(self):
        _, body = self.get("?offset=10000&limit=10")
        self.assertEqual((body["items"], body["total"]), ([], 623))

    def test_multi_disc_album_folder_is_the_album_folder(self):
        from backend.app_runtime import MUSIC_ROOT
        _, body = self.get("?kind=album&limit=10")
        rows = {r["id"]: r for r in body["items"]}
        self.assertEqual(rows["album:1"]["path"], str(MUSIC_ROOT / "Art" / "Multi"))
        self.assertEqual(rows["album:1"]["tracks"], 2)

    def test_beets_down_is_503(self):
        self.adapter.get_items = mock.Mock(side_effect=BeetsUnavailableError("down"))
        status, body = self.get()
        self.assertEqual((status, body["error_code"]), (503, "ENGINE_UNAVAILABLE"))

    def test_new_album_row_passes_the_folder_delete_gate(self):
        """The delete gate (_library_no_mb_album_matches_folder) must accept
        the path this endpoint returns for the same album."""
        from backend import library_service
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("Art/Multi/Disc 1/01.flac", "Art/Multi/Disc 2/01.flac"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_bytes(b"x")
            self.patch("backend.library_service.MUSIC_ROOT", new=root)
            self.patch("backend.pending_review_store.MUSIC_ROOT", new=root)
            _, body = self.get("?kind=album&limit=10")
            row = next(r for r in body["items"] if r["id"] == "album:1")
            self.assertEqual(row["path"], str(root / "Art" / "Multi"))
            self.patch("backend.composite_workflows.get_album",
                       return_value={"id": 1, "mb_albumid": ""})
            self.patch("backend.composite_workflows.find_all_items_by_album_id",
                       return_value=[{"id": 1, "path": "Art/Multi/Disc 2/01.flac"},
                                     {"id": 2, "path": "Art/Multi/Disc 1/01.flac"}])
            self.assertTrue(library_service._library_no_mb_album_matches_folder(
                row["album_id"], str(Path(row["path"]).resolve())))

    def test_decision_endpoint_accepts_new_rows(self):
        _, body = self.get("?limit=500")
        album = next(r for r in body["items"] if r["target_kind"] == "album")
        item = next(r for r in body["items"] if r["target_kind"] == "item")
        res = self.client.post("/api/import-review/decision",
                               json={"entries": [{"item": album}, {"item": item}]})
        self.assertEqual(res.status_code, 200, res.get_json())
        labels = [d.get("action_label") for d in res.get_json()["decisions"]]
        self.assertEqual(labels, ["Match album", "Attach recording ID"])


class ReviewQueueEdgeTests(_ClientCase):
    def setUp(self):
        super().setUp()
        self.adapter = _CountingAdapter([], [])
        self.patch("backend.composite_workflows.beets_adapter", new=self.adapter)
        self.pending = [
            {"path": "/downloads/A/", "folder_name": "A", "added_at": 10, "status": "Pending AI",
             "suggestion": {"album": "A"}},
            {"path": "/downloads//B", "folder_name": "B", "added_at": 20, "status": "Pending AI",
             "suggestion": {"album": "B"}},
        ]
        self.skipped = [{"path": p, "filename": Path(p).name, "folder": "/downloads", "reason": ""}
                        for p in ("/downloads/A", "/downloads/B/", "/downloads/C", "/downloads/D")]
        self.patch("routes_import._load_pending_reviews", side_effect=lambda **kw: list(self.pending))
        self.patch("routes_import._import_skipped_items", side_effect=lambda *a, **kw: list(self.skipped))

    def get(self, qs=""):
        res = self.client.get("/api/import/review-queue" + qs)
        return res.status_code, res.get_json()

    def test_no_adapter_calls(self):
        for qs in ("", "?status=skipped", "?status=library_no_mb", "?status=pending_ai"):
            status, _ = self.get(qs)
            self.assertEqual(status, 200)
        self.assertEqual(self.adapter.calls, [])

    def test_dedupe_normalises_trailing_and_double_slashes(self):
        _, body = self.get()
        self.assertEqual([r["path"] for r in body["items"] if r["type"] == "skipped"],
                         ["/downloads/C", "/downloads/D"])
        self.assertEqual(body["counts"], {"all": 4, "pending_ai": 2, "skipped": 2})

    def test_order_paging_and_clamp(self):
        _, body = self.get("?limit=99999")
        self.assertEqual(body["limit"], 5000)
        self.assertEqual([r["type"] for r in body["items"]], ["pending_ai", "pending_ai", "skipped", "skipped"])
        self.assertEqual(body["items"][0]["path"], "/downloads//B")  # newest pending first
        _, body = self.get("?offset=50")
        self.assertEqual((body["items"], body["total"]), ([], 4))

    def test_counts_follow_search_filter(self):
        _, body = self.get("?search=downloads/c")
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["counts"], {"all": 1, "pending_ai": 0, "skipped": 1})

    def test_moved_stub_keeps_paging_fields(self):
        _, body = self.get("?status=library_no_mb&limit=7&offset=3")
        self.assertEqual((body["ok"], body["limit"], body["offset"]), (True, 7, 3))
        self.assertEqual(body["moved_to"], "/api/library/missing-mb-ids")


class LogTailBoundaryTests(unittest.TestCase):
    def test_over_8mb_drops_only_the_cut_first_line(self):
        from backend.import_service import _IMPORT_LOG_TAIL_BYTES, _read_log_tail
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "beet.log"
            line = "skip /downloads/" + "é" * 40 + "\n"  # multibyte chars at the cut
            n = _IMPORT_LOG_TAIL_BYTES // len(line.encode()) + 50
            with open(log, "w", encoding="utf-8") as fh:
                fh.write("OLDEST\n")
                for i in range(n):
                    fh.write(f"{i:08d} {line}")
            tail = _read_log_tail(log)
            self.assertNotIn("OLDEST", tail)
            self.assertTrue(all("�" not in t for t in tail))  # cut char went with the cut line
            self.assertTrue(tail[-1].startswith(f"{n - 1:08d} skip"))
            # Every kept line is whole and consecutive.
            nums = [int(t[:8]) for t in tail]
            self.assertEqual(nums, list(range(nums[0], n)))

    def test_exactly_max_bytes_keeps_the_first_line(self):
        from backend.import_service import _read_log_tail
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "beet.log"
            log.write_bytes(b"first\nsecond\n")
            self.assertEqual(_read_log_tail(log, max_bytes=13), ["first", "second"])
            self.assertEqual(_read_log_tail(log, max_bytes=12), ["second"])


if __name__ == "__main__":
    unittest.main()
