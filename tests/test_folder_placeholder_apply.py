"""Apply/rollback for album folders whose template placeholder was never
substituted (e.g. ``Hot & Wet (2003){Album MbId}`` -> ``Hot & Wet (2003)``).

Disk-only folders are renamed through folder_cleanup_v1 on a temp library;
DB-tracked folders are relocated through a real Beets library so the DB item
paths follow the files. Covers: safe rename applied, target-exists skipped,
path-escape refused, resume after interruption, single rollback, and a
tracked-folder relocation that updates the DB path.
"""

import os
import threading
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as cw
import backend.library_service as ls
from tests._folder_ops_local import patch_local_folder_ops
from tests.test_wave0_s1_containment import FakeAdapter, _Env


PH = "{Album MbId}"


class _DiskOnly(_Env):
    def setUp(self):
        super().setUp()
        # No Beets items anywhere: safe_rename's tracked guard reads the global
        # adapter; folder_db reads the one we pass. Both must see zero items.
        self.empty = FakeAdapter()
        p = mock.patch.object(cw, "beets_adapter", self.empty)
        p.start()
        self.addCleanup(p.stop)
        patch_local_folder_ops(self, self.music)

    def make_folder(self, rel, *, audio=True):
        folder = self.music / rel
        folder.mkdir(parents=True, exist_ok=True)
        if audio:
            (folder / "01 track.flac").write_bytes(b"audio-bytes")
        return folder

    def apply(self, folders, *, dry_run=False, **kw):
        return ls._apply_folder_name_placeholders(
            [str(f) for f in folders], dry_run=dry_run,
            adapter=self.empty, store=self.store, **kw)

    def test_safe_rename_applied(self):
        src = self.make_folder(f"Jade/Hot & Wet (2003){PH}")
        res = self.apply([src])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["final_summary"]["changed_count"], 1, res)
        self.assertFalse(src.exists())
        self.assertTrue((self.music / "Jade/Hot & Wet (2003)").is_dir())
        self.assertEqual(len(res["operations"]), 1)
        self.assertEqual(res["operations"][0]["kind"], "folder")

    def test_dry_run_changes_nothing(self):
        src = self.make_folder(f"Jade/Hot & Wet (2003){PH}")
        res = self.apply([src], dry_run=True)
        self.assertTrue(src.exists())  # untouched
        self.assertEqual(res["operations"], [])
        self.assertEqual(res["final_summary"]["would_change_count"], 1)

    def test_target_exists_row_skipped(self):
        src = self.make_folder(f"Jade/Hot & Wet (2003){PH}")
        clean = self.make_folder("Jade/Hot & Wet (2003)")  # collision already present
        res = self.apply([src])
        self.assertEqual(res["final_summary"]["changed_count"], 0)
        self.assertEqual(res["skipped"][0]["reason"], "target_exists")
        self.assertTrue(src.exists() and clean.exists())  # neither touched
        self.assertEqual(res["operations"], [])

    def test_path_escape_refused(self):
        outside = self.root / "outside" / f"Album{PH}"
        outside.mkdir(parents=True)
        (outside / "x.flac").write_bytes(b"a")
        res = self.apply([outside])
        self.assertEqual(res["final_summary"]["changed_count"], 0)
        self.assertTrue(res["errors"], res)
        self.assertEqual(res["errors"][0]["code"], "folder_cleanup_path_out_of_root")
        self.assertTrue(outside.exists())  # not moved

    def test_resume_after_interruption(self):
        a = self.make_folder(f"A/One (2001){PH}")
        b = self.make_folder(f"B/Two (2002){PH}")

        # Interrupt after the first folder: a cancel flag that trips once.
        trip = threading.Event()
        real_rename = cw.safe_rename_library_folder

        def rename_then_cancel(*args, **kw):
            out = real_rename(*args, **kw)
            trip.set()
            return out

        with mock.patch.object(cw, "safe_rename_library_folder", rename_then_cancel):
            first = self.apply([a, b], cancel_event=trip)
        self.assertEqual(first["final_summary"]["changed_count"], 1, first)
        self.assertEqual(len(first["done_folders"]), 1)
        done_one = first["done_folders"][0]
        self.assertFalse(Path(done_one).exists())  # first renamed
        remaining = b if Path(done_one) == a else a
        self.assertTrue(remaining.exists())        # second not yet done

        # Resume: skip the finished folder, finish the rest.
        second = self.apply([a, b], resume_done=set(first["done_folders"]))
        self.assertEqual(second["final_summary"]["changed_count"], 1, second)
        self.assertFalse(remaining.exists())
        self.assertTrue((self.music / "A/One (2001)").is_dir())
        self.assertTrue((self.music / "B/Two (2002)").is_dir())

    def test_rollback_restores(self):
        src = self.make_folder(f"Jade/Hot & Wet (2003){PH}")
        res = self.apply([src])
        self.assertFalse(src.exists())
        rb = ls._rollback_folder_placeholder_apply(res["operations"], store=self.store)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(rb["final_summary"]["rolled_back_count"], 1)
        self.assertTrue(src.exists())  # original name restored
        self.assertFalse((self.music / "Jade/Hot & Wet (2003)").exists())

    def test_empty_clean_name_skipped(self):
        src = self.make_folder(PH)  # name is only the placeholder -> empty after strip
        res = self.apply([src])
        self.assertEqual(res["final_summary"]["changed_count"], 0)
        self.assertEqual(res["skipped"][0]["reason"], "no_clean_name")
        self.assertTrue(src.exists())


class TrackedRelocationTests(unittest.TestCase):
    """DB-tracked placeholder folder: relocation updates the DB path."""

    def setUp(self):
        from tests.test_album_relocation_transaction import _Engine
        self.eng = _Engine()
        self.eng.setUp()
        self.addCleanup(self.eng.doCleanups)

    def test_tracked_folder_relocation_updates_db_path(self):
        eng = self.eng
        folder = os.path.join(eng.music, "A", f"B{PH}")
        album = eng.make_album(folder)
        before_paths, _art = eng.beets_state(album.id)
        self.assertTrue(all(os.path.dirname(p) == folder for p in before_paths.values()))

        res = ls._apply_folder_name_placeholders(
            [folder], dry_run=False, adapter=eng.ad, store=eng.store)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["final_summary"]["changed_count"], 1, res)
        self.assertEqual(res["operations"][0]["kind"], "album")

        after_paths, _ = eng.beets_state(album.id)
        dest = os.path.join(eng.music, "A", "B")  # Beets template, placeholder gone
        self.assertTrue(all(os.path.dirname(p) == dest for p in after_paths.values()), after_paths)
        self.assertFalse(os.path.exists(folder))  # old placeholder folder vacated

        # Single rollback restores the DB path.
        rb = ls._rollback_folder_placeholder_apply(res["operations"], adapter=eng.ad, store=eng.store)
        self.assertTrue(rb["ok"], rb)
        restored, _ = eng.beets_state(album.id)
        self.assertEqual(restored, before_paths)


if __name__ == "__main__":
    unittest.main()
