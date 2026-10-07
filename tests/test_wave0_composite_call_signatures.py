"""Wave 0 (LT-18, LT-12): every production call of a composite_workflows
function must bind to its real signature, and the helpers that used to fake
success must not.

On deb4ec3, get_library_health(orphan_sample_limit=...), move_library(query=...),
create_hardlink(expected_size=...), replace_album_art(source=...) and
mbsync(query=...) -> beets_adapter.mbsync(query=...) all raised TypeError at
run time, so Clean All's first step and several routes could never work.
"""

import ast
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.composite_workflows as cw

ROOT = Path(__file__).resolve().parents[1]


def _production_files():
    files = list(ROOT.glob("*.py")) + list((ROOT / "backend").rglob("*.py"))
    return [f for f in files if "tests" not in f.parts]


def _composite_calls():
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "composite_workflows"):
                yield path.relative_to(ROOT).as_posix(), node.lineno, node


#: Pre-existing call sites (on deb4ec3) that do NOT bind and therefore raise
#: TypeError at run time -- they fail closed today. They are outside Wave 0
#: (S1 containment) because making them bind would switch on mutation or
#: playlist/matching flows nobody has reviewed; each is listed with its owner
#: for a later wave. The test fails on any NEW mismatch AND when a listed one
#: starts binding, so this list can only shrink.
KNOWN_UNBOUND_CALLS = {
    # music-identity / backend: read helpers called with arguments the shim lacks
    ("routes_library.py", "get_mbid_sticking_candidates"),
    ("backend/ai_service.py", "discover_import_sources"),
    ("backend/import_service.py", "find_items_by_query"),
    ("backend/matching_service.py", "get_artist_folder_album_mbids"),
    # library-transaction (later wave): mutation flows that would be switched ON
    ("backend/import_reconciliation_service.py", "apply_artist_folder_reconcile"),
    ("backend/import_service.py", "reimport_source"),
    ("backend/import_service.py", "apply_confirmed_import"),
    # integrations-automation: playlist shims (also return fabricated ok)
    ("backend/playlist_service.py", "get_playlist_quality_candidates"),
    ("backend/playlist_service.py", "validate_playlist_staged_track"),
    ("backend/playlist_service.py", "place_playlist_imported_item"),
    ("backend/playlist_service.py", "import_playlist_staged"),
}


class CompositeCallSignatureTests(unittest.TestCase):
    def test_every_call_site_binds_to_the_real_signature(self):
        problems = []
        still_unbound = set()
        seen = 0
        for rel, line, node in _composite_calls():
            name = node.func.attr
            fn = getattr(cw, name, None)
            if fn is None:
                problems.append(f"{rel}:{line} composite_workflows.{name} does not exist")
                continue
            if not callable(fn) or isinstance(fn, type):
                continue
            if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                continue  # *args/**kwargs at the call site: arity unknown statically
            seen += 1
            try:
                inspect.signature(fn).bind(*([None] * len(node.args)), **{k.arg: None for k in node.keywords})
            except TypeError as exc:
                if (rel, name) in KNOWN_UNBOUND_CALLS:
                    still_unbound.add((rel, name))
                    continue
                problems.append(f"{rel}:{line} composite_workflows.{name}: {exc}")
        self.assertGreater(seen, 100, "the AST scan found suspiciously few call sites")
        self.assertEqual(problems, [])
        self.assertEqual(KNOWN_UNBOUND_CALLS - still_unbound, set(),
                         "these now bind -- remove them from KNOWN_UNBOUND_CALLS")

    def test_composite_callees_bind_to_the_adapter(self):
        """mbsync() used to forward query= to an adapter method without it."""
        from backend.beets_adapter import BeetsAdapter
        src = inspect.getsource(cw)
        tree = ast.parse(src)
        problems = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id in ("ad", "beets_adapter")):
                meth = getattr(BeetsAdapter, node.func.attr, None)
                if meth is None or any(isinstance(a, ast.Starred) for a in node.args) \
                        or any(k.arg is None for k in node.keywords):
                    continue
                try:
                    inspect.signature(meth).bind(None, *([None] * len(node.args)),
                                                 **{k.arg: None for k in node.keywords})
                except TypeError as exc:
                    problems.append(f"line {node.lineno} {node.func.attr}: {exc}")
        self.assertEqual(problems, [])


class NoFakeSuccessTests(unittest.TestCase):
    def test_run_command_does_not_claim_success(self):
        ad = mock.MagicMock()
        res = cw.run_command("mbsubmit", ["album_id:5"], adapter=ad)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "not_supported")
        self.assertEqual(ad.method_calls, [])

    def test_get_job_reads_the_engine_and_cancel_is_honest(self):
        ad = mock.MagicMock()
        ad.get_operation.return_value = {"status": "failed", "error": "boom"}
        self.assertEqual(cw.get_job("op1", adapter=ad)["status"], "failed")
        ad.get_operation.return_value = {"status": "succeeded"}
        self.assertEqual(cw.get_job("op1", adapter=ad)["status"], "success")
        self.assertFalse(cw.cancel_job("op1")["cancelled"])

    def test_library_wide_operations_are_refused(self):
        self.assertEqual(cw.move_library(query="", rescan_first=True)["code"], "not_supported")
        self.assertEqual(cw.mbsync(query="", async_job=True)["code"], "not_supported")
        self.assertEqual(cw.replace_album_art(1, "AAAA", source="x")["code"], "not_supported")

    def test_move_album_to_library_uses_the_relocation_family(self):
        ad = mock.MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            from backend.transaction_engine import TransactionStore
            res = cw.move_album_to_library(7, adapter=ad, store=TransactionStore(tmp))
        self.assertTrue(res["ok"])
        ad.move.assert_called_once_with(album_ids=[7])

    def test_create_hardlink_is_staging_contained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            music, dl = root / "music", root / "dl"
            music.mkdir()
            dl.mkdir()
            src = music / "a.flac"
            src.write_bytes(b"12345")
            with mock.patch.dict(os.environ, {"MUSIC_ROOT": str(music), "DOWNLOAD_PATH": str(dl),
                                              "DOWNLOADS_ROOT": str(dl), "WEB_MANAGER_DATA_DIR": str(root / "d")}):
                with self.assertRaises(ValueError):
                    cw.create_hardlink(str(src), str(music / "b.flac"), expected_size=5)
                with self.assertRaises(ValueError):
                    cw.create_hardlink(str(src), str(dl / "b.flac"), expected_size=4)
                try:
                    res = cw.create_hardlink(str(src), str(dl / "t" / "b.flac"), expected_size=5)
                except OSError as exc:  # pragma: no cover - filesystems without hardlinks
                    self.skipTest(f"hardlinks unsupported here: {exc}")
                self.assertTrue(res["ok"])
                again = cw.create_hardlink(str(src), str(dl / "t" / "b.flac"), expected_size=5)
                self.assertTrue(again["already_present"])

    def test_clean_all_health_stage_runs(self):
        import backend.maintenance_service as ms
        ad = mock.MagicMock()
        ad.get_albums.return_value = [{"id": 1, "album": "A", "albumartist": "X", "mb_releasegroupid": "rg"},
                                      {"id": 2, "album": "A", "albumartist": "X", "mb_releasegroupid": "rg"},
                                      {"id": 3, "album": "Empty"}]
        ad.get_items.return_value = [{"id": 10, "album_id": 1, "path": "/nonexistent/x.flac"}]
        with mock.patch.object(cw, "beets_adapter", ad):
            res = ms._library_health_payload()
        self.assertTrue(res["ok"])
        self.assertEqual(res["duplicate_album_count"], 1)
        self.assertEqual(res["rgid_duplicate_group_count"], 1)
        self.assertEqual(res["empty_album_count"], 2)


if __name__ == "__main__":
    unittest.main()
