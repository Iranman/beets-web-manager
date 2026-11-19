"""PR5 / Lane E: end-to-end import reliability + manual-RGID -> release -> import.

Permanent regression fixtures for the mutation-safety seams of the confirmed
import path. Every case uses isolated synthetic fixtures (an in-tmp
TransactionStore and a mock BeetsAdapter) -- never the live library, never a
real ``beet import``.

The resolver's own guarantees (same-release-group-only, fail-closed, never a
cross-RG reassign, MusicBrainz-down -> Review) are locked by
``test_reimport_release_group_fallback`` and ``test_pr3_review_alt_candidates_manual_mbid``.
The confirmed-import apply guards (release_group_mismatch, not_imported,
invalid_plan, second-apply-refused on a *Completed* row) are locked by
``test_import_paths_beets_native``. This file adds the gaps those leave:

* an *interrupted* import (a crash that left the transaction Running mid-apply)
  is idempotent -- a retry is refused and Beets is never re-run, and the restart
  recovery sweep marks it Recovery Required without ever replaying it;
* a combined album (Beets produces more than one album row for the pinned
  release) fails as ``import_ambiguous`` and keeps every row for review, so one
  side is never silently chosen;
* the PR3 shared resolver and the controlled plan/apply pipeline compose: a
  manual release-group id resolves to a compatible release that then imports
  through the controlled path, and a resolver that fails closed ("") routes the
  folder to Review without ever planning or applying a mutation.
"""
import tempfile
import time
import unittest
from unittest import mock

try:
    import test_import_paths_beets_native as base
except ImportError:  # pragma: no cover
    from tests import test_import_paths_beets_native as base

try:
    import test_pr3_review_alt_candidates_manual_mbid as pr3
except ImportError:  # pragma: no cover
    from tests import test_pr3_review_alt_candidates_manual_mbid as pr3

import backend.library_service as lsvc
import backend.transaction_recovery as recovery

REL, RG = base.REL, base.RG
cw = base.cw


class InterruptedImportIdempotencyTests(unittest.TestCase):
    """A confirmed import records Running + ``mutation_started`` BEFORE it asks
    Beets to import (composite_workflows.apply_confirmed_import). A process that
    dies between those two points leaves the transaction Running. It must never
    be replayed: a retry is refused and recovery closes it Recovery Required."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = base.TransactionStore(tmp.name)

    def _plan(self):
        return cw.plan_confirmed_import(
            {"source_folder": "/downloads/A", "mb_albumid": REL, "mb_releasegroupid": RG},
            store=self.store)["operation_id"]

    def _crash_mid_apply(self, ad):
        # KeyboardInterrupt is a BaseException: apply_confirmed_import's
        # ``except Exception`` does not catch it, so it propagates exactly as a
        # killed process would -- after the Running claim, before any finish.
        ad.run_import.side_effect = KeyboardInterrupt
        op = self._plan()
        with self.assertRaises(KeyboardInterrupt):
            cw.apply_confirmed_import(op, adapter=ad, store=self.store, timeout=60.0)
        return op

    def test_interrupted_apply_leaves_running_with_mutation_breadcrumb(self):
        ad = base._adapter({"id": 9, "mb_albumid": REL, "mb_releasegroupid": RG})
        op = self._crash_mid_apply(ad)
        tx = self.store.get(op)
        self.assertEqual(tx["status"], "Running")
        self.assertTrue((tx["metadata"].get("engine_result") or {}).get("mutation_started"))
        self.assertEqual(ad.run_import.call_count, 1)

    def test_retry_of_an_interrupted_import_is_refused_and_never_double_applies(self):
        ad = base._adapter({"id": 9, "mb_albumid": REL, "mb_releasegroupid": RG})
        op = self._crash_mid_apply(ad)
        ad.run_import.side_effect = None  # a fresh attempt would succeed if it ran
        again = cw.apply_confirmed_import(op, adapter=ad, store=self.store, timeout=60.0)
        self.assertEqual(again["code"], "not_applicable")
        self.assertFalse(again["mutated"])
        self.assertEqual(self.store.get(op)["status"], "Running")
        self.assertEqual(ad.run_import.call_count, 1, "a Running import must never be re-run")

    def test_restart_sweep_marks_it_recovery_required_and_never_replays(self):
        ad = base._adapter({"id": 9, "mb_albumid": REL, "mb_releasegroupid": RG})
        op = self._crash_mid_apply(ad)
        ad.run_import.side_effect = None
        # ``before`` = the process start; the Running row predates it, so it is an
        # interrupted apply, not one still live in this process.
        results = recovery.sweep(adapter=ad, store=self.store, before=time.time() + 3600)
        mine = [r for r in results if r.get("operation_id") == op]
        self.assertEqual([r["action"] for r in mine], ["Recovery Required"], results)
        self.assertEqual(self.store.get(op)["status"], "Recovery Required")
        self.assertEqual(ad.run_import.call_count, 1, "recovery must never replay the import")


class CombinedAlbumAmbiguousApplyTests(unittest.TestCase):
    """Beets tagging the folder as more than one album for the pinned release
    (a combined/mixed source) must fail as ``import_ambiguous`` and keep every
    row for review -- one side is never silently chosen or dropped."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = base.TransactionStore(tmp.name)

    def test_two_album_rows_fail_ambiguous_and_keep_both(self):
        ad = mock.MagicMock()
        # Pre-import set empty; after import Beets produced TWO rows for REL.
        ad.find_all_albums_by_mb_albumid.side_effect = [
            [],
            [{"id": 9, "mb_albumid": REL, "mb_releasegroupid": RG},
             {"id": 10, "mb_albumid": REL, "mb_releasegroupid": RG}],
        ]
        op = cw.plan_confirmed_import(
            {"source_folder": "/downloads/A", "mb_albumid": REL, "mb_releasegroupid": RG},
            store=self.store)["operation_id"]
        res = cw.apply_confirmed_import(op, adapter=ad, store=self.store, timeout=60.0)
        self.assertEqual(res["code"], "import_ambiguous")
        self.assertTrue(res["mutated"])
        self.assertEqual(sorted(res["album_ids"]), [9, 10])
        self.assertEqual(self.store.get(op)["status"], "Failed")
        self.assertEqual(ad.run_import.call_count, 1)


class UnsuccessfulMatchTouchesNothingTests(unittest.TestCase):
    """Beets declining the folder (no confident match for the pinned release)
    leaves the library untouched: the apply reports ``not_imported`` with
    ``mutated: False`` and no album ids, so nothing is removed or claimed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = base.TransactionStore(tmp.name)

    def test_not_imported_reports_no_mutation(self):
        # Pre-import set and post-import set identical: Beets added nothing.
        ad = base._adapter(None, existing=[{"id": 4, "mb_albumid": REL}])
        op = cw.plan_confirmed_import(
            {"source_folder": "/downloads/A", "mb_albumid": REL, "mb_releasegroupid": RG},
            store=self.store)["operation_id"]
        res = cw.apply_confirmed_import(op, adapter=ad, store=self.store, timeout=60.0)
        self.assertEqual(res["code"], "not_imported")
        self.assertFalse(res["mutated"])
        self.assertEqual(res["album_ids"], [])
        self.assertEqual(self.store.get(op)["status"], "Failed")


class ManualRgidSeamTests(unittest.TestCase):
    """The PR3 shared resolver and the controlled pipeline compose end to end."""

    FOLDER = "/downloads/Artist/Album"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = base.TransactionStore(tmp.name)

    def _resolve_rgid(self, *, passing):
        """Resolve a release-group id through the real shared entrypoint with
        PR3's MusicBrainz stubs. ``passing`` is the set of release ids the
        folder preflight accepts."""
        free = mock.MagicMock(return_value=[
            {"mb_albumid": pr3.REL2, "tracks": 2, "artist": "Artist", "album": "Album"}])

        def preflight(folder, rid, **k):
            ok = rid in passing
            return {"ok": ok, "matches": 2 if ok else 0, "expected": 2, "audio_count": 2,
                    "release_group": pr3.RG2 if rid == pr3.REL2 else pr3.RG1}

        log = []
        with mock.patch.object(lsvc, "_resolve_release_group_to_release", return_value=pr3.REL1), \
                mock.patch.object(lsvc, "_folder_release_preflight", side_effect=preflight) as pf, \
                mock.patch.object(lsvc, "_preflight_oversized_subset_complete", return_value=False), \
                mock.patch.object(lsvc, "_folder_import_track_count", return_value=2), \
                mock.patch.object(lsvc, "_mb_release_group_candidates",
                                  return_value=[{"mb_albumid": pr3.SAME_RG_REL, "track_count": 2}]), \
                mock.patch.object(lsvc, "_mb_release_search", free), \
                mock.patch.object(lsvc, "_mb_release_search_by_folder_tracks", free):
            resolved = lsvc.resolve_release_group_to_compatible_release(
                f"https://musicbrainz.org/release-group/{pr3.RG1}", "Artist", "Album", "", 2, log,
                source_folder=self.FOLDER, existing_album_id=0)
        return resolved, log, free, pf

    def test_resolved_release_flows_through_the_controlled_pipeline(self):
        resolved, _log, free, pf = self._resolve_rgid(passing={pr3.REL1})
        self.assertEqual(resolved, pr3.REL1)
        free.assert_not_called()  # the resolver stayed inside the group
        # F2: verification is driven by the REAL source_folder, never the ""
        # default that would skip the folder preflight. Prove the preflight ran
        # against our folder for the resolved release -- the default is not relied on.
        self.assertTrue(pf.called, "the folder preflight must run for a manual RGID resolve")
        self.assertTrue(any(call.args[0] == self.FOLDER for call in pf.call_args_list),
                        "the preflight must be given the real source folder")
        # The resolved release pins the controlled import.
        ad = base._adapter({"id": 9, "mb_albumid": resolved, "mb_releasegroupid": pr3.RG1})
        op = cw.plan_confirmed_import(
            {"source_folder": self.FOLDER, "mb_albumid": resolved, "mb_releasegroupid": pr3.RG1},
            store=self.store)["operation_id"]
        res = cw.apply_confirmed_import(op, adapter=ad, store=self.store, timeout=60.0)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["album_id"], 9)
        self.assertEqual(ad.run_import.call_args.kwargs["search_ids"], [resolved],
                         "Beets must be pinned to the release the resolver chose")
        self.assertEqual(self.store.get(op)["status"], "Completed")

    def test_fail_closed_resolution_plans_no_import(self):
        # Only another Release Group's release would pass the folder: the resolver
        # fails closed, so nothing is ever planned or applied.
        resolved, log, free, _pf = self._resolve_rgid(passing={pr3.REL2})
        self.assertEqual(resolved, "")
        free.assert_not_called()
        self.assertTrue(any("REFUSED" in line for line in log), log)
        self.assertEqual(self.store.list()[0], [], "a fail-closed resolution must create no transaction")

    def test_empty_source_folder_skips_the_preflight_so_import_callers_must_pass_one(self):
        """F2 characterization: the shared wrapper inherits ``source_folder=""``
        from the internal resolver, and with no folder the preflight is skipped
        (``_source_accepts_release`` returns the release unverified). Production
        import-time callers therefore MUST pass a real folder (they do:
        import_service.py resolves with ``source_folder=aldir``/``folder_path``);
        the acquisition pre-download path intentionally resolves without a folder
        because no files exist yet. This test fails loudly if the wrapper is ever
        wired at import time with "" expecting verification. Tracked as F2 for the
        resolver owner (making the wrapper fail-closed on an empty folder)."""
        with mock.patch.object(lsvc, "_resolve_release_group_to_release", return_value=pr3.REL1), \
                mock.patch.object(lsvc, "_folder_release_preflight") as pf:
            resolved = lsvc.resolve_release_group_to_compatible_release(
                f"https://musicbrainz.org/release-group/{pr3.RG1}", "Artist", "Album", "", 2, [],
                source_folder="", existing_album_id=0)
        self.assertEqual(resolved, pr3.REL1)
        pf.assert_not_called()  # no folder -> no verification; never use "" at import time


if __name__ == "__main__":
    unittest.main()
