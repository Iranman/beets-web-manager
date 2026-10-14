"""QA for PR #401 (QA-N1 Release Group fill, QA-N4 linked folders in discovery).

Independent of the implementer's tests: these drive the real MusicBrainz
lookup (only the provider boundary is stubbed), the real Metadata Update
plan/apply/rollback writer through ``routes_library._batch_write_album``,
the batch's in-batch duplicate rule, and the real batch worker for linked
folders.
"""
import io
import json
import tempfile
import unittest
import uuid
from unittest import mock

import backend.composite_workflows as cw
import backend.missing_mb_batch as mb
from backend.transaction_engine import TransactionStore

try:
    import test_missing_mb_batch as base
    from test_mutation_identity_safety import MutAdapter, _IdentityEnv
    import test_qa383_import_batch_e2e as e2e
except ImportError:  # pragma: no cover
    import tests.test_missing_mb_batch as base
    from tests.test_mutation_identity_safety import MutAdapter, _IdentityEnv
    import tests.test_qa383_import_batch_e2e as e2e

RG, REL, RG2, OTHER = base.RG, base.REL, base.RG2, base.OTHER_REC
LOOKUP = "backend.missing_mb_batch.composite_workflows._release_group_for_release"


def setUpModule():
    e2e.setUpModule()


class _Fill(base._Case):
    def setUp(self):
        self.w = base._World(self)
        self.store = TransactionStore(tempfile.mkdtemp(prefix="qa401_"))

    def album(self, aid=1, rel=REL, **item_fields):
        row = self.w.add_album(aid)
        self.w.albums[aid].mb_albumid = rel
        for it in self.w.album_items[aid]:
            it.update(item_fields)
        return row


class RealLookupTests(_Fill):
    """The real _release_group_for_release -> _fetch_mb_release_candidate,
    with only provider_boundary.opened stubbed."""

    def lookup_answer(self, body=None, exc=None):
        def opened(provider, req, timeout=None, **_kw):
            self.assertEqual(provider, "musicbrainz")
            self.assertIn(f"/release/{REL}", req.full_url)
            if exc:
                raise exc
            return mock.MagicMock(__enter__=lambda s: io.BytesIO(body), __exit__=lambda *a: False)
        self.patch("helpers_mb.provider_boundary.opened", side_effect=opened)

    def test_resolved_group_is_proven(self):
        self.lookup_answer(json.dumps({"id": REL, "release-group": {"id": RG.upper()}}).encode())
        proof = mb.prove_row(self.album())
        self.assertEqual((proof["proven"], proof["write"]), (True, {"mb_releasegroupid": RG}))

    def test_provider_down_timeout_and_rate_limit_fail_closed(self):
        # QA-N1 N-e: an outage is evidence_unavailable, never a "no group" answer.
        import urllib.error
        outages = (TimeoutError("timed out"), OSError("connection refused"), RuntimeError("429"),
                   urllib.error.HTTPError("u", 429, "rate", {}, None), urllib.error.HTTPError("u", 503, "x", {}, None))
        for exc in outages:
            with self.subTest(repr(exc)):
                self.w.rows.clear()
                self.lookup_answer(exc=exc)
                proof = mb.prove_row(self.album())
                self.assertEqual((proof["proven"], proof["reasons"], proof["write"]),
                                 (False, ["evidence_unavailable"], {}))

    def test_release_not_found_is_a_genuine_no_group(self):
        import urllib.error
        self.lookup_answer(exc=urllib.error.HTTPError("u", 404, "not found", {}, None))
        proof = mb.prove_row(self.album())
        self.assertEqual((proof["proven"], proof["reasons"]), (False, ["release_group_not_resolved"]))

    def test_malformed_responses_fail_closed(self):
        bodies = {"not json": b"<html>", "list": b"[]", "no group": b'{"id": "x"}',
                  "group not object": b'{"release-group": "abc"}', "bad media": b'{"media": [{"track-count": "x"}]}'}
        for name, body in bodies.items():
            with self.subTest(name):
                self.w.rows.clear()
                self.lookup_answer(body)
                proof = mb.prove_row(self.album())
                self.assertFalse(proof["proven"], proof)
                self.assertEqual(proof["write"], {})

    def test_non_uuid_release_group_from_musicbrainz_is_not_proven(self):
        # A malformed id must never be offered as a proven fill in the preview.
        self.lookup_answer(json.dumps({"id": REL, "release-group": {"id": "not-a-uuid"}}).encode())
        proof = mb.prove_row(self.album())
        self.assertFalse(proof["proven"], proof)
        self.assertEqual(proof["write"], {})

    def test_non_uuid_release_id_on_album_never_reaches_musicbrainz(self):
        self.patch("helpers_mb.provider_boundary.opened", side_effect=AssertionError("network"))
        proof = mb.prove_row(self.album(rel="abc"))
        self.assertEqual(proof["reasons"], ["release_group_not_resolved"])


class BatchEdgeTests(_Fill):
    def setUp(self):
        super().setUp()
        self.lookup = self.patch(LOOKUP, return_value=RG)

    def test_two_fills_to_the_same_group_in_one_batch_are_both_refused(self):
        self.album(1)
        self.album(2)
        preview = mb.run_preview(list(self.w.rows), [], store=self.store)
        self.assertEqual(preview["counts"]["proven"], 0)
        self.assertIsNone(preview["transaction_id"])
        for row in preview["rows"]:
            self.assertEqual(row["reasons"], ["duplicate_release_group"])
            self.assertTrue(row["evidence"]["duplicate_in_batch"])

    def test_fill_and_fingerprint_match_to_the_same_group_in_one_batch_are_refused(self):
        self.album(1)
        self.w.add_album(2)  # no Release ID: proven by the fingerprint path to RG
        preview = mb.run_preview(list(self.w.rows), [], store=self.store)
        self.assertEqual(sorted(r["reasons"][0] for r in preview["rows"]), ["duplicate_release_group"] * 2)

    def test_group_held_by_album_with_other_release_is_a_duplicate(self):
        self.w.add_album(2)
        self.w.albums[2].mb_albumid, self.w.albums[2].mb_releasegroupid = OTHER, RG
        self.assertEqual(mb.prove_row(self.album(1))["reasons"], ["duplicate_release_group"])

    def test_mixed_item_release_ids(self):
        row = self.album(1)
        self.w.album_items[1][0]["mb_albumid"] = REL
        self.w.album_items[1][1]["mb_albumid"] = ""
        self.assertTrue(mb.prove_row(row)["proven"])  # blank item ids do not conflict
        self.w.album_items[1][1]["mb_albumid"] = OTHER
        self.assertEqual(mb.prove_row(row)["reasons"], ["item_release_id_conflict"])
        self.w.album_items[1][1]["mb_albumid"] = REL.upper()  # case only
        self.assertTrue(mb.prove_row(row)["proven"])

    def test_mixed_embedded_groups_one_matching(self):
        row = self.album(1)
        self.w.album_items[1][0]["mb_releasegroupid"] = RG
        self.assertTrue(mb.prove_row(row)["proven"])
        self.w.album_items[1][1]["mb_releasegroupid"] = RG2
        self.assertEqual(mb.prove_row(row)["reasons"], ["embedded_release_group_conflict"])

    def test_symlinked_item_path_is_refused_before_musicbrainz(self):
        row = self.album(1)
        self.patch("backend.missing_mb_batch._has_link_component", return_value=True)
        self.assertEqual(mb.prove_row(row)["reasons"], ["symlinked_item_path"])
        self.lookup.assert_not_called()

    def test_strict_duplicate_lookup_failure_is_evidence_unavailable(self):
        row = self.album(1)
        self.w.rg_lookup.side_effect = RuntimeError("malformed")
        self.assertEqual(mb.prove_row(row)["reasons"], ["evidence_unavailable"])

    def _apply(self, writer):
        preview = mb.run_preview(list(self.w.rows), [], store=self.store)
        self.assertEqual(preview["counts"]["proven"], 1, preview)
        return preview, (lambda: mb.run_apply(preview["transaction_id"], apply_album=writer, apply_item=None,
                                              log=[], store=self.store))

    def test_drift_between_preview_and_apply_writes_nothing(self):
        def never(*_a):
            raise AssertionError("written")
        drifts = {
            "release id changed": (lambda: setattr(self.w.albums[1], "mb_albumid", OTHER), "evidence_changed"),
            "group set elsewhere": (lambda: setattr(self.w.albums[1], "mb_releasegroupid", RG),
                                    "no_longer_proven"),
            "item release id changed": (lambda: self.w.album_items[1][0].update(mb_albumid=OTHER),
                                        "no_longer_proven"),
            "item added": (lambda: self.w.album_items[1].append({"id": 99, "path": "Artist/Album1/03.flac"}),
                           "evidence_changed"),
            "lookup now fails": (lambda: setattr(self.lookup, "return_value", ""), "no_longer_proven"),
        }
        for name, (drift, status) in drifts.items():
            with self.subTest(name):
                self.w.rows.clear()
                self.w.albums.clear()
                self.lookup.return_value = RG
                self.album(1)
                _preview, apply = self._apply(never)
                drift()
                res = apply()
                self.assertEqual([r["status"] for r in res["rows"]], [status], res)

    def test_verify_after_write_catches_a_writer_that_changed_nothing(self):
        self.album(1)
        _preview, apply = self._apply(lambda *_a: {"changed": True, "transaction_ids": ["t"]})
        res = apply()
        self.assertEqual((res["status"], res["rows"][0]["status"]), ("Recovery Required", "recovery_required"))

    def test_preview_applies_once(self):
        def write(aid, proof, log):
            self.w.albums[aid].mb_releasegroupid = RG
            return {"changed": True, "transaction_ids": ["t"]}
        self.album(1)
        preview, apply = self._apply(write)
        self.assertEqual(apply()["status"], "Completed")
        self.assertEqual(apply()["code"], "not_preview")
        self.assertEqual(self.w.albums[1].mb_albumid, REL)


class _RecordingAdapter(MutAdapter):
    def modify(self, fields, item_ids=None, album_ids=None, write=True, move=False, **kw):
        self.flags = getattr(self, "flags", []) + [(dict(fields), write, move)]
        return super().modify(fields, item_ids=item_ids, album_ids=album_ids, write=write, move=move, **kw)


class RealWriterTests(_IdentityEnv):
    """routes_library._batch_write_album -> real plan/apply/rollback_album_metadata."""

    def setUp(self):
        super().setUp()
        import routes_library as rl
        self.rl = rl
        self.ad = _RecordingAdapter(
            albums={1: {"id": 1, "album": "T", "albumartist": "A", "mb_albumid": e2e_rel(), "mb_releasegroupid": ""}},
            items={11: {"id": 11, "album_id": 1, "title": "x", "mb_albumid": e2e_rel(), "mb_releasegroupid": "",
                        "path": self.media("A/1.flac")}})
        for target, value in ((cw, "beets_adapter"), (cw, "get_default_store")):
            p = mock.patch.object(target, value, self.ad if value == "beets_adapter" else (lambda: self.store))
            p.start()
            self.addCleanup(p.stop)

    def proof(self, rg):
        return {"write": {"mb_releasegroupid": rg}}

    def test_fill_writes_only_the_group_with_tags_and_rollback_restores(self):
        from tests.test_mutation_identity_safety import RG_A
        with mock.patch.object(self.rl, "_match_album_run", side_effect=AssertionError("Match album used")):
            out = self.rl._batch_write_album(1, self.proof(RG_A), [])
        op = out["transaction_ids"][0]
        self.assertEqual(self.ad.flags, [({"mb_releasegroupid": RG_A}, True, False)])  # tags written, no move
        self.assertEqual((self.ad.albums[1]["mb_albumid"], self.ad.albums[1]["mb_releasegroupid"]),
                         (e2e_rel(), RG_A))
        tx = self.store.get(op)
        self.assertEqual((tx["status"], tx["operation_type"], tx["rollback"]["available"]),
                         ("Completed", "Metadata Update", True))
        res = cw.rollback_album_metadata(op, adapter=self.ad, store=self.store)
        self.assertEqual(res["status"], "Rolled Back", res)
        self.assertEqual(self.ad.albums[1]["mb_releasegroupid"], "")
        self.assertEqual(self.ad.albums[1]["mb_albumid"], e2e_rel())
        self.assertEqual(self.ad.flags[-1][0], {"mb_releasegroupid": ""})
        self.assertTrue(self.ad.flags[-1][1])  # the rollback rewrites the tags too

    def test_plan_reverifies_the_pairing_at_apply(self):
        from tests.test_mutation_identity_safety import RG_B
        with mock.patch.object(self.rl, "_match_album_run", side_effect=AssertionError("Match album used")):
            with self.assertRaises(mb.NotWritten) as ctx:
                self.rl._batch_write_album(1, self.proof(RG_B), [])
        self.assertIn("release_not_in_release_group", str(ctx.exception))
        self.assertEqual(getattr(self.ad, "flags", []), [])

    def test_lookup_failure_at_apply_writes_nothing(self):
        from tests.test_mutation_identity_safety import RG_A
        with mock.patch.object(cw, "_release_group_for_release", return_value=""), \
                mock.patch.object(self.rl, "_match_album_run", side_effect=AssertionError("Match album used")):
            with self.assertRaises(mb.NotWritten):
                self.rl._batch_write_album(1, self.proof(RG_A), [])
        self.assertEqual(getattr(self.ad, "flags", []), [])

    def test_malformed_group_is_refused_by_the_writer(self):
        with mock.patch.object(self.rl, "_match_album_run", side_effect=AssertionError("Match album used")):
            with self.assertRaises(mb.NotWritten):
                self.rl._batch_write_album(1, self.proof("not-a-uuid"), [])
        self.assertEqual(getattr(self.ad, "flags", []), [])


def e2e_rel():
    from tests.test_mutation_identity_safety import REL_A
    return REL_A


class LinkedFoldersEndToEnd(e2e._Roots):
    """QA-N4 through the real batch worker: a linked file or folder is never
    handed to Beets and its folder gets a symlinked_audio review row."""

    def run_batch(self):
        imported = []

        def fake_reimport(folder, opts, timeout):
            imported.append(folder)
            return {"ok": True, "albums_imported": 1, "imported_album_ids": [len(imported)],
                    "skipped": [], "not_matched": [], "not_matched_known": True}

        ai = e2e.ai
        patches = [
            e2e.patch_app_family(e2e.APP, "_ai_api_key", return_value=""),
            mock.patch.object(ai, "_ai_batch_already_in_library", return_value=False),
            mock.patch.object(ai, "_validate_import_source_audio"),
            mock.patch.object(ai.composite_workflows, "reimport_source", side_effect=fake_reimport),
            mock.patch.object(ai.composite_workflows, "get_album", return_value={"mb_releasegroupid": e2e.RG}),
            mock.patch.object(ai, "_record_recent_import_for_album"),
            mock.patch.object(ai.beets_adapter, "get_plugin_status",
                              return_value={"capabilities": ["mbsubmit"], "fpcalc_available": True}),
            mock.patch.object(ai, "_ai_import_folder", side_effect=AssertionError("Stage 2b not expected")),
            mock.patch.object(ai, "identify_import_folder", side_effect=AssertionError("Stage 2 not expected"),
                              create=True),
            mock.patch.object(ai, "_invalidate_lib_cache"),
            mock.patch.object(ai, "_trigger_plex_refresh"),
            e2e.patch_app_family(e2e.APP, "_AI_PENDING_FILE", self.tmp / "pending.json"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        log = []
        out = ai._run_ai_batch_import(f"qa401-n4-{uuid.uuid4().hex[:8]}", str(self.dl), log, None, None)
        pending = json.loads((self.tmp / "pending.json").read_text()) if (self.tmp / "pending.json").exists() else {}
        return out, imported, log, pending

    def link_file(self, link, target):
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")

    def layout(self):
        self.audio("Real/01.flac")
        self.audio("x/01.flac", base=self.outside)
        self.link_file(self.dl / "LinkedFile" / "01.flac", self.outside / "x" / "01.flac")
        (self.dl / "LinkedDir").mkdir()
        self.symlink(self.dl / "LinkedDir" / "CD1", self.outside / "x")
        self.link_file(self.dl / "Dangling" / "02.flac", self.outside / "gone.flac")
        return self.run_batch()

    def test_linked_file_folder_and_dangling_link_are_refused_and_reach_beets_never(self):
        out, imported, log, pending = self.layout()
        self.assertEqual(imported, [str(self.dl / "Real")], "\n".join(log))
        by = {f["source_folder"] if "source_folder" in f else f.get("folder"): f for f in out["folders"]}
        for name in ("LinkedFile", "LinkedDir", "Dangling"):
            folder = by.get(str(self.dl / name))
            self.assertIsNotNone(folder, (name, list(by)))
            self.assertEqual(folder["status"], "review_created", folder)
            self.assertEqual(folder.get("failure_reason"), "symlinked_audio", folder)
        self.assertFalse(any("outside" in f for f in imported))
        self.assertIn("LinkedFile", [p["folder_name"] for p in pending])

    def test_review_rows_for_linked_folder_and_dangling_link_survive_the_queue_read(self):
        # QA401-F1: the pending store must not prune these rows as "resolved"
        # because the folder has no audio pathlib can see.
        self.layout()
        from backend.pending_review_store import _load_pending_reviews
        names = [p["folder_name"] for p in _load_pending_reviews()]
        self.assertEqual(sorted(n for n in names if n != "Real"), ["Dangling", "LinkedDir", "LinkedFile"])

    def test_link_directly_at_the_scan_root_is_logged_and_never_imported(self):
        self.audio("x/01.flac", base=self.outside)
        self.symlink(self.dl / "AlbumLink", self.outside / "x")
        out, imported, log, _pending = self.run_batch()
        self.assertEqual(imported, [])
        self.assertIn(f"symlink not followed, never imported: {self.dl / 'AlbumLink'}", log)
        # The scan root itself is never queued as an album.
        self.assertNotIn(str(self.dl), [f.get("source_folder") or f.get("folder") for f in out["folders"]])


if __name__ == "__main__":
    unittest.main()
