"""Structural regression: every backend/beets_control_agent.py call into
backend/transaction_engine.py that passes a db_path= keyword argument must
pass the real Beets SQLite database file (LIB_PATH), never a media/staging
directory variable.

Wave 24 final review round 3, section 13: independent audit found 23 call
sites across 8 transaction families (track_replacement, bulk_import_
replacement, album_mb_track_repair, existing_album_reconcile,
artist_folder_reconcile, import_folder, folder_cleanup, and
playlist_media_cleanup) passing db_path=MUSIC_LIBRARY_PATH -- a media
directory, not the database file. Every transaction_engine function that
actually reads db_path opens it with sqlite3.connect(db_path, ...), so this
was not just imprecise naming: sqlite3.connect() on a directory path raises
OperationalError, meaning every one of those transaction families would fail
at the first real invocation. Fixed by replacing every occurrence with
LIB_PATH. This test parses the actual source via AST (not a substring grep)
so a future reintroduction -- in any call, in any family -- fails CI
immediately rather than only being caught the next time someone actually
exercises the broken family end-to-end.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROL_AGENT_PATH = ROOT / "backend" / "beets_control_agent.py"

# Variable names that are known to hold a media/staging DIRECTORY, never the
# Beets SQLite database FILE. Extend this list if a new media-root-shaped
# variable is introduced.
_KNOWN_MEDIA_ROOT_NAMES = {
    "MUSIC_LIBRARY_PATH",
    "MUSIC_ROOT",
    "DOWNLOAD_PATH",
    "STAGING_ROOT",
    "DOWNLOADS_ROOT",
    "ALBUM_ART_TRASH_ROOT",
    "RECONCILE_QUARANTINE_DIR",
}


def _find_db_path_violations(source: str) -> list:
    """Return a list of (lineno, function_name, offending_value_repr) for
    every `transaction_engine.<fn>(..., db_path=<expr>, ...)` call whose
    db_path argument is a bare Name matching a known media-root variable.
    """
    tree = ast.parse(source, filename=str(CONTROL_AGENT_PATH))
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_engine_call = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "transaction_engine"
        )
        if not is_engine_call:
            continue
        for kw in node.keywords:
            if kw.arg != "db_path":
                continue
            if isinstance(kw.value, ast.Name) and kw.value.id in _KNOWN_MEDIA_ROOT_NAMES:
                violations.append((node.lineno, func.attr, kw.value.id))
    return violations


class ControlAgentDbPathAuditTests(unittest.TestCase):
    def setUp(self):
        self.source = CONTROL_AGENT_PATH.read_text(encoding="utf-8")

    def test_no_transaction_engine_call_passes_a_media_root_as_db_path(self):
        violations = _find_db_path_violations(self.source)
        self.assertEqual(
            violations, [],
            "backend/beets_control_agent.py passes a media/staging directory as "
            "db_path= to transaction_engine -- every transaction_engine function "
            "that reads db_path opens it via sqlite3.connect(), so this is a "
            "guaranteed runtime failure, not just imprecise naming: "
            f"{violations}",
        )

    def test_every_transaction_engine_db_path_call_uses_lib_path(self):
        """Positive-side check: db_path= is always LIB_PATH (or omitted,
        which falls back to an env-derived DB path inside transaction_engine
        itself) -- never any other bare-name expression that isn't LIB_PATH."""
        tree = ast.parse(self.source, filename=str(CONTROL_AGENT_PATH))
        non_lib_path_calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "transaction_engine"):
                continue
            for kw in node.keywords:
                if kw.arg != "db_path":
                    continue
                if isinstance(kw.value, ast.Name) and kw.value.id != "LIB_PATH":
                    non_lib_path_calls.append((node.lineno, func.attr, kw.value.id))
        self.assertEqual(non_lib_path_calls, [], f"non-LIB_PATH db_path= call(s): {non_lib_path_calls}")

    def test_detector_itself_flags_a_known_bad_pattern(self):
        """Regression guard on the detector: a synthetic call using
        MUSIC_LIBRARY_PATH as db_path must actually be caught."""
        fake_source = (
            "import transaction_engine\n"
            "MUSIC_LIBRARY_PATH = '/data/media/music'\n"
            "transaction_engine.create_track_replacement_plan(store, payload, db_path=MUSIC_LIBRARY_PATH)\n"
        )
        violations = _find_db_path_violations(fake_source)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0][1], "create_track_replacement_plan")
        self.assertEqual(violations[0][2], "MUSIC_LIBRARY_PATH")


if __name__ == "__main__":
    unittest.main()
