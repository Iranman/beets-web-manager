"""ARCH-030 F1-RECID / F1-POSITION: the NO_RESULT deterministic-metadata slot
must veto a contradicting EMBEDDED recording id and must corroborate position
from evidence INDEPENDENT of the circular staged filename.

These tests drive the REAL PRODUCTION producer -- a genuine FLAC written with
mutagen, read back through ``_read_file_media_tags`` (mutagen branch, the branch
production uses because embedded beets 2.14 does not populate
``sys.modules['beets.mediafile']``) and ``embedded_identity_map_for_folder`` --
into the REAL ``_missing_track_recording_proof`` gate. The prior shipped test
injected ``recording_id`` straight onto the proof row, so it never noticed the
producer dropped ``musicbrainz_trackid``; sourcing the embedded map from the
actual file is what makes this coverage honest.

Synthetic identity only; no network, no real fpcalc, no real Beets library.
"""
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mutagen.flac import FLAC

import backend.acoustid_service as acs
import backend.matching_service as ms

RG = "37cb4a72-1111-1111-1111-111111111111"
REL = "083b9009-2222-2222-2222-222222222222"
ART = "a1b2c3d4-4444-4444-4444-444444444444"
REC_INTRO = "aaaaaaaa-1111-2222-3333-444444444444"   # the TARGET recording (track 1)
REC_OTHER = "bbbbbbbb-1111-2222-3333-444444444444"   # a DIFFERENT recording, same title

# Target release: track 1 "Forever (Intro)" (missing), track 2 "Only You" (held).
MB_TRACKS = [
    {"mb_trackid": REC_INTRO, "title": "Forever (Intro)", "artist": "112",
     "track": 1, "disc": 1, "duration_ms": 42000},
    {"mb_trackid": "cccccccc-1111-2222-3333-444444444444", "title": "Only You",
     "artist": "112", "track": 2, "disc": 1, "duration_ms": 255000},
]
ALBUM_ROW = {"album": "Forever", "albumartist": "112",
             "mb_releasegroupid": RG, "mb_albumid": REL}


def _write_flac(path, *, title, rg, albumartist, artist,
                recording_id="", track="", disc=""):
    """A genuine minimal FLAC carrying real Vorbis MB tags (survive the stage
    rename -> genuine embedded evidence, exactly what production reads)."""
    packed = (44100 << 44) | (1 << 41) | (15 << 36) | 44100
    streaminfo = (struct.pack(">HH", 4096, 4096) + b"\x00" * 6
                  + packed.to_bytes(8, "big") + b"\x00" * 16)
    with open(path, "wb") as fh:
        fh.write(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)
    fl = FLAC(str(path))
    if title:
        fl["title"] = [title]
    if rg:
        fl["musicbrainz_releasegroupid"] = [rg]
    if albumartist:
        fl["musicbrainz_albumartistid"] = [albumartist]
    if artist:
        fl["musicbrainz_artistid"] = [artist]
    if recording_id:
        fl["musicbrainz_trackid"] = [recording_id]
    if track:
        fl["tracknumber"] = [str(track)]
    if disc:
        fl["discnumber"] = [str(disc)]
    fl.save()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        # Production reads the mutagen branch: ensure the unit-test-only
        # beets.mediafile branch is NOT taken.
        p = mock.patch.dict(sys.modules, {}, clear=False)
        p.start()
        sys.modules.pop("beets.mediafile", None)
        self.addCleanup(p.stop)

    def _proof(self, *, staged_name, original_name,
               recording_id="", emb_track="", emb_disc="",
               title="Forever (Intro)"):
        """Stage ONE real FLAC for missing track 1 and run the real proof with
        the embedded map sourced from the actual file."""
        staged = self.folder / staged_name
        _write_flac(staged, title=title, rg=RG, albumartist=ART, artist=ART,
                    recording_id=recording_id, track=emb_track, disc=emb_disc)
        embedded = acs.embedded_identity_map_for_folder(self.folder)
        embedded = {p: e for p, e in embedded.items() if e}
        file_hits = [(str(staged), [])]   # genuine AcoustID NO_RESULT
        return str(staged), embedded, ms._missing_track_recording_proof(
            file_hits,
            mb_tracks=[dict(t) for t in MB_TRACKS],
            wanted_tracks=[dict(MB_TRACKS[0])],
            existing_recordings={MB_TRACKS[1]["mb_trackid"]},
            album_row=dict(ALBUM_ROW),
            release_id=REL,
            release_group_id=RG,
            release_title="Forever",
            release_artist="112",
            release_artist_id=ART,
            file_durations={str(staged): 42.0},
            file_original_names={str(staged): original_name},
            file_embedded_ids=embedded,
        )


class F1RecIdProductionPath(_Base):
    def test_producer_reads_embedded_recording_id(self):
        """The exact signal the old test missed: the PRODUCTION reader must
        surface musicbrainz_trackid as mb_recording_id."""
        staged, embedded, _ = self._proof(
            staged_name="01 Forever (Intro).flac",
            original_name="01 Forever (Intro).flac",
            recording_id=REC_OTHER, emb_track="1")
        self.assertEqual(embedded[staged]["mb_recording_id"], REC_OTHER)
        self.assertEqual(embedded[staged]["track"], "1")

    def test_contradicting_recording_id_goes_to_review(self):
        _, _, proof = self._proof(
            staged_name="01 Forever (Intro).flac",
            original_name="01 Forever (Intro).flac",
            recording_id=REC_OTHER, emb_track="1")
        self.assertFalse(proof["ok"], proof)
        self.assertEqual(proof["reason_code"], "file_unverified")

    def test_matching_recording_id_still_auto_accepts(self):
        _, _, proof = self._proof(
            staged_name="01 Forever (Intro).flac",
            original_name="01 Forever (Intro).flac",
            recording_id=REC_INTRO, emb_track="1")
        self.assertTrue(proof["ok"], proof)
        self.assertEqual([f["track"] for f in proof["files"]], [1])

    def test_absent_recording_id_still_auto_accepts(self):
        _, _, proof = self._proof(
            staged_name="01 Forever (Intro).flac",
            original_name="01 Forever (Intro).flac",
            recording_id="", emb_track="1")
        self.assertTrue(proof["ok"], proof)


class F1PositionIndependentOfStagedName(_Base):
    def test_staged_slot_cannot_launder_a_wrong_independent_position(self):
        """Staged name claims the target slot (01) but the INDEPENDENT sources
        (embedded track + original name) say track 7 -> review, not a trivially
        satisfied position."""
        _, _, proof = self._proof(
            staged_name="01 Forever (Intro).flac",   # system-renamed to target slot
            original_name="Forever (Intro).flac",    # no number -> no position
            recording_id=REC_INTRO, emb_track="7")    # embedded disagrees
        self.assertFalse(proof["ok"], proof)
        self.assertEqual(proof["reason_code"], "file_unverified")

    def test_no_independent_position_fails_closed_to_review(self):
        """Neither the original name nor the embedded tags carry a position ->
        position cannot be corroborated -> review (Option C)."""
        _, _, proof = self._proof(
            staged_name="01 Forever (Intro).flac",
            original_name="Forever (Intro).flac",    # no number
            recording_id=REC_INTRO, emb_track="")      # no embedded track
        self.assertFalse(proof["ok"], proof)
        self.assertEqual(proof["reason_code"], "file_unverified")

    def test_genuine_independent_position_auto_accepts(self):
        """Embedded track 1 == target slot 1, everything else genuine -> auto."""
        _, _, proof = self._proof(
            staged_name="01 Forever (Intro).flac",
            original_name="01 Forever (Intro).flac",
            recording_id=REC_INTRO, emb_track="1")
        self.assertTrue(proof["ok"], proof)


if __name__ == "__main__":
    unittest.main()
