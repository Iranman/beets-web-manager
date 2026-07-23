"""Behavioral tests for backend-authoritative enforcement at the Import
Review manual attach endpoint (/api/items/<iid>/attach-recording).

Before this change the endpoint accepted any syntactically valid
MusicBrainz Recording UUID with no check against the PR16 matching
contract -- confirmed_conflicts and candidate were only ever logged, never
enforced, and the frontend's one real call site never even sent them. These
tests import the real app.py (same isolated-temp-environment pattern as
tests/test_ai_batch_retry_race.py) and drive the actual Flask route, with
only the network-touching primitives (AcoustID lookup, MusicBrainz search
"/ details, beet subprocess calls) mocked -- the matching-contract decision
logic, the route's enforcement branches, and the transaction/audit/rollback
plumbing all run for real.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="beets_attach_enforcement_"))
unittest.addModuleCleanup(shutil.rmtree, str(_TMP_ROOT), ignore_errors=True)

_ENV_OVERRIDES = {
    "BEETSDIR": str(_TMP_ROOT / "config"),
    "LIB_PATH": str(_TMP_ROOT / "config" / "musiclibrary.blb"),
    "AI_BATCH_STATE_DIR": str(_TMP_ROOT / "ai_batch_jobs"),
    "METADATA_CACHE_DIR": str(_TMP_ROOT / "cache"),
    "BEETS_TRANSACTION_DIR": str(_TMP_ROOT / "transactions"),
    "BEETS_WEB_AUTH_DISABLED": "1",
}
(_TMP_ROOT / "config").mkdir(parents=True, exist_ok=True)
_env_patcher = mock.patch.dict(os.environ, _ENV_OVERRIDES, clear=False)
_env_patcher.start()
unittest.addModuleCleanup(_env_patcher.stop)


def setUpModule():
    # Re-assert immediately before this module's tests run: other test
    # modules sharing the same process may have mutated BEETS_WEB_AUTH_*
    # env vars without reverting them (see test_ai_batch_retry_race.py's
    # identical comment for the full incident this guards against).
    os.environ.update(_ENV_OVERRIDES)


def _import_app():
    sys.path.insert(0, str(ROOT))
    import app as app_module
    return app_module


try:
    APP = _import_app()
    _APP_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment-dependent
    APP = None
    _APP_IMPORT_ERROR = exc


RECORDING_ID = "11111111-1111-1111-1111-111111111111"
OTHER_RECORDING_ID = "44444444-4444-4444-4444-444444444444"
RELEASE_ID = "22222222-2222-2222-2222-222222222222"
RGID = "33333333-3333-3333-3333-333333333333"
ARBITRARY_UUID = "55555555-5555-5555-5555-555555555555"


def _fake_item(**overrides):
    base = dict(
        title="Test Title", artist="Test Artist", album="", albumartist="",
        year="", genre="", track=0, label="",
        mb_trackid="", mb_albumid="", mb_releasegroupid="",
        path="/music/test.mp3", length=200.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _acoustid_candidate(**overrides):
    base = dict(
        score=95, mb_trackid=RECORDING_ID,
        mb_url=f"https://musicbrainz.org/recording/{RECORDING_ID}",
        title="Test Title", artist="Test Artist", album="Test Album",
        mb_albumid=RELEASE_ID, mb_albumids=[RELEASE_ID],
        year="2020", duration="",
    )
    base.update(overrides)
    return base


def _mb_details(**overrides):
    release = dict(
        mb_albumid=RELEASE_ID, album="Test Album", artist="Test Artist",
        year="2020", mb_releasegroupid=RGID, country="US",
        release_group_primary_type="Album",
    )
    base = dict(
        recording_id=RECORDING_ID, recording_title="Test Title",
        recording_artist="Test Artist", artist="Test Artist",
        linked_releases=[dict(release)],
        selected_release=dict(release),
        mb_albumid=RELEASE_ID, mb_releasegroupid=RGID,
        album="Test Album", year="2020",
    )
    base.update(overrides)
    return base


def _wait_job(job_id, timeout=10):
    deadline = time.time() + timeout
    job = APP.jobs.get(job_id)
    while job is not None and job.status == "running" and time.time() < deadline:
        time.sleep(0.02)
    return job


def _apply_fake_beet_mutation(item, cmd, mb_details_payload):
    """Realistic-enough fake beet mutation: attach-recording's persisted-
    state verification (re-reads the item after the mutation and compares
    actual mb_trackid/mb_albumid/mb_releasegroupid) requires the fake item
    to actually change when a mocked beet subprocess "succeeds" -- a fake
    that always returns rc=0 while leaving the item untouched would make
    every attach job fail verification."""
    if "modify" in cmd:
        for part in cmd:
            if part.startswith("mb_trackid="):
                item.mb_trackid = part.split("=", 1)[1]
            elif part.startswith("mb_albumid="):
                item.mb_albumid = part.split("=", 1)[1]
            elif part.startswith("mb_releasegroupid="):
                item.mb_releasegroupid = part.split("=", 1)[1]
    elif "mbsync" in cmd:
        # Mirrors real beets: mbsync fills in release identity from
        # whatever recording ID is currently on the item.
        details = mb_details_payload or {}
        release = details.get("selected_release") or {}
        if item.mb_trackid:
            item.mb_albumid = str(release.get("mb_albumid") or details.get("mb_albumid") or item.mb_albumid or "")
            item.mb_releasegroupid = str(
                release.get("mb_releasegroupid") or details.get("mb_releasegroupid") or item.mb_releasegroupid or ""
            )


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class AttachRecordingEnforcementTests(unittest.TestCase):
    """Shared plumbing: mock the item lookup and the trusted
    AcoustID/MusicBrainz candidate pipeline so each test can deterministically
    control exactly one matching decision, without real network/subprocess
    calls. The matching-contract decision logic and the route's enforcement
    branches run unmocked."""

    def setUp(self):
        self.item = _fake_item()
        self.client = APP.app.test_client()
        self.beet_calls = []
        self.addCleanup(APP._ATTACH_RECORDING_RESERVED_ITEMS.clear)

        def fake_beet_run(cmd, log, **kwargs):
            self.beet_calls.append(cmd)
            _apply_fake_beet_mutation(self.item, cmd, self._mb_details_payload)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.fake_beet_run = fake_beet_run
        self._acoustid_candidates = [_acoustid_candidate()]
        self._mb_details_payload = _mb_details()

        self._patch(mock.patch.object(APP.lib, "get_item", side_effect=lambda iid: self.item))
        self._patch(mock.patch.object(APP, "_acoustid_lookup_cached",
                                       side_effect=lambda path: self._acoustid_candidates))
        self._patch(mock.patch.object(APP, "_mb_recording_search", return_value=[]))
        self._patch(mock.patch.object(APP, "_fetch_mb_recording_details",
                                       side_effect=lambda *a, **k: self._mb_details_payload))
        self._patch(mock.patch.object(APP, "_beet_run", side_effect=fake_beet_run))
        self._patch(mock.patch.object(APP, "_invalidate_lib_cache", return_value=None))
        self._patch(mock.patch.object(APP, "_trigger_plex_refresh", return_value=None))

    def _patch(self, patcher):
        obj = patcher.start()
        self.addCleanup(patcher.stop)
        return obj

    def _post(self, iid, payload):
        return self.client.post(f"/api/items/{iid}/attach-recording", json=payload)

    def _current_version(self, iid=9001):
        _current, candidates, _path, _fn = APP._reconstruct_track_recording_candidates(self.item, iid)
        return next(c for c in candidates if c["mb_trackid"] == RECORDING_ID)["decision_version"]

    # ---- safe path -------------------------------------------------------

    def test_safe_attach_succeeds_writes_audit_and_is_idempotent(self):
        resp = self._post(9001, {"mb_trackid": RECORDING_ID, "mode": "safe"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["changed"])
        self.assertEqual(body["mode"], "safe")
        self.assertEqual(body["recording_id"], RECORDING_ID)
        audit_id = body["audit_id"]
        job = _wait_job(body["job_id"])
        self.assertIsNotNone(job)
        self.assertEqual(job.status, "success")

        tx = APP.transactions.get(audit_id)
        self.assertEqual(tx["status"], "Completed")
        self.assertEqual(tx["operation_type"], "MusicBrainz Match")
        self.assertTrue(tx["rollback"]["available"])
        self.assertEqual(tx["rollback"]["operations"][0]["type"], "recording_id_restore")
        self.assertEqual(tx["rollback"]["operations"][0]["fields"]["mb_trackid"], "")

        modify_calls = [c for c in self.beet_calls if "modify" in c]
        self.assertTrue(any(f"mb_trackid={RECORDING_ID}" in c for c in modify_calls))

        # Simulate the mutation having landed, then repeat the identical
        # request: must report no-op success, no new transaction/job.
        self.item.mb_trackid = RECORDING_ID
        self.item.mb_albumid = RELEASE_ID
        self.item.mb_releasegroupid = RGID
        before_tx_count, _ = APP.transactions.list(limit=500)
        resp2 = self._post(9001, {"mb_trackid": RECORDING_ID, "mode": "safe"})
        body2 = resp2.get_json()
        self.assertTrue(body2["ok"])
        self.assertFalse(body2["changed"])
        self.assertEqual(body2["reason"], "already_attached")
        self.assertNotIn("job_id", body2)
        after_tx_count, _ = APP.transactions.list(limit=500)
        self.assertEqual(len(before_tx_count), len(after_tx_count))

    # ---- client authority attacks -----------------------------------------

    def test_client_supplied_safety_fields_are_ignored_for_review_required_candidate(self):
        # No shared words with "Test Album" -- _similarity() floors any
        # overlapping-word pair at 0.70, which would stay above the 0.55
        # album_conflict threshold and defeat the point of this fixture.
        self.item.album = "Xyzzyxx Plugh Quorbat"
        payload = {
            "mb_trackid": RECORDING_ID,
            "mode": "safe",
            "attach_without_review": True,
            "confidence_score": 1.0,
            "conflicts": [],
            "matching_contract": {"action_eligibility": {"attach_without_review": True}},
            "safety_result": "Safe to attach",
            "safety_key": "safe",
            "review_required": False,
            "requires_confirmation": False,
        }
        resp = self._post(9001, payload)
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "review_confirmation_required")
        self.assertNotIn("job_id", body)
        self.assertEqual(self.beet_calls, [])

    # ---- identity mismatch --------------------------------------------------

    def test_recording_id_mismatch_when_deterministic_sources_conflict(self):
        # Candidate claims RECORDING_ID but the MusicBrainz details lookup
        # disagrees -- resolved_recording_id collapses to "", so nothing can
        # be requested safely even though the same ID is still displayed.
        self._mb_details_payload = _mb_details(recording_id=OTHER_RECORDING_ID)
        resp = self._post(9001, {"mb_trackid": RECORDING_ID, "mode": "safe"})
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "recording_id_mismatch")
        self.assertEqual(self.beet_calls, [])

    # ---- review-required path ----------------------------------------------

    def test_review_required_without_confirmation_is_rejected(self):
        self.item.artist = "Somebody Else Entirely"
        resp = self._post(9001, {"mb_trackid": RECORDING_ID, "mode": "safe"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["code"], "review_confirmation_required")
        self.assertEqual(self.beet_calls, [])

    def test_confirmed_review_succeeds_and_preserves_conflicts_in_audit(self):
        self.item.artist = "Somebody Else Entirely"
        version = self._current_version()
        resp = self._post(9001, {
            "mb_trackid": RECORDING_ID,
            "mode": "confirmed_review",
            "confirm": True,
            "confirmation_reason": "Reviewed manually, artist tag is stylized differently.",
            "decision_version": version,
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["mode"], "confirmed_review")
        job = _wait_job(body["job_id"])
        self.assertEqual(job.status, "success")
        tx = APP.transactions.get(body["audit_id"])
        self.assertEqual(tx["status"], "Completed")
        self.assertIn("artist_conflict", tx["metadata"]["conflicts"])
        self.assertTrue(tx["metadata"]["review_required"])
        self.assertIn("Reviewed manually", tx["metadata"]["confirmation_reason"])

    def test_confirmed_review_requires_explicit_confirm_boolean(self):
        version = self._current_version()
        resp = self._post(9001, {
            "mb_trackid": RECORDING_ID,
            "mode": "confirmed_review",
            "confirmation_reason": "trying to sneak by",
            "decision_version": version,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["code"], "review_confirmation_required")
        self.assertEqual(self.beet_calls, [])

    def test_confirmed_review_requires_a_reason(self):
        version = self._current_version()
        resp = self._post(9001, {
            "mb_trackid": RECORDING_ID,
            "mode": "confirmed_review",
            "confirm": True,
            "decision_version": version,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["code"], "confirmation_reason_required")
        self.assertEqual(self.beet_calls, [])

    # ---- arbitrary UUID protection ------------------------------------------

    def test_arbitrary_uuid_outside_trusted_set_rejected_even_with_confirmation(self):
        resp = self._post(9001, {
            "mb_trackid": ARBITRARY_UUID,
            "mode": "confirmed_review",
            "confirm": True,
            "confirmation_reason": "I really want this one",
            "decision_version": "drv1:doesnotmatter",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["code"], "candidate_not_in_trusted_set")
        self.assertEqual(self.beet_calls, [])

    # ---- conflict cases block safe attach -----------------------------------

    def test_hard_conflict_blocks_safe_attach(self):
        self.item.artist = "A Completely Different Performer"
        resp = self._post(9001, {"mb_trackid": RECORDING_ID, "mode": "safe"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["code"], "review_confirmation_required")
        self.assertEqual(self.beet_calls, [])

    def test_missing_release_group_id_blocks_safe_attach(self):
        details = _mb_details()
        details["selected_release"]["mb_releasegroupid"] = ""
        details["mb_releasegroupid"] = ""
        details["linked_releases"][0]["mb_releasegroupid"] = ""
        self._mb_details_payload = details
        resp = self._post(9001, {"mb_trackid": RECORDING_ID, "mode": "safe"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["code"], "review_confirmation_required")
        self.assertEqual(self.beet_calls, [])

    # ---- stale decision -------------------------------------------------------

    def test_stale_decision_version_returns_409_for_safe_mode(self):
        resp = self._post(9001, {
            "mb_trackid": RECORDING_ID,
            "mode": "safe",
            "decision_version": "drv1:stale-token-from-an-earlier-view",
        })
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["code"], "matching_decision_stale")
        self.assertEqual(self.beet_calls, [])

    def test_stale_decision_version_returns_409_for_confirmed_review(self):
        self.item.artist = "Somebody Else Entirely"
        resp = self._post(9001, {
            "mb_trackid": RECORDING_ID,
            "mode": "confirmed_review",
            "confirm": True,
            "confirmation_reason": "reviewed",
            "decision_version": "drv1:stale-token-from-an-earlier-view",
        })
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["code"], "matching_decision_stale")
        self.assertEqual(self.beet_calls, [])

    # ---- failure / rollback --------------------------------------------------

    def test_tag_write_failure_marks_transaction_failed_not_completed(self):
        def failing_beet_run(cmd, log, **kwargs):
            self.beet_calls.append(cmd)
            if "modify" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(APP, "_beet_run", side_effect=failing_beet_run):
            resp = self._post(9001, {"mb_trackid": RECORDING_ID, "mode": "safe"})
            body = resp.get_json()
            self.assertTrue(body["ok"])  # job accepted, outcome is async
            job = _wait_job(body["job_id"])
            self.assertEqual(job.status, "failed")
            tx = APP.transactions.get(body["audit_id"])
            self.assertEqual(tx["status"], "Failed")

    # ---- undo ------------------------------------------------------------

    def test_undo_restores_previous_identity_values(self):
        resp = self._post(9001, {"mb_trackid": RECORDING_ID, "mode": "safe"})
        body = resp.get_json()
        job = _wait_job(body["job_id"])
        self.assertEqual(job.status, "success")
        audit_id = body["audit_id"]

        rollback_resp = self.client.post(f"/api/transactions/{audit_id}/rollback")
        self.assertEqual(rollback_resp.status_code, 200)
        rollback_body = rollback_resp.get_json()
        self.assertTrue(rollback_body["ok"])
        rb_job = _wait_job(rollback_body["job_id"])
        self.assertEqual(rb_job.status, "success")

        tx = APP.transactions.get(audit_id)
        self.assertEqual(tx["status"], "Rolled Back")
        restore_calls = [c for c in self.beet_calls if "modify" in c and "mb_trackid=" in " ".join(c)]
        self.assertTrue(any("mb_trackid=" in part and RECORDING_ID not in part
                             for call in restore_calls for part in call if part.startswith("mb_trackid=")))

    # ---- serialization safety --------------------------------------------

    def test_response_never_echoes_arbitrary_client_supplied_fields(self):
        resp = self._post(9001, {
            "mb_trackid": RECORDING_ID,
            "mode": "safe",
            "authorization_header": "Bearer sk-should-not-appear",
            "raw_provider_payload": {"api_key": "super-secret-value"},
        })
        rendered = json.dumps(resp.get_json())
        self.assertNotIn("sk-should-not-appear", rendered)
        self.assertNotIn("super-secret-value", rendered)
        body = resp.get_json()
        job = _wait_job(body["job_id"])
        tx = APP.transactions.get(body["audit_id"])
        tx_rendered = json.dumps(tx)
        self.assertNotIn("sk-should-not-appear", tx_rendered)
        self.assertNotIn("super-secret-value", tx_rendered)

    # ---- basic validation --------------------------------------------------

    def test_invalid_uuid_format_rejected(self):
        resp = self._post(9001, {"mb_trackid": "not-a-uuid", "mode": "safe"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.beet_calls, [])

    def test_unknown_mode_rejected(self):
        resp = self._post(9001, {"mb_trackid": RECORDING_ID, "mode": "yolo"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["code"], "invalid_mode")


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class ManualIdAttachIntegrationTests(unittest.TestCase):
    """Integration coverage between the manual MusicBrainz-ID workflow
    (/api/import-review/manual-id/validate, current main) and attach
    enforcement (/api/items/<iid>/attach-recording, this branch). A manually
    entered Recording ID must only ever become attachable by independently
    reappearing in attach-recording's own trusted-candidate reconstruction --
    manual validation showing "ok: true" must never itself confer attach
    eligibility, and there must be no separate manual-attachment endpoint."""

    def setUp(self):
        self.item = _fake_item()
        self.client = APP.app.test_client()
        self.beet_calls = []
        self.addCleanup(APP._ATTACH_RECORDING_RESERVED_ITEMS.clear)

        def fake_beet_run(cmd, log, **kwargs):
            self.beet_calls.append(cmd)
            _apply_fake_beet_mutation(self.item, cmd, self._mb_details_payload)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self._acoustid_candidates = [_acoustid_candidate()]
        self._mb_details_payload = _mb_details()

        self._patch(mock.patch.object(APP.lib, "get_item", side_effect=lambda iid: self.item))
        self._patch(mock.patch.object(APP, "_acoustid_lookup_cached",
                                       side_effect=lambda path: self._acoustid_candidates))
        self._patch(mock.patch.object(APP, "_mb_recording_search", return_value=[]))
        self._patch(mock.patch.object(APP, "_fetch_mb_recording_details",
                                       side_effect=lambda *a, **k: self._mb_details_payload))
        self._patch(mock.patch.object(APP, "_beet_run", side_effect=fake_beet_run))
        self._patch(mock.patch.object(APP, "_invalidate_lib_cache", return_value=None))
        self._patch(mock.patch.object(APP, "_trigger_plex_refresh", return_value=None))

    def _patch(self, patcher):
        obj = patcher.start()
        self.addCleanup(patcher.stop)
        return obj

    def _manual_validate(self, mbid, item_id=9001):
        return self.client.post(
            "/api/import-review/manual-id/validate",
            json={"musicbrainz_id": mbid, "target_kind": "item", "item_id": item_id},
        )

    def _attach(self, iid, payload):
        return self.client.post(f"/api/items/{iid}/attach-recording", json=payload)

    def test_manually_validated_id_that_matches_reconstructed_set_can_be_safely_attached(self):
        manual_resp = self._manual_validate(RECORDING_ID)
        self.assertEqual(manual_resp.status_code, 200)
        manual_body = manual_resp.get_json()
        self.assertTrue(manual_body["ok"])
        self.assertEqual(manual_body["selected_recording_candidate"]["mb_trackid"], RECORDING_ID)

        # The manual-validate call above never granted attach eligibility by
        # itself -- this only succeeds because RECORDING_ID also comes back
        # from attach-recording's own AcoustID/MB-search reconstruction.
        resp = self._attach(9001, {"mb_trackid": RECORDING_ID, "mode": "safe"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["changed"])

    def test_manually_validated_id_outside_trusted_set_cannot_bypass_enforcement(self):
        # AcoustID/MB-search only ever surface RECORDING_ID for this item.
        # ARBITRARY_UUID resolves fine against MusicBrainz (manual validation
        # only checks the ID exists) but must still be rejected by
        # attach-recording -- even with an explicit confirmation -- because
        # it never appears in attach-recording's own reconstructed set.
        manual_resp = self._manual_validate(ARBITRARY_UUID)
        self.assertEqual(manual_resp.status_code, 200)
        self.assertTrue(manual_resp.get_json()["ok"])

        resp = self._attach(9001, {
            "mb_trackid": ARBITRARY_UUID,
            "mode": "confirmed_review",
            "confirm": True,
            "confirmation_reason": "I manually validated this on musicbrainz.org",
            "decision_version": "drv1:doesnotmatter",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["code"], "candidate_not_in_trusted_set")
        self.assertEqual(self.beet_calls, [])

    def test_manual_validate_candidate_uses_same_matching_contract_shape_as_attach(self):
        manual_candidate = self._manual_validate(RECORDING_ID).get_json()["selected_recording_candidate"]
        _current, candidates, _path, _fn = APP._reconstruct_track_recording_candidates(self.item, 9001)
        attach_candidate = next(c for c in candidates if c["mb_trackid"] == RECORDING_ID)
        # Both are produced by _compact_track_ai_candidate() over a
        # matching_result from the same matching-contract enrichment -- one
        # shared shape, not a second parallel manual-only contract.
        for key in ("decision", "matching_contract", "safety_result", "conflicts", "action_eligibility"):
            self.assertIn(key, manual_candidate)
            self.assertIn(key, attach_candidate)

    def test_safe_mode_ignores_manual_validate_success_when_local_evidence_conflicts(self):
        # Manual validation only confirms the ID resolves on MusicBrainz; it
        # has no opinion on local-tag conflicts, so it reports ok=True even
        # though this item's artist tag conflicts with the recording.
        self.item.artist = "Somebody Else Entirely"
        manual_resp = self._manual_validate(RECORDING_ID)
        self.assertTrue(manual_resp.get_json()["ok"])

        # That must not translate into safe-attach eligibility.
        resp = self._attach(9001, {"mb_trackid": RECORDING_ID, "mode": "safe"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["code"], "review_confirmation_required")
        self.assertEqual(self.beet_calls, [])

    def test_confirmed_review_rejects_missing_decision_version_not_just_stale(self):
        # Distinct from the stale-version case: decision_version omitted
        # entirely must also be rejected for confirmed_review. Safe mode
        # independently recomputes the whole decision at mutation time, so it
        # alone may omit it (see PR description for that asymmetry).
        resp = self._attach(9001, {
            "mb_trackid": RECORDING_ID,
            "mode": "confirmed_review",
            "confirm": True,
            "confirmation_reason": "reviewed",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["code"], "matching_decision_stale")
        self.assertEqual(self.beet_calls, [])

    def test_no_separate_manual_attachment_endpoint_exists(self):
        # Attach enforcement must not create a second mutation path for
        # manually-entered IDs -- manual-id/validate is read-only evidence
        # lookup; the only route that ever writes tags is attach-recording.
        rules = {str(rule) for rule in APP.app.url_map.iter_rules()}
        manual_attach_routes = {
            rule for rule in rules
            if "manual" in rule.lower() and "attach" in rule.lower()
        }
        self.assertEqual(manual_attach_routes, set())


if __name__ == "__main__":
    unittest.main()
