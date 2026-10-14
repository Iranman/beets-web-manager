"""Missing IDs batch fix: proven matches only, preview -> confirm -> apply.

Service tests drive backend.missing_mb_batch with a temp TransactionStore and
fake writers; route tests drive the real endpoints and the real Match album /
attach-recording writers with the engine calls patched.
"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import backend.missing_mb_batch as mb
from backend.app_runtime import MUSIC_ROOT
from backend.provider_boundary import ProviderOutcome, ProviderResult
from backend.transaction_engine import TransactionStore

try:
    from _job_threads import join_jobs  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._job_threads import join_jobs  # noqa: E402

RG = "11111111-1111-1111-1111-111111111111"
REL = "22222222-2222-2222-2222-222222222222"
REC = "33333333-3333-3333-3333-333333333333"
OTHER_REC = "44444444-4444-4444-4444-444444444444"
RG2 = "66666666-6666-6666-6666-666666666666"  # a second, distinct Release Group
FOLDER = str(MUSIC_ROOT / "Artist" / "Album")


def _album_row(aid, **kw):
    return {"id": f"album:{aid}", "target_kind": "album", "album_id": aid, "item_id": 0,
            "path": f"{FOLDER}{aid}", "in_import_review": False, "sort_ts": float(aid), **kw}


def _item_row(iid):
    return {"id": f"item:{iid}", "target_kind": "item", "album_id": 0, "item_id": iid,
            "path": str(MUSIC_ROOT / "Loose" / f"{iid}.mp3"), "sort_ts": float(iid)}


def _ident(aid, eligible=True, files=2, tracks=2, conflicts=(), rg=RG):
    return {
        "auto_import": {"eligible": eligible, "release_group_id": rg if eligible else "",
                        "release_id": REL if eligible else "",
                        "reasons": [] if eligible else ["no_release_group_verified"]},
        "candidates": [{"release_group_id": rg, "representative_release_id": REL,
                        "evidence": {"local_tracks_verified": files, "sources": ["acoustid_release_group"],
                                     "releases_evaluated": [{"release_id": REL, "track_count": tracks}]}}],
        "conflicts": list(conflicts),
        "evidence_capabilities": {"audio_files": files, "acoustid": "available"},
    }


def _candidate(rec=REC, proof="acoustid_recording_id", safe=True, version="v1"):
    return {"mb_trackid": rec, "decision_version": version, "release_id": REL, "release_group_id": RG,
            "decision": {"action_eligibility": {"attach_without_review": safe}, "review_required": not safe,
                         "requires_confirmation": not safe, "conflicts": [], "identity_proof": proof,
                         "eligibility_reason": "test"},
            "matching_contract": {"identity": {"resolved_recording_id": rec}}}


class _World:
    """A tiny fake library: albums/items plus the evidence seams."""

    def __init__(self, case):
        self.albums, self.items, self.album_items, self.idents = {}, {}, {}, {}
        self.candidates, self.fingerprints, self.rows = {}, {}, []
        p = lambda target, **kw: case.patch(target, **kw)  # noqa: E731
        self.get_album = p("backend.missing_mb_batch.lib.get_album", side_effect=self.albums.get)
        self.get_item = p("backend.missing_mb_batch.lib.get_item", side_effect=self.items.get)
        p("backend.missing_mb_batch.composite_workflows.find_all_items_by_album_id",
          side_effect=lambda aid: self.album_items.get(aid, []))
        p("backend.missing_mb_batch.identify_import_folder", side_effect=lambda folder: self.idents[folder])
        p("backend.missing_mb_batch.composite_workflows.find_all_albums_by_releasegroupid",
          side_effect=lambda rg: [{"id": aid, "mb_releasegroupid": a.mb_releasegroupid}
                                  for aid, a in self.albums.items() if a.mb_releasegroupid == rg])
        p("backend.missing_mb_batch._missing_mb_rows", side_effect=lambda search="": list(self.rows))
        self.real_folders = set()
        real_scan = mb._recursive_audio
        p("backend.missing_mb_batch._recursive_audio", side_effect=lambda folder: real_scan(folder)
          if folder in self.real_folders else (self.folder_files.get(folder, []), False))
        self.folder_files = {}
        p("backend.missing_mb_batch._reconstruct_track_recording_candidates",
          side_effect=lambda item, iid: ({}, self.candidates.get(iid, []), item.path, Path(item.path).name))
        p("backend.missing_mb_batch._acoustid_lookup_cached_outcome",
          side_effect=lambda path: self.fingerprints[path])
        p("backend.missing_mb_batch.beets_adapter.get_stats", return_value={"albums": 7, "items": 70})
        self.ai_key = p("backend.matching_service._ai_api_key", side_effect=AssertionError("AI consulted"))
        self.ai_calls = []
        import backend.provider_boundary as pb
        real_opened = pb.opened

        def opened(provider, *a, **kw):
            self.ai_calls.append(provider)
            if provider == "ai":
                raise AssertionError("AI provider called")
            return real_opened(provider, *a, **kw)
        p("backend.provider_boundary.opened", side_effect=opened)

    def add_album(self, aid, **ident_kw):
        self.albums[aid] = SimpleNamespace(mb_albumid="", mb_releasegroupid="", albumartist="A", album="B")
        self.album_items[aid] = [{"id": aid * 10 + n, "path": f"Artist/Album{aid}/0{n}.flac"} for n in (1, 2)]
        self.idents[f"{FOLDER}{aid}"] = _ident(aid, **ident_kw)
        self.folder_files[f"{FOLDER}{aid}"] = [str(MUSIC_ROOT / it["path"]) for it in self.album_items[aid]]
        row = _album_row(aid)
        self.rows.append(row)
        return row

    def add_real_album(self, aid, folder, item_paths):
        """An album on a real temp filesystem: the folder scan is not faked."""
        self.albums[aid] = SimpleNamespace(mb_albumid="", mb_releasegroupid="", albumartist="A", album="B")
        self.album_items[aid] = [{"id": aid * 10 + n, "path": str(p)} for n, p in enumerate(item_paths)]
        self.idents[str(folder)] = _ident(aid, files=len(item_paths), tracks=len(item_paths))
        self.real_folders.add(str(folder))
        row = _album_row(aid, path=str(folder))
        self.rows.append(row)
        return row

    def add_item(self, iid, candidates=None, hits=None):
        path = str(MUSIC_ROOT / "Loose" / f"{iid}.mp3")
        self.items[iid] = SimpleNamespace(album_id=0, mb_trackid="", mb_albumid="", mb_releasegroupid="",
                                          path=path, title="T", artist="A")
        self.candidates[iid] = [_candidate()] if candidates is None else candidates
        hits = [{"mb_trackid": REC, "score": 95}] if hits is None else hits
        self.fingerprints[path] = ProviderResult("acoustid", ProviderOutcome.CONFIRMED, data=hits)
        row = _item_row(iid)
        self.rows.append(row)
        return row

    def write_album(self, aid, proof, log):
        album = self.albums[aid]
        album.mb_albumid, album.mb_releasegroupid = proof["write"]["mb_albumid"], proof["write"]["mb_releasegroupid"]
        return {"changed": True, "transaction_ids": [f"txn_album_{aid}"]}

    def write_item(self, iid, proof, log):
        self.items[iid].mb_trackid = proof["write"]["mb_trackid"]
        return {"changed": True, "transaction_ids": [f"txn_item_{iid}"]}


class _Case(unittest.TestCase):
    def patch(self, target, **kw):
        p = mock.patch(target, **kw)
        self.addCleanup(p.stop)
        return p.start()


class ProofTests(_Case):
    def setUp(self):
        self.w = _World(self)

    def test_proven_album_writes_release_group_and_release(self):
        proof = mb.prove_row(self.w.add_album(1))
        self.assertTrue(proof["proven"], proof["reasons"])
        self.assertEqual(proof["write"], {"mb_releasegroupid": RG, "mb_albumid": REL})

    def test_album_not_eligible_needs_review_and_writes_nothing(self):
        proof = mb.prove_row(self.w.add_album(1, eligible=False))
        self.assertFalse(proof["proven"])
        self.assertEqual((proof["status"], proof["write"]), ("needs_review", {}))
        self.assertIn("no_release_group_verified", proof["reasons"])

    def test_album_track_count_mismatch_is_not_proven(self):
        proof = mb.prove_row(self.w.add_album(1, tracks=3))
        self.assertIn("track_count_mismatch:3/2", proof["reasons"])
        self.assertFalse(proof["proven"])

    def test_album_folder_with_extra_audio_is_not_proven(self):
        proof = mb.prove_row(self.w.add_album(1, files=3))
        self.assertIn("folder_audio_count_mismatch:3/2", proof["reasons"])

    def _tree(self):
        root = Path(tempfile.mkdtemp(prefix="mbbatch_fs_"))
        self.addCleanup(__import__("shutil").rmtree, root, True)
        (root / "A").mkdir()
        (root / "B").mkdir()
        return root

    def _link(self, target, link, **kw):
        try:
            os.symlink(target, link, **kw)
        except (OSError, NotImplementedError) as ex:  # Windows without symlink rights
            self.skipTest(f"symlinks unavailable: {ex}")

    def test_real_album_folder_is_proven(self):
        root = self._tree()
        for n in ("x", "y"):
            (root / "B" / f"{n}.flac").write_bytes(b"a")
        proof = mb.prove_row(self.w.add_real_album(1, root / "B", [root / "B" / "x.flac", root / "B" / "y.flac"]))
        self.assertTrue(proof["proven"], proof["reasons"])

    def test_item_stored_through_a_symlinked_file_is_not_proven(self):
        # album folder B; item 2 is stored as A/y.flac, a link to B/y.flac
        root = self._tree()
        for n in ("x", "y"):
            (root / "B" / f"{n}.flac").write_bytes(b"a")
        self._link(root / "B" / "y.flac", root / "A" / "y.flac")
        proof = mb.prove_row(self.w.add_real_album(1, root / "B", [root / "B" / "x.flac", root / "A" / "y.flac"]))
        self.assertEqual((proof["proven"], proof["reasons"]), (False, ["symlinked_item_path"]))

    def test_item_stored_through_a_symlinked_parent_dir_is_not_proven(self):
        root = self._tree()
        for n in ("x", "y"):
            (root / "B" / f"{n}.flac").write_bytes(b"a")
        self._link(root / "B", root / "L", target_is_directory=True)
        proof = mb.prove_row(self.w.add_real_album(1, root / "B", [root / "B" / "x.flac", root / "L" / "y.flac"]))
        self.assertEqual((proof["proven"], proof["reasons"]), (False, ["symlinked_item_path"]))

    def test_folder_with_same_count_but_other_files_is_not_proven(self):
        root = self._tree()
        for n in ("x", "z"):
            (root / "B" / f"{n}.flac").write_bytes(b"a")
        proof = mb.prove_row(self.w.add_real_album(1, root / "B", [root / "B" / "x.flac", root / "B" / "y.flac"]))
        self.assertEqual((proof["proven"], proof["reasons"]), (False, ["folder_files_mismatch:2/2"]))

    def test_music_root_reached_through_a_symlink_still_proves(self):
        # The operator's mount may be a link; links below it are still refused.
        root = self._tree()
        (root / "A" / "B").mkdir()
        for n in ("x", "y"):
            (root / "A" / "B" / f"{n}.flac").write_bytes(b"a")
        self._link(root / "A", root / "M", target_is_directory=True)
        self.patch("backend.missing_mb_batch.MUSIC_ROOT", new=root / "M")
        folder = root / "M" / "B"
        proof = mb.prove_row(self.w.add_real_album(1, folder, [folder / "x.flac", folder / "y.flac"]))
        self.assertTrue(proof["proven"], proof["reasons"])
        self._link(root / "A" / "B", root / "A" / "L", target_is_directory=True)
        proof = mb.prove_row(self.w.add_real_album(2, folder, [folder / "x.flac", root / "M" / "L" / "y.flac"]))
        self.assertEqual(proof["reasons"], ["symlinked_item_path"])

    def test_existing_other_release_group_is_never_replaced(self):
        row = self.w.add_album(1)
        self.w.albums[1].mb_releasegroupid = "55555555-5555-5555-5555-555555555555"
        proof = mb.prove_row(row)
        self.assertEqual((proof["proven"], proof["write"]), (False, {}))
        self.assertIn("existing_release_group_differs", proof["reasons"])

    def test_release_group_held_by_another_album_is_a_duplicate(self):
        self.w.albums[9] = SimpleNamespace(mb_albumid=REL, mb_releasegroupid=RG)
        proof = mb.prove_row(self.w.add_album(1))
        self.assertEqual((proof["proven"], proof["reasons"]), (False, ["duplicate_release_group"]))
        self.assertEqual(proof["evidence"]["release_group_held_by"], [9])

    def test_album_conflict_or_import_review_claim_is_not_proven(self):
        self.assertIn("conflicts:symlinked_audio_file",
                      mb.prove_row(self.w.add_album(1, conflicts=["symlinked_audio_file"]))["reasons"])
        row = self.w.add_album(2)
        row["in_import_review"] = True
        self.assertEqual(mb.prove_row(row)["reasons"], ["in_import_review"])

    def test_proven_item_needs_one_fingerprint_confirmed_safe_candidate(self):
        proof = mb.prove_row(self.w.add_item(5))
        self.assertTrue(proof["proven"], proof["reasons"])
        self.assertEqual(proof["write"], {"mb_trackid": REC})

    def test_item_without_fingerprint_proof_is_not_proven(self):
        cases = {
            6: dict(candidates=[_candidate(proof="textual_support")]),                 # text only
            7: dict(candidates=[_candidate(safe=False)]),                             # review required
            8: dict(candidates=[_candidate(), _candidate(rec=OTHER_REC)]),            # two safe candidates
            9: dict(hits=[{"mb_trackid": REC, "score": 90}, {"mb_trackid": OTHER_REC, "score": 89}]),  # ambiguous
            10: dict(hits=[{"mb_trackid": OTHER_REC, "score": 95}]),                  # fingerprint disagrees
        }
        for iid, kw in cases.items():
            proof = mb.prove_row(self.w.add_item(iid, **kw))
            self.assertFalse(proof["proven"], (iid, proof))
            self.assertEqual(proof["write"], {}, iid)

    def test_evidence_failure_fails_closed(self):
        row = self.w.add_album(1)
        self.w.idents.clear()  # identify_import_folder raises KeyError
        self.assertEqual(mb.prove_row(row)["reasons"], ["evidence_unavailable"])


class _BatchCase(_Case):
    def setUp(self):
        self.w = _World(self)
        self.store = TransactionStore(tempfile.mkdtemp(prefix="mbbatch_tx_"))

    def preview(self, rows):
        return mb.run_preview(rows, [], store=self.store)

    def apply(self, tx_id, cancel=None, **writers):
        return mb.run_apply(tx_id, apply_album=writers.get("album", self.w.write_album),
                            apply_item=writers.get("item", self.w.write_item),
                            log=[], cancel_event=cancel, store=self.store)


class BatchTests(_BatchCase):
    def test_mixed_batch_writes_only_proven_rows_with_snapshot_and_audit(self):
        rows = [self.w.add_album(1), self.w.add_item(5), self.w.add_album(2, eligible=False)]
        pre = self.preview(rows)
        self.assertEqual(pre["counts"], {"rows": 3, "proven": 2, "needs_review": 1})
        tx = self.store.get(pre["transaction_id"])
        self.assertEqual((tx["status"], tx["dry_run"]), ("Preview", True))
        self.assertEqual([c["id"] for c in tx["changes"]], ["album:1", "item:5"])

        album_writer = mock.Mock(side_effect=self.w.write_album)
        out = self.apply(pre["transaction_id"], album=album_writer)
        self.assertTrue(out["ok"])
        self.assertEqual([(r["row_id"], r["status"]) for r in out["rows"]],
                         [("album:1", "written"), ("item:5", "written")])
        album_writer.assert_called_once()  # album:2 (not proven) never reached a writer
        self.assertEqual(self.w.albums[2].mb_albumid, "")
        tx = self.store.get(pre["transaction_id"])
        self.assertEqual(tx["status"], "Completed")
        snap = tx["metadata"]["pre_apply_snapshot"]
        self.assertEqual((snap["albums"], snap["items"]), (7, 70))
        self.assertEqual(snap["rows"]["album:1"]["mb_albumid"], "")  # captured before the write
        self.assertEqual(tx["metadata"]["results"][0]["transaction_ids"], ["txn_album_1"])

    def test_nothing_proven_creates_no_transaction(self):
        pre = self.preview([self.w.add_album(1, eligible=False)])
        self.assertIsNone(pre["transaction_id"])
        self.assertIsNone(pre["confirm_token"])

    def test_evidence_changed_between_preview_and_apply_skips_the_row(self):
        rows = [self.w.add_album(1), self.w.add_item(5)]
        pre = self.preview(rows)
        self.w.idents[f"{FOLDER}1"]["candidates"][0]["evidence"]["local_tracks_verified"] = 1
        self.w.candidates[5] = [_candidate(version="v2")]
        writer = mock.Mock()
        out = self.apply(pre["transaction_id"], album=writer, item=writer)
        self.assertEqual([r["status"] for r in out["rows"]], ["evidence_changed", "evidence_changed"])
        writer.assert_not_called()

    def test_row_no_longer_proven_at_apply_is_skipped(self):
        pre = self.preview([self.w.add_album(1), self.w.add_album(2, rg=RG2)])
        self.w.idents[f"{FOLDER}1"] = _ident(1, eligible=False)
        out = self.apply(pre["transaction_id"])
        self.assertEqual([r["status"] for r in out["rows"]], ["no_longer_proven", "written"])

    def test_same_release_group_in_one_batch_is_a_duplicate(self):
        pre = self.preview([self.w.add_album(1), self.w.add_album(2), self.w.add_item(5)])
        self.assertEqual([r["reasons"] for r in pre["rows"]],
                         [["duplicate_release_group"], ["duplicate_release_group"], []])
        self.assertEqual(pre["counts"]["proven"], 1)

    def test_duplicate_appearing_after_preview_is_not_written(self):
        pre = self.preview([self.w.add_album(1)])
        self.w.albums[9] = SimpleNamespace(mb_albumid=REL, mb_releasegroupid=RG)
        writer = mock.Mock()
        out = self.apply(pre["transaction_id"], album=writer)
        writer.assert_not_called()
        self.assertEqual(out["rows"][0]["status"], "no_longer_proven")

    def test_identity_set_after_preview_is_evidence_changed(self):
        # Same group, still provable, but the album changed under the preview.
        pre = self.preview([self.w.add_album(1)])
        self.w.albums[1].mb_releasegroupid = RG
        writer = mock.Mock()
        out = self.apply(pre["transaction_id"], album=writer)
        writer.assert_not_called()
        self.assertEqual(out["rows"][0]["status"], "evidence_changed")

    def test_cancel_mid_run_finishes_current_row_then_stops(self):
        pre = self.preview([self.w.add_album(1), self.w.add_album(2, rg=RG2), self.w.add_item(5)])
        cancel = threading.Event()

        def write_then_cancel(aid, proof, log):
            cancel.set()
            return self.w.write_album(aid, proof, log)
        out = self.apply(pre["transaction_id"], cancel=cancel, album=write_then_cancel)
        self.assertEqual([r["status"] for r in out["rows"]], ["written", "cancelled", "not_attempted"])
        self.assertEqual(self.w.albums[2].mb_albumid, "")
        self.assertEqual(self.store.get(pre["transaction_id"])["status"], "Cancelled")

    def test_write_failure_stops_the_batch(self):
        pre = self.preview([self.w.add_album(1), self.w.add_item(5)])
        out = self.apply(pre["transaction_id"], album=mock.Mock(side_effect=RuntimeError("relocate failed")))
        self.assertFalse(out["ok"])
        self.assertEqual([r["status"] for r in out["rows"]], ["write_failed", "not_attempted"])
        self.assertEqual(self.w.items[5].mb_trackid, "")
        tx = self.store.get(pre["transaction_id"])
        self.assertEqual(tx["status"], "Failed")
        self.assertNotIn("relocate failed", str(out["rows"]))  # fixed text out, detail in the log
        self.assertIn("relocate failed", "\n".join(tx["logs"]))

    def test_unverified_write_is_recovery_required(self):
        pre = self.preview([self.w.add_album(1)])
        out = self.apply(pre["transaction_id"], album=mock.Mock(return_value={"changed": True}))
        self.assertEqual(out["rows"][0]["status"], "recovery_required")
        self.assertEqual(self.store.get(pre["transaction_id"])["status"], "Recovery Required")

    def test_writer_refusal_skips_without_stopping(self):
        pre = self.preview([self.w.add_album(1), self.w.add_item(5)])
        out = self.apply(pre["transaction_id"], album=mock.Mock(side_effect=mb.NotWritten("requires_review")))
        self.assertEqual([r["status"] for r in out["rows"]], ["no_longer_proven", "written"])

    def test_preview_applies_once(self):
        pre = self.preview([self.w.add_album(1)])
        self.apply(pre["transaction_id"])
        self.assertEqual(self.apply(pre["transaction_id"])["code"], "not_preview")
        err, status = mb.check_apply_request(pre["transaction_id"], pre["confirm_token"], store=self.store)
        self.assertEqual((err["code"], status), ("not_preview", 409))

    def test_confirm_token_is_required_and_must_match(self):
        pre = self.preview([self.w.add_album(1)])
        tid = pre["transaction_id"]
        self.assertEqual(mb.check_apply_request(tid, None, store=self.store)[1], 400)
        self.assertEqual(mb.check_apply_request(tid, "", store=self.store)[0]["code"], "confirm_token_required")
        self.assertEqual(mb.check_apply_request(tid, "nope", store=self.store)[0]["code"], "confirm_token_mismatch")
        self.assertEqual(mb.check_apply_request(tid, "t\u00f6k\u00e9n\ud800", store=self.store),
                         ({"ok": False, "code": "confirm_token_mismatch",
                           "error": "The confirm_token does not match this preview."}, 409))
        meta = self.store.get(tid)["metadata"]
        self.assertNotIn("confirm_token", meta)  # only its hash is stored
        self.assertNotIn(pre["confirm_token"], str(self.store.get(tid)))
        # The live job result keeps the token for the UI; the persisted job copy does not.
        import job_engine
        self.assertNotIn(pre["confirm_token"], str(job_engine.redact_for_persistence(pre)))
        self.assertEqual(mb.check_apply_request("txn_missing", "x", store=self.store)[1], 404)
        self.assertEqual(mb.check_apply_request(tid, pre["confirm_token"], store=self.store), (None, 200))

    def test_ai_is_never_consulted(self):
        pre = self.preview([self.w.add_album(1), self.w.add_item(5)])
        self.apply(pre["transaction_id"])
        self.w.ai_key.assert_not_called()
        self.assertNotIn("ai", self.w.ai_calls)


class RouteTests(_Case):
    """The real endpoints and the real Match album / attach-recording writers."""

    @classmethod
    def setUpClass(cls):
        import app as app_module  # noqa: F401  (registers routes)
        import routes_library
        cls.client = app_module.app.test_client()
        cls.routes = routes_library

    def setUp(self):
        mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"}).start()
        self.addCleanup(mock.patch.stopall)
        from backend.app_runtime import jobs, transactions
        self.jobs = jobs
        old_root = transactions.root
        transactions.root = Path(tempfile.mkdtemp(prefix="mbbatch_route_tx_"))
        self.addCleanup(setattr, transactions, "root", old_root)
        self.transactions = transactions
        self.w = _World(self)
        r = "routes_library."
        # Earlier suites may rebind routes_library.lib; point the writers at the fake world too.
        self.patch(r + "lib.get_item", side_effect=self.w.items.get)
        self.patch(r + "lib.get_album", side_effect=self.w.albums.get)
        self.patch(r + "_invalidate_lib_cache", return_value=None)
        self.patch(r + "_trigger_plex_refresh", return_value=None)

    def tearDown(self):
        join_jobs(self.jobs)

    def wait(self, job_id):
        job = self.jobs.get(job_id)
        deadline = time.monotonic() + 10
        while job.status == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        return job

    def preview(self, ids):
        res = self.client.post("/api/library/missing-mb-ids/batch/preview", json={"ids": ids})
        self.assertEqual(res.status_code, 200, res.get_json())
        job = self.wait(res.get_json()["job_id"])
        self.assertEqual(job.status, "success", job.log)
        return job.result

    def apply(self, pre, token=None):
        return self.client.post("/api/library/missing-mb-ids/batch/apply", json={
            "transaction_id": pre["transaction_id"],
            "confirm_token": pre["confirm_token"] if token is None else token})

    def patch_match_writer(self, relocate_ok=True):
        r = "routes_library."
        self.patch(r + "_resolve_mb_release_id", side_effect=lambda mb_input, log: mb_input)
        self.patch(r + "_album_mb_match_plan", return_value={"matched_count": 2, "actual_count": 2,
                                                             "expected_count": 2, "unmatched_items": []})
        self.patch(r + "_match_tracks_from_mb", return_value=2)
        self.patch(r + "_strip_year_from_album_name", return_value=None)
        cw = r + "composite_workflows."

        def update_album_metadata(aid, fields, **kw):
            if "mb_albumid" in fields:
                self.w.albums[aid].mb_albumid = fields["mb_albumid"]
                self.w.albums[aid].mb_releasegroupid = RG
                return {"ok": True, "operation_id": f"txn_meta_{aid}"}
            return {"ok": True}
        self.update_metadata = self.patch(cw + "update_album_metadata", side_effect=update_album_metadata)
        self.patch(cw + "plan_album_mb_track_repair", return_value={"ok": True})
        self.relocate = self.patch(cw + "relocate_album", return_value={"ok": True} if relocate_ok
                                   else {"ok": False, "error": "relocate refused"})

        def rollback(op_id):
            aid = int(op_id.rsplit("_", 1)[1])
            self.w.albums[aid].mb_albumid = self.w.albums[aid].mb_releasegroupid = ""
            return {"ok": True}
        self.rollback = self.patch(cw + "rollback_album_metadata", side_effect=rollback)

    def test_apply_without_confirm_token_is_refused(self):
        pre = self.preview([self.w.add_album(1)["id"]])
        res = self.apply(pre, token="")
        self.assertEqual((res.status_code, res.get_json()["code"]), (400, "confirm_token_required"))
        res = self.client.post("/api/library/missing-mb-ids/batch/apply", json={"transaction_id": pre["transaction_id"]})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.transactions.get(pre["transaction_id"])["status"], "Preview")

    def test_preview_rejects_bad_selection(self):
        res = self.client.post("/api/library/missing-mb-ids/batch/preview", json={"ids": []})
        self.assertEqual((res.status_code, res.get_json()["error_code"]), (400, "INVALID_PARAM"))
        res = self.client.post("/api/library/missing-mb-ids/batch/preview",
                               json={"ids": [f"album:{n}" for n in range(mb.MAX_BATCH_ROWS + 1)]})
        self.assertEqual(res.status_code, 400)

    def test_malformed_ids_are_rejected(self):
        for bad in ("album:0", "album:012", "album:12x", " album:12", "track:3", "album:12345678901", "item:-1"):
            res = self.client.post("/api/library/missing-mb-ids/batch/preview", json={"ids": [bad]})
            self.assertEqual((res.status_code, res.get_json()["error_code"]), (400, "INVALID_PARAM"), bad)

    def test_unknown_row_id_is_reported_not_written(self):
        pre = self.preview(["album:999"])
        self.assertEqual(pre["rows"][0]["reasons"], ["not_in_missing_list"])
        self.assertIsNone(pre["transaction_id"])

    def test_album_through_the_real_match_writer(self):
        self.patch_match_writer()
        pre = self.preview([self.w.add_album(1)["id"]])
        res = self.apply(pre)
        self.assertEqual(res.status_code, 200, res.get_json())
        job = self.wait(res.get_json()["job_id"])
        self.assertEqual(job.status, "success", job.log)
        self.assertEqual(job.result["rows"][0]["status"], "written")
        self.assertEqual((self.w.albums[1].mb_albumid, self.w.albums[1].mb_releasegroupid), (REL, RG))
        tx = self.transactions.get(pre["transaction_id"])
        self.assertEqual((tx["status"], tx["originating_job"]), ("Completed", job.job_id))
        self.assertEqual(tx["metadata"]["results"][0]["transaction_ids"], ["txn_meta_1"])

    def test_batch_never_writes_as_operator_selected(self):
        self.patch_match_writer()
        pre = self.preview([self.w.add_album(1)["id"]])
        self.wait(self.apply(pre).get_json()["job_id"])
        stamp = [c for c in self.update_metadata.call_args_list if "mb_albumid" in c.args[1]]
        self.assertEqual([c.kwargs.get("release_selected_by_operator") for c in stamp], [False])
        # The operator's own Match keeps its meaning.
        self.update_metadata.reset_mock()
        self.w.add_album(2, rg=RG2)
        self.wait(self.client.post("/api/albums/2/match", json={"mb_id": REL}).get_json()["job_id"])
        stamp = [c for c in self.update_metadata.call_args_list if "mb_albumid" in c.args[1]]
        self.assertEqual([c.kwargs.get("release_selected_by_operator") for c in stamp], [True])

    def test_transaction_failure_rolls_back_and_stops(self):
        self.patch_match_writer(relocate_ok=False)
        pre = self.preview([self.w.add_album(1)["id"], self.w.add_album(2, rg=RG2)["id"]])
        job = self.wait(self.apply(pre).get_json()["job_id"])
        self.assertEqual(job.status, "failed")
        self.assertEqual([r["status"] for r in job.result["rows"]], ["write_failed", "not_attempted"])
        self.rollback.assert_called_once_with("txn_meta_1")
        self.assertEqual(self.w.albums[1].mb_albumid, "")  # rolled back
        self.assertEqual(self.w.albums[2].mb_albumid, "")  # never attempted
        self.assertEqual(self.transactions.get(pre["transaction_id"])["status"], "Failed")

    def test_item_through_the_real_attach_recording_writer(self):
        row = self.w.add_item(5)
        r = "routes_library."
        self.patch(r + "_reconstruct_track_recording_candidates",
                   side_effect=lambda item, iid: ({"title": "T"}, self.w.candidates[iid], item.path, "5.mp3"))
        self.patch(r + "_fetch_mb_recording_details", return_value={})
        self.patch(r + "composite_workflows.plan_album_mb_track_repair", return_value={"ok": True, "operation_id": "op1"})

        def apply_repair(op_id, write_tags=True):
            self.w.items[5].mb_trackid = REC
            return {"ok": True}
        self.patch(r + "composite_workflows.apply_album_mb_track_repair", side_effect=apply_repair)
        pre = self.preview([row["id"]])
        job = self.wait(self.apply(pre).get_json()["job_id"])
        self.assertEqual(job.status, "success", job.log)
        result = job.result["rows"][0]
        self.assertEqual(result["status"], "written", (result, job.log))
        child = self.transactions.get(result["transaction_ids"][0])
        self.assertEqual((child["status"], child["metadata"]["mode"]), ("Completed", "safe"))
        self.assertEqual(self.w.items[5].mb_trackid, REC)


if __name__ == "__main__":
    unittest.main()
