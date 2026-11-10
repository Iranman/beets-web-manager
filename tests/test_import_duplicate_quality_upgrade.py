"""A proven duplicate (B1) whose incoming files are a quality upgrade over the
library copy gets ONE informational Review row ("upgrade available") instead
of the silent "already in library" skip. Same or lower quality, or quality
unknown on either side, is still skipped with no Review row. Quality is
ranked only by the user's Music Format Preferences. Beets is faked at the
adapter, under the real ``reimport_source`` result shaping; IDs are synthetic.
"""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from tests import test_import_duplicate_already_in_library as d
    from tests import test_import_qbittorrent_source as q
except ImportError:  # pragma: no cover
    import test_import_duplicate_already_in_library as d
    import test_import_qbittorrent_source as q

MP3_320 = {"format": "MP3", "bitrate": 320000, "samplerate": 44100, "bitdepth": None}
FLAC_16 = {"format": "FLAC", "bitrate": 900000, "samplerate": 44100, "bitdepth": 16}
REAL = {}


def setUpModule():
    q.setUpModule()
    # Before any test patch: the real Review Queue writer and lookup.
    REAL["queue"] = q.ai._ai_batch_queue_pending_review
    REAL["has"] = q.ai._pending_review_has_path


def lib_evidence(items_quality):
    ev = d.evidence()
    ev["duplicates"][0].update(albumartist="Some Artist", album="Some Album")
    if items_quality is not None:
        ev["duplicates"][0]["items_quality"] = copy.deepcopy(items_quality)
    return ev


def source_props(fmt="flac", ok=True, bit_depth=16, sample_rate=44100, bitrate=900000):
    return {"ok": ok, "format": fmt, "bit_depth": bit_depth, "sample_rate": sample_rate, "bitrate": bitrate}


class UpgradeRuleTests(unittest.TestCase):
    def test_rule_uses_the_preferred_formats_order_only(self):
        from backend.matching.quality_upgrade import quality_upgrade as rule
        prefs = {"preferred_formats": ["flac", "mp3", "aac"]}
        self.assertEqual(rule([source_props()] * 2, [MP3_320] * 2, prefs),
                         {"library_format": "MP3 320 kbps", "source_format": "FLAC 16-bit/44.1 kHz"})
        self.assertIsNone(rule([source_props()], [FLAC_16], prefs))  # same format: no claim
        self.assertIsNone(rule([source_props()], [dict(FLAC_16, bitdepth=16, samplerate=22050)], prefs))
        self.assertIsNone(rule([source_props("mp3", bit_depth=None)], [FLAC_16], prefs))  # lower
        # The user's own order decides: MP3 ranked above FLAC means no upgrade.
        self.assertIsNone(rule([source_props()], [MP3_320], {"preferred_formats": ["mp3", "flac"]}))
        # One library file already lossless, or one incoming file lossy: no claim.
        self.assertIsNone(rule([source_props()], [MP3_320, FLAC_16], prefs))
        self.assertIsNone(rule([source_props(), source_props("mp3")], [MP3_320], prefs))
        for src, lib in (([], [MP3_320]), ([source_props()], []),
                         ([source_props(ok=False)], [MP3_320]), ([source_props("unknown")], [MP3_320]),
                         ([source_props()], [dict(MP3_320, format=None)]),
                         ([source_props()], [dict(MP3_320, format="APE")])):
            with self.subTest(src=src, lib=lib):
                self.assertIsNone(rule(src, lib, prefs))


class UpgradeBatchTests(q._Batch):
    skipped = d.BatchTests.skipped

    def setUp(self):
        super().setUp()
        self.source = {}  # file name -> inspect_audio_file-like properties; default lossless
        for p in (mock.patch.object(q.ai, "_validate_import_source_audio", side_effect=self.validate),
                  mock.patch.object(q.ai, "_music_format_preferences",
                                    return_value={"preferred_formats": ["flac", "mp3", "aac"]}),
                  mock.patch.object(q.ai, "identify_import_folder",
                                    side_effect=AssertionError("Stage 2 for a proven duplicate"))):
            p.start()
            self.addCleanup(p.stop)

    def validate(self, folder, log, **_kw):
        files = sorted(p for p in Path(folder).rglob("*.flac"))
        return {"accepted": [{"path": str(f), "properties": self.source.get(f.name, source_props())} for f in files],
                "rejected": [], "deferred": []}

    def real_beets(self, items_quality):
        """Beets faked at the adapter with the plugin's result shape; the real
        reimport_source shapes it (Beets skips the folder as a duplicate)."""
        from backend import composite_workflows as cw
        fake = mock.Mock()
        fake.get_stats.return_value = {"albums": 1}
        fake.run_import.side_effect = lambda paths, **kw: {
            "success": True, "imported_album_ids": [], "skipped_paths": [paths],
            "skipped": [{"path": paths, "reason": "duplicate", "duplicate_evidence": lib_evidence(items_quality)}]}
        self.reimport.side_effect = lambda f, opts, timeout: d.REAL_REIMPORT(f, opts, timeout=timeout)
        p = mock.patch.object(cw, "beets_adapter", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def run_torrent(self, name, items_quality):
        self.torrent(name, ["01.flac", "02.flac"])
        folder = self.dl / name
        self.real_beets(items_quality)
        out, folders = self.run_batch()
        return folder, folders[str(folder)]

    def test_lossless_torrent_of_an_mp3_library_album_is_one_upgrade_review_row(self):
        self.torrent("Up", ["01.flac", "02.flac"])
        files = sorted((self.dl / "Up").glob("*.flac"))
        before = [hashlib.sha256(f.read_bytes()).hexdigest() for f in files]
        fake = self.real_beets([MP3_320, MP3_320])
        out, folders = self.run_batch()
        state = folders[str(self.dl / "Up")]
        self.assertEqual((state["status"], state["current_step"]), ("review_required", "upgrade available"), self.log)
        self.queued.assert_called_once()
        ev = self.queued.call_args.kwargs["evidence"]
        self.assertEqual(ev["origin_step"], "quality_upgrade")
        self.assertEqual(ev["duplicate_rule"], "quality_upgrade")
        self.assertEqual(ev["identity_rule"], "recordings_present_in_release_group")
        self.assertEqual(ev["quality_upgrade"], {"release_group_id": d.RG, "library_album_ids": [5],
                                                 "library_format": "MP3 320 kbps",
                                                 "source_format": "FLAC 16-bit/44.1 kHz"})
        sug = self.queued.call_args.args[1]
        self.assertEqual((sug["mb_releasegroupid"], sug["albumartist"], sug["album"]),
                         (d.RG, "Some Artist", "Some Album"))
        self.assertIn("Some Artist - Some Album", sug["reason"])
        # Informational only: one copy-mode Beets run (which skipped it), no
        # pinned import, torrent bytes untouched.
        fake.run_import.assert_called_once()
        self.assertTrue(self.reimport.call_args.args[1]["copy"])
        self.plan.assert_not_called()
        self.assertEqual([hashlib.sha256(f.read_bytes()).hexdigest() for f in files], before)

    def test_same_quality_is_skipped_with_no_review_row(self):
        folder, state = self.run_torrent("Same", [FLAC_16, FLAC_16])
        self.assertEqual((state["status"], state["failure_reason"]), ("skipped", "already in library"), self.log)
        self.queued.assert_not_called()

    def test_lower_quality_is_skipped_with_no_review_row(self):
        self.source = {"01.flac": source_props("mp3", bit_depth=None), "02.flac": source_props("mp3", bit_depth=None)}
        folder, state = self.run_torrent("Lower", [FLAC_16, FLAC_16])
        self.assertEqual(state["status"], "skipped", self.log)
        self.queued.assert_not_called()

    def test_unknown_quality_is_skipped_with_no_review_row(self):
        cases = {
            "old plugin, no items_quality": (None, {}),
            "library format blank": ([dict(MP3_320, format=None), MP3_320], {}),
            "library list shorter than the album": ([MP3_320], {}),
            "incoming file not inspected": ([MP3_320, MP3_320], {"02.flac": source_props(ok=False)}),
            "incoming format unknown": ([MP3_320, MP3_320], {"01.flac": source_props("unknown")}),
        }
        for i, (name, (lib, src)) in enumerate(cases.items()):
            with self.subTest(name):
                self.queued.reset_mock()
                self.source = src
                folder, state = self.run_torrent(f"Unknown {i}", lib)
                self.assertEqual((state["status"], state["failure_reason"]), ("skipped", "already in library"),
                                 self.log)
                self.queued.assert_not_called()

    def test_partial_import_whose_skipped_part_is_an_upgrade_gets_the_review_row(self):
        self.audio("Part/CD1/01.flac")
        self.audio("Part/CD2/01.flac")
        self.audio("Part/CD2/02.flac")
        folder = self.dl / "Part"
        self.local_walk.side_effect = lambda root, log=None, unreadable=None: [str(folder)]
        self.result = {"ok": True, "albums_imported": 1, "imported_album_ids": [7],
                       "skipped": [self.skipped(folder / "CD2", lib_evidence([MP3_320, MP3_320]))],
                       "not_matched": [str(folder / "CD2")], "not_matched_known": True}
        out, folders = self.run_batch(source="local", qbit_inventory=None)
        state = folders[str(folder)]
        self.assertEqual((state["status"], state["current_step"]), ("review_required", "upgrade available"), self.log)
        self.assertEqual(state["album_ids"], [7])
        self.queued.assert_called_once()
        self.assertEqual(self.queued.call_args.kwargs["evidence"]["quality_upgrade"]["library_album_ids"], [5])

    def test_rerun_keeps_exactly_one_review_row(self):
        """The real Review Queue store: a second batch over the same torrent
        must not add a second row."""
        import backend.pending_review_store as prs
        store = Path(tempfile.mkdtemp(prefix="bwm_upgrade_queue_", dir=str(self.tmp))) / "ai_pending_review.json"
        for p in (mock.patch.object(prs, "_AI_PENDING_FILE", store),
                  mock.patch.object(prs, "_library_album_ids_for_folder", return_value=[]),
                  mock.patch.object(q.ai, "_ai_batch_queue_pending_review", wraps=REAL["queue"]),
                  mock.patch.object(q.ai, "_pending_review_has_path", side_effect=REAL["has"])):
            p.start()
            self.addCleanup(p.stop)
        self.torrent("Again", ["01.flac", "02.flac"])
        folder = self.dl / "Again"
        self.real_beets([MP3_320, MP3_320])
        out, folders = self.run_batch()
        self.assertEqual(folders[str(folder)]["current_step"], "upgrade available", self.log)
        self.batch_id += "-rerun"
        out, folders = self.run_batch()
        rows = json.loads(store.read_text())
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["evidence"]["duplicate_rule"], "quality_upgrade")
        self.assertEqual(folders[str(folder)]["status"], "review_created", self.log)


if __name__ == "__main__":
    unittest.main()
