import ast
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List


APP_SOURCE = Path(__file__).resolve().parents[1] / "app.py"


def _load_evidence_namespace():
    tree = ast.parse(APP_SOURCE.read_text(encoding="utf-8"))
    names = {
        "_AI_EVIDENCE_ROOT_DIRS",
        "_AI_EVIDENCE_FMT_SEG_RE",
        "_AI_EVIDENCE_DISC_FOLDER_RE",
        "_AI_EVIDENCE_SCENE_DROP_SEG_RE",
        "_ai_evidence_clean_segment",
        "_ai_evidence_extract_year",
        "_ai_evidence_clean_artist_guess",
        "_ai_evidence_weak_artist_guess",
        "_ai_evidence_weak_album_guess",
        "_ai_evidence_scene_guess",
        "_build_folder_evidence",
    }
    ns = {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Path": Path,
        "re": __import__("re"),
        "AUDIO_EXT": {".flac", ".mp3", ".m4a"},
        "_s": lambda value: (
            value.decode("utf-8", errors="replace")
            if isinstance(value, bytes)
            else str(value or "")
        ),
        "_restore_time_colon_title": lambda value: str(value or ""),
    }
    for node in tree.body:
        node_name = ""
        if isinstance(node, ast.Assign):
            node_name = getattr(node.targets[0], "id", "")
        elif isinstance(node, ast.FunctionDef):
            node_name = node.name
        if node_name in names:
            mod = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(mod)
            exec(compile(mod, str(APP_SOURCE), "exec"), ns)
    return ns


class ImportMatchingEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _load_evidence_namespace()

    def test_disc_folder_uses_parent_album_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = (
                Path(tmp)
                / "The Beatles in Mono [2009-FLAC-CD]"
                / "2009 - Mono Masters"
                / "Disc 1"
            )
            folder.mkdir(parents=True)
            (folder / "01 - Love Me Do.flac").write_bytes(b"")

            evidence = self.ns["_build_folder_evidence"](str(folder))

        self.assertEqual(evidence["guessed_artist"], "The Beatles")
        self.assertEqual(evidence["guessed_album"], "Mono Masters")
        self.assertEqual(evidence["guessed_year"], "2009")
        self.assertEqual(evidence["folder_track_count"], 1)

    def test_scene_folder_drops_release_tags_before_mb_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Usher-Coming_Home-READNFO-EXPANDED_EDITION-16BIT-WEB-FLAC-2024-TVRf"
            folder.mkdir(parents=True)
            (folder / "01-usher-coming_home_(with_burna_boy).flac").write_bytes(b"")

            evidence = self.ns["_build_folder_evidence"](str(folder))

        self.assertEqual(evidence["guessed_artist"], "Usher")
        self.assertEqual(evidence["guessed_album"], "Coming Home Expanded Edition")
        self.assertEqual(evidence["guessed_year"], "2024")
        self.assertNotIn("READNFO", evidence["guessed_album"])
        self.assertNotIn("FLAC", evidence["guessed_album"])


if __name__ == "__main__":
    unittest.main()
