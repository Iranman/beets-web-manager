"""Playlist downloads: SpotiFLAC is opt-in, as for Find Music (#379).

It runs only when PLAYLIST_DOWNLOAD_METHODS lists it or a download asks for it
explicitly; the default "Auto" chain is slskd, YouTube, SoundCloud.
"""
import ast
import unittest
from pathlib import Path
from unittest import mock

import backend.playlist_service as ps

ROOT = Path(__file__).resolve().parents[1]


def _env_default(path: Path, name: str) -> str:
    """The literal default of `NAME = os.environ.get("NAME", "<default>")`."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
            return ast.literal_eval(node.value.args[1])
    raise AssertionError(f"{name} not assigned in {path}")


class PlaylistSpotiflacOptIn(unittest.TestCase):
    def test_shipped_default_has_no_spotiflac(self):
        default = _env_default(ROOT / "backend" / "app_runtime.py", "PLAYLIST_DOWNLOAD_METHODS")
        with mock.patch.object(ps, "PLAYLIST_DOWNLOAD_METHODS", default):
            self.assertEqual(ps._playlist_download_methods(""), ["slskd", "ytdlp", "soundcloud"])

    def test_empty_or_unknown_config_falls_back_without_spotiflac(self):
        for configured in ("", "bogus"):
            with self.subTest(configured=configured), \
                    mock.patch.object(ps, "PLAYLIST_DOWNLOAD_METHODS", configured):
                self.assertEqual(ps._playlist_download_methods(""), ["slskd", "ytdlp", "soundcloud"])

    def test_operator_configured_spotiflac_is_kept_in_order(self):
        with mock.patch.object(ps, "PLAYLIST_DOWNLOAD_METHODS", "slskd,spotiflac,ytdlp,soundcloud"):
            self.assertEqual(ps._playlist_download_methods(""),
                             ["slskd", "spotiflac", "ytdlp", "soundcloud"])

    def test_explicit_per_download_choice_is_honoured(self):
        with mock.patch.object(ps, "PLAYLIST_DOWNLOAD_METHODS", "slskd,ytdlp,soundcloud"):
            self.assertEqual(ps._playlist_download_methods(["spotiflac"]), ["spotiflac"])
            self.assertEqual(ps._playlist_download_methods("ytdlp"), ["ytdlp"])


if __name__ == "__main__":
    unittest.main()
