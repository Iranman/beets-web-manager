"""SEC-13: ffmpeg/ffprobe inputs are passed as ``file:<path>`` so a path
string can never be read as a network URL, another protocol or an option."""
import http.server
import shutil
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest import mock

from backend import audio_preferences


def _wav(path: Path, seconds: float = 1.0) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * int(8000 * seconds))
    return path


class FfmpegArgumentTests(unittest.TestCase):
    def test_every_input_argument_uses_the_file_protocol(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=1, stdout="", stderr="")

        with mock.patch.object(audio_preferences.subprocess, "run", side_effect=fake_run):
            audio_preferences.decoded_audio_md5("/music/a.flac", ffmpeg_bin="ffmpeg")
            audio_preferences.inspect_audio_file("/music/a.flac", ffprobe_bin="ffprobe")
        from backend import playlist_service
        with mock.patch.object(playlist_service.subprocess, "run", side_effect=fake_run):
            playlist_service._audio_duration_seconds("/music/a.flac")
        self.assertGreaterEqual(len(calls), 3)
        for cmd in calls:
            inputs = [a for a in cmd if "music" in a and "a.flac" in a]
            self.assertEqual(len(inputs), 1, cmd)
            self.assertTrue(inputs[0].startswith("file:"), cmd)

    def test_helper(self):
        self.assertEqual(audio_preferences.ffmpeg_file_input("/x/http://y"), "file:/x/http://y")


@unittest.skipUnless(shutil.which("ffprobe") and shutil.which("ffmpeg"), "ffmpeg/ffprobe not installed")
class RealFfmpegTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_local_files_still_work(self):
        path = _wav(Path(self.tmp.name) / "tone (1) - x.wav")
        info = audio_preferences.inspect_audio_file(str(path))
        self.assertTrue(info["ok"], info)
        self.assertRegex(audio_preferences.decoded_audio_md5(str(path)), r"^[0-9a-f]{32}$")
        from backend import playlist_service
        self.assertGreater(playlist_service._audio_duration_seconds(str(path)), 0.5)

    def test_url_shaped_path_is_never_fetched(self):
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # pragma: no cover - must not happen
                hits.append(self.path)
                self.send_response(404)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/x.wav"
        self.assertFalse(audio_preferences.inspect_audio_file(url)["ok"])
        self.assertEqual(audio_preferences.decoded_audio_md5(url), "")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
