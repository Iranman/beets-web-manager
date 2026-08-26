"""SEC-002 / ARCH-003 Wave 25 Docker acceptance round -- album_artwork_fetch_v1.

Direct in-process unit tests for the new controlled transaction family that
wraps online artwork acquisition (fetchart/embedart), replacing the bare,
untracked run_command() composition BeetsClient.fetch_and_embed_album_art()
previously used. These test transaction_engine.py's Plan/Apply/Verify logic
directly with a fake run_beet_command_fn (no real `beet` subprocess, no
Docker needed) -- the real end-to-end path (Control Agent -> real `beet
fetchart`/`beet embedart`) is covered by the Wave 25 two-service Docker
acceptance script instead.
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.transaction_engine import (  # noqa: E402
    TransactionStore,
    create_album_artwork_fetch_plan,
    execute_album_artwork_fetch_apply,
)


class TestAlbumArtworkFetchV1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "musiclibrary.blb"
        self.tx_dir = self.root / "transactions"
        self.tx_dir.mkdir(parents=True, exist_ok=True)
        self.store = TransactionStore(root=str(self.tx_dir))

        con = sqlite3.connect(self.db_path)
        con.execute("""
            CREATE TABLE albums (
                id INTEGER PRIMARY KEY, album TEXT, albumartist TEXT,
                mb_albumid TEXT, mb_releasegroupid TEXT, artpath TEXT
            )
        """)
        con.execute("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY, album_id INTEGER, path TEXT
            )
        """)
        con.execute("""
            INSERT INTO albums (id, album, albumartist, mb_albumid, mb_releasegroupid, artpath)
            VALUES (1, 'Test Album', 'Test Artist',
                    '11111111-1111-1111-1111-111111111111',
                    '22222222-2222-2222-2222-222222222222', '')
        """)
        con.execute("INSERT INTO items (id, album_id, path) VALUES (101, 1, '/music/a/01.mp3')")
        # A second album with a DIFFERENT id, used to prove item/album id
        # confusion (Wave 25 review's core finding) cannot resurface here.
        con.execute("""
            INSERT INTO albums (id, album, albumartist, mb_albumid, mb_releasegroupid, artpath)
            VALUES (101, 'Unrelated Album', 'Unrelated Artist', '', '', '')
        """)
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _artpath(self, album_id: int) -> str:
        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT artpath FROM albums WHERE id=?", (album_id,)).fetchone()
        con.close()
        return row[0] if row else None

    # ── Plan ─────────────────────────────────────────────────────────────

    def test_plan_rejects_missing_album_id(self):
        res = create_album_artwork_fetch_plan(self.store, {}, db_path=str(self.db_path))
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "album_artwork_fetch_invalid_payload")

    def test_plan_rejects_nonexistent_album(self):
        res = create_album_artwork_fetch_plan(self.store, {"album_id": 9999}, db_path=str(self.db_path))
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code"), "album_artwork_fetch_album_not_found")

    def test_plan_does_not_mutate(self):
        """Plan must never call fetchart/embedart or write to the DB."""
        before = self._artpath(1)
        res = create_album_artwork_fetch_plan(self.store, {"album_id": 1}, db_path=str(self.db_path))
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(self._artpath(1), before)
        self.assertEqual(res.get("existing_artpath"), "")

    def test_plan_captures_real_album_id_not_confused_with_item_id(self):
        """album_id=1 in this fixture has item id 101 -- an album with id
        101 also exists. Plan must operate on the literal album_id given,
        never something derived from an item id."""
        res = create_album_artwork_fetch_plan(self.store, {"album_id": 1}, db_path=str(self.db_path))
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("album_id"), 1)

    # ── Apply: success ───────────────────────────────────────────────────

    def test_apply_success_runs_fetchart_then_embedart_and_verifies(self):
        plan_res = create_album_artwork_fetch_plan(self.store, {"album_id": 1}, db_path=str(self.db_path))
        self.assertTrue(plan_res.get("ok"), plan_res)
        op_id = plan_res["operation_id"]

        calls = []

        def fake_runner(command, args):
            calls.append((command, args))
            if command == "fetchart":
                return {"ok": True}
            if command == "embedart":
                # Simulate the real engine's side effect: fetchart/embedart
                # actually wrote to the DB.
                con = sqlite3.connect(self.db_path)
                con.execute("UPDATE albums SET artpath=? WHERE id=?", ("/music/a/cover.jpg", 1))
                con.commit()
                con.close()
                return {"ok": True}
            return {"ok": False, "error": f"unexpected command {command}"}

        apply_res = execute_album_artwork_fetch_apply(
            self.store, op_id, db_path=str(self.db_path), run_beet_command_fn=fake_runner
        )
        self.assertTrue(apply_res.get("ok"), apply_res)
        self.assertEqual(apply_res.get("status"), "Completed")
        self.assertEqual(apply_res.get("artpath"), "/music/a/cover.jpg")
        self.assertEqual([c[0] for c in calls], ["fetchart", "embedart"])
        self.assertEqual(calls[0][1], ["album_id:1"])
        self.assertEqual(calls[1][1], ["-y", "album_id:1"])

        # Re-applying a Completed transaction must be idempotent, not
        # re-run fetchart/embedart a second time.
        apply_res2 = execute_album_artwork_fetch_apply(
            self.store, op_id, db_path=str(self.db_path), run_beet_command_fn=fake_runner
        )
        self.assertTrue(apply_res2.get("ok"))
        self.assertEqual(len(calls), 2, "Completed transaction must not re-run fetchart/embedart")

    def test_apply_requires_a_runner(self):
        plan_res = create_album_artwork_fetch_plan(self.store, {"album_id": 1}, db_path=str(self.db_path))
        apply_res = execute_album_artwork_fetch_apply(self.store, plan_res["operation_id"], db_path=str(self.db_path))
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "album_artwork_fetch_no_runner")

    # ── Apply: fetchart failure (clean Failed, no partial mutation) ────────

    def test_apply_fetchart_failure_is_clean_failed_not_recovery(self):
        plan_res = create_album_artwork_fetch_plan(self.store, {"album_id": 1}, db_path=str(self.db_path))

        def fake_runner(command, args):
            return {"ok": False, "error": "no art found"}

        apply_res = execute_album_artwork_fetch_apply(
            self.store, plan_res["operation_id"], db_path=str(self.db_path), run_beet_command_fn=fake_runner
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("status"), "Failed")
        self.assertEqual(apply_res.get("code"), "album_artwork_fetch_fetchart_failed")

    # ── Apply: embedart failure after fetchart succeeded -> Recovery Required ──

    def test_apply_embedart_failure_after_fetchart_is_recovery_required(self):
        plan_res = create_album_artwork_fetch_plan(self.store, {"album_id": 1}, db_path=str(self.db_path))

        def fake_runner(command, args):
            if command == "fetchart":
                return {"ok": True}
            return {"ok": False, "error": "embed failed"}

        apply_res = execute_album_artwork_fetch_apply(
            self.store, plan_res["operation_id"], db_path=str(self.db_path), run_beet_command_fn=fake_runner
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("status"), "Recovery Required")
        self.assertEqual(apply_res.get("code"), "album_artwork_fetch_embedart_failed")

        # The status must actually persist as "Recovery Required" -- proves
        # the STATUSES-allowlist fix (previously silently downgraded to
        # "Pending").
        tx = self.store.get(plan_res["operation_id"])
        self.assertEqual(tx["status"], "Recovery Required")

        # A subsequent Apply attempt on a Recovery Required transaction
        # must refuse rather than silently retry fetchart/embedart again.
        calls = []
        def fake_runner2(command, args):
            calls.append(command)
            return {"ok": True}
        apply_res2 = execute_album_artwork_fetch_apply(
            self.store, plan_res["operation_id"], db_path=str(self.db_path), run_beet_command_fn=fake_runner2
        )
        self.assertFalse(apply_res2.get("ok"))
        self.assertEqual(apply_res2.get("code"), "album_artwork_fetch_recovery_required")
        self.assertEqual(calls, [], "a Recovery Required transaction must not silently re-run")

    def test_apply_verification_failure_when_artpath_still_empty(self):
        """Both commands report ok=True but the DB artpath was never
        actually set -- must not be reported as Completed."""
        plan_res = create_album_artwork_fetch_plan(self.store, {"album_id": 1}, db_path=str(self.db_path))

        def fake_runner(command, args):
            return {"ok": True}  # never actually writes artpath

        apply_res = execute_album_artwork_fetch_apply(
            self.store, plan_res["operation_id"], db_path=str(self.db_path), run_beet_command_fn=fake_runner
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("status"), "Recovery Required")
        self.assertEqual(apply_res.get("code"), "album_artwork_fetch_verification_failed")

    # ── Stale plan / identity change between Plan and Apply ────────────────

    def test_apply_rejects_stale_plan_on_identity_change(self):
        plan_res = create_album_artwork_fetch_plan(self.store, {"album_id": 1}, db_path=str(self.db_path))

        # Album identity changes between Plan and Apply (e.g. a concurrent
        # MB re-tag pointed it at a different release).
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE albums SET mb_albumid=? WHERE id=?", ("99999999-9999-9999-9999-999999999999", 1))
        con.commit()
        con.close()

        calls = []
        def fake_runner(command, args):
            calls.append(command)
            return {"ok": True}

        apply_res = execute_album_artwork_fetch_apply(
            self.store, plan_res["operation_id"], db_path=str(self.db_path), run_beet_command_fn=fake_runner
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "album_artwork_fetch_stale_plan")
        self.assertEqual(calls, [], "fetchart/embedart must never run against a stale-identity plan")

    def test_apply_rejects_when_album_deleted_between_plan_and_apply(self):
        plan_res = create_album_artwork_fetch_plan(self.store, {"album_id": 1}, db_path=str(self.db_path))
        con = sqlite3.connect(self.db_path)
        con.execute("DELETE FROM albums WHERE id=1")
        con.commit()
        con.close()

        apply_res = execute_album_artwork_fetch_apply(
            self.store, plan_res["operation_id"], db_path=str(self.db_path),
            run_beet_command_fn=lambda c, a: {"ok": True},
        )
        self.assertFalse(apply_res.get("ok"))
        self.assertEqual(apply_res.get("code"), "album_artwork_fetch_album_not_found")


if __name__ == "__main__":
    unittest.main()
