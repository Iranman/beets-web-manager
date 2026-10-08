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

    def test_import_never_links_and_restores_link_settings(self):
        """L1: a user's `link: yes` must not turn an import into symlinks, and
        ImportSession's own link/hardlink/reflink reset must not persist."""
        src = os.path.join(self.downloads, "Some Artist - Linked")
        _album(src, "Some Artist", "Linked")
        before = {k: beets_config["import"][k].get() for k in ("link", "hardlink", "reflink")}
        beets_config["import"]["link"] = True
        try:
            res = self._import(paths=[src], copy=False, move=False)
            self.assertEqual(res.status_code, 200, res.get_json())
            self.assertEqual(len(self.lib.albums()), 1)
            self.assertEqual(os.listdir(self.music), [])
            self.assertIs(beets_config["import"]["link"].get(), True)
            other = os.path.join(self.downloads, "Some Artist - Moved")
            _album(other, "Some Artist", "Moved")
            res = self._import(paths=[other], move=True)
            self.assertEqual(res.status_code, 200, res.get_json())
            self.assertIs(beets_config["import"]["link"].get(), True)
            for item in self.lib.items():
                self.assertFalse(os.path.islink(item.path), item.path)
        finally:
            for k, v in before.items():
                beets_config["import"][k] = v

    def test_in_place_refused_when_library_dir_covers_config_dir(self):
        """L2: the same _covers_config_dir guard as _derived_allowed_roots."""
        beets_config["directory"] = str(beets_config.config_dir())
        res = self._import(paths=[os.path.join(self.downloads, "X")], copy=False, move=False)
        self.assertEqual((res.status_code, res.get_json()["error_code"]), (400, "PATH_NOT_ALLOWED"))


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
            listed = yaml.safe_load(new)["plugins"]
            listed = listed.split() if isinstance(listed, str) else listed
            self.assertEqual(listed[:len(kept)], kept, text)
            self.assertTrue({"web", "webmanager", "musicbrainz"} <= set(listed), text)
            self.assertEqual(parse_configured_plugins(new), listed)
        unchanged = "plugins: musicbrainz web webmanager\npluginpath:\n  - /config/beetsplug\nweb:\n  include_paths: yes\n"
        self.assertFalse(_plan_config_yaml_plugins(unchanged, REQUIRED_CONFIG_PLUGINS, ["/config/beetsplug"])[2])

    def tearDown(self):
        import backend.beets_plugins as bp
        bp._REFUSED_CONFIG_PLUGINS = []

    def _update(self, text):
        """Run the startup edit on ``text``; returns (new_text, refused)."""
        from backend.beets_plugins import BeetsConfigEditError, refused_config_plugins, update_config_yaml_plugins
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.yaml")
            with open(path, "wb") as f:
                f.write(text.encode("utf-8"))
            try:
                update_config_yaml_plugins(path)
            except BeetsConfigEditError:
                with open(path, "rb") as f:
                    self.assertEqual(f.read(), text.encode("utf-8"), "a refused edit must not touch the file")
                self.assertEqual(os.listdir(td), ["config.yaml"], "a refused edit must not leave a backup")
                self.assertIn("musicbrainz", refused_config_plugins())
                return None, True
            with open(path, encoding="utf-8") as f:
                return f.read(), False

    def _assert_kept(self, text, new):
        import yaml

        def names(t):
            v = (yaml.safe_load(t) or {}).get("plugins") or []
            return set(v.split() if isinstance(v, str) else v)

        self.assertEqual(names(new), names(text) | {"web", "webmanager", "musicbrainz"}, new)
        self.assertIn("/config/beetsplug", yaml.safe_load(new)["pluginpath"])
        for key in ("plugins:", "pluginpath:", "web:", "webmanager:"):
            self.assertLessEqual(new.count("\n" + key) + new.startswith(key), 1, (key, new))

    def test_security_probe_layouts_keep_every_plugin(self):
        """PR #299 security probes: (a) comment inside a block list,
        (b) zero-indent block list, (c) multi-line flow list."""
        for text in (
            "plugins:\n  - web\n  - webmanager\n  # disabled\n  - fetchart\n",
            "plugins:\n- web\n- webmanager\n",
            "plugins: [web,\n  webmanager, fetchart]\n",
            "plugins:\n  - web\n\n  - fetchart  # mine\npluginpath:\n- /config/beetsplug\ndirectory: /music\n",
            "directory: /music\nplugins:\n  - chroma\nweb:\n  port: 9000\n",
        ):
            new, refused = self._update(text)
            self.assertFalse(refused, text)
            self._assert_kept(text, new)
        new, _ = self._update("plugins:\n  - web\n  - webmanager\n  # disabled\n  - fetchart\n")
        self.assertIn("  # disabled\n  - fetchart\n  - musicbrainz", new)

    def test_unsafe_layouts_are_refused_byte_identical(self):
        from backend.beets_plugins import refused_config_plugins
        for text in (
            "plugins: web\n  bad: [\n",           # original is not YAML
            "plugins: >\n  web\n  fetchart\n",    # folded scalar the text edit cannot follow
            "plugins: {web: 1}\n",                # not a list of names
            "- just\n- a list\n",                 # not a mapping
        ):
            new, refused = self._update(text)
            self.assertTrue(refused, (text, new))
        self.assertFalse(self._update("plugins: web\n")[1])
        self.assertEqual(refused_config_plugins(), [])

    def test_duplicate_key_edits_that_hide_user_settings_are_refused(self):
        """A quoted top-level key the text edit cannot see makes the planner
        add a second `pluginpath:` / `web:`; YAML keeps the last one, so the
        pluginpath and web checks are what stop the write."""
        for text in (
            'plugins: web webmanager\n"pluginpath": /config/mine\n',
            'plugins: fetchart\npluginpath:\n  - /config/beetsplug\n"web": {host: 1.2.3.4}\n',
        ):
            new, refused = self._update(text)
            self.assertTrue(refused, (text, new))

    def test_opt_in_apply_shares_the_check(self):
        from backend.beets_plugins import BeetsConfigEditError, apply_recommended_plugins
        text = "plugins: >\n  web\n  webmanager\n"
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.yaml")
            with open(path, "wb") as f:
                f.write(text.encode("utf-8"))
            with self.assertRaises(BeetsConfigEditError):
                apply_recommended_plugins(path, ["fetchart"])
            with open(path, "rb") as f:
                self.assertEqual(f.read(), text.encode("utf-8"))
            self.assertEqual(os.listdir(td), ["config.yaml"])


if __name__ == "__main__":
    unittest.main()
