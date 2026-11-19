"""Regression tests for #533 items 1-7 (dedup evidence honesty, symlink scope,
provider-exception redaction, the ai-review null scan_jid 500, and the dedup
scan/AI-review logging edge cases).

These lock the honest-label and safety fixes in place; the broader end-to-end
behavior is also covered by tests/test_dedup_characterization.py's golden.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
import routes_cleanup
import backend.dedup_service as dedup_service
import backend.provider_boundary as provider_boundary

_CSRF = {"Origin": "http://localhost", "X-Beets-CSRF": "1"}


class Item1AcoustidFallbackEvidence(unittest.TestCase):
    """The AcoustID fallback fingerprints only the SOURCE and finds the library
    copy by its embedded recording ID, so it must NOT claim the library copy was
    fingerprinted."""

    def test_library_fingerprint_not_claimed(self):
        state = {"log": []}
        ev = dedup_service._dedup_verify_candidate(
            Path("/does/not/matter.flac"), "/lib/copy.flac",
            "AcoustID fingerprint", True, "REC-ID", state)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["src_fp_ids"], ["REC-ID"])   # source genuinely heard
        self.assertEqual(ev["lib_fp_ids"], [])           # library copy NOT fingerprinted here
        self.assertTrue(ev["verified"])


class Item2SizeMatchLabel(unittest.TestCase):
    """A size-only match is not byte-verified and not high confidence."""

    def test_size_only_is_not_byte_verified(self):
        self.assertEqual(dedup_service._dedup_evidence_tag("same", False, "identical file size"), "SIZE MATCH")

    def test_byte_verified_reserved_for_hash(self):
        # BYTE VERIFIED must no longer be produced by a size-only scan match.
        for rel in ("same", "different_release"):
            self.assertNotEqual(dedup_service._dedup_evidence_tag(rel, False, "identical file size"), "BYTE VERIFIED")

    def test_size_only_confidence_medium_and_honest_reason(self):
        conf, reason = dedup_service._dedup_confidence_and_reason(
            "identical file size", {"verified": False, "mbid": ""}, "same")
        self.assertEqual(conf, "medium")
        self.assertNotIn("byte-identical", reason)
        self.assertIn("bytes not yet compared", reason)


class Item4SymlinkScope(unittest.TestCase):
    """rglob()+is_file() follow symlinks; a symlink whose real target is outside
    the allowed roots must be excluded from the scan listing."""

    def test_within_roots_resolves_symlink_target(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            root_p, outside_p = Path(root).resolve(), Path(outside).resolve()
            inside = root_p / "real.flac"
            inside.write_bytes(b"x")
            outside_file = outside_p / "secret.flac"
            outside_file.write_bytes(b"y")
            link = root_p / "planted.flac"
            try:
                os.symlink(outside_file, link)
            except (OSError, NotImplementedError) as exc:  # pragma: no cover (symlink priv)
                self.skipTest(f"symlinks unavailable: {exc}")
            with mock.patch.object(dedup_service, "_BROWSE_ALLOWED_ROOTS", (root_p,)):
                self.assertTrue(dedup_service._dedup_listing_within_roots(inside))
                self.assertFalse(dedup_service._dedup_listing_within_roots(link))  # out-of-root target skipped

    def test_both_scan_sites_apply_the_filter(self):
        # dedup_service scan listing and routes_cleanup AI-review listing both guard rglob.
        import inspect
        self.assertIn("_dedup_listing_within_roots(p)", inspect.getsource(dedup_service._dedup_list_scan_sources))
        self.assertIn("_dedup_listing_within_roots(p)", inspect.getsource(routes_cleanup._ai_review_job))


class Item5ProviderExceptionRedaction(unittest.TestCase):
    """A non-ProviderError from the AI provider must not leak detail/credentials
    into the job log."""

    # Non-key sentinel: a generic Exception's message is dropped entirely by
    # classify_exception (only the classified category/type survives), so any
    # provider detail disappears. (Avoids tripping the repo's secret scanner.)
    SENTINEL = "SENSITIVE-PROVIDER-DETAIL-SENTINEL-DO-NOT-LOG"

    def test_classify_exception_strips_detail(self):
        leak = f"connect failed {self.SENTINEL} at https://provider/internal"
        msg = str(provider_boundary.classify_exception(Exception(leak)))
        self.assertNotIn(self.SENTINEL, msg)
        self.assertNotIn("provider/internal", msg)

    def test_batch_logs_redacted_message(self):
        leak = f"boom {self.SENTINEL} https://host/path"
        state = {"log": [], "found": 0}
        with mock.patch.object(routes_cleanup, "_ai_review_file_meta", return_value=[]), \
             mock.patch.object(routes_cleanup, "_ai_review_relevant_tracks", return_value=[]), \
             mock.patch.object(routes_cleanup, "_ai_model_and_endpoint", return_value=("m", "http://e")), \
             mock.patch.object(routes_cleanup, "_ai_review_request_body", return_value=b"{}"), \
             mock.patch.object(routes_cleanup, "_ai_review_call_provider", side_effect=Exception(leak)):
            routes_cleanup._ai_review_batch(0, [Path("/tmp/a.flac")], [], "key", [], None, state, None)
        joined = "\n".join(state["log"])
        self.assertNotIn(self.SENTINEL, joined)
        self.assertTrue(any("error:" in line for line in state["log"]))


class Item6AiReviewNullScanJid(unittest.TestCase):
    """scan_jid: null is malformed input -> clean 4xx, never a 500 AttributeError."""

    def _post(self, body):
        return app_module.app.test_client().post("/api/dedup/ai-review", json=body, headers=_CSRF)

    def test_null_scan_jid_is_400_not_500(self):
        resp = self._post({"scan_jid": None})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["ok"])

    def test_non_string_scan_jid_is_400(self):
        resp = self._post({"scan_jid": 123})
        self.assertEqual(resp.status_code, 400)

    def test_non_object_body_is_400_not_500(self):
        # A JSON array/string/number body must not 500 via payload.get().
        for body in ([1, 2], "hello", 7):
            resp = app_module.app.test_client().post("/api/dedup/ai-review", json=body, headers=_CSRF)
            self.assertEqual(resp.status_code, 400, f"body={body!r}")

    def test_non_string_scan_path_is_not_500(self):
        # A truthy non-string scan_path must not crash at .strip().
        resp = self._post({"scan_jid": "", "scan_path": 123})
        self.assertLess(resp.status_code, 500, resp.status_code)


class Item3AiLibIdConstrained(unittest.TestCase):
    """The AI may only point at a candidate it was shown, and its self-asserted
    'high' is capped to 'medium' without a confirming fingerprint."""

    def _run_match(self, lib_id, shown_ids):
        new_dupes, state = [], {"log": [], "found": 0}
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src.flac"; src.write_bytes(b"a")
            libf = Path(d) / "lib.flac"; libf.write_bytes(b"b")
            lib_obj = mock.Mock(); lib_obj.path = str(libf); lib_obj.id = lib_id
            file_meta = [{"path": str(src), "filename": "src.flac", "artist": "Artist",
                          "title": "Song One", "_derived_title": "Song One", "album": ""}]
            lib_index = [{"id": lib_id, "artist": "Artist", "title": "Song One", "album": ""}]
            m = {"file_idx": 1, "lib_id": lib_id, "confidence": "high", "reason": "same song"}
            with mock.patch.object(routes_cleanup, "lib") as fake_lib, \
                 mock.patch.object(routes_cleanup, "_acoustid_fingerprint_match", return_value=("", [], [])):
                fake_lib.get_item.return_value = lib_obj
                routes_cleanup._ai_review_apply_match(m, file_meta, lib_index, shown_ids, new_dupes, state, None)
        return new_dupes, state

    def test_lib_id_not_shown_is_rejected(self):
        new_dupes, state = self._run_match(lib_id=1, shown_ids=set())
        self.assertEqual(new_dupes, [])
        self.assertTrue(any("not among the candidates shown" in line for line in state["log"]))

    def test_shown_lib_id_accepted_with_capped_confidence(self):
        new_dupes, state = self._run_match(lib_id=1, shown_ids={1})
        self.assertEqual(len(new_dupes), 1)
        # AI said "high" but no fingerprint confirmed it -> capped to "medium".
        self.assertEqual(new_dupes[0]["confidence"], "medium")
        self.assertFalse(new_dupes[0]["fingerprint_verified"])


class Item7DedupLoggingEdges(unittest.TestCase):
    """Scan/AI-review logging edge cases: a cancel is not an ERROR, a refusal
    still finishes the batch, and one malformed match drops only itself."""

    # -- 7a: a cancel during listing is not a listing error --------------------
    def test_cancel_during_listing_reraises_not_logged_as_error(self):
        state = {"log": []}
        # A cancel surfaces here as RuntimeError("cancelled"); it must re-raise
        # (flow through the normal cancel path), not record a failed listing.
        with self.assertRaises(RuntimeError):
            dedup_service._dedup_record_listing_failure(
                state, RuntimeError("cancelled"), "Listing source audio files", None)
        self.assertEqual(state["log"], [])
        self.assertNotIn("status", state)

    def test_real_listing_error_still_recorded(self):
        state = {"log": []}
        dedup_service._dedup_record_listing_failure(
            state, OSError("disk gone"), "Listing source audio files", None)
        self.assertTrue(any("ERROR listing files" in line for line in state["log"]))
        self.assertEqual(state["status"], "done")

    def _batch_mocks(self, provider_return):
        return (
            mock.patch.object(routes_cleanup, "_ai_review_file_meta", return_value=[]),
            mock.patch.object(routes_cleanup, "_ai_review_relevant_tracks", return_value=[]),
            mock.patch.object(routes_cleanup, "_ai_model_and_endpoint", return_value=("m", "http://e")),
            mock.patch.object(routes_cleanup, "_ai_review_request_body", return_value=b"{}"),
            mock.patch.object(routes_cleanup, "_ai_review_call_provider", return_value=provider_return),
        )

    # -- 7b: an AI refusal still emits the batch's "done" log ------------------
    def test_refusal_still_emits_done_log(self):
        state, applied = {"log": [], "found": 0}, []
        mocks = self._batch_mocks({"refusal": "I won't do that"})
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], \
             mock.patch.object(routes_cleanup, "_ai_review_apply_match",
                               side_effect=lambda *a, **k: applied.append(1)):
            routes_cleanup._ai_review_batch(0, [Path("/tmp/a.flac")], [], "key", [], None, state, None)
        self.assertEqual(applied, [])  # a refusal processes no matches
        self.assertTrue(any("refusal" in line.lower() for line in state["log"]))
        self.assertTrue(any("done" in line for line in state["log"]), state["log"])

    # -- 7c: one malformed match skips only itself ----------------------------
    def test_one_malformed_match_skips_only_itself(self):
        applied = []

        def fake_apply(m, *a, **k):
            if m.get("bad"):
                raise ValueError("malformed match")
            applied.append(m)

        content = json.dumps({"matches": [{"file_idx": 1}, {"bad": True}, {"file_idx": 2}]})
        state = {"log": [], "found": 0}
        mocks = self._batch_mocks({"content": content})
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4], \
             mock.patch.object(routes_cleanup, "_ai_review_apply_match", side_effect=fake_apply):
            routes_cleanup._ai_review_batch(0, [Path("/tmp/a.flac")], [], "key", [], None, state, None)
        # Both good matches applied despite the malformed one between them.
        self.assertEqual(applied, [{"file_idx": 1}, {"file_idx": 2}])
        joined = "\n".join(state["log"])
        self.assertIn("skipped a malformed match", joined)
        self.assertTrue(any("done" in line for line in state["log"]), state["log"])


if __name__ == "__main__":
    unittest.main()
