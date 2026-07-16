import difflib
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List


class PlaylistMatchQualityTests(unittest.TestCase):
    def _helpers(self, items):
        root = Path(__file__).resolve().parents[1]
        source = (root / "app.py").read_text(encoding="utf-8")
        start = source.index("def _norm(s):")
        end = source.index("def _playlist_item_payload")
        namespace = {
            "difflib": difflib,
            "re": re,
            "Path": Path,
            "PLAYLIST_MIN_DOWNLOAD_SECONDS": 45,
            "MUSIC_ROOT": Path("/tmp/music"),
            "_s": lambda v: "" if v is None else str(v),
            "_normalize_name": lambda s: str(s or ""),
            "_normalize_albumartist": lambda s: re.sub(
                r"\s*[\(\[]?(?:feat(?:uring)?\.?|ft\.?|with)\b.*",
                "",
                str(s or ""),
                flags=re.I,
            ).strip(),
            "lib": SimpleNamespace(items=lambda _query: items),
        }
        exec(source[start:end], namespace)
        return namespace

    def test_wrong_chris_brown_titles_stay_missing(self):
        items = [
            SimpleNamespace(title="Take You Down", artist="Chris Brown", albumartist="Chris Brown"),
            SimpleNamespace(title="Beg For It", artist="Chris Brown", albumartist="Chris Brown"),
            SimpleNamespace(title="Wet The Bed", artist="Chris Brown", albumartist="Chris Brown"),
        ]
        helpers = self._helpers(items)

        self.assertIsNone(helpers["_match_track"]("Chris Brown", "Make Up Your Mind"))
        self.assertIsNone(helpers["_match_track"]("Chris Brown", "Sweet Lullaby"))

    def test_exact_short_title_still_requires_artist_match(self):
        wrong_only = [
            SimpleNamespace(title="Bed", artist="jholiday", albumartist="jholiday"),
        ]
        helpers = self._helpers(wrong_only)
        self.assertIsNone(helpers["_match_track"]("Jacquees", "B.E.D."))

    def test_exact_title_and_artist_match_is_kept(self):
        items = [
            SimpleNamespace(title="Bed", artist="jholiday", albumartist="jholiday"),
            SimpleNamespace(title="B.E.D.", artist="Jacquees", albumartist="Jacquees"),
        ]
        helpers = self._helpers(items)

        item, score = helpers["_match_track"]("Jacquees", "B.E.D.")
        self.assertEqual("Jacquees", item.artist)
        self.assertGreaterEqual(score, 0.95)

    def test_common_version_suffix_does_not_block_match(self):
        items = [
            SimpleNamespace(title="Daddy", artist="Beyonce", albumartist="Beyonce"),
        ]
        helpers = self._helpers(items)

        item, score = helpers["_match_track"]("Beyonce", "Daddy (Album Version)")
        self.assertEqual("Daddy", item.title)
        self.assertGreaterEqual(score, 0.90)

    def _download_helpers(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "app.py").read_text(encoding="utf-8")
        namespace = {
            "Any": Any,
            "Dict": Dict,
            "List": List,
            "difflib": difflib,
            "re": re,
            "Path": Path,
            "_s": lambda v: "" if v is None else str(v),
            "_normalize_name": lambda s: str(s or ""),
            "_normalize_albumartist": lambda s: re.sub(
                r"\s*[\(\[]?(?:feat(?:uring)?\.?|ft\.?|with)\b.*",
                "",
                str(s or ""),
                flags=re.I,
            ).strip(),
            "_audio_identity_decision": lambda _path, **kwargs: {
                "identity_status": "text_match_only",
                "final_action": "accept" if (kwargs.get("text_match") or {}).get("ok") else "review",
            },
        }
        exec(
            source[
                source.index("_TRACK_FILENAME_SOURCE_ID_SUFFIX_RE"):
                source.index("def _track_filename_has_source_id_suffix")
            ],
            namespace,
        )
        exec(
            source[
                source.index("def _slskd_title_guess_from_name"):
                source.index("def _wanted_track_key")
            ],
            namespace,
        )
        exec(
            source[
                source.index("def _norm(s):"):
                source.index("def _playlist_item_text_variants")
            ],
            namespace,
        )
        exec(
            source[
                source.index("def _playlist_download_text_candidates"):
                source.index("def _playlist_stamp_download_tags")
            ],
            namespace,
        )
        return namespace

    def assertDownloadMatch(self, filename, artist, title, min_artist=0.82):
        helpers = self._download_helpers()
        match = helpers["_playlist_download_match"](filename, artist, title)
        self.assertTrue(match.get("ok"), match)
        self.assertGreaterEqual(match.get("title_score", 0), 0.96, match)
        self.assertGreaterEqual(match.get("artist_score", 0), min_artist, match)

    def assertDownloadNoMatch(self, filename, artist, title):
        helpers = self._download_helpers()
        match = helpers["_playlist_download_match"](filename, artist, title)
        self.assertFalse(match.get("ok"), match)

    def test_download_match_accepts_soundcloud_artist_title_channel_shapes(self):
        self.assertDownloadMatch(
            "/tmp/K8do x Meez - Go Off - Rareexclusives.mp3",
            "k8do",
            "go off",
        )
        self.assertDownloadMatch(
            "/tmp/001 go off - K8do x Meez - Go Off.mp3",
            "k8do",
            "go off",
        )

    def test_download_match_accepts_title_artist_compact_soundcloud_shapes(self):
        self.assertDownloadMatch(
            "/tmp/20 LOVES TRAIN-CONFUNKSHUN - Cerissa Battershell.mp3",
            "confunkshun band",
            "loves train",
        )
        self.assertDownloadMatch(
            "/tmp/001 belt - Chopz x Babyfxce E - Belt.mp3",
            "chopz",
            "belt",
        )

    def test_download_match_accepts_feature_and_unreleased_noise(self):
        self.assertDownloadMatch(
            "/tmp/001 space - SPACE ft. TIA LOWE.mp3",
            "jay malakhi",
            "space",
            min_artist=0,
        )
        self.assertDownloadMatch(
            "/tmp/001 killer where i go unreleased - Future - Killer where i go.mp3",
            "future",
            "killer where i go unreleased",
        )

    def test_download_match_ignores_injected_ytdlp_request_prefix(self):
        self.assertDownloadNoMatch(
            "/tmp/001 make up your mind - Chris Brown - Take You Down.mp3",
            "Chris Brown",
            "Make Up Your Mind",
        )
        self.assertDownloadNoMatch(
            "/tmp/001 jr meets rj - RJ PAYNE X BENNY THE BUTCHER - BUTCHER MEETS LEATHERFACE produced by Tricky Trippz.mp3",
            "rj payne",
            "jr meets rj",
        )


if __name__ == "__main__":
    unittest.main()

