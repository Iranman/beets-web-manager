"""QA edge cases for #332 / PR #334 (preserved torrent source relocation)."""

import os
import unittest

import backend.composite_workflows as cw
import beetsplug.webmanager.operations as ops_mod
from tests.test_album_relocation_torrent_source import TorrentSourceRelocationTests, exdev
from tests.test_album_relocation_transaction import tree


class TorrentRelocationEdgeTests(TorrentSourceRelocationTests):
    # Run only the QA tests below, not the inherited ones again.
    def _plugin_rollback(self, op, made, key):
        items = [{"id": int(k), "path": made[0][k], "restore_path": self.old[0][k]} for k in made[0]]
        body = {"album_id": self.album.id, "items": items, "artpath": made[1], "restore_artpath": self.old[1],
                "apply_operation_id": op}
        return self.client.post("/webmanager/album-relocation/rollback", json=body,
                                headers={"Authorization": f"Bearer {self.token}", "Idempotency-Key": key})

    def test_qa_web_manager_modify_tag_write_breaks_link_and_rolls_back(self):
        op = self.relocate()["operation_id"]
        iid = min(self.old[0])
        r = self.client.post("/webmanager/modify", json={"item_ids": [iid], "fields": {"title": "Edited"},
                                                          "write": True, "move": False},
                             headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assert_originals_untouched()
        self.assertEqual(os.stat(self.old[0][iid]).st_nlink, 1)
        self.rolled_back(op)

    def test_qa_partial_album_copy_fallback_and_reapply(self):
        # A copy fallback, a rollback, then a fresh re-apply and a second rollback.
        from unittest import mock
        from beets import util
        with mock.patch.object(util, "hardlink", side_effect=exdev):
            op1 = self.relocate()["operation_id"]
        self.rolled_back(op1)
        op2 = self.relocate()["operation_id"]
        self.assertNotEqual(op1, op2)
        self.rolled_back(op2)

    def test_qa_second_rollback_changes_nothing_and_lists_no_phantom_files(self):
        op = self.relocate()["operation_id"]
        made = self.beets_state(self.album.id)
        r1 = self._plugin_rollback(op, made, "qa-rb-1")
        self.assertEqual(r1.status_code, 200, r1.get_json())
        self.assertEqual(self.beets_state(self.album.id), self.old)
        self.assertEqual(tree(self.music), {})
        r2 = self._plugin_rollback(op, made, "qa-rb-2")  # a second rollback with a new key
        body = r2.get_json()
        self.assertEqual(r2.status_code, 200, body)
        self.assert_originals_untouched()
        self.assertEqual(self.beets_state(self.album.id), self.old)
        # Files already gone are "not an error" per the docs; they should not be reported as kept.
        self.assertEqual(body.get("kept_library_files"), [], body)

    def test_qa_tag_write_after_beets_restart_keeps_rollback_usable(self):
        # A Beets restart: the registry is unbound until a folder-op/relocation request binds it.
        op = self.relocate()["operation_id"]
        saved = dict(ops_mod._operations)
        durable = ops_mod._durable_file
        with ops_mod._operations_lock:
            ops_mod._operations.clear()
        ops_mod._durable_file = None
        try:
            iid = min(self.old[0])
            r = self.client.post("/webmanager/modify", json={"item_ids": [iid], "fields": {"title": "Edited"},
                                                              "write": True, "move": False},
                                 headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(r.status_code, 200, r.get_json())
            self.assert_originals_untouched()
        finally:
            if ops_mod._durable_file is None:
                ops_mod._durable_file = None  # let the rollback's own bind reload from disk
        self.assertTrue(durable and saved)
        rb = cw.rollback_album_relocation(op, adapter=self.ad, store=self.store)
        self.assertEqual((rb["ok"], rb.get("status")), (True, "Rolled Back"), rb)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(TorrentRelocationEdgeTests):
        if name.startswith("test_qa_"):
            suite.addTest(TorrentRelocationEdgeTests(name))
    return suite


if __name__ == "__main__":
    unittest.main()
