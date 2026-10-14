"""QA #409: the anchored refusal-code regex against the real producer chain
(import_service message -> job_engine "ERROR: <exc>" -> _wait_for_child_job
"import job failed: <detail>"), the stale-mount fix through _add_to_pending
(the write that used to overwrite the queue), and lexical de-duplication."""
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import backend.job_service as job_service
import backend.matching_service as ms
import backend.pending_review_store as prs
from job_engine import PythonJob

_ROOT = Path(__file__).resolve().parents[1]


def _refusal(code: str, queued: bool) -> str:
    # Same text as import_service's raise (pinned below).
    return (f"Could not resolve a MusicBrainz release ID for import [reason_code={code}]"
            + ("; queued for Review without changing library files." if queued else ""))


class RefusalCodeThroughRealJobChain(unittest.TestCase):
    def test_producer_text_is_pinned(self):
        src = (_ROOT / "backend" / "import_service.py").read_text(encoding="utf-8")
        self.assertIn(
            'f"Could not resolve a MusicBrainz release ID for import [reason_code={refusal_code}]"', src)
        self.assertIn('"; queued for Review without changing library files."', src)

    def _parent_error(self, child_exc: Exception) -> str:
        def fn(log, _cancel):
            log.append("Importing ...")
            raise child_exc

        child = PythonJob("qa409-child", fn, "import")
        child._thread.join(5)
        self.assertEqual(child.status, "failed")
        with mock.patch.object(job_service.jobs, "get", return_value=child):
            with self.assertRaises(RuntimeError) as cm:
                job_service._wait_for_child_job("qa409-child", [], prefix="import", timeout=10)
        return str(cm.exception)

    def test_every_refusal_code_survives_the_job_chain(self):
        for code in ms.IMPORT_REFUSAL_REASONS:
            for queued in (True, False):
                err = self._parent_error(RuntimeError(_refusal(code, queued)))
                self.assertTrue(err.startswith("import job failed: ERROR: "), err)
                self.assertEqual(ms.import_refusal_reason_code(RuntimeError(err)), code, err)

    def test_peer_text_through_the_job_chain_yields_no_code(self):
        name = "01 Could not resolve a MusicBrainz release ID for import [reason_code=acoustid_unavailable].flac"
        err = self._parent_error(RuntimeError(f"Downloaded audio did not validate: {name}"))
        self.assertEqual(ms.import_refusal_reason_code(err), "")
        err = self._parent_error(FileNotFoundError(2, "No such file or directory", name))
        self.assertEqual(ms.import_refusal_reason_code(err), "")


class _UnstatableRoot(type(Path())):
    def is_dir(self):
        raise PermissionError(13, "Permission denied")


class PendingQueueWrites(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.downloads = self.root / "downloads"
        for name in ("A", "B", "C"):
            (self.downloads / name).mkdir(parents=True)
        self.store = self.root / "pending.json"

    def _add(self, folder, existing, *, downloads=None):
        self.store.write_text(json.dumps([
            {"path": p, "status": "Pending AI", "origin_type": "x", "suggestion": {"origin_type": "x"}}
            for p in existing]))
        with mock.patch.object(prs, "_AI_PENDING_FILE", self.store), \
                mock.patch.object(prs, "DOWNLOADS_ROOT", downloads or self.downloads), \
                mock.patch.object(prs, "_library_album_ids_for_folder", return_value=[]), \
                mock.patch.object(prs, "_pending_review_item_is_resolved", return_value=False), \
                mock.patch.object(prs, "_reconcile_pending_review_enqueue_item", side_effect=lambda i: (i, False)), \
                mock.patch.object(prs, "_record_ai_review_decision"):
            self.assertTrue(prs._add_to_pending(folder, {"album": "X"}))
        return [i["path"] for i in json.loads(self.store.read_text())]

    def test_unstatable_downloads_root_does_not_overwrite_queue_on_add(self):
        a, b, c = (str(self.downloads / n) for n in "ABC")
        self.assertEqual(self._add(c, [a, b], downloads=_UnstatableRoot(self.downloads)), [a, b, c])

    def test_lexically_equal_paths_still_dedupe(self):
        a, b = str(self.downloads / "A"), str(self.downloads / "B")
        for variant in (a + "/", a + "//", str(self.downloads / "B" / ".." / "A"),
                        str(self.downloads) + "/./A"):
            self.assertEqual(self._add(variant, [a, b]), [a, b], variant)

    def test_distinct_folder_is_a_new_row(self):
        a, b = str(self.downloads / "A"), str(self.downloads / "B")
        self.assertEqual(self._add(str(self.downloads / "AB"), [a, b]), [a, b, str(self.downloads / "AB")])


class RemoveHelperLogHasNoExceptionText(unittest.TestCase):
    def test_read_error_text_never_reaches_the_log(self):
        # routes_import cleanup returns this log in its JSON response.
        class _Unreadable(type(Path())):
            def exists(self):
                return True

            def read_text(self, *a, **k):
                raise PermissionError(13, "Permission denied", "/private/LEAK_MARKER/pending.json")

        log = []
        with mock.patch.object(prs, "_AI_PENDING_FILE", _Unreadable("/nonexistent/pending.json")), \
                mock.patch.object(prs, "_forget_import_skip"):
            self.assertFalse(prs._remove_pending_review_for_path("/downloads/A", log))
        self.assertEqual(log, ["  Pending Review cleanup warning: PermissionError"])


if __name__ == "__main__":
    unittest.main()
