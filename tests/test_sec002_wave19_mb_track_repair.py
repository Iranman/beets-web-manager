"""SEC-002 / ARCH-003 Wave 19: MusicBrainz Album Track Repair Controlled Mutation Boundary.

Comprehensive focused test suite for album_mb_track_repair_v1 mutation family.
"""
import ast
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from backend.transaction_engine import (
    TransactionStore,
    create_album_mb_track_repair_plan,
    execute_album_mb_track_repair_apply,
    rollback_album_mb_track_repair,
)
from backend.beets_client import BeetsClient
import app as app_module

ITEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    album TEXT,
    albumartist TEXT,
    mb_albumid TEXT,
    mb_releasegroupid TEXT,
    year INTEGER
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    album_id INTEGER,
    title TEXT,
    artist TEXT,
    album TEXT,
    albumartist TEXT,
    disc INTEGER,
    track INTEGER,
    path BLOB,
    mb_trackid TEXT,
    mb_albumid TEXT,
    mb_releasegroupid TEXT,
    length REAL,
    size INTEGER,
    mtime REAL
);
"""

RG_A = "aaaaaaaa-0000-0000-0000-000000000000"
RG_B = "bbbbbbbb-0000-0000-0000-000000000000"
REL_A = "11111111-1111-1111-1111-111111111111"
REL_B = "22222222-2222-2222-2222-222222222222"
REC_1 = "33333333-3333-3333-3333-333333333331"
REC_2 = "33333333-3333-3333-3333-333333333332"
REC_TARGET_1 = "44444444-4444-4444-4444-444444444441"
REC_TARGET_2 = "44444444-4444-4444-4444-444444444442"


def _fake_tracklist_a(rel_id=REL_A, rg_id=RG_A):
    return {
        "ok": True,
        "release_id": rel_id,
        "release_group": rg_id,
        "release_title": "Test Album Title",
        "tracks": [
            {
                "disc": 1,
                "track": 1,
                "title": "Track 1 Fixed Title",
                "mb_trackid": REC_TARGET_1,
                "duration_ms": 180000,
            },
            {
                "disc": 1,
                "track": 2,
                "title": "Track 2 Fixed Title",
                "mb_trackid": REC_TARGET_2,
                "duration_ms": 200000,
            },
        ],
    }


class Wave19FixtureBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)

        self.music_root = self.tmp_path / "music"
        self.music_root.mkdir()
        self.outside_root = self.tmp_path / "outside"
        self.outside_root.mkdir()

        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir()
        self.store = TransactionStore(root=str(store_dir))

        self.db_path = self.tmp_path / "musiclibrary.db"
        con = sqlite3.connect(self.db_path)
        con.executescript(ITEMS_SCHEMA)
        con.commit()
        con.close()

        app_module.app.config["TESTING"] = True
        self._env_patch = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def _create_album_and_items(self, album_id=1, rg_id=RG_A, rel_id=REL_A, item_count=2, outside=False):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO albums (id, album, albumartist, mb_albumid, mb_releasegroupid, year) VALUES (?, ?, ?, ?, ?, ?)",
            (album_id, "Test Album Title", "Test Artist", rel_id, rg_id, 2024),
        )
        item_paths = []
        for i in range(1, item_count + 1):
            root = self.outside_root if outside else self.music_root
            album_dir = root / f"album_{album_id}"
            album_dir.mkdir(parents=True, exist_ok=True)
            file_path = album_dir / f"track{i}.flac"
            file_path.write_bytes(b"FLAC_DATA_" + str(i).encode())

            rec_id = REC_1 if i == 1 else REC_2
            con.execute(
                "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    i + (album_id - 1) * 10,
                    album_id,
                    f"Track {i} Old Title",
                    "Test Artist",
                    "Test Album Title",
                    "Test Artist",
                    1,
                    i,
                    str(file_path),
                    rec_id,
                    rel_id,
                    rg_id,
                    180.0 if i == 1 else 200.0,
                ),
            )
            item_paths.append(file_path)
        con.commit()
        con.close()
        return item_paths


class PlanTests(Wave19FixtureBase):
    def test_create_plan_valid(self):
        self._create_album_and_items(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"), res)
        op_id = res["operation_id"]
        self.assertEqual(res["updated"], 2)

        tx = self.store.get(op_id)
        self.assertEqual(tx["status"], "Preview")
        self.assertEqual(tx["metadata"]["mutation_family"], "album_mb_track_repair_v1")
        self.assertEqual(len(tx["changes"]), 2)
        self.assertEqual(tx["changes"][0]["after"]["mb_trackid"], REC_TARGET_1)
        self.assertEqual(tx["changes"][1]["after"]["mb_trackid"], REC_TARGET_2)

    def test_plan_non_mutating(self):
        item_paths = self._create_album_and_items(album_id=1)
        mtimes_before = [p.stat().st_mtime_ns for p in item_paths]

        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"))

        # Verify DB and files completely untouched after Plan
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT mb_trackid FROM items WHERE album_id=1 ORDER BY id").fetchall()
        con.close()
        self.assertEqual(rows[0]["mb_trackid"], REC_1)
        self.assertEqual(rows[1]["mb_trackid"], REC_2)

        mtimes_after = [p.stat().st_mtime_ns for p in item_paths]
        self.assertEqual(mtimes_before, mtimes_after)

    def test_plan_invalid_album(self):
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 999},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertFalse(res.get("ok"))
        self.assertIn("not found", res.get("error", "").lower())

    def test_plan_invalid_item_membership(self):
        con = sqlite3.connect(self.db_path)
        con.execute("INSERT INTO albums (id, album, mb_albumid) VALUES (5, 'Empty Album', ?)", (REL_A,))
        con.commit()
        con.close()

        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 5},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertFalse(res.get("ok"))
        self.assertIn("no track items", res.get("error", "").lower())

    def test_plan_missing_target_file(self):
        item_paths = self._create_album_and_items(album_id=1)
        item_paths[0].unlink()  # delete physical file

        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "repair_item_missing")

    def test_plan_release_group_mismatch(self):
        self._create_album_and_items(album_id=1, rg_id=RG_A)
        # Pass override REL_B which returns RG_B
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1, "mb_albumid": REL_B},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(rel_id=REL_B, rg_id=RG_B),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "repair_identity_mismatch")
        self.assertIn("different Release Group", res.get("error", ""))

    def test_plan_parent_symlink(self):
        item_paths = self._create_album_and_items(album_id=1)
        # Create a symlink dir inside music_root pointing to album_1
        link_dir = self.music_root / "link_album_1"
        try:
            os.symlink(item_paths[0].parent, link_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported on this host environment")

        symlink_file = link_dir / item_paths[0].name
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET path=? WHERE id=1", (str(symlink_file),))
        con.commit()
        con.close()

        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "repair_symlink_rejected")

    def test_plan_final_symlink(self):
        item_paths = self._create_album_and_items(album_id=1)
        symlink_file = self.music_root / "album_1" / "link_track.flac"
        try:
            os.symlink(item_paths[0], symlink_file)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported on this host environment")

        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET path=? WHERE id=1", (str(symlink_file),))
        con.commit()
        con.close()

        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "repair_symlink_rejected")

    def test_plan_path_outside_root(self):
        self._create_album_and_items(album_id=1, outside=True)

        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "repair_path_out_of_root")


class ApplyTests(Wave19FixtureBase):
    def test_apply_successful_repair(self):
        item_paths = self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(plan_res.get("ok"))
        op_id = plan_res["operation_id"]

        apply_res = execute_album_mb_track_repair_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply_res.get("ok"), apply_res)
        self.assertEqual(apply_res["status"], "Completed")

        # Verify DB updated
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT id, mb_trackid, title FROM items WHERE album_id=1 ORDER BY id").fetchall()
        con.close()
        self.assertEqual(rows[0]["mb_trackid"], REC_TARGET_1)
        self.assertEqual(rows[1]["mb_trackid"], REC_TARGET_2)
        self.assertEqual(rows[0]["title"], "Track 1 Fixed Title")
        self.assertEqual(rows[1]["title"], "Track 2 Fixed Title")

        # Verify tx record status
        tx = self.store.get(op_id)
        self.assertEqual(tx["status"], "Completed")
        self.assertTrue(tx["metadata"]["mutated"])

    def test_apply_stale_item_recording_id(self):
        self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]

        # Mutate DB recording ID after Plan before Apply
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET mb_trackid='modified-outside-plan' WHERE id=1")
        con.commit()
        con.close()

        apply_res = execute_album_mb_track_repair_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "repair_toctou_mismatch")

    def test_apply_stale_album_membership(self):
        self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]

        # Change item's album_id in DB after Plan
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET album_id=99 WHERE id=1")
        con.commit()
        con.close()

        apply_res = execute_album_mb_track_repair_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "repair_album_membership_changed")

    def test_apply_mtime_changed(self):
        item_paths = self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]

        # Modify file on disk to change mtime/size
        time.sleep(0.01)
        item_paths[0].write_bytes(b"MODIFIED_AFTER_PLAN")

        apply_res = execute_album_mb_track_repair_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "repair_toctou_mismatch")

    def test_apply_already_completed_replay(self):
        self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]

        apply1 = execute_album_mb_track_repair_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply1.get("ok"))

        apply2 = execute_album_mb_track_repair_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply2.get("ok"))
        self.assertTrue(apply2.get("already_completed"))

    def test_apply_wrong_mutation_family(self):
        tx = self.store.create(
            operation_type="Replace",
            status="Preview",
            summary="Fake family",
            metadata={"mutation_family": "track_replacement_v1"},
        )
        res = execute_album_mb_track_repair_apply(
            self.store,
            tx["id"],
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(res.get("ok"))
        self.assertIn("mismatch", res.get("error", "").lower())


class MatchingTests(Wave19FixtureBase):
    def test_matching_correct_recording_id_needs_no_repair(self):
        self._create_album_and_items(album_id=1)
        # Set item recording IDs to match target exactly
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE items SET mb_trackid=? WHERE id=1", (REC_TARGET_1,))
        con.execute("UPDATE items SET mb_trackid=? WHERE id=2", (REC_TARGET_2,))
        con.commit()
        con.close()

        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["updated"], 0)

    def test_matching_same_title_wrong_recording_id(self):
        self._create_album_and_items(album_id=1)
        res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        self.assertTrue(res.get("ok"))
        self.assertEqual(res["updated"], 2)


class ConcurrencyTests(Wave19FixtureBase):
    def test_concurrency_same_operation_twice(self):
        self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]

        results = []
        def _worker():
            r = execute_album_mb_track_repair_apply(
                self.store,
                op_id,
                db_path=str(self.db_path),
                music_allowed_roots=[str(self.music_root)],
            )
            results.append(r)

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].get("ok"))
        self.assertTrue(results[1].get("ok"))
        completed_count = sum(1 for r in results if r.get("status") == "Completed")
        self.assertGreaterEqual(completed_count, 1)

    def test_concurrency_unrelated_albums(self):
        self._create_album_and_items(album_id=1)
        self._create_album_and_items(album_id=2)

        plan1 = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        plan2 = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 2},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )

        res1 = []
        res2 = []

        t1 = threading.Thread(
            target=lambda: res1.append(execute_album_mb_track_repair_apply(
                self.store, plan1["operation_id"], db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)]
            ))
        )
        t2 = threading.Thread(
            target=lambda: res2.append(execute_album_mb_track_repair_apply(
                self.store, plan2["operation_id"], db_path=str(self.db_path), music_allowed_roots=[str(self.music_root)]
            ))
        )

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertTrue(res1[0].get("ok"))
        self.assertTrue(res2[0].get("ok"))


class RollbackTests(Wave19FixtureBase):
    def test_rollback_full_success(self):
        self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]

        apply_res = execute_album_mb_track_repair_apply(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(apply_res.get("ok"))

        rollback_res = rollback_album_mb_track_repair(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertTrue(rollback_res.get("ok"))
        self.assertEqual(rollback_res["status"], "Rolled Back")

        # Verify DB restored to original recording IDs
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT mb_trackid FROM items WHERE album_id=1 ORDER BY id").fetchall()
        con.close()
        self.assertEqual(rows[0]["mb_trackid"], REC_1)
        self.assertEqual(rows[1]["mb_trackid"], REC_2)

    def test_rollback_unmutated_refused(self):
        self._create_album_and_items(album_id=1)
        plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = plan_res["operation_id"]

        # Attempt rollback on Preview status (not mutated)
        res = rollback_album_mb_track_repair(
            self.store,
            op_id,
            db_path=str(self.db_path),
            music_allowed_roots=[str(self.music_root)],
        )
        self.assertFalse(res.get("ok"))
        self.assertIn("unavailable", res.get("error", "").lower())


class RealProductionPathIntegrationTests(Wave19FixtureBase):
    def test_production_path_repair_flow(self):
        self._create_album_and_items(album_id=1)

        app_module.app.config["TESTING"] = True
        with mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}):
            client = BeetsClient(base_url="http://mock-beets:8338")

        mock_plan_res = create_album_mb_track_repair_plan(
            self.store,
            {"album_id": 1},
            music_allowed_roots=[str(self.music_root)],
            db_path=str(self.db_path),
            fetch_tracklist_fn=lambda _: _fake_tracklist_a(),
        )
        op_id = mock_plan_res["operation_id"]

        def _mock_plan(payload, **kwargs):
            return mock_plan_res

        def _mock_apply(operation_id, **kwargs):
            return execute_album_mb_track_repair_apply(
                self.store,
                operation_id,
                db_path=str(self.db_path),
                music_allowed_roots=[str(self.music_root)],
            )

        with mock.patch.object(app_module, "beets_client") as mock_client, \
             mock.patch.object(app_module.lib, "get_album", return_value=mock.Mock(albumartist="Test Artist", album="Test Album Title")), \
             mock.patch.object(app_module, "_invalidate_lib_cache"), \
             mock.patch.object(app_module, "_trigger_plex_refresh"):

            mock_client.plan_album_mb_track_repair.side_effect = _mock_plan
            mock_client.apply_album_mb_track_repair.side_effect = _mock_apply

            with app_module.app.test_client() as c:
                res = c.post("/api/albums/1/repair-mb-tracks", json={"mb_albumid": REL_A})
                self.assertEqual(res.status_code, 200)
                data = res.get_json()
                self.assertTrue(data.get("ok"))
                job_id = data.get("job_id")
                self.assertTrue(job_id)

                # Execute job synchronously to verify end-to-end flow
                for _ in range(50):
                    job = app_module.jobs.get(job_id)
                    if job and job.status != "running":
                        break
                    time.sleep(0.1)

                job = app_module.jobs.get(job_id)
                self.assertIsNotNone(job)
                self.assertIn(job.status, ("finished", "success"), getattr(job, "log", []))

        # Confirm DB updated via complete production path
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT mb_trackid FROM items WHERE album_id=1 ORDER BY id").fetchall()
        con.close()
        self.assertEqual(rows[0]["mb_trackid"], REC_TARGET_1)
        self.assertEqual(rows[1]["mb_trackid"], REC_TARGET_2)


class WebManagerMutationProhibitionTests(unittest.TestCase):
    def test_app_py_repair_endpoint_contains_no_direct_mutations(self):
        app_path = Path(app_module.__file__)
        source = app_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        repair_fn_def = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "repair_album_mb_tracks":
                repair_fn_def = node
                break

        self.assertIsNotNone(repair_fn_def, "repair_album_mb_tracks not found in app.py")
        fn_source = ast.get_source_segment(source, repair_fn_def)

        # Verify no direct file unlink / rename / remove / move or SQL write calls in repair_album_mb_tracks
        prohibited_strings = [
            "Path.unlink", "os.unlink", "os.remove", "os.rename", "os.replace",
            "shutil.move", "shutil.rmtree", "UPDATE items SET", "_beet_run",
        ]
        for p in prohibited_strings:
            self.assertNotIn(p, fn_source, f"Prohibited call '{p}' found in repair_album_mb_tracks")


if __name__ == "__main__":
    unittest.main()
