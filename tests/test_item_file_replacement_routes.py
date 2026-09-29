"""Routes for replacing an album slot's file with a tracked duplicate.

/api/items/<iid>/replacement/plan with candidate_item_id: the candidate's
path comes from Beets (never the client), the pair is AcoustID-verified,
and a Preview transaction is created. Apply goes through the generic
/api/transactions/<id>/approve + /apply routes (or the item apply route)
and reaches the Beets engine only after approval.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as flask_app
import backend.acoustid_service as acoustid_service
import backend.composite_workflows as composite_workflows
import backend.item_replacement as item_replacement
from backend.transaction_engine import TransactionStore

REC = "2513c401-c500-42fe-9113-ce9d9c3295d5"


class ItemFileReplacementRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.mp3 = root / "17 Exotic.mp3"
        self.mp3.write_bytes(b"MP3")
        self.flac = root / "exotic (00).flac"
        self.flac.write_bytes(b"FLAC")
        self.store = TransactionStore(str(root / "transactions"))

        items = {
            24258: {"id": 24258, "title": "Exotic", "album_id": 1935, "mb_trackid": REC, "mb_albumid": "rel",
                    "mb_releasegroupid": "rg", "disc": 1, "track": 17, "format": "MP3", "path": str(self.mp3)},
            22575: {"id": 22575, "title": "exotic (00)", "album_id": None, "mb_trackid": "", "disc": 0,
                    "track": 17, "format": "FLAC", "path": str(self.flac)},
        }
        self.items = items
        self.adapter = mock.MagicMock()
        self.adapter.get_item.side_effect = lambda iid: items.get(int(iid))

        def lib_item(iid):
            row = items.get(int(iid))
            if row is None:
                return None
            obj = mock.MagicMock()
            for k, v in row.items():
                setattr(obj, k, v)
            return obj

        for target, attr, value in (
            (flask_app.lib, "get_item", lib_item),
        ):
            p = mock.patch.object(target, attr, side_effect=value)
            p.start()
            self.addCleanup(p.stop)
        for module in (composite_workflows, item_replacement):
            p = mock.patch.object(module, "beets_adapter", self.adapter)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(composite_workflows, "_default_store", self.store)
        p.start()
        self.addCleanup(p.stop)
        for module in ("routes_maintenance",):
            mod = __import__(module)
            p = mock.patch.object(mod, "transactions", self.store)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})
        p.start()
        self.addCleanup(p.stop)
        flask_app.app.config["TESTING"] = True
        self.client = flask_app.app.test_client()

    def _plan(self, fingerprint=(REC, [REC], [REC])):
        # The replacement authority reads fingerprints from the AcoustID service.
        with mock.patch.object(acoustid_service, "_acoustid_fingerprint_match", return_value=fingerprint), \
             mock.patch.object(acoustid_service, "_acoustid_fingerprint_ids", return_value=[]):
            return self.client.post("/api/items/24258/replacement/plan", json={"candidate_item_id": 22575})

    def test_plan_with_candidate_item_creates_preview_without_mutation(self):
        res = self._plan()
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()
        self.assertEqual(data["status"], "Preview")
        self.assertEqual(data["replacement_item"]["item_id"], 22575)
        tx = self.store.get(data["operation_id"])
        self.assertEqual(tx["metadata"]["mutation_family"], composite_workflows.ITEM_FILE_REPLACEMENT_FAMILY)
        self.assertEqual(tx["metadata"]["before"]["mb_trackid"], REC)
        self.adapter.replace_item_file.assert_not_called()

    def test_plan_resolves_library_relative_paths_for_fingerprinting(self):
        """The stock Beets web API reports paths relative to the library."""
        root = self.mp3.parent
        self.items[24258]["path"] = self.mp3.name
        self.items[22575]["path"] = self.flac.name
        with mock.patch.object(acoustid_service, "MUSIC_ROOT", root), \
             mock.patch.object(acoustid_service, "_acoustid_fingerprint_match", return_value=(REC, [REC], [REC])) as fp:
            res = self.client.post("/api/items/24258/replacement/plan", json={"candidate_item_id": 22575})
        self.assertEqual(res.status_code, 200, res.get_json())
        fp.assert_called_once_with(str(root / self.flac.name), str(root / self.mp3.name))

    def _occupy_canonical_flac(self):
        occupant = self.mp3.with_suffix(".flac")  # where Beets will put the FLAC
        occupant.write_bytes(b"FLAC-untracked")
        return occupant

    def test_identical_audio_occupant_is_planned_for_displacement(self):
        import hashlib
        import backend.replacement_service as replacement_service
        occupant = self._occupy_canonical_flac()
        with mock.patch.object(replacement_service, "_decoded_audio_md5", return_value="a" * 32):
            res = self._plan()
        self.assertEqual(res.status_code, 200, res.get_json())
        displace = res.get_json()["displace_destination"]
        self.assertEqual(displace["path"], str(occupant))
        self.assertEqual(displace["sha256"], hashlib.sha256(b"FLAC-untracked").hexdigest())
        op_id = res.get_json()["operation_id"]

        self.client.post(f"/api/transactions/{op_id}/approve")
        self.adapter.replace_item_file.return_value = {"new_target_path": str(occupant), "quarantine_id": "0" * 32}
        self.items[24258] = {**self.items[24258], "path": str(occupant)}
        self.client.post(f"/api/transactions/{op_id}/apply")
        self.adapter.replace_item_file.assert_called_once_with(
            24258, 22575, idempotency_key=op_id, displace_destination_sha256=displace["sha256"])
        self.assertTrue(occupant.exists())  # the Web Manager itself never moves it

    def test_different_audio_occupant_fails_closed(self):
        import backend.replacement_service as replacement_service
        occupant = self._occupy_canonical_flac()
        md5s = {str(self.flac): "a" * 32, str(occupant): "b" * 32}
        with mock.patch.object(replacement_service, "_decoded_audio_md5", side_effect=lambda p: md5s[p]):
            res = self._plan()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["code"], "destination_occupied")
        self.assertEqual([f for f in self.store.root.glob("*.json") if f.name != "settings.json"], [])

    def test_undecodable_occupant_fails_closed(self):
        import backend.replacement_service as replacement_service
        self._occupy_canonical_flac()
        with mock.patch.object(replacement_service, "_decoded_audio_md5", return_value=""):
            res = self._plan()
        self.assertEqual(res.status_code, 409)

    def test_plan_refuses_an_unverified_candidate(self):
        res = self._plan(fingerprint=("", [], ["other-recording"]))
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["code"], "candidate_not_verified")
        self.assertEqual([f for f in self.store.root.glob("*.json") if f.name != "settings.json"], [])

    def test_plan_rejects_unknown_candidate_item(self):
        with mock.patch.object(acoustid_service, "_acoustid_fingerprint_match", return_value=(REC, [REC], [REC])):
            res = self.client.post("/api/items/24258/replacement/plan", json={"candidate_item_id": 999})
        self.assertEqual(res.status_code, 404)

    def test_preview_approve_apply_through_generic_transaction_routes(self):
        op_id = self._plan().get_json()["operation_id"]

        early = self.client.post(f"/api/transactions/{op_id}/apply")
        self.assertEqual(early.status_code, 409)
        self.assertEqual(early.get_json()["code"], "not_approved")
        self.adapter.replace_item_file.assert_not_called()

        self.assertEqual(self.client.post(f"/api/transactions/{op_id}/approve").status_code, 200)
        new_path = str(self.mp3.with_suffix(".flac"))
        self.adapter.replace_item_file.return_value = {
            "success": True, "new_target_path": new_path, "quarantine_id": "0" * 32, "quarantine_path": "/config/webmanager-quarantine/x/a.mp3",
            "target_snapshot": {"id": 24258}, "source_snapshot": {"id": 22575},
        }
        self.items[24258] = {**self.items[24258], "format": "FLAC", "path": new_path}
        res = self.client.post(f"/api/transactions/{op_id}/apply")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["status"], "Completed")
        self.adapter.replace_item_file.assert_called_once_with(24258, 22575, idempotency_key=op_id, displace_destination_sha256=None)

        self.adapter.rollback_replace_item_file.return_value = {"success": True, "recreated_source_item_id": 30000}
        rb = self.client.post(f"/api/transactions/{op_id}/rollback")
        self.assertEqual(rb.status_code, 200, rb.get_json())
        self.assertEqual(self.store.get(op_id)["status"], "Rolled Back")


@unittest.skipUnless(__import__("shutil").which("ffmpeg"), "ffmpeg not installed")
class DecodedAudioMd5Tests(unittest.TestCase):
    def test_same_audio_matches_and_different_audio_does_not(self):
        import wave
        from backend.audio_preferences import decoded_audio_md5
        with tempfile.TemporaryDirectory() as td:
            def wav(name, frames):
                path = os.path.join(td, name)
                with wave.open(path, "w") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(8000)
                    w.writeframes(frames)
                return path
            a = wav("a.wav", b"\x00\x01" * 4000)
            b = wav("b.wav", b"\x00\x01" * 4000)
            c = wav("c.wav", b"\x00\x02" * 4000)
            self.assertRegex(decoded_audio_md5(a), r"^[0-9a-f]{32}$")
            self.assertEqual(decoded_audio_md5(a), decoded_audio_md5(b))
            self.assertNotEqual(decoded_audio_md5(a), decoded_audio_md5(c))
            self.assertEqual(decoded_audio_md5(os.path.join(td, "missing.wav")), "")


if __name__ == "__main__":
    unittest.main()
