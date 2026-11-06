"""#428: a confirmed import Beets skipped carries the plugin's skip reason as a
stable refusal code, so the slskd peer loop stops when the skip is not about
the downloaded copy (no_candidates, duplicate) and keeps trying peers when it
is (no_strong_match)."""

import tempfile
import types
import unittest
from unittest import mock

import backend.composite_workflows as cw
import backend.import_service as imp
from backend.matching_service import IMPORT_REFUSAL_REASONS, import_refusal_reason_code
from backend.transaction_engine import TransactionStore
from tests.test_import_paths_beets_native import REL, _adapter
import tests.test_slskd_noncandidate_refusal as t406


def _skipped(*reasons):
    return {"success": True, "skipped": [{"path": f"/downloads/A/{i}", "reason": r}
                                         for i, r in enumerate(reasons)]}


class ConfirmedImportReportsSkipReason(unittest.TestCase):
    def _run(self, run_import_result):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = TransactionStore(tmp.name)
        ad = _adapter(None)
        ad.run_import.return_value = run_import_result
        plan = cw.plan_confirmed_import({"source_folder": "/downloads/A", "mb_albumid": REL}, store=store)
        return cw.apply_confirmed_import(plan["operation_id"], adapter=ad, store=store, timeout=60.0)

    def test_single_reason_is_reported(self):
        for reason in ("no_candidates", "duplicate", "no_strong_match"):
            res = self._run(_skipped(reason, reason))
            self.assertEqual((res["code"], res["skip_reason"]), ("not_imported", reason))

    def test_mixed_missing_or_malformed_reasons_report_none(self):
        for result in (_skipped("no_candidates", "no_strong_match"), {"success": True},
                       {"skipped": "no_candidates"}, {"skipped": ["no_candidates"]}, None):
            res = self._run(result)
            self.assertEqual((res["code"], res["skip_reason"]), ("not_imported", ""), result)


class RefusalMessageCarriesCode(unittest.TestCase):
    def _raise(self, skip_reason):
        ctx = types.SimpleNamespace(guess_artist="A", guess_album="B", log=[])
        with mock.patch.object(imp, "_reimport_queue_review", return_value=True) as queue, \
                self.assertRaises(RuntimeError) as cm:
            imp._reimport_refuse_failed_import(
                ctx, {"ok": False, "code": "not_imported", "skip_reason": skip_reason}, "/d/A", REL, 0)
        # The Review entry keeps the plain, code-free reason.
        self.assertNotIn("reason_code", queue.call_args[0][3])
        return str(cm.exception)

    def test_known_reasons_become_stable_codes(self):
        for reason, code in (("no_candidates", "beets_no_candidates"), ("duplicate", "beets_duplicate"),
                             ("no_strong_match", "beets_no_strong_match")):
            msg = self._raise(reason)
            self.assertIn(f"[reason_code={code}]", msg)
            self.assertEqual(import_refusal_reason_code(f"import job failed: ERROR: {msg}"), code)

    def test_unknown_or_missing_reason_keeps_the_old_message(self):
        for reason in ("", "blank_identity", "weird"):
            msg = self._raise(reason)
            self.assertEqual(msg, "Beets could not confidently match this source to the selected "
                                  "MusicBrainz release. Queued for Review without changing library files.")
            self.assertEqual(import_refusal_reason_code(msg), "")

    def test_codes_classify_copy_specific_skips(self):
        self.assertTrue(IMPORT_REFUSAL_REASONS["beets_no_strong_match"])
        self.assertFalse(IMPORT_REFUSAL_REASONS["beets_no_candidates"])
        self.assertFalse(IMPORT_REFUSAL_REASONS["beets_duplicate"])

    def test_unregistered_beets_code_and_peer_text_are_ignored(self):
        base = "Beets could not confidently match this source to the selected MusicBrainz release"
        self.assertEqual(import_refusal_reason_code(f"{base} [reason_code=beets_made_up]."), "")
        self.assertEqual(import_refusal_reason_code(
            f"import job failed: ERROR: Beets import failed for 'x {base} [reason_code=beets_duplicate].flac'"), "")


def _beets_skip_error(code):
    return ("import job failed: ERROR: Beets could not confidently match this source to the selected "
            f"MusicBrainz release [reason_code={code}]. Queued for Review without changing library files.")


class SlskdLoopHonorsBeetsSkipCode(unittest.TestCase):
    _run = t406.NonCandidateRefusalStopsPeerLoop._run  # the #406 peer-loop harness

    def test_non_copy_skip_stops_after_one_candidate(self):
        for code in ("beets_no_candidates", "beets_duplicate"):
            m, log = self._run("", error=_beets_skip_error(code))
            self.assertEqual(m["_slskd_search_and_queue"].call_count, 1, code)
            stop = [line for line in log if "Not trying other Soulseek peers" in line]
            self.assertEqual(len(stop), 1, log)
            self.assertIn(code, stop[0])

    def test_copy_specific_skip_keeps_trying_peers(self):
        m, log = self._run("", error=_beets_skip_error("beets_no_strong_match"))
        self.assertEqual(m["_slskd_search_and_queue"].call_count, 12)
        self.assertFalse([line for line in log if "Not trying other" in line])


if __name__ == "__main__":
    unittest.main()
