"""QA-N1: an album with a Release ID but no Release Group (the canonical
album identity) is listed in Library > Missing IDs, and the batch fills the
Release Group only from MusicBrainz's own release -> release-group lookup
of that Release ID. Every other case is refused with a reason.
"""
import tempfile
import unittest
from unittest import mock

import backend.composite_workflows as cw
import backend.missing_mb_batch as mb
from backend.transaction_engine import TransactionStore

try:
    import test_missing_mb_batch as base
except ImportError:  # pragma: no cover
    import tests.test_missing_mb_batch as base

RG, REL, RG2 = base.RG, base.REL, base.RG2
LOOKUP = "backend.missing_mb_batch.composite_workflows._release_group_for_release"


class ListingTests(unittest.TestCase):
    def test_release_id_without_release_group_is_listed_with_its_reason(self):
        ad = mock.Mock()
        ad.get_items.return_value = [{"id": n * 10, "album_id": n, "path": f"A/{n}/01.flac"} for n in (1, 2, 3)]
        ad.get_albums.return_value = [
            {"id": 1, "mb_albumid": "", "mb_releasegroupid": ""},
            {"id": 2, "mb_albumid": REL, "mb_releasegroupid": ""},
            {"id": 3, "mb_albumid": REL, "mb_releasegroupid": RG},
        ]
        albums = cw.get_unmatched_review_items(adapter=ad, limit=0)["albums"]
        self.assertEqual([a["id"] for a in albums], [1, 2])

        from backend.library_service import _missing_mb_album_row
        rows = {a["id"]: _missing_mb_album_row(a) for a in albums}
        self.assertEqual((rows[2]["missing_reason"], rows[2]["missing_id_type"], rows[2]["current_mb_albumid"]),
                         ("missing_release_group", "Release Group ID", REL))
        self.assertEqual((rows[1]["missing_reason"], rows[1]["missing_id_type"]),
                         ("missing_release_id", "Release ID"))


class _FillCase(base._Case):
    def setUp(self):
        self.w = base._World(self)
        self.lookup = self.patch(LOOKUP, return_value=RG)

    def album(self, aid=1, **item_fields):
        row = self.w.add_album(aid)
        self.w.albums[aid].mb_albumid = REL
        for it in self.w.album_items[aid]:
            it.update(item_fields)
        return row


class ProofTests(_FillCase):
    def test_release_group_from_musicbrainz_is_proven_and_only_it_is_written(self):
        proof = mb.prove_row(self.album(mb_albumid=REL))
        self.assertTrue(proof["proven"], proof["reasons"])
        self.assertEqual(proof["write"], {"mb_releasegroupid": RG})
        self.lookup.assert_called_once_with(REL)
        self.w.ai_key.assert_not_called()
        # The fill needs no folder identification (no AcoustID/fingerprint run).
        self.assertEqual(proof["evidence"]["sources"], ["musicbrainz_release_lookup"])

    def test_refusals(self):
        cases = {
            "release_group_not_resolved": ({}, ""),
            "embedded_release_group_conflict": ({"mb_releasegroupid": RG2}, RG),
            "item_release_id_conflict": ({"mb_albumid": base.REC}, RG),
        }
        for reason, (fields, looked_up) in cases.items():
            with self.subTest(reason):
                self.w.rows.clear()
                self.lookup.return_value = looked_up
                proof = mb.prove_row(self.album(**fields))
                self.assertFalse(proof["proven"])
                self.assertEqual((proof["reasons"], proof["write"]), ([reason], {}))

    def test_release_group_held_by_another_album_is_a_duplicate(self):
        self.w.add_album(2)
        self.w.albums[2].mb_releasegroupid = RG
        proof = mb.prove_row(self.album(1))
        self.assertEqual(proof["reasons"], ["duplicate_release_group"])

    def test_album_in_import_review_is_refused(self):
        row = self.album()
        row["in_import_review"] = True
        self.assertEqual(mb.prove_row(row)["reasons"], ["in_import_review"])
        self.lookup.assert_not_called()


class ApplyTests(_FillCase):
    def setUp(self):
        super().setUp()
        self.store = TransactionStore(tempfile.mkdtemp(prefix="rgfill_"))

    def write(self, aid, proof, log):
        self.assertEqual(proof["write"], {"mb_releasegroupid": RG})
        self.w.albums[aid].mb_releasegroupid = proof["write"]["mb_releasegroupid"]
        return {"changed": True, "transaction_ids": ["txn"]}

    def test_preview_confirm_apply_reproves_and_verifies(self):
        self.album()
        preview = mb.run_preview(list(self.w.rows), [], store=self.store)
        self.assertEqual(preview["counts"]["proven"], 1)
        err, _ = mb.check_apply_request(preview["transaction_id"], "wrong", store=self.store)
        self.assertEqual(err["code"], "confirm_token_mismatch")
        res = mb.run_apply(preview["transaction_id"], apply_album=self.write, apply_item=None, log=[],
                           store=self.store)
        self.assertEqual([r["status"] for r in res["rows"]], ["written"])
        self.assertEqual(self.w.albums[1].mb_albumid, REL)  # the Release ID is never changed
        self.assertEqual(self.lookup.call_count, 2)  # proven again at apply

    def test_lookup_changing_after_preview_writes_nothing(self):
        self.album()
        preview = mb.run_preview(list(self.w.rows), [], store=self.store)
        self.lookup.return_value = RG2
        res = mb.run_apply(preview["transaction_id"], apply_album=self.write, apply_item=None, log=[],
                           store=self.store)
        self.assertEqual([r["status"] for r in res["rows"]], ["evidence_changed"])
        self.assertEqual(self.w.albums[1].mb_releasegroupid, "")


class RouteWriterTests(unittest.TestCase):
    def test_fill_uses_the_metadata_writer_never_match_album(self):
        import routes_library as rl
        with mock.patch.object(rl, "_match_album_run", side_effect=AssertionError("Match album used")), \
                mock.patch.object(rl.composite_workflows, "plan_album_metadata",
                                  return_value={"ok": True, "operation_id": "op1"}) as plan, \
                mock.patch.object(rl.composite_workflows, "apply_album_metadata",
                                  return_value={"ok": True}) as apply:
            out = rl._batch_write_album(7, {"write": {"mb_releasegroupid": RG}}, [])
        plan.assert_called_once_with({"album_id": 7, "updates": {"mb_releasegroupid": RG}})
        apply.assert_called_once_with("op1")
        self.assertEqual(out, {"changed": True, "transaction_ids": ["op1"]})

    def test_writer_refusals(self):
        import routes_library as rl
        with mock.patch.object(rl.composite_workflows, "plan_album_metadata",
                               return_value={"ok": False, "code": "repair_identity_mismatch"}):
            with self.assertRaises(mb.NotWritten):
                rl._batch_write_album(7, {"write": {"mb_releasegroupid": RG}}, [])
        with mock.patch.object(rl.composite_workflows, "plan_album_metadata",
                               return_value={"ok": True, "operation_id": "op1"}), \
                mock.patch.object(rl.composite_workflows, "apply_album_metadata",
                                  return_value={"ok": False, "mutated": True, "code": "metadata_apply_failed"}):
            with self.assertRaises(RuntimeError) as ctx:
                rl._batch_write_album(7, {"write": {"mb_releasegroupid": RG}}, [])
            self.assertNotIsInstance(ctx.exception, mb.NotWritten)


if __name__ == "__main__":
    unittest.main()
