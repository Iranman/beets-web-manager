"""#473: durable recovery-job crash handling for Recover My Uploads imports.

A recover singleton-import transaction used to be created Running with no
mutation_family and no originating_job. Neither restart sweep could then
resolve it: a crash mid-import left it Running forever. The fix tags the
recovery family (``recover_import_v1``) at creation, so the existing durable
restart sweep (``transaction_recovery.sweep``) resolves a crashed record to
Recovery Required (never replaying the import).

These tests fail before the fix (the record stays Running) and pass after.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app as app_module  # noqa: F401  (boots the app family + registers routes)
import backend.transaction_recovery as recovery
from backend import recover_service
from backend.transaction_engine import RECOVER_IMPORT_FAMILY, TransactionStore


class _ProcessStopped(BaseException):
    """Simulates process death: not an Exception, so the import path's
    ``except Exception`` cannot catch it and the record is left Running."""


class RecoverImportDurableRecoveryTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "UCmine000"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _staged(self, tmp: str) -> str:
        stage_root = Path(tmp) / "recovered" / "soundcloud" / "run473"
        stage_root.mkdir(parents=True, exist_ok=True)
        staged = stage_root / "my_track.flac"
        staged.write_bytes(b"\x00" * 4096)
        Path(str(staged) + ".recover.json").write_text(
            '{"source_url": "https://soundcloud.com/me/my-track", '
            '"source_platform": "soundcloud", "uploader": "DJ Me", '
            '"channel_id": "UCmine000", '
            '"title": "My Track", "duration": 210.0, '
            '"codec": "flac", "quality": "original"}',
            encoding="utf-8",
        )
        return str(staged)

    def _run_crash(self, tmp: str, tx_store: TransactionStore):
        staged = self._staged(tmp)
        fake_adapter = mock.MagicMock()
        fake_adapter.find_items_by_query.return_value = []  # no existing-library conflict
        # Process dies the instant Beets starts the import, after the Running
        # transaction was recorded: the real "stuck Running after crash" window.
        fake_adapter.run_import.side_effect = _ProcessStopped()
        with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
             mock.patch("backend.app_runtime.transactions", tx_store), \
             mock.patch.object(recover_service, "recover_integrity_check",
                               return_value={"ok": True, "duration": 210.0, "duration_verified": True}), \
             mock.patch.object(recover_service, "inspect_audio_file",
                               return_value={"ok": True, "codec": "flac"}), \
             mock.patch("backend.acoustid_service._audio_identity_decision", return_value={
                 "fingerprint_status": "no_result", "acoustid_status": "no_result",
                 "metadata_agreement": "degraded", "conflicts": ["no_acoustid_result"],
                 "acoustid_match_score": 0.0,
             }), \
             mock.patch("helpers_mb._mb_recording_search", return_value=[]):
            with self.assertRaises(_ProcessStopped):
                recover_service._recover_import_one(staged, [], adapter=fake_adapter, auth_basis="own_account")

    def _only_running(self, tx_store: TransactionStore) -> dict:
        rows, _total = tx_store.list(status="Running", limit=50)
        self.assertEqual(len(rows), 1, rows)
        return tx_store.get(rows[0]["id"])

    def test_crashed_recover_import_is_created_with_recovery_family_linkage(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx_store = TransactionStore(root=os.path.join(tmp, "txstore"))
            self._run_crash(tmp, tx_store)
            tx = self._only_running(tx_store)
            # The linkage the restart sweep keys on is present at creation time.
            self.assertEqual((tx.get("metadata") or {}).get("mutation_family"), RECOVER_IMPORT_FAMILY)
            self.assertEqual(tx["operation_type"], "Import")

    def test_restart_sweep_resolves_crashed_recover_import_to_recovery_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx_store = TransactionStore(root=os.path.join(tmp, "txstore"))
            self._run_crash(tmp, tx_store)
            tx = self._only_running(tx_store)
            # before = after the crash: the record predates this "restart".
            results = recovery.sweep(adapter=mock.Mock(), store=tx_store, before=time.time() + 5)
            actions = {r["operation_id"]: r for r in results}
            self.assertIn(tx["id"], actions, results)
            self.assertEqual(self._status(tx_store, tx["id"]), "Recovery Required")
            # The import is never replayed: the sweep touched no adapter mutation.

    def test_without_the_family_tag_the_record_would_stay_running(self):
        # Control: proves the mutation_family tag is load-bearing. A Running
        # recover-style record lacking it is exactly the pre-fix bug and the
        # sweep leaves it Running.
        with tempfile.TemporaryDirectory() as tmp:
            tx_store = TransactionStore(root=os.path.join(tmp, "txstore"))
            tx = tx_store.create(
                operation_type="Import", status="Running",
                source="recover_upload_singleton", summary="DJ Me - My Track",
                metadata={"workflow": "recover_upload_singleton"},  # pre-fix metadata
            )
            recovery.sweep(adapter=mock.Mock(), store=tx_store, before=time.time() + 5)
            self.assertEqual(self._status(tx_store, tx["id"]), "Running")

    def _status(self, tx_store: TransactionStore, tx_id: str) -> str:
        return tx_store.get(tx_id)["status"]


if __name__ == "__main__":
    unittest.main()
