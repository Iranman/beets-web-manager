"""ARCH-032 (QA #401 N-d): rolling back the Missing IDs Release Group fill
must restore each item's OWN captured value, not the album's.

The fill writes ``mb_releasegroupid`` on an album that has a Release ID but no
Release Group, with tag writes. A Beets album write with tags rewrites every
item's tags, so a rollback that restored only the album's captured value
(empty) would blank an item whose file tag already carried the same Release
Group. The before-state now captures each album item's own value, so the
rollback puts it back.

Fails before the fix (the item is blanked), passes after. Temp store and a
fake adapter only.
"""
import unittest
from unittest import mock

import backend.composite_workflows as cw

try:
    from test_mutation_identity_safety import MutAdapter, _IdentityEnv, REL_A, RG_A
except ImportError:  # pragma: no cover
    from tests.test_mutation_identity_safety import MutAdapter, _IdentityEnv, REL_A, RG_A


class _PropagatingAdapter(MutAdapter):
    """Models Beets: an album modify propagates the written album fields onto
    every item of that album (the DB row and, with ``write``, the file tag)."""

    def modify(self, fields, item_ids=None, album_ids=None, write=True, move=False, **kw):
        res = super().modify(fields, item_ids=item_ids, album_ids=album_ids, write=write, move=move, **kw)
        for aid in album_ids or []:
            for it in self.find_all_items_by_album_id(int(aid)):
                it.update(fields)
        return res


class ReleaseGroupFillRollbackTests(_IdentityEnv):
    def adapter(self, rel=REL_A, rg=RG_A):
        # Album has the Release ID but no Release Group; the item's file tag
        # already carried the same Release Group (mirrored into its row).
        return _PropagatingAdapter(
            albums={1: {"id": 1, "album": "A", "albumartist": "A", "mb_albumid": rel, "mb_releasegroupid": ""}},
            items={11: {"id": 11, "album_id": 1, "title": "t", "mb_albumid": rel,
                        "mb_releasegroupid": rg, "path": self.media("A/1.flac")}})

    def test_rollback_restores_each_items_own_release_group_not_the_albums(self):
        ad = self.adapter()
        plan = cw.plan_album_metadata({"album_id": 1, "updates": {"mb_releasegroupid": RG_A}},
                                      adapter=ad, store=self.store)
        self.assertTrue(plan["ok"], plan)
        op = plan["operation_id"]
        self.assertTrue(cw.apply_album_metadata(op, adapter=ad, store=self.store)["ok"])
        # The fill set the album's Release Group; the item kept the one it had.
        self.assertEqual(ad.albums[1]["mb_releasegroupid"], RG_A)
        self.assertEqual(ad.items[11]["mb_releasegroupid"], RG_A)

        rolled = cw.rollback_album_metadata(op, adapter=ad, store=self.store)
        self.assertEqual(rolled["status"], "Rolled Back", rolled)
        # The album's captured value (empty) is restored, and the item keeps
        # its own Release Group instead of being blanked (ARCH-032).
        self.assertEqual(ad.albums[1]["mb_releasegroupid"], "")
        self.assertEqual(ad.items[11]["mb_releasegroupid"], RG_A)


if __name__ == "__main__":
    unittest.main()
