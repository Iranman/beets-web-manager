"""QA 0.4.0: fpcalc 1.5.1 (the Debian bookworm libchromaprint-tools the
runtime image installs) exits 3 with "Error decoding audio frame (End of
file)" on every file while still printing a valid JSON fingerprint.
Reproduced in the qa040 image for wav/flac/mp3/ogg/m4a; the stock Beets
container's fpcalc 1.6.0 exits 0 on the same input.

acoustid_lookup_outcome treats any non-zero exit as "audio could not be
fingerprinted" -> NO_RESULT, which _acoustid_lookup_cached_outcome caches
permanently. So in the shipped image Web Manager never asks AcoustID and
records every file as "checked, no match" -- the opposite of the D11/#252
rule that a lookup without an answer is "not checked".

expectedFailure until the owner fixes it; remove the decorator then.
"""
import json
import subprocess
import sys
import unittest
import urllib.error
from unittest import mock

import helpers_mb
from backend.provider_boundary import ProviderOutcome

FPCALC_STDOUT = json.dumps({"duration": 40.0, "fingerprint": "AQADtEmUaEkSRZEK" * 8})


class FpcalcExitCodeTests(unittest.TestCase):
    @unittest.expectedFailure
    def test_fingerprint_printed_with_exit_3_is_looked_up(self):
        fpcalc = subprocess.CompletedProcess(["fpcalc"], 3, stdout=FPCALC_STDOUT,
                                             stderr="ERROR: Error decoding audio frame (End of file)\n")
        with mock.patch.object(helpers_mb, "acoustid_api_key", return_value="qa-key"), \
             mock.patch.object(helpers_mb.shutil, "which", return_value=sys.executable), \
             mock.patch.object(helpers_mb.subprocess, "run", return_value=fpcalc), \
             mock.patch.object(helpers_mb, "_ACOUSTID_MIN_INTERVAL_SECONDS", 0), \
             mock.patch("backend.provider_boundary.time.sleep"), \
             mock.patch.object(helpers_mb.provider_boundary, "opened",
                               side_effect=urllib.error.URLError("connection refused")):
            result = helpers_mb.acoustid_lookup_outcome("/music/x.flac")
        # The lookup reached the (failing) network, so the file was NOT checked.
        self.assertNotEqual(result.outcome, ProviderOutcome.NO_RESULT, result.message)
        self.assertFalse(result.answered)


if __name__ == "__main__":
    unittest.main()
