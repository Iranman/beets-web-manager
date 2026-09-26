"""ARCH-009: Release Group ID is canonical album identity for duplicate-album
merges; Release ID is edition evidence and never substitutes for it."""

import unittest

import app as app_module

RG = "11111111-1111-1111-1111-111111111111"
RG2 = "22222222-2222-2222-2222-222222222222"
REL_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
REL_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _row(album_id, mb_albumid, rgid):
    return {"id": album_id, "album": "Album", "albumartist": "Artist",
            "mb_albumid": mb_albumid, "mb_releasegroupid": rgid, "track_count": 2}


def _items(album_id, tracks):
    return [{"id": album_id * 100 + t, "album_id": album_id, "disc": 1, "track": t} for t in tracks]


class MergeIdentityTests(unittest.TestCase):
    def _safety(self, rows):
        items = {int(r["id"]): _items(int(r["id"]), [1, 2] if i == 0 else [3, 4]) for i, r in enumerate(rows)}
        return app_module._library_duplicate_merge_safety(rows, items)

    def test_same_release_group_different_editions_is_mergeable(self):
        self.assertTrue(self._safety([_row(1, REL_A, RG), _row(2, REL_B, RG)])["merge_safe"])

    def test_same_release_group_missing_release_id_is_mergeable(self):
        self.assertTrue(self._safety([_row(1, REL_A, RG), _row(2, "", RG)])["merge_safe"])

    def test_different_release_groups_block(self):
        self.assertFalse(self._safety([_row(1, REL_A, RG), _row(2, REL_B, RG2)])["merge_safe"])

    def test_unknown_release_group_never_inherits_the_other_rows(self):
        result = self._safety([_row(1, REL_A, RG), _row(2, REL_B, "")])
        self.assertFalse(result["merge_safe"])

    def test_same_concrete_release_proves_identity_without_rgid(self):
        self.assertTrue(self._safety([_row(1, REL_A, ""), _row(2, REL_A, "")])["merge_safe"])

    def test_different_releases_without_any_rgid_block(self):
        self.assertFalse(self._safety([_row(1, REL_A, ""), _row(2, REL_B, "")])["merge_safe"])


if __name__ == "__main__":
    unittest.main()
