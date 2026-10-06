"""An AcoustID service rejection (e.g. invalid API key) is recorded and
logged, never silently indistinguishable from "no match". Found live: the
production key was rejected with code 4 and every lookup looked like an
empty result."""

import io
import json
import unittest
import urllib.error
from unittest import mock

import helpers_mb


def _fpcalc_ok():
    return mock.Mock(returncode=0, stdout=json.dumps({"duration": 200, "fingerprint": "AQAA"}))


class AcoustidServiceErrorTests(unittest.TestCase):
    def setUp(self):
        helpers_mb._ACOUSTID_SERVICE_ERRORS_LOGGED.clear()
        helpers_mb.ACOUSTID_LAST_SERVICE_ERROR.clear()
        self.patches = [
            mock.patch("shutil.which", return_value="fpcalc"),
            mock.patch("pathlib.Path.exists", return_value=True),
            mock.patch("subprocess.run", return_value=_fpcalc_ok()),
            # IA-12: there is no built-in fallback key any more.
            mock.patch.dict("os.environ", {"ACOUSTID_API_KEY": "test-app-key"}),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_invalid_api_key_is_recorded_and_logged_once(self):
        def rejected(*_a, **_k):
            body = io.BytesIO(json.dumps({"status": "error", "error": {"code": 4, "message": "invalid API key"}}).encode())
            raise urllib.error.HTTPError("https://api.acoustid.org/v2/lookup", 400, "Bad Request", {}, body)

        with mock.patch.object(helpers_mb._ur, "urlopen", side_effect=rejected), \
                self.assertLogs("helpers_mb", level="WARNING") as logs:
            self.assertEqual(helpers_mb._acoustid_lookup("/music/a.flac"), [])
            self.assertEqual(helpers_mb._acoustid_lookup("/music/b.flac"), [])
        self.assertEqual(len(logs.records), 1)
        self.assertIn("ACOUSTID_API_KEY", logs.output[0])
        self.assertEqual(helpers_mb.ACOUSTID_LAST_SERVICE_ERROR["code"], 4)
        self.assertEqual(helpers_mb.ACOUSTID_LAST_SERVICE_ERROR["http_status"], 400)


if __name__ == "__main__":
    unittest.main()
