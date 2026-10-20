"""Re-QA of PR #401 @ 4defb5d: strict MusicBrainz Release Group lookup through
the real provider boundary, the narrowed auto-resolve rule for refused review
rows, and transaction ids on failed batch rows."""
import http.client
import io
import json
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import backend.missing_mb_batch as mb
import backend.provider_boundary as pb
import helpers_mb
from backend.transaction_engine import TransactionStore

try:
    import test_missing_mb_batch as base
except ImportError:  # pragma: no cover
    import tests.test_missing_mb_batch as base

RG, REL = base.RG, base.REL
URLOPEN = "backend.provider_boundary.urllib.request.urlopen"


def _http_error(code):
    return urllib.error.HTTPError(f"https://musicbrainz.org/ws/2/release/{REL}", code, "x", {}, io.BytesIO(b""))


class StrictLookupThroughTheRealBoundary(unittest.TestCase):
    """Only urlopen is stubbed: provider_boundary's own retry/classification runs."""

    def setUp(self):
        for target, value in ((pb, "MAX_RETRY_AFTER_SECONDS"),):
            p = mock.patch.object(target, value, 0)  # no real backoff sleeps
            p.start()
            self.addCleanup(p.stop)

    def lookup(self, side_effect):
        with mock.patch(URLOPEN, side_effect=side_effect) as op:
            return helpers_mb._fetch_mb_release_group_strict(REL), op

    def test_answers(self):
        body = json.dumps({"id": REL, "release-group": {"id": RG}}).encode()
        self.assertEqual(self.lookup(lambda *a, **k: io.BytesIO(body))[0], RG)
        for code in (400, 404):
            with self.subTest(code):
                self.assertEqual(self.lookup(_http_error(code))[0], "")
        for body in (b"<html>maintenance</html>", b"[]", b'{"release-group": null}', b'{"release-group": "x"}'):
            with self.subTest(body):
                self.assertEqual(self.lookup(lambda *a, b=body, **k: io.BytesIO(b))[0], "")

    def test_outages_raise_and_are_never_an_empty_answer(self):
        for code in (429, 500, 502, 503, 504):
            with self.subTest(code):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.lookup(_http_error(code))
                self.assertEqual(ctx.exception.code, code)
        for exc in (TimeoutError("t"), ConnectionResetError("r"), urllib.error.URLError("dns")):
            with self.subTest(repr(exc)):
                with self.assertRaises(OSError):
                    self.lookup(exc)

    def test_body_failing_mid_read_raises(self):
        class Broken(io.BytesIO):
            def read(self, *a):
                raise http.client.IncompleteRead(b"{")
        with self.assertRaises(http.client.HTTPException):
            self.lookup(lambda *a, **k: Broken())

    def test_non_uuid_release_id_never_reaches_the_network(self):
        with mock.patch(URLOPEN, side_effect=AssertionError("network")):
            self.assertEqual(helpers_mb._fetch_mb_release_group_strict("abc"), "")


class _Fill(base._Case):
    def setUp(self):
        self.w = base._World(self)
        self.store = TransactionStore(tempfile.mkdtemp(prefix="qa401b_"))
        self.lookup = self.patch("backend.missing_mb_batch.composite_workflows._release_group_for_release",
                                 return_value=RG)

    def album(self):
        row = self.w.add_album(1)
        self.w.albums[1].mb_albumid = REL
        return row


class OutageAtApply(_Fill):
    def test_outage_after_preview_is_evidence_unavailable_and_writes_nothing(self):
        self.album()
        preview = mb.run_preview(list(self.w.rows), [], store=self.store)
        self.lookup.side_effect = urllib.error.HTTPError("u", 503, "x", {}, None)
        res = mb.run_apply(preview["transaction_id"], apply_album=lambda *a: self.fail("written"),
                           apply_item=None, log=[], store=self.store)
        self.assertEqual(res["rows"][0]["status"], "evidence_unavailable")


class TransactionIdsOnFailedRows(_Fill):
    def test_written_but_unverified_row_names_the_writer_transaction(self):
        # QA401-R1 (fixed): the writer committed "t1", the re-read did not
        # verify; the recovery_required row must name that transaction.
        self.album()
        preview = mb.run_preview(list(self.w.rows), [], store=self.store)
        res = mb.run_apply(preview["transaction_id"],
                           apply_album=lambda *a: {"changed": True, "transaction_ids": ["t1"]},
                           apply_item=None, log=[], store=self.store)
        self.assertEqual(res["rows"][0]["status"], "recovery_required")
        self.assertEqual(res["rows"][0].get("transaction_ids"), ["t1"])

    def test_refusal_before_write_names_no_transaction(self):
        self.album()
        preview = mb.run_preview(list(self.w.rows), [], store=self.store)

        def refuse(*_a):
            raise mb.NotWritten("refused")
        res = mb.run_apply(preview["transaction_id"], apply_album=refuse, apply_item=None, log=[], store=self.store)
        self.assertEqual(res["rows"][0]["status"], "no_longer_proven")
        self.assertNotIn("transaction_ids", res["rows"][0])


class NarrowedAutoResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qa401_store_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Stop only these patchers: mock.patch.stopall would also stop the
        # suite-wide env patcher that test_ai_batch_retry_race starts at import.
        ids = mock.patch("backend.pending_review_store._library_album_ids_for_folder", return_value=[])
        resolved = mock.patch("backend.pending_review_store._review_album_is_resolved", return_value=True)
        self.ids = ids.start()
        self.addCleanup(ids.stop)
        self.resolved_album = resolved.start()
        self.addCleanup(resolved.stop)

    def item(self, name, conflict, make=True):
        folder = self.tmp / name
        if make:
            folder.mkdir()
        return {"path": str(folder), "suggestion": {}, "evidence": {"conflict": conflict}}

    def resolved(self, item):
        from backend.pending_review_store import _pending_review_item_is_resolved
        return _pending_review_item_is_resolved(item)

    def test_imported_folder_still_resolves_even_when_refused(self):
        item = self.item("A", "symlinked_audio")
        self.ids.return_value = [7]  # Beets now holds an album from this folder
        self.assertTrue(self.resolved(item))

    def test_deleted_folder_resolves_including_a_dangling_folder_link(self):
        self.assertTrue(self.resolved(self.item("Gone", "unverifiable_folder", make=False)))

    def test_folder_that_is_itself_a_link_counts_as_present(self):
        target = self.tmp / "t"
        target.mkdir()
        link = self.tmp / "L"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create symlinks here")
        self.assertFalse(self.resolved({"path": str(link), "suggestion": {}, "evidence": {"conflict": "symlinked_audio"}}))

    def test_other_conflicts_and_rows_without_evidence_keep_the_old_rule(self):
        for conflict in ("hardlinked", "beets_skip", "", None):
            with self.subTest(conflict):
                self.assertTrue(self.resolved(self.item(f"o{conflict}", conflict)))
        self.assertTrue(self.resolved({"path": str(self.tmp / "o"), "suggestion": {}}))

    def test_suggestion_only_evidence_keeps_the_old_rule(self):
        # The batch writes evidence at the top level too; a row that only has
        # suggestion.review_evidence is not covered by the narrowed rule.
        item = self.item("S", None)
        item["evidence"] = {}
        item["suggestion"] = {"review_evidence": {"conflict": "symlinked_audio"}}
        self.assertTrue(self.resolved(item))


if __name__ == "__main__":
    unittest.main()
