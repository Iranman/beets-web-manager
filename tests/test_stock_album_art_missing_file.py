"""Stock /album/<id>/art answers 404, not a 500 traceback, when the
artpath file is gone; artpath itself is left unchanged."""
import os
import shutil
import tempfile
import unittest

from beets.library import Library, Album
from beetsplug.web import app as beets_web_app
from beetsplug.webmanager import WebManagerPlugin


class MissingAlbumArtFileTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        music = os.path.join(self.td, "music")
        os.makedirs(music)
        self.lib = Library(os.path.join(self.td, "library.blb"), directory=music)
        WebManagerPlugin()
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["TESTING"] = True
        self.album = Album(album="Gone", albumartist="Nobody")
        self.lib.add(self.album)
        self.art = os.path.join(music, "albumart.jpg")
        self.album.artpath = os.fsencode(self.art)
        self.album.store()
        self.client = beets_web_app.test_client()

    def tearDown(self):
        try:
            self.lib._connection().close()
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def test_missing_file_is_404_and_artpath_kept(self):
        beets_web_app.config["PROPAGATE_EXCEPTIONS"] = True  # an unguarded send_file would raise here
        try:
            with self.assertNoLogs("beets.webmanager.compat", level="WARNING"):
                res = self.client.get(f"/album/{self.album.id}/art")
        finally:
            beets_web_app.config["PROPAGATE_EXCEPTIONS"] = None
        self.assertEqual(res.status_code, 404)
        self.assertEqual(os.fsdecode(self.lib.get_album(self.album.id).artpath), self.art)

    def test_existing_file_is_served(self):
        with open(self.art, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0JPEG")
        res = self.client.get(f"/album/{self.album.id}/art")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, b"\xff\xd8\xff\xe0JPEG")
        res.close()


if __name__ == "__main__":
    unittest.main()
