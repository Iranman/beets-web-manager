"""webmanager plugin 1.8.0: POST /webmanager/import runs Beets' own quiet
importer (beet import -q [--search-id]) and reports the folders Beets skipped.

These run the real Beets importer offline. With no metadata-source plugin
loaded Beets finds no candidates, so autotag + quiet_fallback=skip skips the
album (left in place); quiet_fallback=asis is refused (plugin 1.16.0).
"""
import os
import shutil
import struct
import tempfile
import types
import unittest
import wave

import mutagen.id3
import mutagen.wave
from beets import config as beets_config
from beets.library import Library
from beetsplug.web import app as beets_web_app
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key
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
        # Hermetic isolation (#466): this module documents "with no
        # metadata-source plugin loaded Beets finds no candidates". Beets'
        # plugin registry is process-global, and under the full suite a sibling
        # in-process test can leave a metadata source (e.g. musicbrainz) in it;
        # `beet import --search-id` would then find candidates and reach
        # musicbrainz.org, turning the documented no_candidates outcome into
        # no_strong_match (and hitting the network). Emptying beets.plugins.
        # _instances is not enough -- beets reloads from config["plugins"] when
        # the list is empty -- so pin Beets' single metadata-source lookup to
        # none for this test. Auto-restored, and the sibling tests that patch
        # this same function with their own sources simply nest inside it.
        from unittest import mock as _mock
        from beets import metadata_plugins as _metadata_plugins
        _no_sources = _mock.patch.object(_metadata_plugins, "find_metadata_source_plugins",
                                         return_value=[])
        _no_sources.start()
        self.addCleanup(_no_sources.stop)

        self.td = tempfile.mkdtemp()
        self.music = os.path.join(self.td, "music")
        self.downloads = os.path.join(self.td, "downloads")
        os.makedirs(self.music)
        os.makedirs(self.downloads)
        self._dir = beets_config["directory"].get()
        beets_config["directory"] = self.music
        self.lib = Library(os.path.join(self.td, "lib.blb"), directory=self.music)
        self.plugin = WebManagerPlugin()
        set_api_key(TOKEN)
        ops_mod.set_allowed_roots([self.music, self.downloads])
        ops_mod.set_import_roots([self.downloads])
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["TESTING"] = True
        self.client = beets_web_app.test_client()

    def tearDown(self):
        beets_config["directory"] = self._dir
        ops_mod.set_allowed_roots(None)
        ops_mod.set_import_roots(None)
        set_api_key(None)
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
        self.assertEqual(data["skipped"], [{"path": src, "reason": "no_candidates"}])
        self.assertEqual(len(self.lib.albums()), 0)
        self.assertEqual(sorted(os.listdir(src)), ["01.wav", "02.wav"])
        self.assertEqual(before, {k: beets_config["import"][k].get() for k in before})

    def test_weak_match_is_reported_as_no_strong_match(self):
        from unittest import mock
        from beets.autotag import Recommendation
        import beets.importer.tasks as tasks_mod
        src = os.path.join(self.downloads, "Some Artist - Weak")
        _album(src, "Some Artist", "Weak")

        def weak(task, search_ids):
            task.cur_artist, task.cur_album = "Some Artist", "Weak"
            task.candidates, task.rec = [object()], Recommendation.medium
        with mock.patch.object(tasks_mod.ImportTask, "lookup_candidates", weak):
            res = self._import(paths=[src], autotag=True, copy=True, move=False)
        self.assertEqual(res.get_json()["skipped"], [{"path": src, "reason": "no_strong_match"}])
        self.assertEqual(len(self.lib.albums()), 0)

    def _pinned_release(self, titles, album_id=REL):
        from beets.autotag.hooks import AlbumInfo, TrackInfo
        return AlbumInfo(
            tracks=[TrackInfo(title=t, track_id=f"00000000-0000-4000-8000-00000000000{i}", index=i,
                              medium=1, medium_index=i, medium_total=len(titles), length=1.0)
                    for i, t in enumerate(titles, 1)],
            album="Partial", album_id=album_id, artist="Some Artist",
            artist_id="11111111-1111-4111-8111-111111111111",
            releasegroup_id="22222222-2222-4222-8222-222222222222",
            mediums=1, data_source="MusicBrainz")

    def _import_pinned(self, info, **body):
        """A real Beets quiet import whose MusicBrainz lookup returns ``info``."""
        from unittest import mock
        import beets.autotag.match as match_mod
        src = os.path.join(self.downloads, "Some Artist - Partial")
        _album(src, "Some Artist", "Partial")  # files: Track 1, Track 2
        with mock.patch.object(match_mod.metadata_plugins, "albums_for_ids", lambda ids: iter([info])):
            res = self._import(paths=[src], autotag=True, search_ids=[REL], copy=True, move=False,
                               duplicate_action="keep", **body)
        self.assertEqual(res.status_code, 200, res.get_json())
        return src, res.get_json()

    def test_missing_track_source_is_always_skipped_without_allow_missing_tracks(self):
        """The live failure (38 Spesh, Beyond Belief): a folder that holds
        exactly the missing tracks of the pinned release, with matching
        titles, lengths and artist, is still skipped by Beets' quiet importer
        (match.max_rec.missing_tracks caps it at medium)."""
        src, data = self._import_pinned(self._pinned_release(["Track 1", "Track 2", "Track 3"]))
        self.assertEqual(data["skipped"], [{"path": src, "reason": "no_strong_match"}])
        self.assertEqual(len(self.lib.albums()), 0)

    def test_allow_missing_tracks_imports_the_pinned_release(self):
        src, data = self._import_pinned(self._pinned_release(["Track 1", "Track 2", "Track 3"]),
                                        allow_missing_tracks=True)
        self.assertEqual((data["skipped"], data["partial_matches"]), ([], 1))
        self.assertEqual(data["partial_artist_relaxed"], [])  # artist matched; nothing set aside
        albums = list(self.lib.albums())
        self.assertEqual([a.mb_albumid for a in albums], [REL])
        self.assertEqual(sorted(i.title for i in albums[0].items()), ["Track 1", "Track 2"])

    def test_before_choice_plugin_can_veto_a_partial_match(self):
        """QA N1 (#429): a plugin answering import_task_before_choice (badfiles'
        import_action_on_error: skip) vetoes the partial override as it does
        stock choose_match; the listener sees the task before any choice."""
        from unittest import mock
        from beets.importer import Action
        from beets.plugins import BeetsPlugin
        seen = []

        def veto(session, task):
            seen.append(task)
            return Action.SKIP
        with mock.patch.dict(BeetsPlugin.listeners, {"import_task_before_choice": [veto]}):
            src, data = self._import_pinned(self._pinned_release(["Track 1", "Track 2", "Track 3"]),
                                            allow_missing_tracks=True)
        self.assertEqual(len(seen), 1)
        self.assertEqual(data["partial_matches"], 0)
        self.assertEqual([s["path"] for s in data["skipped"]], [src])
        self.assertEqual(len(self.lib.albums()), 0)

    def test_allow_missing_tracks_keeps_the_bar_for_everything_else(self):
        """Only the missing-track penalty is set aside: a source whose files
        do not fit the pinned release's tracks, or a candidate that is not
        the pinned release, is still skipped."""
        for name, info in (
            ("titles", self._pinned_release(["Other Song", "Something Else", "Track 3"])),
            ("not pinned", self._pinned_release(["Track 1", "Track 2", "Track 3"],
                                                album_id="b6a6e718-3d8e-4c3e-9eaa-7b54af639bb9")),
        ):
            with self.subTest(name):
                src, data = self._import_pinned(info, allow_missing_tracks=True)
                self.assertEqual(data["skipped"], [{"path": src, "reason": "no_strong_match"}])
                self.assertEqual(len(self.lib.albums()), 0)
                shutil.rmtree(src)

    def test_pinned_import_ignores_the_data_source_penalty(self):
        """With a second metadata source loaded (chroma, deezer), Beets adds a
        data_source penalty to the album and every track of a download, which
        keeps even a complete, exactly matching folder over the strong
        threshold. The one pinned Release is judged without it (1.18.3),
        both for a complete folder and for a missing-track repair."""
        from unittest import mock
        from beets import metadata_plugins
        sources = [types.SimpleNamespace(data_source=n, data_source_mismatch_penalty=0.5)
                   for n in ("MusicBrainz", "Acoustid")]
        with mock.patch.object(metadata_plugins, "find_metadata_source_plugins",
                               lambda: sources):
            src, data = self._import_pinned(self._pinned_release(["Track 1", "Track 2"]))
            self.assertEqual((data["skipped"], data["partial_matches"]), ([], 0))
            shutil.rmtree(src)
            src, data = self._import_pinned(self._pinned_release(["Track 1", "Track 2", "Track 3"]),
                                            allow_missing_tracks=True)
            self.assertEqual((data["skipped"], data["partial_matches"]), ([], 1))
        self.assertEqual([a.mb_albumid for a in self.lib.albums()], [REL, REL])

    def test_pinned_import_without_data_source_relief_was_skipped(self):
        """Control for the test above: Beets' own recommendation for that
        complete folder is not strong, so stock quiet mode would skip it."""
        from unittest import mock
        from beets import metadata_plugins
        import beets.autotag.match as match_mod
        from beets.autotag import Recommendation
        from beets.autotag.source import Source
        from beets.library import Item
        sources = [types.SimpleNamespace(data_source=n, data_source_mismatch_penalty=0.5)
                   for n in ("MusicBrainz", "Acoustid")]
        src = os.path.join(self.downloads, "Some Artist - Full")
        _album(src, "Some Artist", "Partial")
        items = [Item.from_path(os.path.join(src, f)) for f in sorted(os.listdir(src))]
        info = self._pinned_release(["Track 1", "Track 2"])
        with mock.patch.object(metadata_plugins, "find_metadata_source_plugins",
                               lambda: sources),                 mock.patch.object(match_mod.metadata_plugins, "albums_for_ids", lambda ids: iter([info])):
            prop = match_mod.tag_album(Source.from_items(items), search_ids=[REL])
        self.assertNotEqual(prop.recommendation, Recommendation.strong)
        self.assertGreater(prop.candidates[0].distance["data_source"], 0)

    def _relaxation_case(self, recording_of, existing_release=REL, existing_album_id=None):
        """QA (PR #454 b): a real Beets quiet import of a missing-track repair
        whose files name one artist of a two-artist Release credit. Real
        ImportTask, Items, mapping and Library album, so the guards read the
        attributes stock Beets actually provides (2.14: ``task.source``, no
        ``cur_artist``). ``recording_of(item, track_id)`` is the recording
        AcoustID returned for each file, keyed by the Item path chroma uses."""
        from unittest import mock
        from beets.autotag.hooks import AlbumInfo, TrackInfo
        from beets.library import Item
        import beets.autotag.match as match_mod
        album = self.lib.add_album([Item(title="Track 3", artist="Some Artist", album="Partial",
                                         mb_albumid=existing_release)])
        info = AlbumInfo(
            tracks=[TrackInfo(title=t, track_id=f"00000000-0000-4000-8000-00000000000{i}", index=i,
                              medium=1, medium_index=i, medium_total=3, length=1.0)
                    for i, t in enumerate(["Track 1", "Track 2", "Track 3"], 1)],
            album="Partial", album_id=REL, artist="Some Artist & Guest Producer",
            artists=["Some Artist", "Guest Producer"],
            artist_id="11111111-1111-4111-8111-111111111111",
            releasegroup_id="22222222-2222-4222-8222-222222222222",
            mediums=1, data_source="MusicBrainz")
        src = os.path.join(self.downloads, "Some Artist - Partial")
        _album(src, "Some Artist", "Partial")
        matches = {}
        real = ops_mod._pinned_partial_match

        def with_chroma_hits(task, *a, **kw):
            # chroma fills _matches at import_task_start, keyed by item.path.
            mapping = task.candidates[0].mapping if task.candidates else {}
            for item in task.items:
                rec = recording_of(item, getattr(mapping.get(item), "track_id", None))
                if rec:
                    matches[item.path] = ([rec], [REL])
            return real(task, *a, **kw)
        with mock.patch.object(match_mod.metadata_plugins, "albums_for_ids", lambda ids: iter([info])), \
                mock.patch.dict("sys.modules", {"beetsplug.chroma": types.SimpleNamespace(_matches=matches)}), \
                mock.patch.object(ops_mod, "_pinned_partial_match", with_chroma_hits):
            res = self._import(paths=[src], autotag=True, search_ids=[REL], copy=True, move=False,
                               duplicate_action="keep", allow_missing_tracks=True,
                               existing_album_id=album.id if existing_album_id is None else existing_album_id)
        self.assertEqual(res.status_code, 200, res.get_json())
        return src, res.get_json(), album.id

    def test_artist_relaxation_end_to_end_real_beets(self):
        src, data, old_id = self._relaxation_case(lambda item, tid: tid)
        self.assertEqual(data["skipped"], [], data)
        self.assertEqual(data["partial_artist_relaxed"], [src])
        self.assertEqual(len(data["imported_album_ids"]), 1)
        self.assertNotIn(old_id, data["imported_album_ids"])

    def test_stale_acoustid_result_from_an_earlier_import_is_dropped(self):
        """N2: chroma keeps _matches by path for the life of the process and
        only overwrites an entry on a successful lookup. A Web Manager import
        drops the entries for its files when the task is created, so only a
        lookup made for this import (import_task_start) can satisfy G2."""
        from unittest import mock
        src = os.path.join(self.downloads, "Some Artist - Stale")
        _album(src, "Some Artist", "Stale")
        stale = {os.path.join(src, "01.wav").encode(): (["stale"], []), b"/elsewhere/x.wav": (["keep"], [])}
        seen = []
        with mock.patch.dict("sys.modules", {"beetsplug.chroma": types.SimpleNamespace(_matches=stale)}), \
                mock.patch.object(ops_mod, "_pinned_partial_match",
                                  lambda task, *a, **kw: seen.append(dict(stale))):
            self._import(paths=[src], autotag=True, search_ids=[REL], copy=True, move=False)
        self.assertEqual(seen, [{b"/elsewhere/x.wav": (["keep"], [])}])

    def test_explicit_max_rec_data_source_cap_is_honoured(self):
        """N1: an operator's match.max_rec.data_source cap still applies to a
        pinned import; only missing_tracks is exempt from the cap check."""
        from unittest import mock
        from beets import metadata_plugins
        sources = [types.SimpleNamespace(data_source=n, data_source_mismatch_penalty=0.5)
                   for n in ("MusicBrainz", "Acoustid")]
        beets_config["match"]["max_rec"]["data_source"] = "medium"
        try:
            with mock.patch.object(metadata_plugins, "find_metadata_source_plugins", lambda: sources):
                src, data = self._import_pinned(self._pinned_release(["Track 1", "Track 2"]))
        finally:
            beets_config["match"]["max_rec"]["data_source"] = "strong"  # confuse cannot delete; strong = no cap
        self.assertEqual(data["skipped"], [{"path": src, "reason": "no_strong_match"}])
        self.assertEqual(len(self.lib.albums()), 0)

    def test_artist_relaxation_end_to_end_refusals(self):
        for name, rec, release, aid in (
            ("acoustid other recording", lambda item, tid: "99999999-0000-4000-8000-000000000001", REL, None),
            ("acoustid missing", lambda item, tid: None, REL, None),
            ("repaired album on another release", lambda item, tid: tid,
             "b6a6e718-3d8e-4c3e-9eaa-7b54af639bb9", None),
            ("no existing_album_id", lambda item, tid: tid, REL, 0),
        ):
            with self.subTest(name):
                src, data, _ = self._relaxation_case(rec, release, aid)
                self.assertEqual(data["skipped"], [{"path": src, "reason": "no_strong_match"}], data)
                self.assertEqual(data["partial_artist_relaxed"], [])
                self.assertEqual(data["imported_album_ids"], [])
                shutil.rmtree(src)

    def test_allow_missing_tracks_validation(self):
        src = os.path.join(self.downloads, "A")
        os.makedirs(src)
        for body in ({"allow_missing_tracks": True},
                     {"allow_missing_tracks": True, "search_ids": [REL, REL]},
                     {"allow_missing_tracks": 1, "search_ids": [REL]}):
            res = self._import(paths=[src], autotag=True, **body)
            self.assertEqual((res.status_code, res.get_json()["error_code"]),
                             (400, "INVALID_ALLOW_MISSING_TRACKS"), body)
        for bad in (True, -1, "5", 1.5):
            res = self._import(paths=[src], autotag=True, search_ids=[REL], allow_missing_tracks=True,
                               existing_album_id=bad)
            self.assertEqual((res.status_code, res.get_json()["error_code"]),
                             (400, "INVALID_EXISTING_ALBUM_ID"), bad)

    def test_autotag_asis_is_refused(self):
        """An unmatched album never enters the library without a release
        group (#261 F-5 / D3): quiet_fallback=asis is refused before Beets runs."""
        src = os.path.join(self.downloads, "Some Artist - Asis")
        _album(src, "Some Artist", "Asis")
        res = self._import(paths=[src], autotag=True, quiet_fallback="asis", copy=True, move=False)
        self.assertEqual((res.status_code, res.get_json()["error_code"]), (400, "ASIS_REFUSED"))
        self.assertEqual(len(self.lib.albums()), 0)
        self.assertEqual(sorted(os.listdir(src)), ["01.wav", "02.wav"])

    def test_untagged_album_is_never_placed_under_a_blank_identity(self):
        """D3: an as-is import of a folder with no artist/album tags would land
        at "/music/ (0000)/ -  - 01 - ...". It is skipped, nothing is added
        or moved, and the reason is reported."""
        for name, body in (("Plain", {}),):
            with self.subTest(name):
                src = os.path.join(self.downloads, name)
                _album(src, "", "")
                res = self._import(paths=[src], **body)  # move=True, the default
                self.assertEqual(res.status_code, 200, res.get_json())
                self.assertEqual(res.get_json()["skipped"], [{"path": src, "reason": "blank_identity"}])
                self.assertEqual(len(self.lib.albums()), 0)
                self.assertEqual(sorted(os.listdir(src)), ["01.wav", "02.wav"])
                self.assertEqual(os.listdir(self.music), [])

    def test_modify_and_move_never_place_files_under_a_blank_identity(self):
        """D3: modify(move=True) refuses before writing; /move leaves the
        blank-identity target where it is and reports it."""
        src = os.path.join(self.downloads, "Some Artist - Kept")
        _album(src, "Some Artist", "Kept")
        res = self._import(paths=[src], copy=True, move=False)
        album = self.lib.albums()[0]
        self.assertEqual(res.get_json()["imported_album_ids"], [album.id])  # #344 c
        paths = sorted(i.path for i in album.items())
        auth = {"Authorization": f"Bearer {TOKEN}"}
        for body in ({"album_ids": [album.id], "fields": {"album": ""}},
                     {"album_ids": [album.id], "fields": {"albumartist": "[unknown]"}},
                     {"item_ids": [album.items()[0].id], "fields": {"album": " "}}):
            res = self.client.post("/webmanager/modify", headers=auth, json={**body, "move": True})
            self.assertEqual((res.status_code, res.get_json()["error_code"]), (409, "BLANK_IDENTITY"), body)
        self.assertEqual(self.lib.get_album(album.id).album, "Kept")
        self.assertEqual(sorted(i.path for i in self.lib.get_album(album.id).items()), paths)

        # Without a move the metadata edit itself is allowed; a later /move leaves it alone.
        res = self.client.post("/webmanager/modify", headers=auth,
                               json={"album_ids": [album.id], "fields": {"album": ""}, "move": False})
        self.assertEqual(res.status_code, 200, res.get_json())
        res = self.client.post("/webmanager/move", headers=auth, json={"album_ids": [album.id]})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual((res.get_json()["moved_albums"], res.get_json()["blank_identity_album_ids"]),
                         (0, [album.id]))
        self.assertEqual(sorted(i.path for i in self.lib.get_album(album.id).items()), paths)

    def test_placeholder_identity_values(self):
        for value in ("", "  ", "[unknown]", "Unknown Artist", "unknown album", "[Untitled]"):
            self.assertTrue(ops_mod.blank_identity(value), value)
        for value in ("Album", "Artist", "Unknown Mortal Orchestra", "Ke$ha", "!!!"):
            self.assertFalse(ops_mod.blank_identity(value), value)

    def test_duplicate_skip_is_reported(self):
        src = os.path.join(self.downloads, "Some Artist - Dup")
        _album(src, "Some Artist", "Dup")
        self._import(paths=[src], copy=True, move=False)
        res = self._import(paths=[src], copy=True, move=False, duplicate_action="skip")
        self.assertEqual(res.get_json()["skipped_paths"], [src])
        self.assertEqual(res.get_json()["skipped"], [{"path": src, "reason": "duplicate"}])
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
    """The opt-in config.yaml plugin edit keeps every entry, whatever the
    YAML form, and refuses forms it cannot edit safely."""

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
