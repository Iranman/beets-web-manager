"""#269 security review: the S-1 containment check runs on the literal
folder name, but yt-dlp evaluates ``outtmpl`` as a template (``%(...)s``
fields, ``$VAR`` expansion) *after* that check. An artist/album such as
``%(id&..)s`` passes the dot-only test and the realpath check, then yt-dlp
turns it into ``..`` and writes outside DOWNLOADS_ROOT.

These tests capture the ``outtmpl`` the real download functions hand to
yt-dlp and expand it with the real ``YoutubeDL.prepare_filename``."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yt_dlp

import backend.ytdlp_service as yts

_SECRET_ENV = "BWM_SEC269_PROBE"
_SECRET_VALUE = "probe-value-that-must-not-appear"
_INFO = {"id": "abc", "title": "t", "ext": "mp3", "extractor": "youtube", "webpage_url": "u"}


class _Stop(Exception):
    pass


def _captured_outtmpls(call):
    """Run ``call`` with yt-dlp stubbed at the network edge; return every
    outtmpl it would have handed to ``yt_dlp.YoutubeDL``."""
    seen = []

    class _RecordingYDL:
        def __init__(self, opts):
            seen.append(opts["outtmpl"])

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def download(self, _urls):
            return 0

    with mock.patch.object(yt_dlp, "YoutubeDL", _RecordingYDL), \
            mock.patch.object(yts._ytdlp_ready, "wait", return_value=True), \
            mock.patch.object(yts, "_ytdlp_cookie_auths_for_source", return_value=[None]), \
            mock.patch.object(yts, "_ytdlp_source_requires_js", return_value=False), \
            mock.patch.object(yts, "_ytdlp_js_runtime_options", return_value={}), \
            mock.patch.object(yts, "_ytdlp_remote_components", return_value=[]), \
            mock.patch.object(yts, "_ytdlp_apply_source_network_options"), \
            mock.patch.object(yts, "_ytdlp_source_extractor_args", return_value=None), \
            mock.patch.object(yts, "_ytdlp_client_profiles_for_source", return_value=[("web", None)]), \
            mock.patch.dict(os.environ, {_SECRET_ENV: _SECRET_VALUE}):
        try:
            call()
        except RuntimeError:  # "no audio files downloaded": expected with the stub
            pass
    return seen


def _expand(outtmpl, **info):
    with mock.patch.dict(os.environ, {_SECRET_ENV: _SECRET_VALUE}), \
            yt_dlp.YoutubeDL({"outtmpl": outtmpl, "quiet": True}) as ydl:
        return Path(os.path.normpath(ydl.prepare_filename(dict(_INFO, **info))))


class YtdlpTemplateCannotEscapeRoot(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name)) / "dl"
        self.root.mkdir()

    def _dest(self, artist, album):
        import backend.acquisition_service as acq
        import backend.app_runtime as rt
        seen = {}

        def fake_download(_a, _b, _y, dest_dir, log, **_kw):
            seen["dest"] = dest_dir
            raise _Stop()

        with mock.patch.object(rt, "DOWNLOADS_ALLOWED_ROOTS", (self.root,)), \
                mock.patch.object(acq, "jobs") as jobs, \
                mock.patch.object(acq, "_ytdlp_album_download", side_effect=fake_download):
            body, _ = acq.start_album_download(
                {"artist": artist, "album": album, "method": "ytdlp", "auto_import": False})
            if jobs.start_python.called:
                try:
                    jobs.start_python.call_args[0][0]([], None)
                except Exception:
                    pass
        return body, seen.get("dest")

    def _album_final(self, dest):
        tmpls = _captured_outtmpls(lambda: yts._ytdlp_album_download("A", "B", "", dest, []))
        self.assertTrue(tmpls, "album download never reached yt-dlp")
        return _expand(tmpls[0])

    def test_yt_dlp_expanded_outtmpl_stays_under_root(self):
        for artist, album in (("%(id&..)s", "music"), ("%(id&..)s", "%(id&..)s"), ("A", "%(id&..)s")):
            with self.subTest(artist=artist, album=album):
                body, dest = self._dest(artist, album)
                if dest is None:  # refused up front: fine
                    self.assertFalse(body.get("ok", True))
                    continue
                final = self._album_final(dest)
                self.assertIn(self.root, final.parents, f"{dest!r} -> {final}")
                self.assertEqual(final.parent, Path(dest))

    def test_env_var_in_artist_or_album_never_expands(self):
        for artist, album in ((f"${_SECRET_ENV}", "x"), ("x", f"${{{_SECRET_ENV}}}"),
                              (f"$${_SECRET_ENV}", "x")):
            with self.subTest(artist=artist, album=album):
                body, dest = self._dest(artist, album)
                self.assertIsNotNone(dest, body)
                self.assertNotIn("$", dest)
                final = self._album_final(dest)
                self.assertNotIn(_SECRET_VALUE, str(final))
                self.assertEqual(final.parent, Path(dest))

    def test_percent_folder_name_round_trips(self):
        body, dest = self._dest("Artist", "100% Pure")
        self.assertEqual(Path(dest).name, "100% Pure", body)
        final = self._album_final(dest)
        self.assertEqual(final, Path(dest) / "t.mp3")

    def test_outtmpl_builder_refuses_dollar_and_relative_folder(self):
        for bad in (str(self.root / f"${_SECRET_ENV}"), "relative/dir", "~"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                yts._ytdlp_outtmpl(bad, "%(title)s.%(ext)s")
        with self.assertRaises(ValueError):
            yts._ytdlp_outtmpl(str(self.root), "%(title)s", f"${_SECRET_ENV} - ")

    def test_remote_playlist_track_title_is_inert(self):
        # Playlist downloads pass a remote track title as the per-track prefix.
        dest = str(self.root / "playlist" / "downloads")
        for title in ("%(id&..)s", f"${_SECRET_ENV}", f"${{{_SECRET_ENV}}}", "50%( x", "../../up"):
            with self.subTest(title=title):
                tmpls = _captured_outtmpls(lambda: yts._ytdlp_missing_tracks_download(
                    "Artist", "", "", dest, [], [{"title": title}], source="ytdlp"))
                self.assertTrue(tmpls, "track download never reached yt-dlp")
                final = _expand(tmpls[0])
                self.assertEqual(final.parent, Path(dest), f"{title!r} -> {final}")
                self.assertNotIn(_SECRET_VALUE, str(final))
                self.assertTrue(final.name.startswith("001 "), final.name)


if __name__ == "__main__":
    unittest.main()
