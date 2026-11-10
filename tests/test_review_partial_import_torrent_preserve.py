"""F8: a Review-page partial import never moves or deletes a preserved
torrent source file or a hardlinked file. It copies them and keeps them;
genuine app staging is still moved and cleaned as before."""
import contextlib
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import backend.import_service as isvc
import backend.library_service as libsvc
import backend.pending_review_store as prs

REL = "11111111-1111-1111-1111-111111111111"
RG = "22222222-2222-2222-2222-222222222222"


class ReviewPartialImportSourceSafetyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.src = Path(tmp.name) / "torrents" / "Artist - Album"
        self.src.mkdir(parents=True)
        self.selected = self.src / "01 - One.flac"
        self.selected.write_bytes(b"torrent-bytes-1")
        (self.src / "02 - Two.flac").write_bytes(b"torrent-bytes-2")  # not selected
        self.plan_payloads, self.deleted, self.cleaned = [], [], []
        self.result = {}

        def run_now(fn, label="", metadata=None):
            log = []
            try:
                self.result = {"status": "completed", "result": fn(log), "log": log}
            except Exception as ex:
                self.result = {"status": "failed", "error": str(ex), "log": log}
            return types.SimpleNamespace(job_id="job-1")

        def plan(payload):
            self.plan_payloads.append(payload)
            return {"ok": True, "operation_id": "op-1"}

        def delete_file(path):
            self.deleted.append(os.path.normcase(str(Path(path).resolve())))
            return {"ok": True}

        cw = isvc.composite_workflows
        src = str(self.src)
        for target, name, kw in (
            (isvc.jobs, "start_python", {"side_effect": run_now}),
            (isvc, "_folder_import_in_slot", {"side_effect": lambda do, log, ce, **k: do(log, ce)}),
            (isvc, "_resolve_import_review_source_path", {"return_value": (src, None)}),
            (isvc, "_folder_import_selected_sources", {"side_effect": lambda f, p: ([self.selected], None)}),
            (isvc, "_filter_import_review_selected_audio_files", {"side_effect": lambda files, log, **_k: list(files)}),
            # Worst case: staging returns the source folder itself (all audio selected).
            (isvc, "_stage_selected_audio_files", {"side_effect": lambda aldir, *a, **k: aldir}),
            (isvc, "_folder_import_settle_release_group",
             {"return_value": ({"artist": "Artist", "title": "Album", "tracks": []}, RG, RG)}),
            (isvc, "_preserve_torrent_source_path", {"return_value": False}),  # isolate the file-level rule
            (isvc, "_library_album_ids_for_folder", {"return_value": []}),
            (isvc, "_beet_import_timeout", {"return_value": 60}),
            (isvc, "_record_recent_import_for_album", {}),
            (isvc, "_remaining_audio_files",
             {"side_effect": lambda f: sorted(p for p in Path(f).glob("*.flac"))}),
            (isvc, "_folder_import_clean_source", {"side_effect": lambda *a: self.cleaned.append(a)}),
            (isvc, "_remove_pending_review_for_path", {}),
            (isvc, "_mark_pending_review_status", {}),
            (isvc, "_folder_import_dedupe_artist_folders", {}),
            (isvc, "_invalidate_lib_cache", {}),
            (isvc, "_trigger_plex_refresh", {}),
            (isvc, "_folder_import_record_ai_history", {}),
            (cw, "plan_confirmed_import", {"side_effect": plan}),
            (cw, "apply_confirmed_import", {"return_value": {"ok": True, "album_id": 9, "mb_releasegroupid": RG}}),
            (cw, "delete_file", {"side_effect": delete_file}),
            (isvc.job_contract, "held", {"side_effect": lambda *a, **k: contextlib.nullcontext()}),
        ):
            patcher = mock.patch.object(target, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self):
        before = os.stat(self.selected)
        body, code = isvc.start_folder_import_with_id(
            {"path": str(self.src), "mb_albumid": REL, "mb_releasegroupid": RG})
        self.assertEqual(code, 200, body)
        self.assertEqual(self.result.get("status"), "completed", self.result)
        return before

    def _assert_copied_and_untouched(self, before):
        self.assertEqual([p["use_move"] for p in self.plan_payloads], [False])
        self.assertNotIn(os.path.normcase(str(self.selected.resolve())), self.deleted)
        self.assertEqual(self.cleaned, [])  # source-folder cleanup never runs
        after = os.stat(self.selected)
        self.assertEqual(self.selected.read_bytes(), b"torrent-bytes-1")
        self.assertEqual((after.st_ino, after.st_size, after.st_mtime_ns),
                         (before.st_ino, before.st_size, before.st_mtime_ns))

    def test_preserved_torrent_source_is_copied_and_kept(self):
        with mock.patch.object(libsvc, "_preserve_torrent_source_file", return_value=True):
            before = self._run()
        self._assert_copied_and_untouched(before)

    def test_hardlinked_file_is_copied_and_kept(self):
        link = self.src.parent / "seeding-copy.flac"
        try:
            os.link(self.selected, link)
        except OSError as ex:  # pragma: no cover - filesystem without hardlinks
            self.skipTest(f"hardlinks unsupported: {ex}")
        with mock.patch.object(libsvc, "_preserve_torrent_source_file", return_value=False):
            before = self._run()
        self._assert_copied_and_untouched(before)
        self.assertEqual(link.read_bytes(), b"torrent-bytes-1")

    def test_preserve_rule_raising_fails_closed_to_copy(self):
        with mock.patch.object(libsvc, "_preserve_torrent_source_file", side_effect=RuntimeError("boom")):
            before = self._run()
        self._assert_copied_and_untouched(before)

    def test_app_staging_source_is_still_moved_and_cleaned(self):
        with mock.patch.object(libsvc, "_preserve_torrent_source_file", return_value=False):
            self._run()
        self.assertEqual([p["use_move"] for p in self.plan_payloads], [True])
        self.assertIn(os.path.normcase(str(self.selected.resolve())), self.deleted)
        self.assertEqual(len(self.cleaned), 1)

    def test_rule_unit(self):
        with mock.patch.object(libsvc, "_preserve_torrent_source_file", return_value=False):
            self.assertFalse(isvc._selected_sources_need_copy([self.selected]))
            self.assertTrue(isvc._selected_sources_need_copy([self.src / "missing.flac"]))


class QbitReviewRowWholeFolderImportTests(ReviewPartialImportSourceSafetyTests):
    """N-1: a Review row a qBittorrent batch left on a torrent's own folder is
    imported by copy and the torrent files are kept, even with
    ALLOW_TORRENT_SOURCE_MOVE=1 (the folder rule alone would then move)."""

    def setUp(self):
        super().setUp()
        self.rows = [{"path": str(self.src), "evidence": {"source_mode": "qbittorrent"}}]
        (self.src / "cover.jpg").write_bytes(b"torrent-art")
        for target, name, kw in (
            (isvc, "_folder_import_selected_sources", {"return_value": ([], None)}),
            (isvc, "_validate_import_source_audio", {}),
            (prs, "_AI_PENDING_FILE", {"new": self.src.parent / "ai_pending.json"}),
            (isvc, "_app_managed_download_path", {"return_value": False}),
            (libsvc, "TORRENT_SOURCE_MOVE_ALLOWED", {"new": True}),
        ):
            patcher = mock.patch.object(target, name, **kw)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The real folder rule (the parent pins it to False).
        real = mock.patch.object(isvc, "_preserve_torrent_source_path", libsvc._preserve_torrent_source_path)
        real.start()
        self.addCleanup(real.stop)

    def _run(self):
        prs._AI_PENDING_FILE.write_text(json.dumps(self.rows))
        before = os.stat(self.selected)
        body, code = isvc.start_folder_import_with_id(
            {"path": str(self.src), "mb_albumid": REL, "mb_releasegroupid": RG, "move": True})
        self.assertEqual(code, 200, body)
        self.assertEqual(self.result.get("status"), "completed", self.result)
        return before

    def test_qbittorrent_row_is_copied_and_kept_with_move_allowed(self):
        self.assertFalse(isvc._preserve_torrent_source_path(self.src))  # the flag alone would move
        before = self._run()
        self._assert_copied_and_untouched(before)
        self.assertEqual((self.src / "02 - Two.flac").read_bytes(), b"torrent-bytes-2")
        self.assertTrue((self.src / "cover.jpg").is_file())

    def test_row_without_qbittorrent_origin_still_moves(self):
        self.rows = [{"path": str(self.src), "evidence": {}}]
        self._run()
        self.assertEqual([p["use_move"] for p in self.plan_payloads], [True])

    def test_unreadable_queue_copies(self):
        with mock.patch.object(libsvc, "_pending_review_torrent_rows",
                               side_effect=prs.PendingReviewLoadError("unreadable")):
            before = self._run()
        self._assert_copied_and_untouched(before)

    # The parent's partial-import cases do not apply to a whole-folder import.
    test_preserved_torrent_source_is_copied_and_kept = None
    test_hardlinked_file_is_copied_and_kept = None
    test_preserve_rule_raising_fails_closed_to_copy = None
    test_app_staging_source_is_still_moved_and_cleaned = None
    test_rule_unit = None


if __name__ == "__main__":
    unittest.main()
