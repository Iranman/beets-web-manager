"""AI Suggest in Import Review: the Review queue keeps only the identity a
client proposes, never its evidence (F16); AI match history records only a
suggestion that names what Beets imported; a stored suggestion whose folder
changed never drives an unattended import; and Find Match cannot hold every
server worker."""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import test_import_paths_beets_native as base
except ImportError:  # pragma: no cover
    from tests import test_import_paths_beets_native as base

import app as app_module
import backend.ai_service as ai_service
import backend.import_review_service as irs
import backend.pending_review_store as prs
import routes_import
import routes_library

REL, RG = base.REL, base.RG
RG_OTHER = "33333333-3333-3333-3333-333333333333"
REL_OTHER = "44444444-4444-4444-4444-444444444444"
SRC = "/downloads/batch/Plain LP"
_CSRF = {"Origin": "http://localhost", "X-Beets-CSRF": "1"}

# Everything the server computes about a match; a client may send none of it.
EVIDENCE_KEYS = {"mb_valid", "confidence", "confidence_score", "preflight", "track_mapping",
                 "identity_validated", "action_eligibility", "required_review", "review_evidence",
                 "folder_signature", "matching_contract", "status"}

FORGED = {
    "mb_albumid": REL, "mb_releasegroupid": RG, "album": "Plain LP", "albumartist": "Bwm Synthetic",
    "reason": "proposed by the client",
    "mb_valid": True, "confidence": "high", "confidence_score": 0.99,
    "preflight": {"ok": True, "matches": 12, "expected": 12},
    "track_mapping": [{"status": "verified_match", "source_path": "/etc/passwd"}],
    "identity_validated": True, "action_eligibility": "safe", "required_review": False,
    "review_evidence": {"preflight": {"ok": True}}, "folder_signature": "deadbeefdeadbeef",
    "matching_contract": {"action_allowed": True}, "status": "ready_to_import",
    "origin_type": "playlist", "origin_label": "Playlist: Mine", "source_playlist_name": "Mine",
    "source_folder": "/elsewhere/not/the/folder",
}


class ImportWithIdKeepsOnlyTheProposedIdentityTests(unittest.TestCase):
    RETAG_AND_REMOVAL = base.ConfirmedImportJobTests.RETAG_AND_REMOVAL
    setUp = base.ConfirmedImportJobTests.setUp

    def _refused(self, ai_suggestion):
        """The selected Release Group does not hold the Release, so the import
        is refused and the folder goes back to the Review queue."""
        isvc = self.isvc
        self.ad = base._adapter(None)
        isvc._queue_folder_for_manual_review.reset_mock()
        with mock.patch.object(isvc, "_resolve_import_review_source_path", return_value=(SRC, None)), \
                mock.patch.object(isvc, "_preserve_torrent_source_path", return_value=False), \
                mock.patch.object(isvc, "_beet_import_timeout", return_value=60):
            body, code = isvc.start_folder_import_with_id({
                "path": SRC, "mb_albumid": REL, "mb_releasegroupid": RG_OTHER, "ai_suggestion": ai_suggestion})
        self.assertEqual(code, 200, body)
        self.assertEqual(self.result["status"], "failed", self.result)
        queue = isvc._queue_folder_for_manual_review
        self.assertEqual(queue.call_count, 1, self.result)
        self.ad.run_import.assert_not_called()
        return queue.call_args

    def test_client_evidence_never_reaches_the_review_queue(self):
        call = self._refused(FORGED)
        folder, sug, reason = call.args[:3]
        self.assertEqual(folder, SRC)
        self.assertEqual(sorted(EVIDENCE_KEYS & set(sug)), [])
        self.assertIsNone(call.kwargs.get("evidence"))
        self.assertIn(RG_OTHER, reason)
        # A proposed identity and display-only origin stay; the origin's folder
        # is always the validated one.
        self.assertEqual(sug["albumartist"], "Bwm Synthetic")
        self.assertEqual((sug["origin_type"], sug["source_playlist_name"], sug["source_folder"]),
                         ("playlist", "Mine", SRC))

    def test_a_suggestion_that_is_not_an_object_is_ignored_not_a_crash(self):
        for raw in (["not", "an", "object"], "text", 7):
            with self.subTest(raw=raw):
                _folder, sug, reason = self._refused(raw).args[:3]
                self.assertIn(RG_OTHER, reason)
                self.assertIn(RG_OTHER, self.result["error"])  # the real refusal, not a TypeError
                self.assertNotIn("origin_type", sug)

    def test_client_text_is_bounded(self):
        _folder, sug, _reason = self._refused(
            {**FORGED, "albumartist": "x" * 5000, "origin_label": "y" * 5000}).args[:3]
        self.assertEqual((len(sug["albumartist"]), len(sug["origin_label"])), (500, 500))


class AiMatchHistoryTests(unittest.TestCase):
    RETAG_AND_REMOVAL = base.ConfirmedImportJobTests.RETAG_AND_REMOVAL

    def setUp(self):
        base.ConfirmedImportJobTests.setUp(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.history = Path(tmp.name) / "ai_match_history.json"
        patcher = mock.patch.object(ai_service, "_AI_MATCH_HISTORY_FILE", self.history)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _import(self, ai_suggestion):
        """The operator imports Release REL of Release Group RG; Beets verifies it."""
        isvc = self.isvc
        self.ad = base._adapter({"id": 9, "mb_albumid": REL, "mb_releasegroupid": RG})
        src = "/downloads/Artist - Album"
        with mock.patch.object(isvc, "_resolve_import_review_source_path", return_value=(src, None)), \
                mock.patch.object(isvc, "_preserve_torrent_source_path", return_value=True), \
                mock.patch.object(isvc, "_beet_import_timeout", return_value=60):
            body, code = isvc.start_folder_import_with_id({
                "path": src, "mb_albumid": REL, "mb_releasegroupid": RG, "ai_suggestion": ai_suggestion})
        self.assertEqual(code, 200, body)
        self.assertEqual(self.result["status"], "completed", self.result)

    def _history(self):
        return json.loads(self.history.read_text()) if self.history.exists() else []

    def test_an_accepted_suggestion_is_recorded_with_the_imported_identity(self):
        self._import(FORGED)
        (entry,) = self._history()
        self.assertEqual((entry["mb_albumid"], entry["mb_releasegroupid"]), (REL, RG))
        self.assertEqual(entry["mb_url"], f"https://musicbrainz.org/release/{REL}")
        self.assertEqual((entry["artist"], entry["confidence"]), ("Bwm Synthetic", "high"))
        self.assertEqual(entry["evidence"], {})  # client evidence is not history
        self.assertEqual(list(self.history.parent.glob("*.tmp")), [])

    def test_an_operator_override_is_not_recorded_as_an_ai_match(self):
        self._import({**FORGED, "mb_albumid": REL_OTHER, "mb_releasegroupid": RG_OTHER})
        self.assertEqual(self._history(), [])

    def test_an_origin_without_a_suggestion_is_not_an_ai_match(self):
        self._import({"origin_type": "manual_import", "origin_label": "Manual", "source_folder": "/downloads/x"})
        self.assertEqual(self._history(), [])

    def test_a_release_only_suggestion_matches_on_the_release(self):
        self._import({"mb_albumid": REL, "album": "Plain LP", "confidence": "medium"})
        (entry,) = self._history()
        self.assertEqual((entry["mb_releasegroupid"], entry["confidence"]), (RG, "medium"))

    def test_a_confidence_outside_the_labels_is_not_recorded(self):
        self._import({**FORGED, "confidence": "<b>certain</b>"})
        self.assertEqual(self._history()[0]["confidence"], "")

    def test_an_unexpected_history_document_is_replaced_not_a_lost_entry(self):
        self.history.write_text(json.dumps({"unexpected": "object"}))
        self._import(FORGED)
        history = self._history()
        self.assertIsInstance(history, list)
        self.assertEqual([entry["mb_albumid"] for entry in history], [REL])


class StoredSuggestionStalenessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self._album("Album")
        self.pending = self.root / "pending.json"

        def resolve(raw, **_kw):
            path = Path(str(raw))
            inside = str(path).startswith(str(self.root)) and path.is_dir()
            return (path, None) if inside else (None, "outside")

        for target, name, value in ((prs, "_resolve_import_review_source_path", resolve),
                                    (prs, "_AI_PENDING_FILE", self.pending),
                                    (irs, "_AI_PENDING_FILE", self.pending)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _album(self, name):
        folder = self.root / "downloads" / "Artist" / name
        folder.mkdir(parents=True)
        for track in ("01 One.flac", "02 Two.flac"):
            (folder / track).write_bytes(b"audio " + track.encode())
        return folder

    @staticmethod
    def _signature(folder):
        return prs._import_review_folder_signature(str(folder))

    @staticmethod
    def _item(folder, signature):
        return {"path": str(folder), "status": "ready_to_import",
                "suggestion": {"mb_valid": True, "mb_releasegroupid": RG, "mb_albumid": REL,
                               "folder_signature": signature,
                               "track_mapping": [{"status": "matched", "source_path": str(folder / "01 One.flac")}]},
                "evidence": {"preflight": {"ok": True}}}

    def test_an_added_or_replaced_file_makes_the_stored_suggestion_stale(self):
        item = self._item(self.folder, self._signature(self.folder))
        self.assertFalse(irs._import_review_suggestion_is_stale(item))
        (self.folder / "02 Two.flac").write_bytes(b"a different, longer download")
        self.assertTrue(irs._import_review_suggestion_is_stale(item))
        item = self._item(self.folder, self._signature(self.folder))
        (self.folder / "03 Three.flac").write_bytes(b"late file")
        self.assertTrue(irs._import_review_suggestion_is_stale(item))

    def test_an_item_from_before_signatures_is_not_called_stale(self):
        self.assertFalse(irs._import_review_suggestion_is_stale(self._item(self.folder, "")))

    def test_auto_enqueue_ready_never_replays_a_stale_suggestion(self):
        fresh_folder = self._album("Unchanged")
        stale = self._item(self.folder, self._signature(self.folder))
        (self.folder / "03 Three.flac").write_bytes(b"late file")
        fresh = self._item(fresh_folder, self._signature(fresh_folder))
        log = []
        with mock.patch.object(irs, "_load_pending_reviews", return_value=[stale, fresh]), \
                mock.patch.object(irs, "_trusted_stored_review_path", side_effect=lambda p: p), \
                mock.patch.object(irs, "evaluate_import_eligibility",
                                  return_value={"eligible": True, "selected_track_count": 2}), \
                mock.patch.object(irs, "_import_review_start_auto_import",
                                  return_value={"queued": True, "job_id": "job-1"}) as start:
            out = irs._run_import_review_auto_enqueue_ready_batch(limit=5, log=log)
        self.assertEqual([c.args[0]["path"] for c in start.call_args_list], [str(fresh_folder)])
        self.assertEqual((out["queued_count"], out["skipped_count"]), (1, 1))
        skipped = [row for row in out["items"] if row["path"] == str(self.folder)]
        self.assertEqual(skipped, [{"path": str(self.folder), "queued": False,
                                    "reason": irs._STALE_STORED_SUGGESTION}])
        self.assertTrue(any(irs._STALE_STORED_SUGGESTION in line for line in log))

    def _revalidate(self, change_during_compare):
        self.pending.write_text(json.dumps([self._item(self.folder, "0123456789abcdef")]))

        def compare(*_args, **_kwargs):
            if change_during_compare:
                (self.folder / "03 Late.flac").write_bytes(b"arrived while comparing")
            return {"ok": True, "representative_release_id": REL}

        match = {"release_group_id": RG, "representative_release_id": REL, "track_mapping": [],
                 "is_importable": True, "preflight_reason": "Revalidated."}
        with mock.patch.object(irs, "_load_pending_reviews",
                               side_effect=lambda **_kw: json.loads(self.pending.read_text())), \
                mock.patch.object(irs, "_candidate_track_comparison_payload", side_effect=compare), \
                mock.patch.object(irs, "_run_ai_release_preflight", return_value=None), \
                mock.patch.object(irs, "_import_review_revalidation_preflight", return_value={"ok": True}), \
                mock.patch.object(irs, "_import_review_build_revalidated_match",
                                  side_effect=lambda *_a: dict(match)), \
                mock.patch.object(irs, "evaluate_import_eligibility", return_value={"eligible": False}):
            out = irs._run_import_review_revalidation({f"pending:{self.folder}"})
        self.assertEqual(out["updated_count"], 1, out)
        return json.loads(self.pending.read_text())[0]

    def test_revalidation_stores_the_signature_of_the_files_it_compared(self):
        stored = self._revalidate(change_during_compare=False)
        self.assertEqual(stored["suggestion"]["folder_signature"], self._signature(self.folder))
        self.assertFalse(irs._import_review_suggestion_is_stale(stored))

    def test_a_file_arriving_during_revalidation_leaves_the_match_stale(self):
        stored = self._revalidate(change_during_compare=True)
        self.assertTrue(stored["suggestion"]["folder_signature"])
        self.assertTrue(irs._import_review_suggestion_is_stale(stored))

    def test_find_match_signature_predates_its_evidence(self):
        before = self._signature(self.folder)

        def evidence(_path):
            (self.folder / "03 During.flac").write_bytes(b"arrived while fingerprinting")
            return {"audio_files": [], "folder_track_count": 0, "nested_audio_count": 0,
                    "guessed_artist": "", "guessed_album": "", "guessed_year": "",
                    "track_lines": [], "filenames": []}

        with mock.patch.object(ai_service, "_ai_api_key", return_value=""), \
                mock.patch.object(ai_service, "_build_folder_evidence", side_effect=evidence), \
                mock.patch.object(ai_service, "_resolve_import_review_source_path",
                                  return_value=(self.folder, None)), \
                mock.patch.object(ai_service, "_acoustid_multi_file", return_value={}), \
                mock.patch.object(ai_service, "_mb_release_search", return_value=[]), \
                mock.patch.object(ai_service, "_mb_release_search_by_folder_tracks", return_value=[]), \
                mock.patch.object(ai_service, "_discogs_release_fallback_candidate", return_value={}), \
                mock.patch.object(ai_service, "_ai_match_evidence_packet", return_value={}):
            out = ai_service._ai_suggest_folder_internal(str(self.folder))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["suggestion"]["folder_signature"], before)
        self.assertTrue(irs._import_review_suggestion_is_stale(
            {"path": str(self.folder), "suggestion": out["suggestion"]}))


class FindMatchGateTests(unittest.TestCase):
    def setUp(self):
        for target, name, value in ((ai_service, "_find_match_slots", threading.BoundedSemaphore(1)),
                                    (ai_service, "_FIND_MATCH_WAIT_SECONDS", 0.05)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def _post(url, body):
        return app_module.app.test_client().post(url, json=body, headers=_CSRF)

    def test_a_second_find_match_is_refused_at_once_while_one_runs(self):
        entered, release = threading.Event(), threading.Event()
        results = {}

        def slow(_path):
            entered.set()
            release.wait(10)
            return {"ok": True, "suggestion": {"reason": "done"}}

        with mock.patch.object(routes_import, "_ai_suggest_folder_internal", side_effect=slow) as suggest:
            worker = threading.Thread(target=lambda: results.update(
                first=self._post("/api/folders/ai-suggest", {"path": "/downloads/A"})))
            worker.start()
            try:
                self.assertTrue(entered.wait(10), "the first Find Match never started")
                started = time.monotonic()
                busy = self._post("/api/folders/ai-suggest", {"path": "/downloads/B"})
                elapsed = time.monotonic() - started
            finally:
                release.set()
                worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(busy.status_code, 503)
        self.assertEqual(busy.get_json(), ai_service.FIND_MATCH_BUSY)
        self.assertEqual(busy.headers.get("Retry-After"), "5")
        self.assertLess(elapsed, 5.0)
        self.assertEqual(suggest.call_count, 1)  # the refused request started nothing
        self.assertEqual(results["first"].status_code, 200)
        with mock.patch.object(routes_import, "_ai_suggest_folder_internal", return_value={"ok": True}):
            self.assertEqual(self._post("/api/folders/ai-suggest", {"path": "/downloads/C"}).status_code, 200)

    def test_album_item_and_folder_find_match_share_the_limit(self):
        self.assertTrue(ai_service._find_match_slots.acquire(blocking=False))
        try:
            with mock.patch.object(routes_library, "lib") as lib, \
                    mock.patch.object(routes_import, "_ai_suggest_folder_internal") as folder:
                for url in ("/api/albums/7/ai-suggest", "/api/items/7/ai-suggest", "/api/folders/ai-suggest"):
                    with self.subTest(url=url):
                        response = self._post(url, {"path": "/downloads/A"})
                        self.assertEqual(response.status_code, 503)
                        self.assertEqual(response.get_json()["code"], "suggest_busy")
            lib.get_album.assert_not_called()
            lib.get_item.assert_not_called()
            folder.assert_not_called()
        finally:
            ai_service._find_match_slots.release()

    def test_a_failing_find_match_frees_its_slot(self):
        with mock.patch.object(routes_import, "_ai_suggest_folder_internal", side_effect=RuntimeError("boom")):
            self.assertEqual(self._post("/api/folders/ai-suggest", {"path": "/downloads/A"}).status_code, 500)
        self.assertTrue(ai_service._find_match_slots.acquire(blocking=False))
        ai_service._find_match_slots.release()


class MalformedRequestTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def _post(url, body):
        return app_module.app.test_client().post(url, json=body, headers=_CSRF)

    def test_find_match_answers_a_malformed_body_instead_of_failing(self):
        with mock.patch.object(routes_import, "_ai_suggest_folder_internal",
                               return_value={"ok": False, "error": "path required"}) as suggest:
            for body in (["not", "an", "object"], {"path": 7}, {"path": None}):
                with self.subTest(body=body):
                    self.assertEqual(self._post("/api/folders/ai-suggest", body).status_code, 200)
        self.assertEqual([c.args[0] for c in suggest.call_args_list], ["", "7", ""])

    def test_import_with_id_answers_a_malformed_body_instead_of_failing(self):
        for body in (["not", "an", "object"], {"path": 7, "mb_albumid": REL}):
            with self.subTest(body=body):
                response = self._post("/api/folders/import-with-id", body)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
