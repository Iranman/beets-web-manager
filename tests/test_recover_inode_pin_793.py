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


def _ctime_discriminates(tmpdir: str) -> bool:
    """True if this platform's st_ctime_ns bumps on a same-content rewrite with
    a restored mtime (Linux inode-change-time, the canonical runner). On
    platforms where st_ctime is creation time (Windows) it does not, so the
    forged-metadata-restore cases cannot exercise the ctime discriminator and
    are skipped there."""
    # Probe raw os.stat (independent of _staged_identity_pin) so this reflects
    # platform semantics, not whether the pin currently includes ctime.
    p = Path(tmpdir) / "_ctime_probe"
    p.write_bytes(b"x" * 64)
    st1 = os.stat(str(p))
    p.write_bytes(b"x" * 64)  # identical content, fresh write
    os.utime(str(p), ns=(st1.st_atime_ns, st1.st_mtime_ns))  # forge mtime back
    return os.stat(str(p)).st_ctime_ns != st1.st_ctime_ns


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

    def test_pin_includes_ctime_ns(self):
        # Platform-independent guard that ctime is pinned (and thus compared,
        # since the re-check is full-dict equality): fails on a pin that omits
        # it. The behavioural forged-restore cases below are gated on ctime
        # actually discriminating, which is reliable only on the Linux runner.
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.flac"
            self._write(p, b"\x01" * 4096)
            self.assertIn("ctime_ns", recover_service._staged_identity_pin(str(p)))

    def test_pin_detects_forged_metadata_restore(self):
        # Swap-and-restore: same inode, identical content (same size+sha), mtime
        # forged back via os.utime. Only st_ctime_ns (which os.utime cannot set)
        # moves -- the pin must still differ.
        with tempfile.TemporaryDirectory() as tmp:
            if not _ctime_discriminates(tmp):
                self.skipTest("platform st_ctime is not inode-change-time")
            p = Path(tmp) / "a.flac"
            self._write(p, b"\x01" * 4096)
            before = recover_service._staged_identity_pin(str(p))
            st = os.stat(str(p))
            self._write(p, b"\x01" * 4096)  # identical content
            os.utime(str(p), ns=(st.st_atime_ns, st.st_mtime_ns))  # forge mtime back
            after = recover_service._staged_identity_pin(str(p))
            self.assertEqual(after["sha256"], before["sha256"])
            self.assertEqual(after["mtime_ns"], before["mtime_ns"])
            self.assertNotEqual(after, before)  # ctime_ns discriminates

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

    def _run(self, tmp, tx_store, fake_adapter, *, swap: bool,
             swap_during_fingerprint: bool = False, forge_restore: bool = False):
        staged = self._staged(tmp)
        real_verify = recover_service.verify_and_match_staged_upload

        def verify_then_maybe_swap(staged_path, **kw):
            pre_bytes = Path(staged_path).read_bytes() if forge_restore else None
            pre_stat = os.stat(staged_path) if forge_restore else None
            res = real_verify(staged_path, **kw)
            if swap and res.get("ok"):
                # Attacker replaces the regular file's content on the SAME path
                # AFTER verification returns, BEFORE the beets copy.
                Path(res["staged_file"]).write_bytes(b"\xff" * 8192)
            if forge_restore and res.get("ok"):
                # Swap-and-restore: rewrite the ORIGINAL content and forge mtime
                # back after verification. dev/ino/size/sha/mtime all match the
                # pin; only ctime (which os.utime cannot set) moves.
                Path(res["staged_file"]).write_bytes(pre_bytes)
                os.utime(res["staged_file"], ns=(pre_stat.st_atime_ns, pre_stat.st_mtime_ns))
            return res

        # The identity-deciding read. When swap_during_fingerprint is set, the
        # attacker rewrites the staged file DURING verify's internal window --
        # after the pin must already be bound, while verify is still deciding
        # identity (the AcoustID + MusicBrainz round-trips). The swap must still
        # be caught at import time (regression for the return-time-pin defect).
        def identity_decision(path, **kw):
            if swap_during_fingerprint:
                Path(path).write_bytes(b"\xee" * 8192)
            return {
                "fingerprint_status": "no_result", "acoustid_status": "no_result",
                "metadata_agreement": "degraded", "conflicts": ["no_acoustid_result"],
                "acoustid_match_score": 0.0,
            }

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
             mock.patch("backend.acoustid_service._audio_identity_decision",
                        side_effect=identity_decision), \
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

    def test_swap_during_verify_internal_window_is_refused(self):
        # Regression: the pin must bind to the bytes identity was DECIDED from,
        # not to verify's return-time bytes. A swap during the fingerprint /
        # AcoustID / MusicBrainz window must still be refused at import.
        with tempfile.TemporaryDirectory() as tmp:
            tx_store = TransactionStore(root=os.path.join(tmp, "txstore"))
            fake_adapter = self._adapter()
            res = self._run(tmp, tx_store, fake_adapter, swap=False, swap_during_fingerprint=True)
            self.assertFalse(res.get("imported"))
            self.assertEqual(res.get("status"), "verification_failed")
            fake_adapter.run_import.assert_not_called()

    def test_forged_metadata_restore_is_refused(self):
        # Swap-and-restore with forged mtime: dev/ino/size/sha/mtime match the
        # pin, only ctime moved. The import must still be refused (ctime pin).
        with tempfile.TemporaryDirectory() as tmp:
            if not _ctime_discriminates(tmp):
                self.skipTest("platform st_ctime is not inode-change-time")
            tx_store = TransactionStore(root=os.path.join(tmp, "txstore"))
            fake_adapter = self._adapter()
            res = self._run(tmp, tx_store, fake_adapter, swap=False, forge_restore=True)
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
