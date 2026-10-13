"""QA re-check (#381, after 8603dda): adversarial edges for the competing-proof
threshold (``2 * coverage >= audio_files``) and the symlink exclusion.
Synthetic data only; no provider is called."""
import os
import unittest
from pathlib import Path

import backend.import_identity as ii
from backend.matching.folder_identity import could_be_folder_identity, decide_auto_import
from tests.test_import_folder_identity import (
    OTHER_REC, RECS, REL_A, REL_B, RG_A, RG_B, TITLES, _FolderFixture, _hit, _tracks,
)

RG_X = "e1000000-0000-0000-0000-00000000000e"  # never evaluated


class ThresholdPure(unittest.TestCase):
    def test_boundaries(self):
        self.assertFalse(could_be_folder_identity(0, 0))  # zero audio: nothing covers nothing
        self.assertFalse(could_be_folder_identity(0, 4))
        self.assertTrue(could_be_folder_identity(1, 0))
        self.assertTrue(could_be_folder_identity(1, 1))
        self.assertTrue(could_be_folder_identity(1, 2))   # exactly half
        self.assertTrue(could_be_folder_identity(2, 4))   # exactly half
        self.assertFalse(could_be_folder_identity(1, 3))  # odd, under half
        self.assertTrue(could_be_folder_identity(2, 3))   # odd, over half
        self.assertFalse(could_be_folder_identity(2, 5))
        self.assertTrue(could_be_folder_identity(3, 5))

    def test_unknown_unconfirmed_count_means_all_files(self):
        # unconfirmed_files=None: the unevaluated group covers 1 + all 9 -> blocks.
        auto = decide_auto_import([], skip_reason="", audio_count=9, fingerprint_groups={RG_X: 1})
        self.assertIn("fingerprint_group_not_evaluated", auto["reasons"])
        auto = decide_auto_import([], skip_reason="", audio_count=9, fingerprint_groups={RG_X: 1},
                                  unconfirmed_files=0)
        self.assertNotIn("fingerprint_group_not_evaluated", auto["reasons"])

    def test_empty_group_id_is_ignored(self):
        auto = decide_auto_import([], skip_reason="", audio_count=2, fingerprint_groups={"": 2},
                                  unconfirmed_files=0)
        self.assertNotIn("fingerprint_group_not_evaluated", auto["reasons"])


class ThresholdEndToEnd(_FolderFixture):
    def _grow(self, n):
        """Grow the default 3-file folder (all proven RG_A) to n files."""
        for i in range(len(self.paths), n):
            p = self.folder / f"0{i + 1} {TITLES[i]}.flac"
            p.write_bytes(b"synthetic")
            self.paths.append(str(p))
            self.file_hits[str(p)] = [_hit(RECS[i])]
        self.releases[REL_A] = (RG_A, _tracks(n))

    def _competitor_proves(self, k):
        recs = RECS[:k] + [OTHER_REC] * (5 - k)
        self.releases[REL_B] = (RG_B, _tracks(5, recs))
        self.search = [REL_A, REL_B]

    def _eligible(self):
        auto = self.identify()["auto_import"]
        return auto["eligible"], auto["reasons"]

    def test_even_exactly_half_blocks(self):
        self._grow(4)
        self._competitor_proves(2)
        ok, reasons = self._eligible()
        self.assertFalse(ok)
        self.assertIn("other_release_group_has_track_proof", reasons)

    def test_even_under_half_is_eligible(self):
        self._grow(4)
        self._competitor_proves(1)
        ok, reasons = self._eligible()
        self.assertTrue(ok, reasons)

    def test_odd_five_files_boundary(self):
        self._grow(5)
        self._competitor_proves(2)
        ok, reasons = self._eligible()
        self.assertTrue(ok, reasons)
        self._competitor_proves(3)
        ok, reasons = self._eligible()
        self.assertFalse(ok)
        self.assertIn("other_release_group_has_track_proof", reasons)

    def test_single_file_folder_any_competitor_blocks(self):
        self.paths = self.paths[:1]
        for p in list(self.folder.iterdir())[1:]:
            p.unlink()
        self.releases[REL_A] = (RG_A, _tracks(1))
        self._competitor_proves(1)
        ok, reasons = self._eligible()
        self.assertFalse(ok)

    def test_duplicate_hits_count_files_not_hits(self):
        # One file, many hits all naming RG_X: coverage is 1 file of 4, not 4.
        self._grow(4)
        dup = _hit(RECS[0]) | {"mb_releasegroupids": [RG_A, RG_X]}
        self.file_hits[self.paths[0]] = [dup, dict(dup), dict(dup), dict(dup)]
        ok, reasons = self._eligible()
        self.assertTrue(ok, reasons)

    def test_duplicate_hits_over_half_of_files_blocks(self):
        self._grow(4)
        for i in (0, 1):
            dup = _hit(RECS[i]) | {"mb_releasegroupids": [RG_A, RG_X]}
            self.file_hits[self.paths[i]] = [dup, dict(dup)]
        ok, reasons = self._eligible()
        self.assertFalse(ok)
        self.assertIn("fingerprint_group_not_evaluated", reasons)

    def test_unconfirmed_files_count_toward_unevaluated_group(self):
        # 4 files; file 4 has no fingerprint but an embedded Recording ID proves it.
        # RG_X on 1 confirmed file + 1 unconfirmed file = 2 of 4 -> blocks.
        self._grow(4)
        self.file_hits[self.paths[3]] = []
        self.embedded = {self.paths[3]: {"mb_trackid": RECS[3]}}
        self.file_hits[self.paths[0]] = [_hit(RECS[0]) | {"mb_releasegroupids": [RG_A, RG_X]}]
        ok, reasons = self._eligible()
        self.assertFalse(ok)
        self.assertIn("fingerprint_group_not_evaluated", reasons)

    def test_zero_audio_never_eligible(self):
        self.paths = []
        for p in self.folder.iterdir():
            p.unlink()
        ok, reasons = self._eligible()
        self.assertFalse(ok)


class SymlinkEdges(_FolderFixture):
    def _link(self, link: Path, target: Path, is_dir=False):
        try:
            os.symlink(target, link, target_is_directory=is_dir)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported here")

    def test_symlink_to_file_inside_folder_blocks(self):
        self._link(self.folder / "04 Copy.flac", Path(self.paths[0]))
        files, linked = ii._recursive_audio(str(self.folder))
        self.assertTrue(linked)
        self.assertEqual(len(files), 3)
        self.assertNotEligible(self.identify(), "folder_conflict:symlinked_audio_file")

    def test_nested_symlinked_file_blocks(self):
        outside = Path(self.tmp.name) / "elsewhere.flac"
        outside.write_bytes(b"synthetic")
        (self.folder / "CD2").mkdir()
        self._link(self.folder / "CD2" / "01 X.flac", outside)
        self.assertNotEligible(self.identify(), "folder_conflict:symlinked_audio_file")

    def test_symlinked_directory_with_audio_blocks(self):
        # Beets' importer walk (beets.util.sorted_walk) follows symlinked
        # directories, so a pinned import would pick up these files unproven.
        outside = Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        (outside / "01 Unproven.flac").write_bytes(b"synthetic")
        self._link(self.folder / "Disc 2", outside, is_dir=True)
        files, linked = ii._recursive_audio(str(self.folder))
        self.assertEqual(len(files), 3)
        self.assertTrue(linked, "audio under a symlinked directory is not flagged")
        self.assertNotEligible(self.identify(), "folder_conflict:symlinked_audio_file")

    def _outside_with_audio(self, rel="01 Unproven.flac"):
        outside = Path(self.tmp.name) / "elsewhere"
        target = outside / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"synthetic")
        return outside

    def test_deeply_nested_linked_dir_blocks(self):
        deep = self.folder / "a" / "b" / "c"
        deep.mkdir(parents=True)
        self._link(deep / "Disc 2", self._outside_with_audio(), is_dir=True)
        files, linked = ii._recursive_audio(str(self.folder))
        self.assertEqual((len(files), linked), (3, True))
        self.assertNotEligible(self.identify(), "folder_conflict:symlinked_audio_file")

    def test_linked_dir_with_audio_only_in_plain_subdir_blocks(self):
        outside = self._outside_with_audio("sub/deeper/01 Unproven.flac")
        self._link(self.folder / "Extras", outside, is_dir=True)
        self.assertNotEligible(self.identify(), "folder_conflict:symlinked_audio_file")

    def test_linked_dir_chain_blocks(self):
        # link -> plain dir containing another link -> audio
        outside = Path(self.tmp.name) / "hop1"
        outside.mkdir()
        self._link(outside / "hop2", self._outside_with_audio(), is_dir=True)
        self._link(self.folder / "Extras", outside, is_dir=True)
        self.assertNotEligible(self.identify(), "folder_conflict:symlinked_audio_file")

    def test_dangling_audio_symlink_blocks_and_does_not_crash(self):
        self._link(self.folder / "04 Gone.flac", Path(self.tmp.name) / "missing.flac")
        files, linked = ii._recursive_audio(str(self.folder))
        self.assertEqual((len(files), linked), (3, True))
        self.assertNotEligible(self.identify(), "folder_conflict:symlinked_audio_file")

    def test_dangling_dir_symlink_does_not_block(self):
        self._link(self.folder / "Gone", Path(self.tmp.name) / "missing_dir", is_dir=True)
        files, linked = ii._recursive_audio(str(self.folder))
        self.assertEqual((len(files), linked), (3, False))
        auto = self.identify()["auto_import"]
        self.assertTrue(auto["eligible"], auto)

    def test_link_back_into_folder_subdir_blocks_and_terminates(self):
        sub = self.folder / "CD2"
        sub.mkdir()
        (sub / "01 X.flac").write_bytes(b"synthetic")
        self._link(sub / "up", self.folder, is_dir=True)          # cycle to root
        self._link(self.folder / "again", sub, is_dir=True)       # sibling alias
        files, linked = ii._recursive_audio(str(self.folder))
        self.assertTrue(linked)
        self.assertEqual(len(files), 4)  # real files counted once, none via links

    def test_symlinked_directory_without_audio_does_not_block(self):
        outside = Path(self.tmp.name) / "art"
        outside.mkdir()
        (outside / "cover.jpg").write_bytes(b"synthetic")
        self._link(self.folder / "Scans", outside, is_dir=True)
        auto = self.identify()["auto_import"]
        self.assertTrue(auto["eligible"], auto)


if __name__ == "__main__":
    unittest.main()
