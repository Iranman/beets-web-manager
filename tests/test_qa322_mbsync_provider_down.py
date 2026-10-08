"""QA #322: MusicBrainz unreachable during MBSync All.

The plugin's own tests patch ``metadata_plugins.album_for_id`` to raise, which
skips Beets' ``maybe_handle_plugin_error``. In real Beets (``raise_on_error:
no``, the default) a provider error is logged and ``album_for_id`` returns
None, so mbsync treats the album exactly like a Release MusicBrainz no longer
has. This test goes through the real ``metadata_plugins.album_for_id`` with a
source plugin whose lookup fails, as it does when MusicBrainz is down or rate
limits.

Documented behaviour (ARCHITECTURE.md, plugin_ops docstring): "Ten albums in a
row that raise (MusicBrainz unreachable, for example) stop the sync with
MBSYNC_ABORTED". A provider outage must not be reported as a successful sync
with every album "not found at MusicBrainz".
"""

import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import beets.metadata_plugins as metadata_plugins
import beets.plugins as beets_plugins_mod
from beets import config as beets_config
from beets.library import Item, Library
from beetsplug.mbsync import MBSyncPlugin
import beetsplug.webmanager.plugin_ops as plugin_ops


class _DownSource:
    data_source = "MusicBrainz"

    def album_for_id(self, _id):
        raise ConnectionError("Max retries exceeded (Connection refused)")

    def track_for_id(self, _id):
        raise ConnectionError("Max retries exceeded (Connection refused)")


class MusicBrainzDownTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.lib = Library(os.path.join(self.td, "lib.blb"), directory=os.path.join(self.td, "music"))
        self._saved = list(beets_plugins_mod._instances)
        beets_plugins_mod._instances[:] = [MBSyncPlugin()]
        self._raise = beets_config["raise_on_error"].get()
        beets_config["raise_on_error"] = False  # Beets' default
        # get_metadata_source() is cached by name; drop any source an
        # earlier test resolved so the patched plugin list is used.
        metadata_plugins.get_metadata_source.cache_clear()
        self.addCleanup(metadata_plugins.get_metadata_source.cache_clear)
        for n in range(plugin_ops.MAX_CONSECUTIVE_FAILURES + 2):
            items = [Item(path=os.path.join(self.td, f"{n}-{t}.mp3").encode(), title=f"t{t}", album="Old",
                          track=t, mb_trackid=f"rec-{n}-{t}") for t in (1, 2)]
            album = self.lib.add_album(items)
            album.mb_albumid = f"rel-{n}"
            album.store()

    def tearDown(self):
        beets_config["raise_on_error"] = self._raise
        beets_plugins_mod._instances[:] = self._saved
        try:
            self.lib._connection().close()
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def test_provider_outage_is_not_reported_as_not_found(self):
        with mock.patch.object(metadata_plugins, "find_metadata_source_plugins", return_value=[_DownSource()]):
            res = plugin_ops.run_mbsync_library(self.lib, False, threading.Event(), threading.RLock())
        # Nothing changed either way.
        self.assertEqual(res["changed_albums"], 0)
        # An outage must surface as failures (and abort), not as N albums
        # "not found at MusicBrainz" in a run that ends succeeded.
        self.assertEqual(res["not_found"], 0, f"outage counted as not_found: {res['not_found']}")
        self.assertTrue(res["aborted"])


if __name__ == "__main__":
    unittest.main()
