"""#555 item 1: an album attach records a repair row, a relocation row and an
outer audit row. Only the audit row owns Undo: the two step rows refuse a
direct rollback and point at it, and the audit's Undo reverses the whole
attach (relocation, then repair) exactly once.

Temp stores and fake adapters only; no real library."""

import contextlib
import os
from unittest import mock

import backend.composite_workflows as cw
from tests.test_mutation_identity_safety import REC_1, REL_A, RG_A, RG_OF, MutAdapter
from tests.test_staging_mutation_hardening import _RouteEnv


class RelocatingAdapter(MutAdapter):
    """Fake Beets whose album move and move-back change the fake rows' paths."""

    def relocate_album(self, aid, items, idempotency_key=None, **_kw):
        before = {k: self.items[int(k)]["path"] for k in items}
        after = {k: p.replace(os.sep + "A" + os.sep, os.sep + "Renamed" + os.sep) for k, p in before.items()}
        for k, p in after.items():
            self.items[int(k)]["path"] = p
        self.calls.append(("relocate", aid))
        return {"before": {"items": before, "artpath": ""}, "after": {"items": after, "artpath": ""}}

    def rollback_album_relocation(self, aid, items, after_art, before_art, **_kw):
        for it in items:
            self.items[int(it["id"])]["path"] = it["restore_path"]
        self.calls.append(("relocate_back", aid))
        return {"restored_items": len(items)}


class _AttachEnv(_RouteEnv):
    AUTO_ATTACH = True

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(cw, "_release_group_for_release", side_effect=lambda rel: RG_OF.get(rel, "")),
                  mock.patch.object(cw, "_preserved_torrent_file", return_value=False)):
            p.start()
            self.addCleanup(p.stop)
        self.path = self.media("A/1.flac")
        self.ad = RelocatingAdapter(
            albums={1: {"id": 1, "album": "Old Title", "albumartist": "A", "mb_albumid": REL_A,
                        "mb_releasegroupid": RG_A}},
            items={11: {"id": 11, "album_id": 1, "title": "Old Track", "mb_trackid": "", "path": self.path}})
        p = mock.patch.object(cw, "beets_adapter", self.ad)
        p.start()
        self.addCleanup(p.stop)
        import routes_maintenance
        self.rm = routes_maintenance
        self.before = ({k: dict(v) for k, v in self.ad.albums.items()}, {k: dict(v) for k, v in self.ad.items.items()})
        if self.AUTO_ATTACH:
            self.audit, self.repair, self.relocation = self._attach()

    def _attach(self, *, relocate_error=None, record_relocation=True):
        """What the album attach-recording job does (routes_library
        _item_attach_recording), with this audit row as owner. Returns
        (audit, repair, relocation or None)."""
        audit = self.store.create(operation_type="MusicBrainz Match", status="Running")["id"]
        undo = {"type": "album_attach_undo", "item_id": 11}

        def record():
            self.store.update(audit, rollback={"available": True, "operations": [dict(undo)]})

        plan = cw.plan_album_mb_track_repair(
            {"album_id": 1, "track_mbids": {"11": REC_1}, cw.UNDO_OWNER_KEY: audit}, store=self.store)
        repair = plan["operation_id"]
        undo["repair_operation_id"] = repair
        record()
        self.assertTrue(cw.apply_album_mb_track_repair(repair, store=self.store)["ok"])
        self.store.update(audit, metadata={"engine_result": {"mutated": True}})

        def on_planned(op_id):
            if record_relocation:  # False: a crash lost the audit update
                undo["relocation_operation_id"] = op_id
                record()

        patcher = (mock.patch.object(self.ad, "relocate_album", side_effect=relocate_error)
                   if relocate_error else contextlib.nullcontext())
        with patcher:
            try:
                rel = cw.relocate_album(1, mode="rename", store=self.store, undo_owner=audit,
                                        on_planned=on_planned)
            except cw.BeetsAdapterError:
                self.store.update(audit, status="Failed")
                return audit, repair, None
        self.assertTrue(rel["ok"], rel)
        self.store.update(audit, status="Completed")
        # The attach really changed the album: metadata and path.
        self.assertEqual((self.ad.items[11]["mb_trackid"], self.ad.items[11]["title"]), (REC_1, "Synced Track"))
        self.assertNotEqual(self.ad.items[11]["path"], self.path)
        return audit, repair, rel["operation_id"]

    def _undo(self, tx_id):
        resp = self.client.post(f"/api/transactions/{tx_id}/rollback")
        if resp.status_code == 200 and resp.get_json().get("job_id"):
            from backend.app_runtime import jobs
            from tests._job_threads import join_jobs
            join_jobs(jobs)
        return resp



class AlbumAttachUndoOwnerTests(_AttachEnv):
    def test_child_rows_refuse_undo_and_point_at_the_audit_row(self):
        for child in (self.repair, self.relocation):
            tx = self.store.get(child)
            self.assertEqual(tx["metadata"][cw.UNDO_OWNER_KEY], self.audit)
            self.assertIn(self.audit, tx["rollback"]["reason"])
            self.assertFalse(tx["rollback"]["available"])
            verdict = self.rm.rollback_eligibility(tx)
            self.assertEqual((verdict["allowed"], verdict["code"]), (False, "undo_owned_elsewhere"), child)
            self.assertIn(self.audit, verdict["reason"])
            resp = self._undo(child)
            self.assertEqual(resp.status_code, 409, resp.get_json())
            body = resp.get_json()
            self.assertEqual((body["code"], body["undo_transaction_id"]), ("undo_owned_elsewhere", self.audit))
            self.assertEqual(self.store.get(child)["status"], "Completed")
        # Nothing was restored by the refused attempts.
        self.assertEqual(self.ad.items[11]["mb_trackid"], REC_1)
        self.assertNotEqual(self.ad.items[11]["path"], self.path)
        self.assertNotIn(("relocate_back", 1), self.ad.calls)

    def test_audit_row_undo_restores_the_full_before_state(self):
        self.assertTrue(self.rm.rollback_eligibility(self.store.get(self.audit))["allowed"])
        start = len(self.ad.calls)
        resp = self._undo(self.audit)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(self.store.get(self.audit)["status"], "Rolled Back", self.store.get(self.audit)["logs"])
        self.assertEqual((self.ad.albums, self.ad.items), self.before)
        self.assertEqual(self.store.get(self.repair)["status"], "Rolled Back")
        self.assertEqual(self.store.get(self.relocation)["status"], "Rolled Back")
        # Relocation is moved back before the repair is restored.
        order = [c[0] for c in self.ad.calls[start:]]
        self.assertEqual(order[0], "relocate_back", self.ad.calls)

    def test_second_undo_is_refused(self):
        self.assertEqual(self._undo(self.audit).status_code, 200)
        calls = len(self.ad.calls)
        resp = self._undo(self.audit)
        self.assertEqual(resp.status_code, 409, resp.get_json())
        self.assertEqual(len(self.ad.calls), calls)
        self.assertEqual(self.store.get(self.audit)["status"], "Rolled Back")
        verdict = self.rm.rollback_eligibility(self.store.get(self.audit))
        self.assertFalse(verdict["allowed"])

    def test_undo_stops_before_the_repair_when_the_move_back_fails(self):
        with mock.patch.object(self.ad, "rollback_album_relocation",
                               side_effect=cw.BeetsAdapterError("x", status_code=409, error_code="ALBUM_CHANGED")):
            self._undo(self.audit)
        self.assertNotEqual(self.store.get(self.audit)["status"], "Rolled Back")
        self.assertEqual(self.store.get(self.repair)["status"], "Completed")
        self.assertEqual(self.ad.items[11]["mb_trackid"], REC_1)


class AttachCrashWindowTests(_AttachEnv):
    """#555 QA of #677 edge (b): a move step that never reached the audit
    row's op is still found by its owner key and undone first."""
    AUTO_ATTACH = False

    def test_relocation_id_is_on_the_audit_row_before_beets_moves(self):
        seen = {}
        real = self.ad.relocate_album

        def relocate(*a, **kw):
            ops = self.store.get(self.current_audit)["rollback"]["operations"]
            seen["relocation"] = ops[0].get("relocation_operation_id")
            return real(*a, **kw)

        real_create = self.store.create

        def create(**kw):
            tx = real_create(**kw)
            if kw.get("operation_type") == "MusicBrainz Match" and kw.get("status") == "Running":
                self.current_audit = tx["id"]
            return tx

        with mock.patch.object(self.store, "create", side_effect=create),                 mock.patch.object(self.ad, "relocate_album", side_effect=relocate):
            _audit, _repair, relocation = self._attach()
        self.assertEqual(seen["relocation"], relocation)

    def test_relocate_raising_after_the_plan_refuses_undo_and_keeps_the_repair(self):
        for record in (True, False):
            with self.subTest(recorded_on_audit=record):
                self.ad.items[11].update(self.before[1][11])
                self.ad.albums[1].update(self.before[0][1])
                audit, repair, _ = self._attach(relocate_error=cw.BeetsAdapterConnectionError("down"),
                                                record_relocation=record)
                owned = cw._owned_steps(self.store, audit)[cw.ALBUM_RELOCATION_FAMILY]
                self.assertEqual(self.store.get(owned[0])["status"], "Recovery Required")
                self.assertTrue(self.rm.rollback_eligibility(self.store.get(audit))["allowed"])
                self._undo(audit)
                self.assertNotEqual(self.store.get(audit)["status"], "Rolled Back")
                self.assertIn("relocation_outcome_unknown", " ".join(self.store.get(audit)["logs"]))
                self.assertEqual(self.store.get(repair)["status"], "Completed")
                self.assertEqual(self.ad.items[11]["mb_trackid"], REC_1)

    def test_crash_after_the_move_before_the_audit_update_still_moves_back_first(self):
        audit, repair, relocation = self._attach(record_relocation=False)
        op = self.store.get(audit)["rollback"]["operations"][0]
        self.assertNotIn("relocation_operation_id", op)
        start = len(self.ad.calls)
        self.assertEqual(self._undo(audit).status_code, 200)
        self.assertEqual(self.store.get(audit)["status"], "Rolled Back", self.store.get(audit)["logs"])
        self.assertEqual((self.ad.albums, self.ad.items), self.before)
        self.assertEqual(self.store.get(relocation)["status"], "Rolled Back")
        self.assertEqual(self.store.get(repair)["status"], "Rolled Back")
        self.assertEqual(self.ad.calls[start][0], "relocate_back", self.ad.calls)
