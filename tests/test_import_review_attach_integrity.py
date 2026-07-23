"""Behavioral tests for the concurrency, strict-subprocess-outcome,
persisted-state-verification, and secret-sanitization hardening added on
top of the base backend-authoritative enforcement covered by
tests/test_import_review_attach_enforcement.py.

That module already proves candidate reconstruction, safe/confirmed-review
mode gating, decision_version staleness, and basic transaction/audit
plumbing are backend-authoritative. This module proves the parts a prior
review pass flagged as still missing:

  * per-item (not global) reservation so two concurrent attach-recording
    requests for the SAME item can never race (different items still run
    fully in parallel);
  * strict beet subprocess return-code handling (-9 cancelled, 124 timed
    out, other nonzero failed) that stops immediately and never claims
    success;
  * truthful persisted-vs-candidate identity verification (the transaction
    is only ever "Completed" against what was actually re-read from Beets,
    never the pre-mutation candidate expectation);
  * confirmation-reason / reconstruction-exception sanitization so secrets
    never reach the response, transaction record, or logs.

Reuses the fixtures/module import from test_import_review_attach_enforcement
(same isolated-temp-environment app import) rather than re-deriving them.
"""
import json
import os
import threading
import time
import unittest
import unittest.mock as mock
import uuid
from types import SimpleNamespace

from tests.test_import_review_attach_enforcement import (
    APP,
    _APP_IMPORT_ERROR,
    _ENV_OVERRIDES,
    _fake_item,
    _acoustid_candidate,
    _wait_job,
    RECORDING_ID,
    RELEASE_ID,
    RGID,
)


def setUpModule():
    # test_import_review_attach_enforcement's own module-level env override
    # is reverted by its addModuleCleanup once ITS tests finish running --
    # when this module's tests run afterward in the same process (full
    # discovery), BEETS_WEB_AUTH_DISABLED and friends would otherwise be
    # unset again, and every request here would 401/503 on the auth
    # boundary instead of exercising attach-recording. Re-assert immediately
    # before this module's tests run (same defensive pattern documented in
    # test_import_review_attach_enforcement.py / test_ai_batch_retry_race.py).
    os.environ.update(_ENV_OVERRIDES)

SECOND_RECORDING_ID = "66666666-6666-6666-6666-666666666666"
SECOND_RELEASE_ID = "77777777-7777-7777-7777-777777777777"
SECOND_RGID = "88888888-8888-8888-8888-888888888888"
PRIOR_RECORDING_ID = "99999999-9999-9999-9999-999999999999"


def _mb_details_for(recording_id, release_id, rgid):
    release = dict(
        mb_albumid=release_id, album="Test Album", artist="Test Artist",
        year="2020", mb_releasegroupid=rgid, country="US",
        release_group_primary_type="Album",
    )
    return dict(
        recording_id=recording_id, recording_title="Test Title",
        recording_artist="Test Artist", artist="Test Artist",
        linked_releases=[dict(release)], selected_release=dict(release),
        mb_albumid=release_id, mb_releasegroupid=rgid,
        album="Test Album", year="2020",
    )


def _iid_from_cmd(cmd):
    for part in cmd:
        if isinstance(part, str) and part.startswith("id:"):
            try:
                return int(part[3:])
            except ValueError:
                return None
    return None


def _stage_of(cmd):
    for stage in ("modify", "mbsync", "write", "move"):
        if stage in cmd:
            return stage
    return ""


def _mutate_fake_item(item, cmd, mb_details_by_recording):
    if "modify" in cmd:
        for part in cmd:
            if part.startswith("mb_trackid="):
                item.mb_trackid = part.split("=", 1)[1]
            elif part.startswith("mb_albumid="):
                item.mb_albumid = part.split("=", 1)[1]
            elif part.startswith("mb_releasegroupid="):
                item.mb_releasegroupid = part.split("=", 1)[1]
    elif "mbsync" in cmd:
        details = mb_details_by_recording.get(item.mb_trackid) or {}
        release = details.get("selected_release") or {}
        if item.mb_trackid:
            item.mb_albumid = str(release.get("mb_albumid") or details.get("mb_albumid") or item.mb_albumid or "")
            item.mb_releasegroupid = str(
                release.get("mb_releasegroupid") or details.get("mb_releasegroupid") or item.mb_releasegroupid or ""
            )


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class _AttachIntegrityTestCase(unittest.TestCase):
    """Shared multi-item plumbing: unlike the base enforcement test file
    (one item per test), several tests here need two independent items
    (different-item concurrency) or per-stage/per-item control over beet
    subprocess outcomes and persisted mutation, so the fixture is keyed by
    item id throughout."""

    def setUp(self):
        self.client = APP.app.test_client()
        self.items = {}
        self.candidates_by_path = {}
        self.mb_details = {}
        self.beet_calls = []
        self.gates = {}
        self.stage_rc = {}
        self.suppress_mutation = set()
        self.persist_override = {}
        self.addCleanup(APP._ATTACH_RECORDING_RESERVED_ITEMS.clear)

        def get_item(iid):
            return self.items.get(iid)

        def acoustid_lookup(path):
            return self.candidates_by_path.get(path, [])

        def mb_search(*a, **k):
            return []

        def fetch_details(mbid, *a, **k):
            return self.mb_details.get(mbid) or _mb_details_for(mbid, RELEASE_ID, RGID)

        def fake_beet_run(cmd, log, **kwargs):
            self.beet_calls.append(cmd)
            iid = _iid_from_cmd(cmd)
            gate = self.gates.get(iid)
            if gate is not None:
                gate.wait(timeout=10)
            stage = _stage_of(cmd)
            rc = self.stage_rc.get((iid, stage), 0)
            if rc == 0 and iid not in self.suppress_mutation:
                item = self.items.get(iid)
                if item is not None:
                    _mutate_fake_item(item, cmd, self.mb_details)
                    if stage == "mbsync" and iid in self.persist_override:
                        forced = self.persist_override[iid]
                        if "mb_albumid" in forced:
                            item.mb_albumid = forced["mb_albumid"]
                        if "mb_releasegroupid" in forced:
                            item.mb_releasegroupid = forced["mb_releasegroupid"]
            stderr = "" if rc == 0 else f"{stage} boom Authorization: Bearer subprocess-stderr-secret"
            return SimpleNamespace(returncode=rc, stdout="", stderr=stderr)

        self._patch(mock.patch.object(APP.lib, "get_item", side_effect=get_item))
        self._patch(mock.patch.object(APP, "_acoustid_lookup_cached", side_effect=acoustid_lookup))
        self._patch(mock.patch.object(APP, "_mb_recording_search", side_effect=mb_search))
        self._patch(mock.patch.object(APP, "_fetch_mb_recording_details", side_effect=fetch_details))
        self._patch(mock.patch.object(APP, "_beet_run", side_effect=fake_beet_run))
        self._patch(mock.patch.object(APP, "_invalidate_lib_cache", return_value=None))
        self._patch(mock.patch.object(APP, "_trigger_plex_refresh", return_value=None))

    def _patch(self, patcher):
        obj = patcher.start()
        self.addCleanup(patcher.stop)
        return obj

    def add_item(self, iid, *, recording_id=RECORDING_ID, release_id=RELEASE_ID, rgid=RGID, **overrides):
        item = _fake_item(path=f"/music/item{iid}.mp3", **overrides)
        self.items[iid] = item
        resolved_path = APP._item_ai_abs_path(item)
        self.candidates_by_path[resolved_path] = [
            _acoustid_candidate(mb_trackid=recording_id, mb_albumid=release_id, mb_albumids=[release_id])
        ]
        self.mb_details[recording_id] = _mb_details_for(recording_id, release_id, rgid)
        return item

    def _post_safe(self, iid, mb_trackid=RECORDING_ID):
        return self.client.post(f"/api/items/{iid}/attach-recording", json={"mb_trackid": mb_trackid, "mode": "safe"})

    def _post_confirmed(self, iid, mb_trackid=RECORDING_ID, reason="Reviewed manually.", version=None):
        if version is None:
            version = self._current_version(iid, mb_trackid)
        return self.client.post(f"/api/items/{iid}/attach-recording", json={
            "mb_trackid": mb_trackid, "mode": "confirmed_review", "confirm": True,
            "confirmation_reason": reason, "decision_version": version,
        })

    def _current_version(self, iid, mb_trackid=RECORDING_ID):
        item = self.items[iid]
        _current, candidates, _path, _fn = APP._reconstruct_track_recording_candidates(item, iid)
        return next(c for c in candidates if c["mb_trackid"] == mb_trackid)["decision_version"]

    def _stages_called_for(self, iid):
        out = []
        for cmd in self.beet_calls:
            if _iid_from_cmd(cmd) == iid:
                out.append(_stage_of(cmd))
        return out


# ── Step 12: per-item reservation / concurrency ───────────────────────────────

class ConcurrencyReservationTests(_AttachIntegrityTestCase):
    def _block_item(self, iid):
        gate = threading.Event()
        self.gates[iid] = gate
        return gate

    def test_a_safe_plus_safe_same_item_second_gets_409(self):
        self.add_item(9101)
        gate = self._block_item(9101)
        resp1 = self._post_safe(9101)
        self.assertEqual(resp1.status_code, 200)
        job1_id = resp1.get_json()["job_id"]

        resp2 = self._post_safe(9101)
        self.assertEqual(resp2.status_code, 409)
        body2 = resp2.get_json()
        self.assertFalse(body2["ok"])
        self.assertEqual(body2["code"], "attachment_in_progress")

        gate.set()
        job1 = _wait_job(job1_id)
        self.assertEqual(job1.status, "success")

        txs, _total = APP.transactions.list(limit=500)
        matching = [t for t in txs if t.get("metadata", {}).get("item_id") == 9101]
        self.assertEqual(len(matching), 1, "exactly one transaction must exist for this item")
        # Exactly one transaction/job's worth of modify calls ran for this item.
        modify_calls = [s for s in self._stages_called_for(9101) if s == "modify"]
        self.assertEqual(len(modify_calls), 1)

    def test_b_safe_plus_confirmed_review_same_item_second_gets_409(self):
        self.add_item(9102)
        gate = self._block_item(9102)
        resp1 = self._post_safe(9102)
        self.assertEqual(resp1.status_code, 200)
        job1_id = resp1.get_json()["job_id"]

        resp2 = self._post_confirmed(9102, version="drv2:doesnotmatter")
        self.assertEqual(resp2.status_code, 409)
        self.assertEqual(resp2.get_json()["code"], "attachment_in_progress")

        gate.set()
        _wait_job(job1_id)

    def test_c_confirmed_review_plus_confirmed_review_same_item_only_one_proceeds(self):
        self.add_item(9103)
        gate = self._block_item(9103)
        version = self._current_version(9103)
        resp1 = self._post_confirmed(9103, version=version)
        self.assertEqual(resp1.status_code, 200)
        job1_id = resp1.get_json()["job_id"]

        resp2 = self._post_confirmed(9103, version=version)
        self.assertEqual(resp2.status_code, 409)
        self.assertEqual(resp2.get_json()["code"], "attachment_in_progress")

        gate.set()
        job1 = _wait_job(job1_id)
        self.assertEqual(job1.status, "success")
        modify_calls = [s for s in self._stages_called_for(9103) if s == "modify"]
        self.assertEqual(len(modify_calls), 1)

    def test_d_different_items_proceed_concurrently_lock_is_not_global(self):
        self.add_item(9104)
        self.add_item(9105, recording_id=SECOND_RECORDING_ID, release_id=SECOND_RELEASE_ID, rgid=SECOND_RGID)
        gate = self._block_item(9104)

        resp1 = self._post_safe(9104)
        self.assertEqual(resp1.status_code, 200)
        job1_id = resp1.get_json()["job_id"]

        # Item 9104's job is stalled on the gate right now; a completely
        # unrelated item must still proceed and finish without waiting.
        resp2 = self._post_safe(9105, mb_trackid=SECOND_RECORDING_ID)
        self.assertEqual(resp2.status_code, 200)
        job2_id = resp2.get_json()["job_id"]
        job2 = _wait_job(job2_id, timeout=5)
        self.assertEqual(job2.status, "success")

        gate.set()
        job1 = _wait_job(job1_id)
        self.assertEqual(job1.status, "success")

    def test_e_release_after_success_later_request_not_blocked(self):
        self.add_item(9106)
        resp1 = self._post_safe(9106)
        _wait_job(resp1.get_json()["job_id"])
        self.assertNotIn(9106, APP._ATTACH_RECORDING_RESERVED_ITEMS)

        resp2 = self._post_safe(9106)
        self.assertNotEqual(resp2.status_code, 409)
        body2 = resp2.get_json()
        self.assertTrue(body2["ok"])
        # Either a fresh job or an idempotent no-op -- never blocked.
        self.assertNotEqual(body2.get("code"), "attachment_in_progress")

    def test_f_release_after_ordinary_failure(self):
        self.add_item(9107)
        self.stage_rc[(9107, "modify")] = 1
        resp1 = self._post_safe(9107)
        job1 = _wait_job(resp1.get_json()["job_id"])
        self.assertEqual(job1.status, "failed")
        self.assertNotIn(9107, APP._ATTACH_RECORDING_RESERVED_ITEMS)

        del self.stage_rc[(9107, "modify")]
        resp2 = self._post_safe(9107)
        self.assertNotEqual(resp2.status_code, 409)

    def test_g_release_after_cancellation(self):
        self.add_item(9108)
        self.stage_rc[(9108, "modify")] = -9
        resp1 = self._post_safe(9108)
        audit_id = resp1.get_json()["audit_id"]
        job1 = _wait_job(resp1.get_json()["job_id"])
        self.assertEqual(job1.status, "failed")  # PythonJob has no separate cancelled state
        tx = APP.transactions.get(audit_id)
        self.assertEqual(tx["status"], "Cancelled")
        self.assertNotIn(9108, APP._ATTACH_RECORDING_RESERVED_ITEMS)

        del self.stage_rc[(9108, "modify")]
        resp2 = self._post_safe(9108)
        self.assertNotEqual(resp2.status_code, 409)

    def test_h_job_start_exception_releases_reservation(self):
        self.add_item(9109)
        with mock.patch.object(APP.jobs, "start_python", side_effect=RuntimeError("boom")):
            resp1 = self._post_safe(9109)
        self.assertEqual(resp1.status_code, 500)
        self.assertEqual(resp1.get_json()["code"], "attachment_job_start_failed")
        self.assertNotIn(9109, APP._ATTACH_RECORDING_RESERVED_ITEMS)

        resp2 = self._post_safe(9109)
        self.assertNotEqual(resp2.status_code, 409)


# ── Step 13: strict beet subprocess outcomes ──────────────────────────────────

class SubprocessOutcomeTests(_AttachIntegrityTestCase):
    def test_attach_stage_outcomes_stop_immediately_and_are_truthful(self):
        stages = ["modify", "mbsync", "write", "move"]
        outcomes = [(-9, "Cancelled"), (124, "Failed"), (1, "Failed")]
        for stage_idx, stage in enumerate(stages):
            for rc_idx, (rc, expected_status) in enumerate(outcomes):
                iid = 40000 + stage_idx * 10 + rc_idx
                with self.subTest(stage=stage, rc=rc):
                    self.add_item(iid)
                    self.stage_rc[(iid, stage)] = rc
                    resp = self._post_safe(iid)
                    self.assertEqual(resp.status_code, 200)
                    audit_id = resp.get_json()["audit_id"]
                    job = _wait_job(resp.get_json()["job_id"])
                    self.assertEqual(job.status, "failed")

                    tx = APP.transactions.get(audit_id)
                    self.assertEqual(tx["status"], expected_status)
                    self.assertNotEqual(tx["status"], "Completed")

                    called = self._stages_called_for(iid)
                    self.assertEqual(called, stages[:stage_idx + 1], "a later stage ran after a fatal outcome")
                    self.assertNotIn(iid, APP._ATTACH_RECORDING_RESERVED_ITEMS)

                    rendered = json.dumps(tx)
                    self.assertNotIn("subprocess-stderr-secret", rendered)
                    self.assertNotIn("Bearer", rendered)

    def test_rollback_stage_outcomes_never_report_success(self):
        stages = ["modify", "write", "move"]
        outcomes = [-9, 124, 1]
        for stage_idx, stage in enumerate(stages):
            for rc_idx, rc in enumerate(outcomes):
                iid = 41000 + stage_idx * 10 + rc_idx
                with self.subTest(stage=stage, rc=rc):
                    self.add_item(iid)
                    resp = self._post_safe(iid)
                    self.assertEqual(resp.status_code, 200)
                    audit_id = resp.get_json()["audit_id"]
                    job = _wait_job(resp.get_json()["job_id"])
                    self.assertEqual(job.status, "success")

                    self.stage_rc[(iid, stage)] = rc
                    rb_resp = self.client.post(f"/api/transactions/{audit_id}/rollback")
                    self.assertEqual(rb_resp.status_code, 200)
                    rb_job = _wait_job(rb_resp.get_json()["job_id"])
                    self.assertIn(rb_job.status, ("success", "failed"))

                    tx = APP.transactions.get(audit_id)
                    self.assertNotEqual(tx["status"], "Rolled Back")

                    rendered = json.dumps(tx)
                    self.assertNotIn("subprocess-stderr-secret", rendered)

    def test_rollback_mbsync_failure_with_prior_recording_id_fails_restore(self):
        # Only reachable when the rollback target mb_trackid is non-empty --
        # attach a second recording ID on top of an item that already had
        # one, then fail mbsync while rolling the second attach back.
        iid = 42001
        self.add_item(iid, recording_id=SECOND_RECORDING_ID, release_id=SECOND_RELEASE_ID, rgid=SECOND_RGID,
                       mb_trackid=PRIOR_RECORDING_ID, mb_albumid=RELEASE_ID, mb_releasegroupid=RGID)
        resp = self._post_confirmed(iid, mb_trackid=SECOND_RECORDING_ID,
                                     reason="Switching recording identity.")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        audit_id = resp.get_json()["audit_id"]
        job = _wait_job(resp.get_json()["job_id"])
        self.assertEqual(job.status, "success")

        tx = APP.transactions.get(audit_id)
        self.assertEqual(tx["rollback"]["operations"][0]["fields"]["mb_trackid"], PRIOR_RECORDING_ID)

        self.stage_rc[(iid, "mbsync")] = 1
        rb_resp = self.client.post(f"/api/transactions/{audit_id}/rollback")
        _wait_job(rb_resp.get_json()["job_id"])
        tx_after = APP.transactions.get(audit_id)
        self.assertNotEqual(tx_after["status"], "Rolled Back")
        self.assertIn("mbsync", self._stages_called_for(iid)[-3:])


# ── Step 14: persisted-state verification ─────────────────────────────────────

class PersistedStateVerificationTests(_AttachIntegrityTestCase):
    def test_a_correct_persisted_state_matches_reread_item(self):
        iid = 43001
        self.add_item(iid)
        resp = self._post_safe(iid)
        audit_id = resp.get_json()["audit_id"]
        job = _wait_job(resp.get_json()["job_id"])
        self.assertEqual(job.status, "success")

        tx = APP.transactions.get(audit_id)
        self.assertEqual(tx["status"], "Completed")
        persisted = tx["metadata"]["persisted_identity"]
        self.assertEqual(persisted["mb_trackid"], RECORDING_ID)
        self.assertEqual(persisted["mb_albumid"], RELEASE_ID)
        self.assertEqual(persisted["mb_releasegroupid"], RGID)
        self.assertEqual(tx["changes"][0]["new_metadata"], persisted)
        self.assertEqual(self.items[iid].mb_trackid, RECORDING_ID)

    def test_b_recording_id_fails_to_persist_fails_job_and_transaction(self):
        iid = 43002
        self.add_item(iid)
        self.suppress_mutation.add(iid)
        resp = self._post_safe(iid)
        audit_id = resp.get_json()["audit_id"]
        job = _wait_job(resp.get_json()["job_id"])
        self.assertEqual(job.status, "failed")

        tx = APP.transactions.get(audit_id)
        self.assertEqual(tx["status"], "Failed")
        self.assertNotEqual(tx["status"], "Completed")
        rendered = json.dumps(tx)
        self.assertNotIn('"status": "Completed"', rendered)

    def test_c_different_release_id_persisted_completes_but_audits_truthfully(self):
        iid = 43003
        self.add_item(iid)
        self.persist_override[iid] = {"mb_albumid": SECOND_RELEASE_ID}
        resp = self._post_safe(iid)
        audit_id = resp.get_json()["audit_id"]
        job = _wait_job(resp.get_json()["job_id"])
        self.assertEqual(job.status, "success")

        tx = APP.transactions.get(audit_id)
        self.assertEqual(tx["status"], "Completed")
        self.assertTrue(tx["metadata"]["release_identity_mismatch"])
        self.assertEqual(tx["metadata"]["persisted_identity"]["mb_albumid"], SECOND_RELEASE_ID)
        self.assertEqual(tx["metadata"]["candidate_identity"]["mb_albumid"], RELEASE_ID)
        self.assertNotEqual(
            tx["metadata"]["persisted_identity"]["mb_albumid"],
            tx["metadata"]["candidate_identity"]["mb_albumid"],
        )

    def test_d_missing_release_group_id_after_sync_reported_truthfully(self):
        iid = 43004
        self.add_item(iid)
        self.persist_override[iid] = {"mb_releasegroupid": ""}
        resp = self._post_safe(iid)
        audit_id = resp.get_json()["audit_id"]
        job = _wait_job(resp.get_json()["job_id"])
        self.assertEqual(job.status, "success")

        tx = APP.transactions.get(audit_id)
        self.assertEqual(tx["status"], "Completed")
        self.assertTrue(tx["metadata"]["release_group_identity_mismatch"])
        self.assertEqual(tx["metadata"]["persisted_identity"]["mb_releasegroupid"], "")
        self.assertEqual(tx["metadata"]["candidate_identity"]["mb_releasegroupid"], RGID)


# ── Step 10: new structured error codes ───────────────────────────────────────

class NewStructuredErrorCodeTests(_AttachIntegrityTestCase):
    def test_item_not_found_returns_review_item_not_found(self):
        resp = self._post_safe(90001)  # never registered in self.items
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["code"], "review_item_not_found")

    def test_missing_recording_id_returns_recording_id_required(self):
        resp = self.client.post("/api/items/90002/attach-recording", json={"mode": "safe"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["code"], "recording_id_required")

    def test_invalid_uuid_returns_invalid_recording_id(self):
        resp = self.client.post("/api/items/90003/attach-recording",
                                 json={"mb_trackid": "not-a-uuid", "mode": "safe"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["code"], "invalid_recording_id")


# ── Step 15: secret sanitization ──────────────────────────────────────────────

class SecuritySanitizationTests(_AttachIntegrityTestCase):
    SECRETS = [
        "Authorization: Bearer bearer-secret",
        "Cookie: session=cookie-secret",
        "api_key=query-secret",
        "https://user:password@example.test/",
    ]

    def _assert_no_secrets(self, *blobs):
        haystacks = [json.dumps(b) if not isinstance(b, str) else b for b in blobs]
        for blob in haystacks:
            self.assertNotIn("bearer-secret", blob)
            self.assertNotIn("cookie-secret", blob)
            self.assertNotIn("query-secret", blob)
            self.assertNotIn("password", blob)

    def test_confirmation_reason_secrets_never_survive(self):
        iid = 50001
        self.add_item(iid)
        reason = " / ".join(self.SECRETS)
        resp = self._post_confirmed(iid, reason=reason)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        audit_id = resp.get_json()["audit_id"]
        job = _wait_job(resp.get_json()["job_id"])
        self.assertEqual(job.status, "success")

        tx = APP.transactions.get(audit_id)
        self._assert_no_secrets(resp.get_json(), tx, tx.get("logs"), tx.get("changes"), tx.get("metadata"))
        self.assertNotIn("bearer-secret", tx["reason"])
        self.assertNotIn("bearer-secret", tx["changes"][0]["reason"])
        self.assertNotIn("bearer-secret", tx["metadata"]["confirmation_reason"])

    def test_confirmation_reason_multiline_becomes_single_line_and_bounded(self):
        iid = 50002
        self.add_item(iid)
        reason = "line one\r\nline two\twith a tab\nline three " + ("x" * 600)
        resp = self._post_confirmed(iid, reason=reason)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        audit_id = resp.get_json()["audit_id"]
        _wait_job(resp.get_json()["job_id"])
        tx = APP.transactions.get(audit_id)
        stored = tx["metadata"]["confirmation_reason"]
        self.assertNotIn("\n", stored)
        self.assertNotIn("\r", stored)
        self.assertNotIn("\t", stored)
        self.assertLessEqual(len(stored), 500)

    def test_confirmation_reason_empty_after_sanitization_is_rejected(self):
        iid = 50003
        self.add_item(iid)
        resp = self._post_confirmed(iid, reason="   \n\t   ")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["code"], "confirmation_reason_required")
        self.assertEqual(self.beet_calls, [])
        self.assertNotIn(iid, APP._ATTACH_RECORDING_RESERVED_ITEMS)

    def test_reconstruction_exception_sanitized_in_response_and_logs(self):
        iid = 50004
        self.add_item(iid)

        def raising_lookup(path):
            raise RuntimeError("boom Authorization: Bearer recon-exc-secret Cookie: session=recon-cookie-secret")

        with mock.patch.object(APP, "_acoustid_lookup_cached", side_effect=raising_lookup), \
             mock.patch.object(APP.app.logger, "error") as mock_log:
            resp = self._post_safe(iid)

        self.assertEqual(resp.status_code, 503)
        body = resp.get_json()
        self.assertEqual(body["code"], "matching_evidence_unavailable")
        self.assertNotIn("detail", body)
        rendered = json.dumps(body)
        self.assertNotIn("recon-exc-secret", rendered)
        self.assertNotIn("recon-cookie-secret", rendered)

        self.assertTrue(mock_log.called)
        logged = " ".join(str(a) for call in mock_log.call_args_list for a in call.args)
        self.assertNotIn("recon-exc-secret", logged)
        self.assertNotIn("recon-cookie-secret", logged)
        self.assertNotIn(iid, APP._ATTACH_RECORDING_RESERVED_ITEMS)

    def test_stderr_secrets_from_failed_stage_never_leak(self):
        iid = 50005
        self.add_item(iid)
        self.stage_rc[(iid, "modify")] = 1
        resp = self._post_safe(iid)
        audit_id = resp.get_json()["audit_id"]
        _wait_job(resp.get_json()["job_id"])
        tx = APP.transactions.get(audit_id)
        rendered = json.dumps(tx)
        self.assertNotIn("subprocess-stderr-secret", rendered)

    def test_long_password_in_confirmation_reason_redacted_not_truncated_away(self):
        # A prior pass fixed a fail-open bug where the URL-credential regex
        # was length-capped at 256 chars per segment: a password longer than
        # that survived redaction entirely (it wasn't merely truncated by
        # the separate ~500-char confirmation_reason bound -- it was never
        # matched at all). This proves a long password is actually redacted
        # by _redact_security_text before the ~500-char bound in
        # _sanitize_confirmation_reason ever truncates anything.
        iid = 50006
        self.add_item(iid)
        long_password = _unique_secret(350, "longpw")
        reason = f"See https://svcuser:{long_password}@example.test/path for context."
        self.assertLess(len(reason), APP._CONFIRMATION_REASON_MAX_LEN,
                         "test reason must fit under the bound unmodified so truncation isn't a confound")
        resp = self._post_confirmed(iid, reason=reason)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        audit_id = resp.get_json()["audit_id"]
        _wait_job(resp.get_json()["job_id"])
        tx = APP.transactions.get(audit_id)
        stored = tx["metadata"]["confirmation_reason"]
        self.assertNotIn(long_password, stored)
        self.assertIn("example.test", stored)
        self._assert_no_secrets(resp.get_json(), tx, tx.get("logs"), tx.get("changes"), tx.get("metadata"))
        self.assertNotIn(long_password, json.dumps(tx))

    def test_long_username_in_confirmation_reason_redacted(self):
        iid = 50007
        self.add_item(iid)
        long_username = _unique_secret(300, "longuser")
        reason = f"Credential was https://{long_username}:shortpw@example.test/ -- reviewed and reattached."
        resp = self._post_confirmed(iid, reason=reason)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        audit_id = resp.get_json()["audit_id"]
        _wait_job(resp.get_json()["job_id"])
        tx = APP.transactions.get(audit_id)
        stored = tx["metadata"]["confirmation_reason"]
        self.assertNotIn(long_username, stored)
        self.assertNotIn("shortpw", stored)
        self.assertIn("example.test", stored)

    def test_candidate_reconstruction_exception_long_password_not_leaked(self):
        iid = 50008
        self.add_item(iid)
        long_password = _unique_secret(400, "reconpw")

        def raising_lookup(path):
            raise RuntimeError(f"boom https://svcuser:{long_password}@internal.example/api")

        with mock.patch.object(APP, "_acoustid_lookup_cached", side_effect=raising_lookup), \
             mock.patch.object(APP.app.logger, "error") as mock_log:
            resp = self._post_safe(iid)

        self.assertEqual(resp.status_code, 503)
        body = resp.get_json()
        self.assertEqual(body["code"], "matching_evidence_unavailable")
        rendered = json.dumps(body)
        self.assertNotIn(long_password, rendered)

        self.assertTrue(mock_log.called)
        logged = " ".join(str(a) for call in mock_log.call_args_list for a in call.args)
        self.assertNotIn(long_password, logged)

        txs, _total = APP.transactions.list(limit=500)
        self.assertNotIn(long_password, json.dumps(txs))
        self.assertNotIn(iid, APP._ATTACH_RECORDING_RESERVED_ITEMS)


# ── URL-credential redaction: direct unit coverage + adversarial perf ─────────

def _unique_secret(length, tag):
    """A unique, deterministic-enough-for-assertions secret value of an
    exact character length, built from a random UUID rather than a
    predictable literal like "password" -- so a passing assertion proves the
    redactor actually matched this specific generated value, not merely that
    some common keyword vanished. Only uses characters that are legal in
    both the URL username and password grammar exercised here (no '/', '@',
    ':', or whitespace)."""
    base = f"{tag}-{uuid.uuid4().hex}-{uuid.uuid4().hex}"
    if len(base) >= length:
        return base[:length]
    return (base * ((length // len(base)) + 1))[:length]


@unittest.skipIf(APP is None, f"app.py could not be imported: {_APP_IMPORT_ERROR}")
class URLCredentialRedactionUnitTests(unittest.TestCase):
    """Direct coverage of _redact_security_text() / _URL_CREDENTIALS_RE,
    independent of the attach-recording HTTP flow. A prior review pass
    capped the username/password segments at 256 chars each to satisfy a
    CodeQL polynomial-backtracking warning, but that cap made the regex
    fail to match -- and therefore fail to redact -- any credential longer
    than 256 chars. The fix removes the cap; because the username character
    class already excludes ':', the ':' delimiter between username and
    password is unambiguous without a length bound, so removing the cap
    does not reintroduce backtracking risk."""

    def test_short_url_credentials_are_redacted(self):
        secret_user = _unique_secret(8, "u")
        secret_pass = _unique_secret(8, "p")
        text = f"failed against https://{secret_user}:{secret_pass}@example.test/path"
        redacted = APP._redact_security_text(text)
        self.assertNotIn(secret_user, redacted)
        self.assertNotIn(secret_pass, redacted)
        self.assertIn("https://", redacted)
        self.assertIn("example.test", redacted)
        self.assertIn(APP._REDACTED_SECRET, redacted)

    def test_long_username_over_256_chars_is_redacted(self):
        long_username = _unique_secret(300, "user")
        self.assertGreater(len(long_username), 256)
        text = f"https://{long_username}:shortpw@example.test/"
        redacted = APP._redact_security_text(text)
        self.assertNotIn(long_username, redacted)
        self.assertNotIn("shortpw", redacted)
        self.assertIn("example.test", redacted)
        self.assertIn(APP._REDACTED_SECRET, redacted)

    def test_long_password_over_256_chars_is_redacted(self):
        long_password = _unique_secret(400, "pass")
        self.assertGreater(len(long_password), 256)
        text = f"https://shortuser:{long_password}@example.test/"
        redacted = APP._redact_security_text(text)
        self.assertNotIn(long_password, redacted)
        self.assertNotIn("shortuser", redacted)
        self.assertIn("example.test", redacted)
        self.assertIn(APP._REDACTED_SECRET, redacted)

    def test_both_username_and_password_over_256_chars_are_redacted(self):
        long_username = _unique_secret(500, "bothuser")
        long_password = _unique_secret(500, "bothpass")
        text = f"https://{long_username}:{long_password}@example.test/"
        redacted = APP._redact_security_text(text)
        self.assertNotIn(long_username, redacted)
        self.assertNotIn(long_password, redacted)
        self.assertIn("example.test", redacted)

    def test_scheme_and_host_remain_readable_after_redaction(self):
        secret_user = _unique_secret(20, "ru")
        secret_pass = _unique_secret(300, "rp")
        text = f"https://{secret_user}:{secret_pass}@svc.internal.example.test:8443/path?x=1"
        redacted = APP._redact_security_text(text)
        self.assertIn("https://", redacted)
        self.assertIn("svc.internal.example.test", redacted)
        self.assertIn(":8443/path?x=1", redacted)

    def test_redaction_is_idempotent_on_repeated_application(self):
        secret_user = _unique_secret(20, "iu")
        secret_pass = _unique_secret(300, "ip")
        text = f"https://{secret_user}:{secret_pass}@example.test/"
        once = APP._redact_security_text(text)
        twice = APP._redact_security_text(once)
        thrice = APP._redact_security_text(twice)
        self.assertEqual(once, twice)
        self.assertEqual(twice, thrice)
        self.assertNotIn(secret_user, thrice)
        self.assertNotIn(secret_pass, thrice)

    def test_adversarial_colon_heavy_input_completes_quickly(self):
        # Regression guard for the original CodeQL-flagged catastrophic
        # backtracking: an attacker-controlled string with many ':'
        # characters between "://" and a trailing "@" used to risk
        # exponential/polynomial-time matching attempts if username and
        # password segments could both stretch across the same ':'.
        # Because the username class excludes ':', the engine can only ever
        # take the username up to the *first* ':' and must hand everything
        # else to the (':'-permitting) password segment in one pass -- this
        # must stay true, and stay fast, whether or not either segment is
        # length-capped.
        text = "https://" + ("a:" * 50000) + "@example.test/"
        start = time.monotonic()
        redacted = APP._redact_security_text(text)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0,
                         f"redaction of adversarial input took {elapsed:.3f}s -- possible backtracking regression")
        self.assertIn("example.test", redacted)
        self.assertIn(APP._REDACTED_SECRET, redacted)
        self.assertNotIn("a:a:a", redacted)

    def test_adversarial_input_with_no_trailing_at_sign_completes_quickly(self):
        # Same adversarial shape but with NO terminating '@', so the regex
        # engine must give up on the match entirely after scanning -- the
        # worst case for a backtracking-prone pattern. Must still be fast.
        text = "https://" + ("a:" * 50000) + "not-a-credential-suffix"
        start = time.monotonic()
        redacted = APP._redact_security_text(text)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0,
                         f"redaction of unterminated adversarial input took {elapsed:.3f}s -- possible backtracking regression")
        self.assertEqual(redacted, APP._CONTROL_CHAR_RE.sub("?", text))


if __name__ == "__main__":
    unittest.main()
