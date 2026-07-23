"""Concurrency, rollback, persistence-verification, and secret-redaction
coverage for the playlist safe-suggestions migration -- the same class of
hardening tests/test_import_review_attach_integrity.py adds on top of
tests/test_import_review_attach_enforcement.py for PR #19's attach-
recording endpoint, applied here to:

  POST /api/playlists/<name>/apply-safe-suggestions
  POST /api/transactions/<id>/rollback  (playlist_track_restore operations)

Reuses the harness from tests/test_playlist_safe_suggestions.py rather
than re-deriving the isolated-temp-environment app import.
"""
import json
import threading
import time
import unittest
import unittest.mock as mock
import uuid

from tests.test_playlist_safe_suggestions import (
    APP,
    PlaylistSuggestionsRouteTestCase,
    _fake_lib_item,
)


def _wait_job(job_id, timeout=10):
    deadline = time.time() + timeout
    job = APP.jobs.get(job_id)
    while job is not None and job.status == "running" and time.time() < deadline:
        time.sleep(0.02)
        job = APP.jobs.get(job_id)
    return job


class ConcurrencyTests(PlaylistSuggestionsRouteTestCase):
    def _safe_row(self):
        self._set_library_items([_fake_lib_item()])
        self._seed_missing_tracks([{"artist": "M83", "title": "Midnight  City"}])
        _, body = self._get_suggestions()
        row = body["rows"][0]
        best = row["best"]
        return {
            "track_key": row["track_key"],
            "mb_trackid": best.get("mb_trackid") or "",
            "item_id": best.get("item_id"),
            "decision_version": best["decision_version"],
        }

    def test_same_playlist_second_apply_gets_409_while_first_is_running(self):
        row = self._safe_row()
        entered = threading.Event()
        release = threading.Event()
        original = APP._playlist_apply_manifest_replacements

        def slow_apply(*args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            return original(*args, **kwargs)

        results = {}

        def run_first():
            with mock.patch.object(APP, "_playlist_apply_manifest_replacements", slow_apply):
                results["first"] = self._apply_suggestions([row])

        t = threading.Thread(target=run_first)
        t.start()
        try:
            self.assertTrue(entered.wait(timeout=5), "first request never entered the critical section")
            status2, body2 = self._apply_suggestions([row])
        finally:
            release.set()
            t.join(timeout=5)

        self.assertEqual(status2, 409)
        self.assertEqual(body2["code"], "playlist_update_in_progress")
        self.assertEqual(results["first"][0], 200)
        self.assertEqual(len(results["first"][1]["applied"]), 1)

    def test_different_playlists_proceed_concurrently(self):
        row_a = self._safe_row()
        other_clean = APP._clean_playlist_name("Other Playlist For Concurrency")
        APP._playlist_write_manifest(other_clean, [{"artist": "Other Artist", "title": "Other  Song"}], source="test")
        self._set_library_items(self._lib_items + [_fake_lib_item(id=2, title="Other Song", artist="Other Artist")])
        APP._invalidate_lib_cache()

        both_entered = threading.Barrier(2, timeout=5)
        original = APP._playlist_apply_manifest_replacements

        def barrier_apply(*args, **kwargs):
            both_entered.wait()
            return original(*args, **kwargs)

        results = {}

        def apply_for(clean_name, key, out_key):
            resp = self.client.post(
                f"/api/playlists/{clean_name}/apply-safe-suggestions",
                data=json.dumps({"suggestions": [key]}),
                content_type="application/json",
            )
            results[out_key] = (resp.status_code, resp.get_json())

        # Recompute suggestions for the second playlist under its own name.
        resp = self.client.get(f"/api/playlists/{other_clean}/suggestions")
        other_row = resp.get_json()["rows"][0]
        other_best = other_row["best"]
        row_b = {
            "track_key": other_row["track_key"],
            "mb_trackid": other_best.get("mb_trackid") or "",
            "item_id": other_best.get("item_id"),
            "decision_version": other_best["decision_version"],
        }

        with mock.patch.object(APP, "_playlist_apply_manifest_replacements", barrier_apply):
            t1 = threading.Thread(target=apply_for, args=(self.clean_name, row_a, "a"))
            t2 = threading.Thread(target=apply_for, args=(other_clean, row_b, "b"))
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        self.assertEqual(results["a"][0], 200)
        self.assertEqual(results["b"][0], 200)
        self.assertEqual(len(results["a"][1]["applied"]), 1)
        self.assertEqual(len(results["b"][1]["applied"]), 1)

    def test_reservation_released_after_success(self):
        row = self._safe_row()
        status, _ = self._apply_suggestions([row])
        self.assertEqual(status, 200)
        self.assertNotIn(self.clean_name, APP._PLAYLIST_APPLY_RESERVED_NAMES)

    def test_reservation_released_after_stale_rejection(self):
        row = self._safe_row()
        row["decision_version"] = "drv2:stale"
        status, body = self._apply_suggestions([row])
        self.assertEqual(status, 409)
        self.assertNotIn(self.clean_name, APP._PLAYLIST_APPLY_RESERVED_NAMES)

    def test_reservation_released_after_unhandled_exception(self):
        row = self._safe_row()
        with mock.patch.object(
            APP, "_playlist_apply_manifest_replacements",
            side_effect=RuntimeError("simulated failure"),
        ):
            resp = self.client.post(
                f"/api/playlists/{self.clean_name}/apply-safe-suggestions",
                data=json.dumps({"suggestions": [row]}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn(self.clean_name, APP._PLAYLIST_APPLY_RESERVED_NAMES)


class PersistenceVerificationTests(PlaylistSuggestionsRouteTestCase):
    def test_manifest_write_that_does_not_verify_is_reported_failed(self):
        self._set_library_items([_fake_lib_item()])
        self._seed_missing_tracks([{"artist": "M83", "title": "Midnight  City"}])
        _, body = self._get_suggestions()
        row = body["rows"][0]
        best = row["best"]

        # Simulate a write that silently no-ops (e.g. a filesystem issue) --
        # the route re-reads the manifest afterward and must not claim
        # success for a row that didn't actually land.
        with mock.patch.object(APP, "_playlist_apply_manifest_replacements",
                              return_value={"ok": True, "resolved_count": 0}):
            status, apply_body = self._apply_suggestions([{
                "track_key": row["track_key"],
                "mb_trackid": best.get("mb_trackid") or "",
                "item_id": best.get("item_id"),
                "decision_version": best["decision_version"],
            }])
        self.assertEqual(status, 200)
        self.assertEqual(apply_body["applied"], [])
        self.assertEqual(apply_body["conflicts"][0]["code"], "playlist_persistence_failed")
        tx = APP.transactions.get(apply_body["audit_id"])
        self.assertEqual(tx["status"], "Failed")


class RollbackTests(PlaylistSuggestionsRouteTestCase):
    def test_rollback_restores_previous_desired_track_identity(self):
        self._set_library_items([_fake_lib_item()])
        self._seed_missing_tracks([{"artist": "M83", "title": "Midnight  City"}])
        _, body = self._get_suggestions()
        row = body["rows"][0]
        best = row["best"]
        status, apply_body = self._apply_suggestions([{
            "track_key": row["track_key"],
            "mb_trackid": best.get("mb_trackid") or "",
            "item_id": best.get("item_id"),
            "decision_version": best["decision_version"],
        }])
        self.assertEqual(status, 200)
        audit_id = apply_body["audit_id"]
        tracks_after_apply = self._manifest_tracks()
        self.assertTrue(any(t.get("title") == "Midnight City" for t in tracks_after_apply))

        resp = self.client.post(f"/api/transactions/{audit_id}/rollback")
        self.assertEqual(resp.status_code, 200)
        job_id = resp.get_json()["job_id"]
        job = _wait_job(job_id)
        self.assertIsNotNone(job)
        self.assertNotEqual(job.status, "running")

        tx = APP.transactions.get(audit_id)
        self.assertEqual(tx["status"], "Rolled Back")
        tracks_after_rollback = self._manifest_tracks()
        self.assertTrue(any(t.get("title") == "Midnight  City" for t in tracks_after_rollback))
        self.assertFalse(any(t.get("title") == "Midnight City" for t in tracks_after_rollback))

    def test_failed_restore_is_not_reported_rolled_back(self):
        self._set_library_items([_fake_lib_item()])
        self._seed_missing_tracks([{"artist": "M83", "title": "Midnight  City"}])
        _, body = self._get_suggestions()
        row = body["rows"][0]
        best = row["best"]
        _, apply_body = self._apply_suggestions([{
            "track_key": row["track_key"],
            "mb_trackid": best.get("mb_trackid") or "",
            "item_id": best.get("item_id"),
            "decision_version": best["decision_version"],
        }])
        audit_id = apply_body["audit_id"]

        with mock.patch.object(
            APP, "_playlist_apply_manifest_replacements",
            return_value={"ok": True, "resolved_count": 0},
        ):
            resp = self.client.post(f"/api/transactions/{audit_id}/rollback")
            job = _wait_job(resp.get_json()["job_id"])

        self.assertIsNotNone(job)
        tx = APP.transactions.get(audit_id)
        self.assertEqual(tx["status"], "Partially Rolled Back")


class SecretRedactionTests(PlaylistSuggestionsRouteTestCase):
    def test_exception_text_with_secret_shaped_value_never_reaches_response(self):
        secret = uuid.uuid4().hex
        self._set_library_items([_fake_lib_item()])
        self._seed_missing_tracks([{"artist": "M83", "title": "Midnight  City"}])
        _, body = self._get_suggestions()
        row = body["rows"][0]
        best = row["best"]

        with mock.patch.object(
            APP, "_playlist_apply_manifest_replacements",
            side_effect=RuntimeError(f"boom mysql://user:{secret}@db.internal/beets"),
        ):
            resp = self.client.post(
                f"/api/playlists/{self.clean_name}/apply-safe-suggestions",
                data=json.dumps({"suggestions": [{
                    "track_key": row["track_key"],
                    "mb_trackid": best.get("mb_trackid") or "",
                    "item_id": best.get("item_id"),
                    "decision_version": best["decision_version"],
                }]}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn(secret, resp.get_data(as_text=True))
        # The reservation must still be released even though the handler
        # raised (matching attach-recording's "released on any exception"
        # rule -- see _reserve_attach_recording_item's own finally block).
        self.assertNotIn(self.clean_name, APP._PLAYLIST_APPLY_RESERVED_NAMES)


if __name__ == "__main__":
    unittest.main()
