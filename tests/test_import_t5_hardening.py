"""0.7.0 T5: Import backend hardening (F10, F13, F16, F17, import-log tail, QA #391 N1)."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import app as APP
import backend.import_review_service as irs
import backend.import_service as isvc
import backend.pending_review_store as prs
import backend.serializers as ser
import routes_import
from job_engine import JobStore


class _Base(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.downloads = self.root / "downloads"
        self.music = self.root / "music"
        self.album = self.downloads / "Artist - Album"
        self.album.mkdir(parents=True)
        self.music.mkdir()
        (self.album / "01.flac").write_bytes(b"x")
        self.client = APP.app.test_client()

    def patch(self, target, name, value):
        p = mock.patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)


class RevalidateRunsAsJobTests(_Base):
    """F10: revalidate-all is one typed, deduplicated job, never request-thread work."""

    def setUp(self):
        super().setUp()
        self.store = JobStore()
        self.addCleanup(self.store.close, 2)
        self.patch(routes_import, "jobs", self.store)
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.calls = []

        def fake_run(review_ids, **kw):
            self.calls.append((set(review_ids), kw.get("log") is not None))
            if kw.get("log") is not None:
                self.release.wait(10)
            return {"ok": True, "reviewed_count": len(review_ids)}
        self.patch(routes_import, "_run_import_review_revalidation", fake_run)

    def test_all_starts_a_typed_deduplicated_job_and_a_second_start_is_409(self):
        res = self.client.post("/api/import-review/revalidate", json={"all": True, "auto_enqueue": True})
        self.assertEqual(res.status_code, 202, res.get_json())
        body = res.get_json()
        self.assertEqual(body["mode"], "job")
        job = self.store.get(body["job_id"])
        self.assertEqual(job.metadata["type"], "import-review-revalidate")
        self.assertEqual(job.metadata["dedupe_key"], "import-review-revalidate")
        self.assertIs(job.metadata["mutating"], True)

        again = self.client.post("/api/import-review/revalidate", json={"all": True})
        self.assertEqual(again.status_code, 409, again.get_json())
        self.assertEqual(again.get_json()["code"], "job_already_running")
        self.assertEqual(again.get_json()["job_id"], body["job_id"])

    def test_more_than_the_sync_cap_of_explicit_ids_also_runs_as_a_job(self):
        ids = [f"pending:/d/{i}" for i in range(irs.IMPORT_REVIEW_REVALIDATE_SYNC_MAX + 1)]
        res = self.client.post("/api/import-review/revalidate", json={"review_item_ids": ids})
        self.assertEqual(res.status_code, 202, res.get_json())

    def test_a_small_explicit_id_list_stays_synchronous(self):
        res = self.client.post("/api/import-review/revalidate", json={"review_item_ids": ["pending:/d/a"]})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["reviewed_count"], 1)
        self.assertEqual(self.calls, [({"pending:/d/a"}, False)])
        self.assertEqual(self.store.all(), [])

    def test_auto_enqueue_ready_job_is_typed_and_deduplicated(self):
        self.patch(routes_import, "_run_import_review_auto_enqueue_ready_batch",
                   lambda limit, log=None, cancel_event=None: self.release.wait(10))
        res = self.client.post("/api/import-review/auto-enqueue-ready/job", json={"limit": 3})
        self.assertEqual(res.status_code, 200, res.get_json())
        job = self.store.get(res.get_json()["job_id"])
        self.assertEqual(job.metadata["type"], "import-review-auto-enqueue-ready")
        self.assertEqual(job.metadata["dedupe_key"], "import-review-auto-enqueue-ready")
        again = self.client.post("/api/import-review/auto-enqueue-ready/job", json={"limit": 4})
        self.assertEqual(again.status_code, 409, again.get_json())


class RevalidateFixedErrorTextTests(_Base):
    """F17: a queueing failure answers fixed text, never the exception string."""

    def test_queue_exception_text_is_not_returned(self):
        path = str(self.album)
        rg = "11111111-1111-1111-1111-111111111111"
        item = {"path": path, "suggestion": {"mb_releasegroupid": rg}}
        with mock.patch.object(irs, "_load_pending_reviews", return_value=[item]), \
             mock.patch.object(irs, "_candidate_track_comparison_payload", return_value={"ok": True}), \
             mock.patch.object(irs, "_import_review_revalidation_preflight", return_value={}), \
             mock.patch.object(irs, "_import_review_build_revalidated_match", return_value={}), \
             mock.patch.object(irs, "_update_pending_review_revalidation", return_value=False), \
             mock.patch.object(irs, "evaluate_import_eligibility", return_value={"eligible": True}), \
             mock.patch.object(irs, "_import_review_start_auto_import",
                               side_effect=RuntimeError("SECRET-DETAIL /internal/path")):
            out = irs._run_import_review_revalidation({f"pending:{path}"}, auto_enqueue=True)
        self.assertEqual(out["failed_count"], 1)
        self.assertEqual(out["items"][0]["error"], "Could not queue this import.")
        self.assertNotIn("SECRET-DETAIL", json.dumps(out))


class LimitParsingTests(_Base):
    """F13: a non-integer limit is a 400 INVALID_PARAM, never a 500."""

    def test_bad_limits_are_400(self):
        self.patch(routes_import, "LOG_FILE", str(self.root / "beet.log"))
        for method, url, kw in (
            ("get", "/api/import-log?limit=abc", {}),
            ("get", "/api/import-skipped?limit=abc", {}),
            ("get", "/api/ai-match-history?limit=abc", {}),
            ("get", "/api/ai-review-decisions?limit=abc", {}),
            ("get", "/api/unmatched-tracks?limit=abc", {}),
            ("post", "/api/import-review/revalidate", {"json": {"all": True, "limit": "abc"}}),
            ("post", "/api/import-review/auto-enqueue-ready", {"json": {"limit": "abc"}}),
            ("post", "/api/import-review/auto-enqueue-ready/job", {"json": {"limit": "abc"}}),
        ):
            with self.subTest(url=url):
                res = getattr(self.client, method)(url, **kw)
                self.assertEqual(res.status_code, 400, res.get_data(as_text=True)[:200])
                self.assertEqual(res.get_json()["error_code"], "INVALID_PARAM")


class ImportLogTailTests(_Base):
    def test_import_log_reads_only_the_tail(self):
        log = self.root / "beet.log"
        filler = ("x" * 99 + "\n") * (9 * 1024 * 1024 // 100)
        log.write_text("OLDEST-LINE\n" + filler + "NEWEST-LINE\n")
        self.patch(routes_import, "LOG_FILE", str(log))
        body = self.client.get("/api/import-log?limit=1000").get_json()
        self.assertEqual(body["lines"][-1], "NEWEST-LINE")
        self.assertLess(body["total"], filler.count("\n"))  # not the whole file

    def test_missing_log_and_zero_limit(self):
        self.patch(routes_import, "LOG_FILE", str(self.root / "none.log"))
        self.assertEqual(self.client.get("/api/import-log").get_json(), {"ok": True, "lines": [], "total": 0})
        log = self.root / "beet.log"
        log.write_text("a\nb\n")
        self.patch(routes_import, "LOG_FILE", str(log))
        self.assertEqual(self.client.get("/api/import-log?limit=0").get_json()["lines"], [])


class AddPendingReviewTrustTests(_Base):
    """F16: path must be under an Import Review root; trust fields are dropped."""

    def setUp(self):
        super().setUp()
        self.pending_file = self.root / "ai_pending_review.json"
        self.pending_file.write_text("[]")
        self.patch(prs, "_AI_PENDING_FILE", self.pending_file)
        self.patch(prs, "_load_pending_reviews", lambda **_: json.loads(self.pending_file.read_text()))
        self.patch(prs, "_library_album_ids_for_folder", lambda *_: [])
        self.patch(prs, "_record_ai_review_decision", lambda *a, **k: None)
        downloads, music = self.downloads, self.music
        self.patch(ser, "_import_review_cleanup_roots",
                   lambda *, allow_music=False: [downloads] + ([music] if allow_music else []))

    def _items(self):
        return json.loads(self.pending_file.read_text())

    def test_paths_outside_the_roots_or_in_the_library_are_refused(self):
        (self.music / "A").mkdir()
        for path in (str(self.root / "elsewhere"), str(self.music / "A"), str(self.downloads), "relative/x"):
            with self.subTest(path=path):
                res = self.client.post("/api/ai-pending-review", json={"path": path})
                self.assertEqual(res.status_code, 400, res.get_json())
        self.assertEqual(self._items(), [])

    def test_client_trust_fields_are_dropped(self):
        res = self.client.post("/api/ai-pending-review", json={
            "path": str(self.album),
            "suggestion": {"album": "Album", "albumartist": "Artist", "mb_valid": True,
                           "confidence": "high", "confidence_score": 0.99, "is_importable": True,
                           "preflight": {"ok": True}, "track_mapping": [{"x": 1}],
                           "review_evidence": {"preflight": {"ok": True}}, "status": "ready_to_import"},
            "evidence": {"preflight": {"ok": True}},
            "status": "ready_to_import",
        })
        self.assertEqual(res.status_code, 200, res.get_json())
        [item] = self._items()
        self.assertEqual(item["path"], str(self.album))
        self.assertNotIn("evidence", item)
        self.assertNotEqual(item.get("status"), "ready_to_import")
        suggestion = item["suggestion"]
        self.assertEqual((suggestion["album"], suggestion["albumartist"]), ("Album", "Artist"))
        for field in ("mb_valid", "confidence", "confidence_score", "is_importable",
                      "preflight", "track_mapping", "review_evidence", "status"):
            self.assertNotIn(field, suggestion)
        self.assertFalse(irs._pending_item_is_ready_for_backend_auto_import(item))


    def test_client_origin_cannot_override_the_folder_and_is_bounded(self):
        res = self.client.post("/api/ai-pending-review", json={
            "path": str(self.album),
            "source_folder": "/etc", "origin": {"source_folder": "/etc"},
            "origin_label": "L" * 5000, "source_playlist_name": {"nested": "dict"},
            "suggestion": {"album": "A" * 5000, "mb_url": "https://evil.example/x"},
        })
        self.assertEqual(res.status_code, 200, res.get_json())
        [item] = self._items()
        self.assertEqual(item["source_folder"], str(self.album))
        self.assertEqual(item["suggestion"]["source_folder"], str(self.album))
        self.assertLessEqual(len(item["origin_label"]), 500)
        self.assertIsInstance(item["source_playlist_name"], str)
        self.assertEqual(len(item["suggestion"]["album"]), 500)
        self.assertNotIn("mb_url", item["suggestion"])


class StoredPendingPathRevalidationTests(_Base):
    """Security LOW-4: an entry stored before paths were validated authorizes
    nothing unless its path still resolves under an approved root."""

    def setUp(self):
        super().setUp()
        self.pending_file = self.root / "ai_pending_review.json"
        self.patch(prs, "_AI_PENDING_FILE", self.pending_file)
        downloads, music = self.downloads, self.music
        self.patch(ser, "_import_review_cleanup_roots",
                   lambda *, allow_music=False: [downloads] + ([music] if allow_music else []))
        self.outside = self.root / "elsewhere" / "Album"
        self.outside.mkdir(parents=True)

    def _store(self, *paths):
        self.pending_file.write_text(json.dumps([
            {"path": str(p), "suggestion": {"mb_valid": True}, "status": "ready_to_import"} for p in paths]))

    def test_legacy_outside_entry_does_not_authorize_a_delete(self):
        self._store(self.outside, self.album)
        self.assertFalse(prs._pending_review_matches(str(self.outside)))
        self.assertTrue(prs._pending_review_matches(str(self.album)))

    def test_legacy_outside_entry_is_not_auto_enqueued(self):
        self._store(self.outside)
        with mock.patch.object(irs, "_load_pending_reviews", return_value=json.loads(self.pending_file.read_text())), \
             mock.patch.object(irs, "evaluate_import_eligibility", side_effect=AssertionError("must not evaluate")):
            out = irs._run_import_review_auto_enqueue_ready_batch(5)
        self.assertEqual((out["queued_count"], out["skipped_count"], out["failed_count"]), (0, 1, 0))
        self.assertEqual(out["items"][0]["reason"], irs._UNTRUSTED_STORED_REVIEW_PATH)

    def test_format_policy_rejection_response_never_echoes_exception_text(self):
        # CodeQL #1423: the handled-rejection row returns fixed text, not the
        # exception message (which only the stored review note keeps).
        self._store(self.album)
        leaked = "Downloaded audio does not match Music Format Preferences. Traceback /secret/path"
        with mock.patch.object(irs, "_load_pending_reviews", return_value=json.loads(self.pending_file.read_text())), \
             mock.patch.object(irs, "_pending_item_is_ready_for_backend_auto_import", return_value=True), \
             mock.patch.object(irs, "_import_review_payload_from_pending_item", return_value={}), \
             mock.patch.object(irs, "evaluate_import_eligibility", return_value={"eligible": True}), \
             mock.patch.object(irs, "_import_review_start_auto_import", side_effect=RuntimeError(leaked)), \
             mock.patch.object(irs, "_finalize_pending_review_format_policy_rejection",
                               return_value={"status": "needs_review", "note": leaked}):
            out = irs._run_import_review_auto_enqueue_ready_batch(5)
        row = out["items"][0]
        self.assertTrue(row["handled"])
        from backend.ai_batch_state_service import _MUSIC_FORMAT_POLICY_REVIEW_NOTE
        self.assertEqual(row["note"], _MUSIC_FORMAT_POLICY_REVIEW_NOTE)
        self.assertNotIn("secret", json.dumps(out))

    def test_symlink_from_downloads_into_music_never_matches(self):
        # Security LOW-5: the music library is an approved root, but a stored
        # downloads path may not reach it through a symlink.
        self.patch(prs, "MUSIC_ROOT", self.music)
        (self.music / "Artist" / "Album").mkdir(parents=True)
        link = self.downloads / "into-music"
        try:
            os.symlink(self.music / "Artist", link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self._store(link / "Album")
        self.assertFalse(prs._pending_review_matches(str(link / "Album")))
        in_music = self.music / "Artist" / "Album"
        self._store(in_music)
        self.assertTrue(prs._pending_review_matches(str(in_music)))


class RevalidateItemIsolationTests(_Base):
    """Security LOW-3: one failing item never aborts the run or leaks text."""

    def test_a_raising_item_fails_alone_with_fixed_text(self):
        rg = "11111111-1111-1111-1111-111111111111"
        bad, good = str(self.downloads / "bad"), str(self.album)
        items = [{"path": bad, "suggestion": {"mb_releasegroupid": rg}},
                 {"path": good, "suggestion": {"mb_releasegroupid": rg, "existing_album_ids": ["not-an-int"]}}]

        def comparison(rep, path, release_group_id=""):
            if path == bad:
                raise RuntimeError("SECRET-DETAIL /internal/path")
            return {"ok": True}
        with mock.patch.object(irs, "_load_pending_reviews", return_value=items), \
             mock.patch.object(irs, "_candidate_track_comparison_payload", side_effect=comparison), \
             mock.patch.object(irs, "_import_review_revalidation_preflight", return_value={}), \
             mock.patch.object(irs, "_import_review_build_revalidated_match", return_value={}), \
             mock.patch.object(irs, "_update_pending_review_revalidation", return_value=True), \
             mock.patch.object(irs, "evaluate_import_eligibility", return_value={"eligible": False}):
            log = []
            out = irs._run_import_review_revalidation(set(), log=log)
        self.assertEqual((out["reviewed_count"], out["updated_count"], out["failed_count"]), (2, 1, 1))
        self.assertEqual(out["items"][0]["error"], "Could not revalidate this item.")
        self.assertTrue(out["items"][1]["ok"])
        self.assertNotIn("SECRET-DETAIL", json.dumps(out) + " ".join(log))


class ImportedFolderLeavesSkipLedgerTests(_Base):
    """QA #391 N1: a folder that imports drops out of the recorded skip ledger,
    with no Beets read."""

    def test_folder_done_hook_forgets_the_skip(self):
        self.patch(prs, "_IMPORT_SKIPPED_FILE", self.root / "import_skipped.json")
        self.patch(prs, "_AI_PENDING_FILE", self.root / "no_pending.json")
        self.patch(isvc, "LOG_FILE", str(self.root / "beet.log"))
        other = self.downloads / "Other"
        other.mkdir()
        (other / "01.flac").write_bytes(b"x")
        prs._record_import_skips(str(self.downloads), [
            {"path": str(self.album), "reason": "no_strong_match"},
            {"path": str(self.album / "CD2"), "reason": "no_strong_match"},
            {"path": str(other), "reason": "duplicate"},
        ])
        with mock.patch.object(prs.composite_workflows, "get_album", side_effect=AssertionError("Beets read")):
            prs._remove_pending_review_for_path(str(self.album), [])
        self.assertEqual([r["path"] for r in prs._load_recorded_import_skips()], [str(other)])
        self.assertEqual([r["path"] for r in isvc._import_skipped_items()], [str(other)])



class SkipLedgerNeverRaisesTests(_Base):
    """QA N1: _forget_import_skip runs unwrapped inside import paths."""

    def test_non_string_rows_and_unexpected_errors_are_swallowed(self):
        ledger = self.root / "import_skipped.json"
        self.patch(prs, "_IMPORT_SKIPPED_FILE", ledger)
        ledger.write_text(json.dumps([{"path": 5}, {"path": ["x"]}, {"path": str(self.album)}, "junk"]))
        self.assertTrue(prs._forget_import_skip(str(self.album)))
        self.assertEqual(prs._load_recorded_import_skips(), [])
        with mock.patch.object(prs, "_write_recorded_import_skips", side_effect=RuntimeError("boom")):
            ledger.write_text(json.dumps([{"path": str(self.album)}]))
            self.assertFalse(prs._forget_import_skip(str(self.album)))


class RevalidateJobResultIsCompactTests(unittest.TestCase):
    """QA N3: the persisted job result keeps counts and small capped rows."""

    def test_rows_are_capped_and_stripped(self):
        items = [{"ok": True, "review_item_id": f"pending:/d/{i}", "path": f"/d/{i}",
                  "selected_match": {"track_mapping": [1] * 50}, "eligibility": {"eligible": i % 2 == 0},
                  "queued": i == 0} for i in range(250)]
        items.append({"ok": False, "review_item_id": "pending:/d/x", "path": "/d/x", "error": "E"})
        out = irs._compact_revalidation_summary({"ok": True, "reviewed_count": 251, "failed_count": 1, "items": items})
        self.assertEqual((out["reviewed_count"], out["failed_count"]), (251, 1))
        self.assertEqual(len(out["items"]), irs.IMPORT_REVIEW_REVALIDATE_JOB_ITEMS_MAX)
        self.assertTrue(out["items_truncated"])
        self.assertEqual(set(out["items"][0]), {"review_item_id", "path", "status", "error"})
        self.assertEqual([r["status"] for r in out["items"][:3]], ["queued", "revalidated", "eligible"])
        self.assertNotIn("track_mapping", json.dumps(out))
        small = irs._compact_revalidation_summary({"items": items[-1:]})
        self.assertEqual(small["items"][0]["status"], "failed")
        self.assertFalse(small["items_truncated"])


class ReviewRowSearchTextTests(unittest.TestCase):
    """#395 QA finding 1: server-side search matches what the client search did."""

    def test_each_restored_field_is_searchable(self):
        cases = {
            "year": ({"year": 1997}, "1997"),
            "confidence": ({"confidence": "medium"}, "medium"),
            "blocked_reason": ({"blocked_reason": "Tracklist preflight failed"}, "preflight failed"),
            "blocked_next_action": ({"blocked_next_action": "Pick another release"}, "another release"),
            "guessed_artist": ({"evidence": {"folder": {"guessed_artist": "Boards of Canada"}}}, "boards of canada"),
            "guessed_album": ({"evidence": {"folder": {"guessed_album": "Geogaddi"}}}, "geogaddi"),
            "guessed_year": ({"evidence": {"folder": {"guessed_year": "2002"}}}, "2002"),
            "audio_mismatch": ({"evidence": {"preflight": {"acoustid_mismatch": True, "acoustid_target_hits": 1,
                                                           "acoustid_top_hits": 9, "acoustid_top_release": "Other LP"}}},
                               "fingerprint mismatch"),
        }
        for name, (row, needle) in cases.items():
            with self.subTest(field=name):
                self.assertIn(needle, routes_import._review_row_search_text(row))
        self.assertIn("other lp", routes_import._review_row_search_text(cases["audio_mismatch"][0]))
        self.assertNotIn("mismatch", routes_import._review_row_search_text({"evidence": {"preflight": {}}}))

if __name__ == "__main__":
    unittest.main()
