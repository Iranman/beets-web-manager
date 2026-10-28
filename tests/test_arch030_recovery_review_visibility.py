"""ARCH-030 F9 (release gate): a NO_RESULT recovery the gate declines under
Option C lands in ``review_required`` WITHOUT an enqueued pending-review row
(the advancer's keep path only transitions carrier state). Before the fix
GET /api/import/review-queue built its list ONLY from the pending-review store,
so such a carrier was INVISIBLE and the user could neither see nor act on their
missing-track recovery (defeating req-3/req-6/req-8 for the main Option-C path).

These tests drive the carrier through the REAL advancer into review_required
and then assert the endpoint surfaces it with its full recovery packet, that an
auto-accepted (gate-passing) carrier is not surfaced, and that a carrier the
non-auto hand-off already enqueued is not duplicated.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import app as APP
import routes_import
import backend.recovery_carrier as rc
import backend.recovery_service as recovery_service
import backend.matching_service as matching_service
import backend.import_service as import_service

RGID = "37cb4a72-1111-2222-3333-444444444444"
RELEASE = "55555555-6666-7777-8888-999999999999"


class RecoveryReviewVisibilityTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._prev = os.environ.get("RECOVERY_CARRIER_STATE_DIR")
        os.environ["RECOVERY_CARRIER_STATE_DIR"] = self.tmp
        rc._store_registry.clear()
        self.addCleanup(self._restore_state_dir)
        self.client = APP.app.test_client()

    def _restore_state_dir(self):
        rc._store_registry.clear()
        if self._prev is None:
            os.environ.pop("RECOVERY_CARRIER_STATE_DIR", None)
        else:
            os.environ["RECOVERY_CARRIER_STATE_DIR"] = self._prev

    def _staged_dir(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "01 track.flac"), "wb") as fh:
            fh.write(b"\x00" * 16)
        return d

    def _make_carrier(self, name, album_id, *, rid, title, track):
        staged = self._staged_dir(name)
        cid = rc.create_carrier(
            existing_album_id=album_id, mb_releasegroupid=RGID, release_id=RELEASE,
            destination_album_path="/data/media/music/Artist/" + name,
            wanted_tracks=[{"track": track, "recording_id": rid}],
            expected_tracks=[{"recording_id": rid, "track": track, "disc": 1, "title": title}],
            staged_folder_path=staged, state="downloaded")["carrier_id"]
        return cid, staged

    def _run_advancer(self, preflight):
        with mock.patch.object(matching_service, "_folder_release_preflight", preflight), \
             mock.patch.object(matching_service, "_carrier_expected_as_wanted",
                               side_effect=lambda c: list(c.get("expected_tracks") or [])), \
             mock.patch.object(import_service, "start_folder_import_with_id",
                               side_effect=lambda payload: (
                                   rc.advance_state(payload["recovery_carrier_id"], "importing"),
                                   ({"ok": True, "job_id": "imp"}, 200))[1]):
            return recovery_service.advance_stuck_carriers(log=[])

    def _get_queue(self, pending=None):
        pending = pending or []
        with mock.patch.object(APP.composite_workflows, "get_unmatched_review_items",
                               return_value={"albums": [], "singletons": []}), \
             mock.patch.object(routes_import, "_load_pending_reviews", return_value=pending), \
             mock.patch.object(routes_import, "_import_skipped_items", return_value=[]):
            res = self.client.get("/api/import/review-queue")
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()

    def test_noresult_review_required_carrier_is_surfaced_with_packet(self):
        """End-to-end: a NO_RESULT download whose gate declines (Option C) is
        advanced to review_required by the real advancer, and the review-queue
        endpoint surfaces it with its full carrier packet -- which tracks are
        missing, per-track status, and the clickable canonical MBID."""
        no_cid, no_staged = self._make_carrier("missing", 42, rid="r-no", title="Lost Track", track=7)
        pass_cid, _ = self._make_carrier("found", 43, rid="r-ok", title="Found Track", track=3)

        def _preflight(folder, release, album_id, *, log=None, **kw):
            if folder == no_staged:
                return {"ok": False, "error": "no fingerprint evidence for these tracks",
                        "matching_decision": {"action_allowed": False,
                                              "identity_proof": "release_group_id"}}
            return {"ok": True, "matching_decision": {
                "action_allowed": True, "identity_proof": "deterministic_metadata_slot"}}

        self._run_advancer(_preflight)
        # Gate correctly declined the NO_RESULT carrier under Option C; the other
        # was auto-handed to insertion.
        self.assertEqual(rc.get_carrier(no_cid)["state"], "review_required")
        self.assertEqual(rc.get_carrier(pass_cid)["state"], "importing")

        body = self._get_queue()
        recovery_rows = [r for r in body["items"]
                         if (r.get("evidence") or {}).get("recovery_carrier")]
        surfaced = {r["evidence"]["recovery_carrier"]["recovery_carrier_id"]: r
                    for r in recovery_rows}
        # The stuck NO_RESULT carrier IS visible; the auto-accepted one is NOT.
        self.assertIn(no_cid, surfaced, body["items"])
        self.assertNotIn(pass_cid, surfaced)

        row = surfaced[no_cid]
        pkt = row["evidence"]["recovery_carrier"]
        self.assertTrue(pkt.get("recovery"))
        self.assertEqual(pkt["mb_releasegroupid"], RGID)
        self.assertEqual(row["mb_releasegroupid"], RGID)
        self.assertEqual(pkt["existing_album_id"], 42)
        self.assertEqual(row["type"], "pending_ai")
        self.assertEqual(os.path.normpath(row["path"]), os.path.normpath(no_staged))
        # The packet names which track is missing, with a per-track status.
        tracks = pkt["tracks"]
        self.assertEqual([t["title"] for t in tracks], ["Lost Track"])
        self.assertEqual(tracks[0]["position"], 7)
        self.assertEqual(tracks[0]["recording_id"], "r-no")
        self.assertEqual(tracks[0]["status"], "downloaded")
        # Contract-derived confidence, not the stale coarse "low".
        self.assertEqual(row["confidence"], "review_required")

    def test_auto_accepted_carrier_is_not_duplicated(self):
        """A carrier that is NOT in review_required (imported/importing) never
        appears as a surfaced recovery row."""
        cid, staged = self._make_carrier("done", 42, rid="r1", title="T", track=1)
        rc.advance_state(cid, "imported")
        body = self._get_queue()
        surfaced = [r for r in body["items"]
                    if (r.get("evidence") or {}).get("recovery_carrier")]
        self.assertEqual(surfaced, [])

    def test_carrier_already_enqueued_is_not_doubled(self):
        """When the non-auto hand-off already enqueued a pending row for the
        carrier, the read-side surfacing dedupes it (by carrier id) -- exactly
        one row, not two."""
        cid, staged = self._make_carrier("dup", 42, rid="r1", title="T", track=1)
        rc.advance_state(cid, "review_required")
        pending_item = {
            "path": staged,
            "status": "Pending AI",
            "suggestion": {"recovery": True, "recovery_carrier_id": cid},
            "evidence": {"recovery_carrier": {"recovery_carrier_id": cid,
                                              "recovery": True, "mb_releasegroupid": RGID}},
            "added_at": 1.0,
        }
        body = self._get_queue(pending=[pending_item])
        matched = [r for r in body["items"]
                   if (r.get("evidence") or {}).get("recovery_carrier", {})
                   .get("recovery_carrier_id") == cid]
        self.assertEqual(len(matched), 1, matched)
        # The single row is the enqueued pending row, not a synthesized recovery row.
        self.assertEqual(matched[0]["id"], "pending:" + staged)


if __name__ == "__main__":
    unittest.main()
