"""#555 item 1: an album attach records a repair row, a relocation row and an
outer audit row. Only the audit row owns Undo: the two step rows refuse a
direct rollback and point at it, and the audit's Undo reverses the whole
attach (relocation, then repair) exactly once.

Temp stores and fake adapters only; no real library."""

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


class AlbumAttachUndoOwnerTests(_RouteEnv):
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
        self.audit, self.repair, self.relocation = self._attach()

    def _attach(self):
        """What the album attach-recording job does, with this audit row as owner."""
        audit = self.store.create(operation_type="MusicBrainz Match", status="Running")["id"]
        plan = cw.plan_album_mb_track_repair(
            {"album_id": 1, "track_mbids": {"11": REC_1}, cw.UNDO_OWNER_KEY: audit}, store=self.store)
        repair = plan["operation_id"]
        self.assertTrue(cw.apply_album_mb_track_repair(repair, store=self.store)["ok"])
        rel = cw.relocate_album(1, mode="rename", store=self.store, undo_owner=audit)
        self.assertTrue(rel["ok"], rel)
        self.store.update(audit, status="Completed", metadata={"engine_result": {"mutated": True}}, rollback={
            "available": True, "operations": [{"type": "album_attach_undo", "item_id": 11,
                                               "repair_operation_id": repair,
                                               "relocation_operation_id": rel["operation_id"]}]})
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
