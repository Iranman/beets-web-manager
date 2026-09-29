"""Where library files live and whether duplicates may be deleted unattended
are independent controls.

MUSIC_ROOT only configures paths. Unattended duplicate deletion needs an
explicit operator authorization (backend/dedup_authorization), off by default;
changing or fixing MUSIC_ROOT can make the scan find files but can never
authorize deleting them.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
from backend import dedup_authorization as auth  # noqa: E402
from backend import dedup_service  # noqa: E402

try:  # ARCH-001: patch app.py and the modules extracted from it
    from _app_family import patch_app_family  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_family import patch_app_family  # noqa: E402


class AuthorizationStateTests(unittest.TestCase):
    def test_off_by_default_and_on_any_doubt(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            self.assertFalse(auth.unattended_delete_enabled(data))
            (data / auth.STATE_FILE_NAME).write_text("{not json", encoding="utf-8")
            self.assertFalse(auth.unattended_delete_enabled(data))
            (data / auth.STATE_FILE_NAME).write_text(json.dumps({"unattended_delete_enabled": "true"}), encoding="utf-8")
            self.assertFalse(auth.unattended_delete_enabled(data))
            (data / auth.STATE_FILE_NAME).write_text(json.dumps([True]), encoding="utf-8")
            self.assertFalse(auth.unattended_delete_enabled(data))

    def test_explicit_toggle_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            auth.set_unattended_delete(data, True, actor="tester", reason="reviewed six")
            self.assertTrue(auth.unattended_delete_enabled(data))
            state = auth.load_authorization(data)
            self.assertEqual((state["changed_by"], state["reason"]), ("tester", "reviewed six"))
            auth.set_unattended_delete(data, False, actor="tester", reason="")
            self.assertFalse(auth.unattended_delete_enabled(data))

    def test_authorization_does_not_depend_on_configuration(self):
        import ast
        tree = ast.parse((ROOT / "backend" / "dedup_authorization.py").read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        imports = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        imports |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertNotIn("MUSIC_ROOT", names)
        self.assertFalse({"environ", "getenv"} & attrs)
        self.assertFalse({m for m in imports if "app_runtime" in m or m == "app"})


class MusicRootSettingTests(unittest.TestCase):
    def _root_for(self, env_value):
        env = dict(os.environ)
        env.pop("MUSIC_ROOT", None)
        if env_value is not None:
            env["MUSIC_ROOT"] = env_value
        with tempfile.TemporaryDirectory() as tmp:
            env["WEB_MANAGER_DATA_DIR"] = tmp
            out = subprocess.check_output(
                [sys.executable, "-c", "from backend import app_runtime as r; print(r.MUSIC_ROOT); print(r.PLAYLIST_DIR)"],
                cwd=str(ROOT), env=env, text=True,
            ).strip().splitlines()
        return [Path(v).as_posix() for v in out[-2:]]

    def test_music_root_is_one_configurable_setting_defaulting_to_the_stack_mount(self):
        self.assertEqual(self._root_for(None)[0], "/music")
        root, playlists = self._root_for("/srv/library")
        self.assertEqual(root, "/srv/library")
        self.assertEqual(playlists, "/srv/library/playlists")

    def test_no_module_hard_codes_the_legacy_library_root_as_its_root(self):
        """Code (not comments or docstrings) must use MUSIC_ROOT; the legacy
        path may only appear as one alias in a path-translation list."""
        import ast
        offenders = []
        for path in [ROOT / "app.py", *ROOT.glob("routes_*.py"), *(ROOT / "backend").glob("*_service.py")]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            allowed = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                    allowed.add(id(node.value))  # docstring
                elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                    allowed.update(id(e) for e in node.elts)  # alias lists
                elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_remote_path":
                    allowed.update(id(a) for a in node.args)  # engine-side status probe
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and "/data/media/music" in node.value and id(node) not in allowed):
                    offenders.append(f"{path.name}:{node.lineno}: {node.value[:80]!r}")
        self.assertEqual(offenders, [])


class ScheduledDuplicateStepTests(unittest.TestCase):
    """The maintenance step proposes but never deletes unless authorized --
    whatever MUSIC_ROOT is."""

    def _run(self, *, authorized):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "library"
        data = Path(tmp.name) / "data"
        (root / "A").mkdir(parents=True)
        data.mkdir()
        keep = root / "A" / "01 - Song.flac"
        drop = root / "A" / "01 - Song.1.flac"
        keep.write_bytes(b"keep-copy")
        drop.write_bytes(b"other-bytes")
        if authorized:
            auth.set_unattended_delete(data, True, actor="tester", reason="test")
        scan = {"scanned": 2, "duplicates": [{
            "source_path": str(drop), "lib_path": str(keep), "source_item_id": 20, "lib_id": 10,
            "source_album_id": 5, "lib_album_id": 5, "release_relation": "same_release_position",
            "match_type": "MB Track ID", "confidence": "high", "fingerprint_verified": True,
            "fingerprint_mbid": "11111111-1111-1111-1111-111111111111",
            "source_fingerprint_ids": ["11111111-1111-1111-1111-111111111111"],
            "lib_fingerprint_ids": ["11111111-1111-1111-1111-111111111111"],
            "source_recording_id": "3xjazw6zbadid", "lib_recording_id": "3xjazw6zbadid",
            "source_disc": 1, "source_track": 1, "lib_disc": 1, "lib_track": 1,
        }]}
        cleanup = mock.Mock(return_value={"ok": True, "deleted": 1, "skipped": 0, "folders_removed": 0, "results": []})
        patches = [
            patch_app_family(app_module, "MUSIC_ROOT", root),
            patch_app_family(app_module, "WEB_MANAGER_DATA_DIR", data),
            patch_app_family(app_module, "_running_job_of_type", return_value=None),
            patch_app_family(app_module, "start_dedup_scan", return_value=({"ok": True, "job_id": "j1"}, 200)),
            patch_app_family(app_module, "_wait_for_child_job", return_value=scan),
            patch_app_family(app_module, "_unattended_reviewed_cleanup", cleanup),
            patch_app_family(app_module, "_maintenance_save_last_report"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        log = []
        result = dedup_service._maintenance_full_duplicate_scan(log)
        return result, cleanup, log, drop, keep

    def test_disabled_by_default_proposes_with_full_evidence_and_deletes_nothing(self):
        result, cleanup, log, drop, keep = self._run(authorized=False)
        cleanup.assert_not_called()
        summary = result["final_summary"]
        self.assertFalse(summary["unattended_delete_enabled"])
        self.assertEqual((summary["proposed_deletions"], summary["deleted_files"]), (1, 0))
        [row] = result["proposal"]
        self.assertEqual(row["delete"]["path"], str(drop))
        self.assertEqual(row["keep"]["path"], str(keep))
        self.assertEqual((row["delete"]["size"], row["keep"]["size"]), (len(b"other-bytes"), len(b"keep-copy")))
        self.assertEqual((row["delete"]["disc"], row["delete"]["track"]), (1, 1))
        self.assertEqual(row["release_relation"], "same_release_position")
        self.assertTrue(row["fingerprint"]["verified"])
        self.assertTrue(row["embedded_id_contradicts_fingerprint"])
        self.assertTrue(any("Unattended deletion is disabled" in line for line in log))
        self.assertTrue(any(line.startswith("[duplicates] PROPOSED delete") for line in log))

    def test_scheduled_step_only_checks_beets_tracked_files(self):
        self._run(authorized=False)
        payload = app_module.start_dedup_scan.call_args.args[0]
        self.assertIs(payload.get("tracked_only"), True)

    def test_only_the_explicit_authorization_lets_the_step_delete(self):
        _result, cleanup, _log, drop, _keep = self._run(authorized=True)
        # Authorized runs go through the reviewed-cleanup authority (re-verify,
        # engine quarantine, verify) with exactly the proposed delete rows.
        cleanup.assert_called_once()
        proposal = cleanup.call_args.args[0]
        self.assertEqual([row["delete"]["path"] for row in proposal if row["action"] == "delete"], [str(drop)])


class AuthorizationRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch_app_family(app_module, "WEB_MANAGER_DATA_DIR", Path(self.tmp.name)),
                  mock.patch.dict(os.environ, {"BEETS_WEB_AUTH_DISABLED": "1"})):
            p.start()
            self.addCleanup(p.stop)
        self.client = app_module.app.test_client()

    def test_enabling_needs_the_exact_confirmation_and_disabling_does_not(self):
        url = "/api/dedup/unattended-cleanup"
        self.assertFalse(self.client.get(url).get_json()["authorization"]["unattended_delete_enabled"])
        self.assertEqual(self.client.post(url, json={"enabled": True}).status_code, 400)
        self.assertEqual(self.client.post(url, json={"enabled": "yes", "confirm": auth.ENABLE_CONFIRMATION}).status_code, 400)
        self.assertFalse(auth.unattended_delete_enabled(Path(self.tmp.name)))
        resp = self.client.post(url, json={"enabled": True, "confirm": auth.ENABLE_CONFIRMATION, "reason": "reviewed"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["authorization"]["unattended_delete_enabled"])
        resp = self.client.post(url, json={"enabled": False})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(auth.unattended_delete_enabled(Path(self.tmp.name)))



class TrackedOnlyScanTests(unittest.TestCase):
    """tracked_only enumerates Beets-tracked files, never every file on disk."""

    def test_untracked_files_are_not_scanned(self):
        import time
        from types import SimpleNamespace
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "Tracked").mkdir()
        (root / "Stray").mkdir()
        tracked = root / "Tracked" / "01 - A.flac"
        tracked.write_bytes(b"a")
        (root / "Stray" / "01 - B.flac").write_bytes(b"b")
        (root / "Stray" / "02 - C.flac").write_bytes(b"c")
        item = SimpleNamespace(id=1, album_id=1, path="Tracked/01 - A.flac", mb_trackid="", title="A", artist="X",
                               albumartist="X", album="Al", disc=1, track=1, length=10.0, mb_albumid="")
        fake_lib = SimpleNamespace(items=lambda *_a, **_k: [item], get_item=lambda _i: item)
        for patcher in (patch_app_family(app_module, "MUSIC_ROOT", root),
                        patch_app_family(app_module, "lib", fake_lib),
                        patch_app_family(app_module, "_resolve_dedup_scan_path", return_value=(root, None)),
                        patch_app_family(app_module, "_acoustid_fingerprint_ids", return_value=[]),
                        patch_app_family(app_module, "_acoustid_fingerprint_match", return_value=("", [], []))):
            patcher.start()
            self.addCleanup(patcher.stop)
        body, status = dedup_service.start_dedup_scan({"path": str(root), "tracked_only": True})
        self.assertEqual(status, 200, body)
        job = app_module.jobs.get(body["job_id"])
        for _ in range(200):
            if job.status in ("success", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(job.status, "success", job.log[-5:] if job.log else None)
        self.assertIn("Found 1 audio file to check", "\n".join(job.log))


if __name__ == "__main__":
    unittest.main()
