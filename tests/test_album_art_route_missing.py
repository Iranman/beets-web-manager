"""GET /api/albums/<id>/art with no art anywhere is a 404, not a 200
transparent image: the Library grid's <img> error handler only shows its
placeholder sleeve (or tries the folder image) when the load fails."""
import unittest
from unittest import mock

import app as app_module  # noqa: F401  -- loads the module family in order
import routes_library
from backend.beets_adapter import BeetsAdapterNotFoundError
from tests._app_family import patch_app_family


class AlbumArtRouteMissingTests(unittest.TestCase):
    def test_album_without_any_art_is_404(self):
        album = mock.Mock(artpath="", mb_releasegroupid="", mb_albumid="")
        lib = mock.Mock()
        lib.get_album.return_value = album
        lib.items.return_value = []
        adapter = mock.Mock()
        adapter.open_album_art.side_effect = BeetsAdapterNotFoundError("Art not found", status_code=404)
        with patch_app_family("app", "_security_auth_disabled", return_value=True), \
                mock.patch.object(routes_library, "lib", lib), \
                mock.patch.object(routes_library, "beets_adapter", adapter):
            res = app_module.app.test_client().get("/api/albums/3/art")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data, b"")


if __name__ == "__main__":
    unittest.main()
