"""#143: setup's Music Library check must follow MUSIC_ROOT, not a
hard-coded /data/media/music, and must not report fpcalc missing when
stock Beets reports chroma (mbsubmit) loaded.

The check is Web Manager-local: Web Manager reads library files itself, so
its own mount is the one that has to be readable. The status payload comes
from the stock-Beets plugin handshake (mocked here at the BeetsAdapter
boundary, as in tests/test_routes_setup.py).
"""
import os
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

from tests.test_routes_setup import _load_routes_setup_against_stub_app


def _plugin_status(**overrides):
    status = {
        "protocol_version": "1.0",
        "plugin_version": "1.5.0",
        "beets_version": "2.14.1",
        "capabilities": ["import", "modify", "remove", "move", "operations", "status", "mbsubmit"],
        "loaded_plugins": ["musicbrainz", "chroma", "fetchart"],
        "library_ready": True,
        "upstream_web_readonly": True,
        "plugin_mutations_enabled": True,
    }
    status.update(overrides)
    return status


def _plugins_report():
    names = ["musicbrainz", "chroma", "fetchart"]
    plugins = [{"name": n, "enabled": True, "loaded": True, "healthy": True} for n in names]
    return {
        "ok": True, "all_required_healthy": True, "required_count": 1, "required_healthy_count": 1,
        "plugins": plugins, "categories": {"required": [], "optional": [], "integration": []},
        "summary": {"total": len(plugins), "healthy": len(plugins), "errors": []},
    }


class SetupMusicRootTests(unittest.TestCase):
    def setUp(self):
        self.flask_app, self.module = _load_routes_setup_against_stub_app(self)
        self.client = self.flask_app.test_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _status(self, env, plugin_status=None, side_effect=None):
        from backend.beets_adapter import beets_adapter
        kw = {"side_effect": side_effect} if side_effect else {"return_value": plugin_status or _plugin_status()}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(beets_adapter, "get_plugin_status", **kw), \
             mock.patch.object(beets_adapter, "get_stats", return_value={"items": 0, "albums": 0}), \
             mock.patch("backend.beets_plugins.verify_all_plugins", return_value=_plugins_report()):
            self.module._invalidate_setup_status_cache()
            response = self.client.get("/api/setup/status")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def _reasons(self, body):
        return " | ".join(body["blocking_reasons"])

    def test_readable_music_root_is_reported_and_not_blocking(self):
        music = self.root / "music"
        music.mkdir()
        body = self._status({"MUSIC_ROOT": str(music)})

        check = body["paths"]["music_library"]
        self.assertEqual(check["path"], str(music))
        self.assertTrue(check["exists"])
        self.assertTrue(check["readable"])
        self.assertTrue(check["ok"])
        self.assertNotIn("Music library path", self._reasons(body))
        self.assertIn("music_library", body["beets"]["paths"])

    def test_missing_music_root_blocks_and_names_the_configured_path(self):
        missing = self.root / "not-mounted"
        body = self._status({"MUSIC_ROOT": str(missing)})

        self.assertEqual(body["paths"]["music_library"]["path"], str(missing))
        self.assertFalse(body["paths"]["music_library"]["readable"])
        reasons = self._reasons(body)
        self.assertIn(f"Music library path {missing} is not accessible", reasons)
        self.assertIn("MUSIC_ROOT", reasons)

    def test_unset_music_root_defaults_to_documented_mount(self):
        body = self._status({"MUSIC_ROOT": ""})
        self.assertEqual(Path(body["paths"]["music_library"]["path"]).as_posix(), "/music")

    def test_unreachable_beets_falls_back_to_configured_paths_not_legacy_defaults(self):
        from backend.beets_adapter import BeetsAdapterConnectionError
        body = self._status({"MUSIC_ROOT": ""}, side_effect=BeetsAdapterConnectionError("down"))
        self.assertEqual(Path(body["paths"]["music_library"]["path"]).as_posix(), "/music")
        self.assertEqual(body["paths"]["downloads"]["path"], "/downloads")
        text = repr(body["paths"])
        self.assertNotIn("/data/media/music", text)
        self.assertNotIn("/data/torrents", text)

    def test_fpcalc_reported_available_when_beets_reports_chroma(self):
        music = self.root / "music"
        music.mkdir()
        # IA-12: there is no built-in AcoustID key; "configured" requires a
        # user-supplied one (fake, non-secret value).
        body = self._status({
            "MUSIC_ROOT": str(music),
            "ACOUSTID_API_KEY": "fake-test-acoustid-key",
            "ACOUSTID_KEY": "",
        })
        self.assertTrue(body["fpcalc"]["available"])
        self.assertNotIn("fpcalc", self._reasons(body))
        self.assertEqual(body["integrations"]["acoustid"]["state"], "configured")

    def test_acoustid_not_configured_without_user_key_even_with_chroma(self):
        music = self.root / "music"
        music.mkdir()
        body = self._status({"MUSIC_ROOT": str(music), "ACOUSTID_API_KEY": "", "ACOUSTID_KEY": ""})
        self.assertTrue(body["fpcalc"]["available"])
        self.assertEqual(body["integrations"]["acoustid"]["state"], "not_configured")

    def test_fpcalc_still_blocks_when_beets_lacks_chroma(self):
        status = _plugin_status(
            capabilities=["import", "modify", "remove", "move", "operations", "status"],
            loaded_plugins=["musicbrainz", "fetchart"],
        )
        body = self._status({"MUSIC_ROOT": str(self.root)}, plugin_status=status)
        self.assertFalse(body["fpcalc"]["available"])
        self.assertIn("fpcalc (chromaprint) not found", self._reasons(body))
        self.assertEqual(body["integrations"]["acoustid"]["state"], "dependency_plugin_missing")


class SetupSourceHasNoLegacyLibraryDefaultTests(unittest.TestCase):
    def test_routes_setup_has_no_hardcoded_legacy_library_or_staging_default(self):
        source = (Path(__file__).resolve().parents[1] / "routes_setup.py").read_text(encoding="utf-8")
        self.assertNotIn('"/data/media/music"', source)
        self.assertNotIn('_remote_path("downloads", "/data/torrents"', source)


if __name__ == "__main__":
    unittest.main()
