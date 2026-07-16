import json
import unittest
from unittest.mock import patch

from helpers_mb import _resolve_release_group_to_release


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class MusicBrainzReleaseGroupResolutionTests(unittest.TestCase):
    def test_track_count_beats_country_when_resolving_release_group(self):
        payload = {
            "releases": [
                {
                    "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "title": "Part III",
                    "status": "Official",
                    "country": "US",
                    "date": "2001-03-20",
                    "media": [{"track-count": 18}],
                },
                {
                    "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    "title": "Part III",
                    "status": "Official",
                    "country": "GB",
                    "date": "2001-03-20",
                    "media": [{"track-count": 16}],
                },
            ],
        }

        with patch("helpers_mb._ur.urlopen", return_value=_FakeResponse(payload)):
            resolved = _resolve_release_group_to_release(
                "11111111-1111-1111-1111-111111111111",
                [],
                year="2001",
                track_count=16,
            )

        self.assertEqual(resolved, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


if __name__ == "__main__":
    unittest.main()
