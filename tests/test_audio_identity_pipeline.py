import ast
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
HELPERS_SOURCE = (ROOT / "helpers_mb.py").read_text(encoding="utf-8")


def load_symbols(names, namespace):
    wanted = set(names)
    tree = ast.parse(APP_SOURCE)
    body = []
    for node in tree.body:
        if getattr(node, "name", None) in wanted:
            body.append(node)
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, "app.py", "exec"), namespace)


def norm(value):
    return " ".join(str(value or "").casefold().split())


def score(a, b):
    return 1.0 if norm(a) == norm(b) else 0.0


class SharedAudioIdentityDecisionTests(unittest.TestCase):
    def namespace(self, lookup=None):
        ns = {
            "Any": Any,
            "Dict": Dict,
            "List": List,
            "Optional": Optional,
            "Path": Path,
            "os": os,
            "_s": lambda value: value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or ""),
            "_playlist_title_score": score,
            "_playlist_artist_name_score": score,
            "_acoustid_lookup_cached": lookup or (lambda path: []),
        }
        load_symbols([
            "_audio_identity_score",
            "_audio_identity_compact_candidate",
            "_audio_identity_decision",
            "_playlist_identity_status_fields",
        ], ns)
        return ns

    def audio_path(self, tmp):
        path = Path(tmp) / "wrong filename.mp3"
        path.write_bytes(b"fake audio")
        return str(path)

    def test_correct_fingerprint_accepts_even_when_text_match_is_bad(self):
        ns = self.namespace()
        with tempfile.TemporaryDirectory() as tmp:
            result = ns["_audio_identity_decision"](
                self.audio_path(tmp),
                expected_artist="Artist",
                expected_title="Right Song",
                text_match={"ok": False, "title_score": 0.1, "artist_score": 0.1},
                acoustid_candidates=[{
                    "score": 94,
                    "acoustid_id": "aid-1",
                    "mb_trackid": "rec-1",
                    "mb_releasegroupid": "rg-1",
                    "title": "Right Song",
                    "artist": "Artist",
                }],
            )
        self.assertEqual(result["final_action"], "accept")
        self.assertEqual(result["identity_status"], "verified")
        self.assertEqual(result["acoustid_match_score"], 0.94)
        self.assertEqual(result["mb_recording_id_candidate"], "rec-1")

    def test_musicbrainz_recording_id_confirms_ambiguous_text(self):
        ns = self.namespace()
        with tempfile.TemporaryDirectory() as tmp:
            result = ns["_audio_identity_decision"](
                self.audio_path(tmp),
                expected_artist="Alias",
                expected_title="Alias Title",
                expected_mb_trackid="rec-expected",
                text_match={"ok": False},
                acoustid_candidates=[{
                    "score": 88,
                    "acoustid_id": "aid-2",
                    "mb_trackid": "rec-expected",
                    "title": "Canonical Title",
                    "artist": "Canonical Artist",
                }],
            )
        self.assertEqual(result["final_action"], "accept")
        self.assertIn("MusicBrainz recording ID", result["decision_reason"])

    def test_strong_fingerprint_mismatch_rejects_text_only_match(self):
        ns = self.namespace()
        with tempfile.TemporaryDirectory() as tmp:
            result = ns["_audio_identity_decision"](
                self.audio_path(tmp),
                expected_artist="Artist",
                expected_title="Right Song",
                text_match={"ok": True, "title_score": 1.0, "artist_score": 1.0},
                acoustid_candidates=[{
                    "score": 93,
                    "acoustid_id": "aid-3",
                    "mb_trackid": "rec-wrong",
                    "title": "Other Song",
                    "artist": "Other Artist",
                }],
            )
        self.assertEqual(result["final_action"], "reject")
        self.assertEqual(result["identity_status"], "conflict")
        self.assertIn("text_metadata_disagrees_with_fingerprint", result["conflicts"])

    def test_no_acoustid_result_requires_review_not_text_only_acceptance(self):
        ns = self.namespace()
        with tempfile.TemporaryDirectory() as tmp:
            result = ns["_audio_identity_decision"](
                self.audio_path(tmp),
                expected_artist="Artist",
                expected_title="Right Song",
                text_match={"ok": True, "title_score": 1.0, "artist_score": 1.0},
                acoustid_candidates=[],
            )
        self.assertEqual(result["final_action"], "review")
        self.assertEqual(result["fingerprint_status"], "no_result")
        self.assertIn("no_acoustid_result", result["conflicts"])

    def test_fingerprint_lookup_failure_is_recorded(self):
        def failing_lookup(path):
            raise RuntimeError("temporary API failure")

        ns = self.namespace(failing_lookup)
        with tempfile.TemporaryDirectory() as tmp:
            result = ns["_audio_identity_decision"](
                self.audio_path(tmp),
                expected_artist="Artist",
                expected_title="Right Song",
                text_match={"ok": True},
            )
        self.assertEqual(result["fingerprint_status"], "failed")
        self.assertEqual(result["final_action"], "review")
        self.assertIn("temporary API failure", result["decision_reason"])

    def test_playlist_state_fields_carry_compact_evidence(self):
        ns = self.namespace()
        fields = ns["_playlist_identity_status_fields"]({
            "identity": {
                "identity_status": "verified",
                "fingerprint_status": "matched",
                "acoustid_status": "confirmed",
                "acoustid_match_score": 0.94,
                "decision_reason": "verified",
                "mb_recording_id_candidate": "rec-1",
                "mb_release_group_id_candidate": "rg-1",
            }
        })
        self.assertEqual(fields["identity_status"], "verified")
        self.assertEqual(fields["identity_mb_trackid"], "rec-1")
        self.assertEqual(fields["identity_mb_releasegroupid"], "rg-1")


class AudioIdentityPipelineStaticTests(unittest.TestCase):
    def function_source(self, name):
        start = APP_SOURCE.index(f"def {name}(")
        end = APP_SOURCE.find("\ndef ", start + 5)
        if end == -1:
            end = len(APP_SOURCE)
        return APP_SOURCE[start:end]

    def test_cached_lookup_is_used_by_shared_identity_decision(self):
        body = self.function_source("_audio_identity_decision")
        self.assertIn("_acoustid_lookup_cached", body)
        self.assertIn("final_action", body)
        self.assertIn("metadata_agreement", body)

    def test_playlist_download_gate_uses_shared_identity_decision(self):
        body = self.function_source("_playlist_download_match")
        self.assertIn("_audio_identity_decision", body)
        self.assertIn('match["ok"] = identity.get("final_action") == "accept"', body)

    def test_playlist_new_downloads_hold_unverified_audio_for_review(self):
        body = self.function_source("_playlist_validate_downloaded_files")
        self.assertIn("review_out.append", body)
        self.assertIn("accepted fingerprint-verified download", body)
        self.assertIn("rejected mismatched download", body)

    def test_resume_reconciliation_uses_cached_acoustid_once_per_file(self):
        body = self.function_source("_playlist_reconcile_staged_files")
        self.assertIn("_acoustid_lookup_cached(audio_path)", body)
        self.assertIn("_audio_identity_decision", body)
        self.assertIn("_playlist_identity_status_fields", body)

    def test_import_downloaded_uses_same_identity_gate(self):
        body = self.function_source("_playlist_run_import_downloaded")
        self.assertIn("_playlist_download_match", body)
        self.assertIn("No downloaded playlist staging files are fingerprint-verified", body)

    def test_replacement_verification_requires_fingerprint_evidence(self):
        body = self.function_source("_music_format_find_verified_replacement")
        self.assertIn("_acoustid_fingerprint_match", body)
        self.assertIn("_acoustid_fingerprint_ids(str(final_path))", body)
        self.assertIn("fingerprint_validation", body)

    def test_ai_suggest_prompt_and_evidence_include_fingerprint_conflict_rule(self):
        self.assertIn("Do not silently override strong contradictory fingerprint evidence", APP_SOURCE)
        body = self.function_source("_track_ai_evidence_packet")
        self.assertIn('"fingerprint"', body)
        self.assertIn('"acoustid_id"', body)

    def test_acoustid_lookup_has_retry_rate_limit_and_release_group_fields(self):
        self.assertIn("_ACOUSTID_LOOKUP_LOCK", HELPERS_SOURCE)
        self.assertIn("for attempt in range(2)", HELPERS_SOURCE)
        self.assertIn('"acoustid_id"', HELPERS_SOURCE)
        self.assertIn('"mb_releasegroupid"', HELPERS_SOURCE)


if __name__ == "__main__":
    unittest.main()