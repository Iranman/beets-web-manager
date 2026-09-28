"""Which copy unattended duplicate cleanup keeps, and the album-slot gate.

Keeper order (backend.duplicate_identity.keeper_rank):
1. attached to the album row over a loose/singleton copy;
2. valid canonical metadata (Recording ID, Release Group ID, release ID, disc/track);
3. embedded Recording ID agrees with AcoustID;
4. canonical Beets path over a duplicate/decorated filename;
5. quality (lossless, bitrate, sample rate, bit depth; size only within one format);
6. lowest item id -- final tie-breaker only.
Gate: never delete a copy attached to an album row unless the keeper is a
tracked item in that same album row.
"""

import tempfile
import unittest
from pathlib import Path

from backend.duplicate_identity import plan_unattended_cleanup, select_unattended_cleanup_paths

REC = "11111111-1111-1111-1111-111111111111"
REL = "22222222-2222-2222-2222-222222222222"
RG = "33333333-3333-3333-3333-333333333333"


def meta(**kw):
    base = {"album_id": None, "recording_id": "", "release_id": REL, "releasegroup_id": "",
            "disc": 1, "track": 11, "format": "FLAC", "bitrate": 900000, "samplerate": 44100, "bitdepth": 16}
    base.update(kw)
    return base


class KeeperPolicyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _file(self, name, size=10):
        path = self.root / name
        path.write_bytes(b"x" * size)
        return path

    def _dup(self, src, src_id, src_meta, lib, lib_id, lib_meta, relation="same_release_position"):
        return {
            "source_path": str(src), "lib_path": str(lib), "source_item_id": src_id, "lib_id": lib_id,
            "source_album_id": src_meta.get("album_id"), "lib_album_id": lib_meta.get("album_id"),
            "source_meta": src_meta, "lib_meta": lib_meta,
            "source_fingerprint_ids": [REC], "lib_fingerprint_ids": [REC], "fingerprint_mbid": REC,
            "release_relation": relation, "match_type": "fuzzy match 100%", "confidence": "high",
            "fingerprint_verified": True,
        }

    def _plan(self, *dups):
        return plan_unattended_cleanup({"duplicates": list(dups)}, self.root, lambda p, r: True)

    def test_bossman_shape_keeps_the_album_copy_and_deletes_the_loose_single(self):
        """The failure the live run exposed: the album copy (higher id, tagged)
        must be kept; the loose single (lower id, no Recording ID) goes."""
        album_copy = self._file("BossMan Dlow - 2 Slippery - 11 - Top Notch.1.flac", 12)
        single = self._file("bossman dlow - 2 Slippery - 11 - top notch (00).flac", 13)
        [decision] = self._plan(self._dup(
            album_copy, 25264, meta(album_id=1935, recording_id=REC, disc=1),
            single, 22577, meta(album_id=None, recording_id="", disc=0),
        ))
        self.assertEqual(decision["keep"]["path"], str(album_copy.resolve()))
        self.assertEqual(decision["delete"]["path"], str(single.resolve()))
        self.assertIn("album slot", decision["keep_reason"])

    def test_album_slot_gate_never_empties_an_album_row(self):
        """Both copies attached to different album rows: deleting either would
        leave its row's slot without a tracked item, so nothing is deleted."""
        a = self._file("A - 11 - Song.flac")
        b = self._file("B - 11 - Song.flac")
        plan = self._plan(self._dup(a, 30, meta(album_id=7, recording_id=REC), b, 20, meta(album_id=8, recording_id=REC)))
        self.assertEqual(plan, [])

    def test_same_album_row_duplicate_is_still_cleaned(self):
        a = self._file("Song.flac")
        b = self._file("Song.1.flac")
        [decision] = self._plan(self._dup(b, 30, meta(album_id=7, recording_id=REC), a, 20, meta(album_id=7, recording_id=REC)))
        self.assertEqual(decision["delete"]["path"], str(b.resolve()))

    def test_canonical_metadata_beats_item_id(self):
        good = self._file("x - 01 - Song.flac")
        bare = self._file("y - 01 - Song.flac")
        [decision] = self._plan(self._dup(
            good, 50, meta(album_id=None, recording_id=REC, releasegroup_id=RG),
            bare, 10, meta(album_id=None, recording_id=""),
        ))
        self.assertEqual(decision["keep"]["item_id"], 50)

    def test_embedded_id_agreeing_with_acoustid_wins(self):
        agrees = self._file("x - 01 - Song.flac")
        wrong = self._file("y - 01 - Song.flac")
        other = "44444444-4444-4444-4444-444444444444"
        [decision] = self._plan(self._dup(agrees, 50, meta(recording_id=REC), wrong, 10, meta(recording_id=other)))
        self.assertEqual(decision["keep"]["item_id"], 50)
        self.assertIn("AcoustID", decision["keep_reason"])

    def test_canonical_path_beats_duplicate_filename(self):
        canon = self._file("Artist - 01 - Song.flac")
        dupname = self._file("Artist - 01 - Song.1.flac")
        [decision] = self._plan(self._dup(canon, 50, meta(recording_id=REC), dupname, 10, meta(recording_id=REC)))
        self.assertEqual(decision["keep"]["item_id"], 50)

    def test_lossless_beats_lossy_and_size_only_breaks_same_format_ties(self):
        flac = self._file("x - 01 - Song.flac", 10)
        mp3 = self._file("y - 01 - Song.mp3", 99)
        [decision] = self._plan(self._dup(
            flac, 50, meta(recording_id=REC, format="FLAC", bitrate=900000),
            mp3, 10, meta(recording_id=REC, format="MP3", bitrate=320000),
        ))
        self.assertEqual(decision["keep"]["item_id"], 50)
        small = self._file("a - 01 - Song.flac", 10)
        big = self._file("b - 01 - Song.flac", 20)
        [decision] = self._plan(self._dup(small, 10, meta(recording_id=REC), big, 50, meta(recording_id=REC)))
        self.assertEqual(decision["keep"]["item_id"], 50)
        self.assertIn("larger file", decision["keep_reason"])

    def test_item_id_is_only_the_final_tie_breaker(self):
        a = self._file("a - 01 - Song.flac")
        b = self._file("b - 01 - Song.flac")
        [decision] = self._plan(self._dup(a, 50, meta(recording_id=REC), b, 10, meta(recording_id=REC)))
        self.assertEqual(decision["keep"]["item_id"], 10)
        self.assertIn("tie-breaker", decision["keep_reason"])

    def test_embedded_recording_id_alone_is_still_never_proof(self):
        a = self._file("a - 01 - Song.flac", 10)
        b = self._file("b - 01 - Song.flac", 11)
        dup = self._dup(a, 50, meta(recording_id=REC), b, 10, meta(recording_id=REC))
        dup.update(fingerprint_verified=False, match_type="MB Track ID")
        self.assertEqual(self._plan(dup), [])

    def test_paths_wrapper_matches_the_plan(self):
        album_copy = self._file("Top Notch.1.flac")
        single = self._file("top notch (00).flac")
        dup = self._dup(album_copy, 25264, meta(album_id=1935, recording_id=REC), single, 22577, meta(disc=0))
        self.assertEqual(
            select_unattended_cleanup_paths({"duplicates": [dup]}, self.root, lambda p, r: True),
            [str(single.resolve())],
        )


if __name__ == "__main__":
    unittest.main()
