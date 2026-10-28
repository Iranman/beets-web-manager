"""ARCH-030 B4: an authorized/human-confirmed NO_RESULT recovery completes the
merge into the existing album instead of stranding a duplicate; plus the two
non-blocking reconcile/staging hardenings.

B4 root cause: the post-import AcoustID gate
(``import_service._validate_wanted_album_items_with_acoustid``) refused on
``no_result`` unconditionally, so even a slot the matching gate had already
authorized (matching_decision.action_allowed=True, VERIFIED_SUBSET /
DETERMINISTIC_METADATA_SLOT -- docs/adr/0007, Option C) was imported-then-
refused and left as a separate album row. The gate now HONORS that already-set
authorization for the authorized wanted subset, while everything stronger (a
different-song mismatch, an infra not_checked failure, a file that maps to no
requested track) still refuses.

The authorization signal is the SAME one the unattended auto-insert gate
consumes, read from the folder-bound carrier -- never recomputed here. Both the
unattended advance and the human-confirmed merge of that slot read it, so a
carrier that is action_allowed=True but (under Option C) routed to manual review
still merges when the human confirms; a genuinely unauthorized/unverified item
still goes to review.
"""
import contextlib
import os
import tempfile
import types
import unittest
from unittest import mock

import backend.import_service as isvc
import backend.pending_review_store as prs
import backend.recovery_service as rsvc

# The real validator, captured before any suite patches isvc with a mock.
from backend.import_service import _validate_wanted_album_items_with_acoustid as _REAL_VALIDATOR
from tests.test_fill_merge_fail_closed import _JobHarness, REL, RG, EXISTING, IMPORTED, WANTED, CODE


def _no_result_lookup(_path):
    # Fingerprinted, AcoustID returned zero candidates (NO_RESULT, not an infra
    # failure): .data == [] and acoustid_failure_status() == "".
    return types.SimpleNamespace(data=[])


class _RealValidatorMixin:
    """Run the REAL post-import AcoustID gate with every imported file a
    NO_RESULT, a real MB tracklist carrying the wanted slot, and a controllable
    slot-authorization signal."""

    def setUp(self):
        super().setUp()
        # Restore the real validator over the parent harness's MagicMock.
        p = mock.patch.object(isvc, "_validate_wanted_album_items_with_acoustid", _REAL_VALIDATOR)
        p.start(); self.addCleanup(p.stop)
        # Imported + existing rows with the fields the real gate/merge read.
        self.items = {
            IMPORTED: [{"id": 92, "album_id": IMPORTED, "disc": 1, "track": 2,
                        "mb_trackid": "rec-2", "title": "Two",
                        "path": "/music/Artist/Album/02 Two.flac", "length": 180.0}],
            EXISTING: [{"id": 51, "album_id": EXISTING, "disc": 1, "track": 1,
                        "mb_trackid": "rec-1", "title": "One",
                        "path": "/music/Artist/Album/01 One.flac", "length": 181.0}],
        }
        tracklist = {"ok": True, "release_group": RG, "tracks": [
            {"disc": 1, "track": 2, "mb_trackid": "rec-2", "title": "Two", "artist": "Artist"}]}

        def apply_move(op_id):
            moved = [r for r in self.items[IMPORTED] if r["id"] == 92]
            self.items[IMPORTED] = [r for r in self.items[IMPORTED] if r["id"] != 92]
            self.items[EXISTING] = self.items[EXISTING] + moved
            return {"ok": True}

        cw = isvc.composite_workflows
        for target, name, kw in (
            (isvc, "_fetch_mb_release_tracklist", {"return_value": tracklist}),
            (isvc, "_acoustid_lookup_cached_outcome", {"side_effect": _no_result_lookup}),
            (isvc, "acoustid_failure_status", {"return_value": ""}),
            # The real _merge_imported_album_into_existing runs; only the engine
            # reconcile transaction is simulated (same as the mtr merge suite).
            (cw, "plan_existing_album_reconcile", {"return_value": {"ok": True, "operation_id": "op-rec"}}),
            (cw, "apply_existing_album_reconcile", {"side_effect": apply_move}),
        ):
            patcher = mock.patch.object(target, name, **kw)
            patcher.start(); self.addCleanup(patcher.stop)


class B4AuthorizedNoResultMergesTests(_RealValidatorMixin, _JobHarness):
    def _run_real_merge(self, authorized):
        with mock.patch.object(isvc, "_recovery_slot_authorized", return_value=authorized):
            return self._run()  # merge=None -> the REAL merge runs

    def test_authorized_no_result_slot_merges_into_the_existing_album(self):
        """The headline 112 NO_RESULT case: the gate-authorized slot MERGES,
        leaving no separate/stranded album row and no Review entry."""
        res = self._run_real_merge(authorized=True)
        self.assertEqual(res["status"], "completed", res)
        self.assertEqual(res["result"]["album_ids"], [EXISTING])
        self.assertEqual(self.reviews, [])
        # Proof the fill actually landed on the existing album row.
        self.assertEqual({r["id"] for r in self.items[EXISTING]}, {51, 92})
        self.assertEqual(self.items[IMPORTED], [])
        self.assertTrue(any("merge honored under the carrier's deterministic-metadata-slot" in line
                            for line in res["log"]), res["log"])

    def test_unauthorized_no_result_still_goes_to_review(self):
        """Same NO_RESULT evidence, but no slot authorization: the gate refuses
        and the imported row is kept for Review (strict behavior unchanged)."""
        res = self._run_real_merge(authorized=False)
        self.assertEqual(res["status"], "failed", res)
        self.assertEqual(len(self.reviews), 1)
        self.assertIn("AcoustID did not confirm", self.reviews[0])
        # Nothing was merged: the imported row is still its own album.
        self.assertEqual({r["id"] for r in self.items[IMPORTED]}, {92})
        self.assertEqual({r["id"] for r in self.items[EXISTING]}, {51})


class B4ValidatorSubsetBoundaryTests(unittest.TestCase):
    """The authorization relaxes ONLY an authorized NO_RESULT -- never a
    different-song mismatch, nor a file outside the authorized wanted subset."""

    REL = REL
    WANTED = [{"disc": 1, "track": 2, "mb_trackid": "rec-2", "title": "Two"}]

    def _validate(self, rows, *, authorized, verdict=None):
        cw = isvc.composite_workflows
        tracklist = {"ok": True, "release_group": RG, "tracks": [
            {"disc": 1, "track": 2, "mb_trackid": "rec-2", "title": "Two", "artist": "Artist"}]}
        with mock.patch.object(isvc, "_fetch_mb_release_tracklist", return_value=tracklist), \
                mock.patch.object(cw, "find_all_items_by_album_id", return_value=rows), \
                mock.patch.object(isvc, "acoustid_failure_status", return_value=""), \
                mock.patch.object(isvc, "_acoustid_lookup_cached_outcome",
                                  side_effect=lambda p: types.SimpleNamespace(
                                      data=[] if verdict is None else [{"mb_trackid": "other"}])), \
                mock.patch.object(isvc, "verify_audio_against_request",
                                  return_value=verdict or {"decision": "review", "recording_id": ""}):
            return _REAL_VALIDATOR(7, self.REL, self.WANTED, [], slot_authorized=authorized)

    def _row(self, **over):
        base = {"id": 92, "disc": 1, "track": 2, "mb_trackid": "rec-2",
                "title": "Two", "path": "/music/x/02.flac", "length": 180.0}
        base.update(over)
        return base

    def test_authorized_no_result_passes(self):
        res = self._validate([self._row()], authorized=True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["no_result"], 1)

    def test_unauthorized_no_result_refuses(self):
        res = self._validate([self._row()], authorized=False)
        self.assertFalse(res["ok"], res)
        self.assertIn("AcoustID did not confirm", res["reason"])

    def test_authorized_but_mismatch_still_refuses(self):
        # A confident AcoustID verdict that rejects (a different song) is never
        # relaxed, even for an authorized slot.
        res = self._validate([self._row()], authorized=True,
                             verdict={"decision": "reject", "recording_id": "other"})
        self.assertFalse(res["ok"], res)
        self.assertIn("different song", res["reason"])

    def test_authorized_but_file_outside_subset_still_refuses(self):
        # An imported file that maps to NO requested track is outside the
        # authorized subset -> unverified -> refused even when authorized.
        extra = self._row(id=93, track=9, mb_trackid="rec-9", title="Nine")
        res = self._validate([self._row(), extra], authorized=True)
        self.assertFalse(res["ok"], res)
        self.assertTrue(res["unverified"])


class RecoverySlotAuthorizedTests(unittest.TestCase):
    """``_recovery_slot_authorized`` only READS the folder-bound carrier's
    matching_decision.action_allowed; fails closed otherwise."""

    def _with(self, carrier):
        return mock.patch.object(isvc.recovery_carrier, "load_for_folder", return_value=carrier)

    def test_action_allowed_true_is_authorized(self):
        with self._with({"matching_decision": {"action_allowed": True}}):
            self.assertTrue(isvc._recovery_slot_authorized("/d/x"))

    def test_action_allowed_false_is_not(self):
        with self._with({"matching_decision": {"action_allowed": False}}):
            self.assertFalse(isvc._recovery_slot_authorized("/d/x"))

    def test_no_carrier_is_not(self):
        with self._with(None):
            self.assertFalse(isvc._recovery_slot_authorized("/d/x"))

    def test_missing_or_nondict_decision_is_not(self):
        for carrier in ({}, {"matching_decision": None}, {"matching_decision": "x"}):
            with self._with(carrier):
                self.assertFalse(isvc._recovery_slot_authorized("/d/x"))

    def test_store_error_fails_closed(self):
        with mock.patch.object(isvc.recovery_carrier, "load_for_folder", side_effect=RuntimeError):
            self.assertFalse(isvc._recovery_slot_authorized("/d/x"))


class PendingReviewOverlapTests(unittest.TestCase):
    """RECONCILE-OVERLAP: a queued Review path that is an ancestor OR a
    descendant of the folder about to be deleted is protected, not only an
    exact match -- while a mere sibling-prefix is not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def _rows(self, *paths):
        return [{"path": p} for p in paths]

    def _check(self, folder, rows, *, unknown=False):
        with mock.patch.object(prs, "_load_pending_reviews", return_value=rows):
            return prs._pending_review_has_path_or_overlap(folder, unknown=unknown)

    def test_exact_match_protects(self):
        f = os.path.join(self.root, "album")
        self.assertTrue(self._check(f, self._rows(f)))

    def test_descendant_review_protects_the_scanned_parent(self):
        # Sweep scans/deletes <root>/album; a Review row lives at album/CD 02.
        parent = os.path.join(self.root, "album")
        child = os.path.join(parent, "CD 02")
        self.assertTrue(self._check(parent, self._rows(child)))

    def test_ancestor_review_protects_a_child_folder(self):
        parent = os.path.join(self.root, "album")
        child = os.path.join(parent, "CD 02")
        self.assertTrue(self._check(child, self._rows(parent)))

    def test_sibling_prefix_is_not_protected(self):
        self.assertFalse(self._check(os.path.join(self.root, "album"),
                                     self._rows(os.path.join(self.root, "album-2"))))

    def test_unreadable_queue_fails_closed(self):
        with mock.patch.object(prs, "_load_pending_reviews", side_effect=RuntimeError):
            self.assertTrue(prs._pending_review_has_path_or_overlap(
                os.path.join(self.root, "album"), unknown=True))
            self.assertFalse(prs._pending_review_has_path_or_overlap(
                os.path.join(self.root, "album"), unknown=False))


class StagingRootReadbackFailClosedTests(unittest.TestCase):
    """STAGING-ROOT-READBACK: when the /proc/self/fd read-back fails on an
    fd-capable host, ``_staging_root`` fails closed (returns "") instead of
    re-resolving the path by name (which would reopen the TOCTOU window the
    O_NOFOLLOW fd-open just closed)."""

    def test_readback_failure_returns_empty_not_by_name_realpath(self):
        fake_open = mock.MagicMock(side_effect=[3, 4])  # parent_fd, dir_fd
        with mock.patch("backend.app_runtime.validated_downloads_root",
                        return_value="/downloads"), \
                mock.patch.object(rsvc.os, "O_NOFOLLOW", 0x100, create=True), \
                mock.patch.object(rsvc.os, "O_DIRECTORY", 0x10000, create=True), \
                mock.patch.object(rsvc.os, "open", fake_open), \
                mock.patch.object(rsvc.os, "supports_dir_fd", {fake_open}), \
                mock.patch.object(rsvc.os, "close", lambda fd: None), \
                mock.patch.object(rsvc.os, "readlink", side_effect=OSError("no /proc")), \
                mock.patch.object(rsvc.os.path, "realpath",
                                  side_effect=AssertionError("by-name realpath must not run")):
            self.assertEqual(rsvc._staging_root(), "")


if __name__ == "__main__":
    unittest.main()
