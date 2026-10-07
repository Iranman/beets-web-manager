"""webmanager plugin 1.8.0: POST /webmanager/import runs Beets' own quiet
importer (beet import -q [--search-id]) and reports the folders Beets skipped.

These run the real Beets importer offline. With no metadata-source plugin
loaded Beets finds no candidates, so autotag + quiet_fallback=skip skips the
album (left in place) and quiet_fallback=asis imports it with its tags.
"""
import os
import shutil
import struct
import tempfile
import unittest
import wave

import mutagen.id3
import mutagen.wave
from beets import config as beets_config
from beets.library import Library
from beetsplug.web import app as beets_web_app
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key_file
import beetsplug.webmanager.operations as ops_mod

TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
REL = "a6a6e718-3d8e-4c3e-9eaa-7b54af639bb9"


def _album(folder, artist, album, n=2):
    os.makedirs(folder)
    for i in range(1, n + 1):
        path = os.path.join(folder, f"0{i}.wav")
        with wave.open(path, "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(struct.pack("<h", 0) * 8000)
        mw = mutagen.wave.WAVE(path)
        mw.add_tags()
        for frame in (mutagen.id3.TIT2(encoding=3, text=[f"Track {i}"]),
                      mutagen.id3.TPE1(encoding=3, text=[artist]),
                      mutagen.id3.TALB(encoding=3, text=[album]),
                      mutagen.id3.TRCK(encoding=3, text=[str(i)])):
            mw.tags.add(frame)
        mw.save()


class PluginImportAutotagTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.music = os.path.join(self.td, "music")
        self.downloads = os.path.join(self.td, "downloads")
        os.makedirs(self.music)
        os.makedirs(self.downloads)
        self._dir = beets_config["directory"].get()
        beets_config["directory"] = self.music
        self.lib = Library(os.path.join(self.td, "lib.blb"), directory=self.music)
        key = os.path.join(self.td, ".key")
        with open(key, "w", encoding="utf-8") as f:
            f.write(TOKEN + "\n")
        self.plugin = WebManagerPlugin()
        set_api_key_file(key)
        ops_mod.set_allowed_roots([self.music, self.downloads])
        ops_mod.set_import_roots([self.downloads])
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["TESTING"] = True
        self.client = beets_web_app.test_client()

    def tearDown(self):
        beets_config["directory"] = self._dir
        ops_mod.set_allowed_roots(None)
        ops_mod.set_import_roots(None)
        set_api_key_file(None)
        try:
            self.lib._connection().close()
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def _import(self, **body):
        return self.client.post("/webmanager/import", headers={"Authorization": f"Bearer {TOKEN}"}, json=body)

    def test_option_validation(self):
        src = os.path.join(self.downloads, "A")
        os.makedirs(src)
        for body, code in (
            ({"quiet_fallback": "ask"}, "INVALID_QUIET_FALLBACK"),
            ({"autotag": True, "search_ids": [REL.upper()]}, "INVALID_SEARCH_IDS"),
            ({"autotag": True, "search_ids": REL}, "INVALID_SEARCH_IDS"),
            ({"search_ids": [REL]}, "INVALID_SEARCH_IDS"),
        ):
            res = self._import(paths=[src], **body)
            self.assertEqual((res.status_code, res.get_json()["error_code"]), (400, code), body)

    def test_autotag_skip_reports_skipped_folder_and_restores_config(self):
        src = os.path.join(self.downloads, "Some Artist - Unmatched")
        _album(src, "Some Artist", "Unmatched")
        before = {k: beets_config["import"][k].get() for k in ("autotag", "search_ids", "quiet_fallback", "copy")}
        res = self._import(paths=[src], autotag=True, search_ids=[REL], copy=True, move=False)
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()
        self.assertTrue(data["autotag"])
        self.assertEqual((data["quiet_fallback"], data["search_ids"]), ("skip", [REL]))
        self.assertEqual(data["skipped_paths"], [src])
        self.assertEqual(len(self.lib.albums()), 0)
        self.assertEqual(sorted(os.listdir(src)), ["01.wav", "02.wav"])
        self.assertEqual(before, {k: beets_config["import"][k].get() for k in before})

    def test_autotag_asis_imports_when_asked(self):
        src = os.path.join(self.downloads, "Some Artist - Asis")
        _album(src, "Some Artist", "Asis")
        res = self._import(paths=[src], autotag=True, quiet_fallback="asis", copy=True, move=False)
        self.assertEqual(res.get_json()["skipped_paths"], [])
        self.assertEqual([a.album for a in self.lib.albums()], ["Asis"])
        self.assertTrue(os.path.isdir(src))

    def test_duplicate_skip_is_reported(self):
        src = os.path.join(self.downloads, "Some Artist - Dup")
        _album(src, "Some Artist", "Dup")
        self._import(paths=[src], copy=True, move=False)
        res = self._import(paths=[src], copy=True, move=False, duplicate_action="skip")
        self.assertEqual(res.get_json()["skipped_paths"], [src])
        self.assertEqual(len(self.lib.albums()), 1)

    def test_in_place_only_inside_the_library_directory(self):
        inside = os.path.join(self.music, "Some Artist", "In Place")
        _album(inside, "Some Artist", "In Place")
        res = self._import(paths=[inside], copy=True, move=False)
        self.assertEqual(res.get_json()["error_code"], "PATH_NOT_ALLOWED")
        res = self._import(paths=[inside], move=True)
        self.assertEqual(res.get_json()["error_code"], "PATH_NOT_ALLOWED")
        res = self._import(paths=[self.music], copy=False, move=False)
        self.assertEqual(res.get_json()["error_code"], "PATH_NOT_ALLOWED")
        res = self._import(paths=[inside], copy=False, move=False)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(len(self.lib.albums()), 1)
        self.assertEqual(sorted(os.listdir(inside)), ["01.wav", "02.wav"])


class MusicbrainzProvisioningTests(unittest.TestCase):
    """Beets >= 2.4 needs `musicbrainz` in plugins:; provisioning adds it and
    keeps every entry, whatever the YAML form."""

    def test_musicbrainz_added_and_user_entries_kept(self):
        from backend.beets_plugins import REQUIRED_CONFIG_PLUGINS, _plan_config_yaml_plugins, parse_configured_plugins
        import yaml
        for text, kept in (
            ("plugins: fetchart web  # mine\n", ["fetchart", "web"]),
            ("plugins: [chroma, 'web']\n", ["chroma", "web"]),
            ("plugins:\n  - lastgenre # mine\n  - \"webmanager\"\nother: 1\n", ["lastgenre", "webmanager"]),
            ("directory: /music\n", []),
        ):
            new, _, changed = _plan_config_yaml_plugins(text, REQUIRED_CONFIG_PLUGINS, ["/config/beetsplug"])
            self.assertTrue(changed)
            listed = yaml.safe_load(new)["plugins"].split()
            self.assertEqual(listed[:len(kept)], kept, text)
            self.assertTrue({"web", "webmanager", "musicbrainz"} <= set(listed), text)
            self.assertEqual(parse_configured_plugins(new), listed)
        unchanged = "plugins: musicbrainz web webmanager\npluginpath:\n  - /config/beetsplug\nweb:\n  include_paths: yes\n"
        self.assertFalse(_plan_config_yaml_plugins(unchanged, REQUIRED_CONFIG_PLUGINS, ["/config/beetsplug"])[2])


if __name__ == "__main__":
    unittest.main()
