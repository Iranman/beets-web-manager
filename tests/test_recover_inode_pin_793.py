"""#793 R2: pin the fingerprint-verified staged file through the Beets copy.

``verify_and_match_staged_upload`` decides identity from a staged file's audio
bytes; ``_recover_import_one`` then copies that file into the library via
``beets_adapter.run_import`` (which ingests BY PATH). A plain regular-file swap
on the SAME path after verification but before the copy would import
substituted audio under the verified identity.

These tests fail before the fix (the swapped bytes reach ``run_import``) and
pass after (the identity-pin re-check refuses the import, fail-closed).
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module  # noqa: F401  (boots the app family)
from backend import recover_service
from backend.transaction_engine import TransactionStore


class StagedIdentityPinUnitTests(unittest.TestCase):
    def _write(self, path: Path, data: bytes) -> None:
        path.write_bytes(data)

    def test_pin_stable_for_unchanged_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.flac"
            self._write(p, b"\x01" * 4096)
            self.assertEqual(
                recover_service._staged_identity_pin(str(p)),
                recover_service._staged_identity_pin(str(p)),
            )

    def test_pin_detects_inplace_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.flac"
            self._write(p, b"\x01" * 4096)
            before = recover_service._staged_identity_pin(str(p))
            self._write(p, b"\x02" * 4096)  # same size, different content
            self.assertNotEqual(recover_service._staged_identity_pin(str(p)), before)

    def test_pin_detects_rename_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.flac"
            self._write(p, b"\x01" * 4096)
            before = recover_service._staged_identity_pin(str(p))
            other = Path(tmp) / "evil.flac"
            self._write(other, b"\x09" * 2048)
            os.replace(str(other), str(p))  # new inode under the same path
            self.assertNotEqual(recover_service._staged_identity_pin(str(p)), before)

    def test_missing_file_pins_to_none(self):
        self.assertIsNone(recover_service._staged_identity_pin("/no/such/file.flac"))


class RecoverImportInodePinTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"RECOVER_OWNED_ACCOUNTS": "UCmine000"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _staged(self, tmp: str) -> str:
        stage_root = Path(tmp) / "recovered" / "soundcloud" / "run793"
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

    def _run(self, tmp, tx_store, fake_adapter, *, swap: bool):
        staged = self._staged(tmp)
        real_verify = recover_service.verify_and_match_staged_upload

        def verify_then_maybe_swap(staged_path, **kw):
            res = real_verify(staged_path, **kw)
            if swap and res.get("ok"):
                # Attacker replaces the regular file's content on the SAME path
                # AFTER fingerprint verification, BEFORE the beets copy.
                Path(res["staged_file"]).write_bytes(b"\xff" * 8192)
            return res

        with mock.patch.object(recover_service, "validated_downloads_root", return_value=tmp), \
             mock.patch("backend.app_runtime.transactions", tx_store), \
             mock.patch.object(recover_service, "verify_and_match_staged_upload",
                               side_effect=verify_then_maybe_swap), \
             mock.patch.object(recover_service, "recover_integrity_check",
                               return_value={"ok": True, "duration": 210.0, "duration_verified": True}), \
             mock.patch.object(recover_service, "inspect_audio_file",
                               return_value={"ok": True, "codec": "flac"}), \
             mock.patch.object(recover_service, "_find_recovered_item_by_token",
                               return_value={"id": 1, "path": staged}), \
             mock.patch("backend.acoustid_service._audio_identity_decision", return_value={
                 "fingerprint_status": "no_result", "acoustid_status": "no_result",
                 "metadata_agreement": "degraded", "conflicts": ["no_acoustid_result"],
                 "acoustid_match_score": 0.0,
             }), \
             mock.patch("helpers_mb._mb_recording_search", return_value=[]):
            return recover_service._recover_import_one(
                staged, [], adapter=fake_adapter, auth_basis="own_account"
            )

    def _adapter(self):
        fake_adapter = mock.MagicMock()
        fake_adapter.find_items_by_query.return_value = []
        fake_adapter.find_all_items_by_mbid.return_value = []
        fake_adapter.run_import.return_value = {"ok": True, "added": 1}
        return fake_adapter

    def test_swap_between_verify_and_copy_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx_store = TransactionStore(root=os.path.join(tmp, "txstore"))
            fake_adapter = self._adapter()
            res = self._run(tmp, tx_store, fake_adapter, swap=True)
            # Fail closed: import refused, verified bytes never copied.
            self.assertFalse(res.get("imported"))
            self.assertEqual(res.get("status"), "verification_failed")
            fake_adapter.run_import.assert_not_called()

    def test_unswapped_happy_path_still_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            tx_store = TransactionStore(root=os.path.join(tmp, "txstore"))
            fake_adapter = self._adapter()
            res = self._run(tmp, tx_store, fake_adapter, swap=False)
            # No swap: the pin matches and the copy proceeds.
            fake_adapter.run_import.assert_called_once()
            self.assertTrue(res.get("imported"))


if __name__ == "__main__":
    unittest.main()
