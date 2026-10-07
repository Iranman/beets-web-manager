"""SEC-002 / ARCH-003 Wave 20: Existing-Album Duplicate & Move Bookkeeping Controlled Mutation Boundary.

Comprehensive focused test suite for existing_album_reconcile_v1 mutation family.
"""
import ast
import math
import os
import sqlite3
import struct
import tempfile
import unittest
import wave
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from backend.transaction_engine import TransactionStore
import app as app_module
try:  # ARCH-001: patch app.py and the modules extracted from it
    from _app_family import patch_app_family  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_family import patch_app_family  # noqa: E402
try:  # ARCH-001: app.py module family
    from _app_ast_cache import app_family_source  # noqa: E402
except ImportError:  # pragma: no cover
    from tests._app_ast_cache import app_family_source  # noqa: E402

ITEMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    album TEXT,
    albumartist TEXT,
    mb_albumid TEXT,
    mb_releasegroupid TEXT,
    year INTEGER
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    album_id INTEGER,
    title TEXT,
    artist TEXT,
    album TEXT,
    albumartist TEXT,
    disc INTEGER,
    track INTEGER,
    path BLOB,
    mb_trackid TEXT,
    mb_albumid TEXT,
    mb_releasegroupid TEXT,
    length REAL,
    size INTEGER,
    mtime REAL
);
"""

RG_A = "aaaaaaaa-0000-0000-0000-000000000000"
RG_B = "bbbbbbbb-0000-0000-0000-000000000000"
REL_A = "11111111-1111-1111-1111-111111111111"
REC_1 = "33333333-3333-3333-3333-333333333331"
REC_2 = "33333333-3333-3333-3333-333333333332"
REC_3 = "33333333-3333-3333-3333-333333333333"


def _write_test_audio(path: Path, *, freq: float = 220.0, duration: float = 0.2) -> None:
    """Write a real, minimal, playable WAV file. SEC-002 Wave 20 final
    review: the original fixture wrote fake b"AUDIO_DATA_N" bytes, which the
    new survivor-readability check (_read_file_audio_tags) correctly
    refuses to treat as usable media -- exactly the "survivor must be
    readable / valid media" requirement this review closes. Real tests need
    real, independently-readable audio (self-synthesized sine wave, same
    technique as scripts/seed_demo_library.py's demo library)."""
    sample_rate = 8000
    n_samples = max(1, int(sample_rate * duration))
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            frames += struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * i / sample_rate)))
        w.writeframes(bytes(frames))


class Wave20FixtureBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)

        self.music_root = self.tmp_path / "music"
        self.music_root.mkdir()
        self.staging_root = self.tmp_path / "staging"
        self.staging_root.mkdir()
        self.quarantine_root = self.tmp_path / "quarantine"
        self.quarantine_root.mkdir()
        self.outside_root = self.tmp_path / "outside"
        self.outside_root.mkdir()

        store_dir = self.tmp_path / "transactions"
        store_dir.mkdir()
        self.store = TransactionStore(root=str(store_dir))

        self.db_path = self.tmp_path / "musiclibrary.db"
        with sqlite3.connect(self.db_path) as con:
            con.executescript(ITEMS_SCHEMA)
            con.commit()

        app_module.app.config["TESTING"] = True

        @contextmanager
        def _mock_db_cm(*args, **kwargs):
            con = sqlite3.connect(self.db_path)
            if "text_factory" in kwargs:
                con.text_factory = kwargs["text_factory"]
            if "row_factory" in kwargs:
                con.row_factory = kwargs["row_factory"]
            try:
                yield con
            finally:
                con.close()

        self._db_patch = patch_app_family(app_module, "_db", side_effect=_mock_db_cm)
        self._db_patch.start()

        def fake_get_album(aid):
            with sqlite3.connect(self.db_path) as con:
                con.row_factory = sqlite3.Row
                row = con.execute("SELECT * FROM albums WHERE id=?", (int(aid),)).fetchone()
                return dict(row) if row else None

        def fake_find_items_by_album(aid):
            with sqlite3.connect(self.db_path) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute("SELECT * FROM items WHERE album_id=?", (int(aid),)).fetchall()
                return [dict(r) for r in rows]

        self._get_album_patch = mock.patch.object(app_module.composite_workflows, "get_album", side_effect=fake_get_album)
        self._get_album_patch.start()
        self._find_items_patch = mock.patch.object(app_module.composite_workflows, "find_all_items_by_album_id", side_effect=fake_find_items_by_album)
        self._find_items_patch.start()

        self._env_patch = mock.patch.dict(os.environ, {
            "BEETS_WEB_AUTH_DISABLED": "1",
            "RECONCILE_QUARANTINE_DIR": str(self.quarantine_root),
        })
        self._env_patch.start()

    def tearDown(self):
        self._find_items_patch.stop()
        self._get_album_patch.stop()
        self._env_patch.stop()
        self._db_patch.stop()
        try:
            self._tmpdir.cleanup()
        except Exception:
            pass

    def _create_album(self, album_id: int, title: str, artist: str = "Test Artist", rg_id: str = RG_A, rel_id: str = REL_A):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO albums (id, album, albumartist, mb_albumid, mb_releasegroupid, year) VALUES (?, ?, ?, ?, ?, ?)",
                (album_id, title, artist, rel_id, rg_id, 2024),
            )
            con.commit()

    def _create_item(
        self,
        item_id: int,
        album_id: int,
        title: str,
        disc: int,
        track: int,
        filename: str,
        staging: bool = False,
        outside: bool = False,
        rec_id: str = REC_1,
        rg_id: str = RG_A,
        write_audio: bool = True,
    ) -> Path:
        root = self.outside_root if outside else (self.staging_root if staging else self.music_root)
        album_dir = root / f"album_{album_id}"
        album_dir.mkdir(parents=True, exist_ok=True)
        file_path = album_dir / filename
        if write_audio:
            _write_test_audio(file_path, freq=220.0 + item_id)
        else:
            file_path.write_bytes(b"NOT_REAL_AUDIO_" + str(item_id).encode())

        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO items (id, album_id, title, artist, album, albumartist, disc, track, path, mb_trackid, mb_albumid, mb_releasegroupid, length) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    album_id,
                    title,
                    "Test Artist",
                    "Test Album",
                    "Test Artist",
                    disc,
                    track,
                    str(file_path),
                    rec_id,
                    REL_A,
                    rg_id,
                    180.0,
                ),
            )
            con.commit()
        return file_path












class Wave18InteractionTests(Wave20FixtureBase):
    def test_merge_job_never_applies_a_replacement_and_moves_the_rest(self):
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")

        self._create_item(10, 1, "Track 1 Old", 1, 1, "track1_existing.wav")
        self._create_item(20, 2, "Track 1 New", 1, 1, "track1_imported.wav", staging=True)
        self._create_item(21, 2, "Track 2 New", 1, 2, "track2_imported.wav", staging=True)

        import backend.item_replacement as item_replacement
        plan_repl_mock = mock.MagicMock(return_value={"ok": True, "operation_id": "op_repl"})
        apply_repl_mock = mock.MagicMock(return_value={"ok": True})
        plan_rec_mock = mock.MagicMock(return_value={"ok": True, "operation_id": "op_rec_20"})
        apply_rec_mock = mock.MagicMock(return_value={"ok": True})

        with mock.patch.object(item_replacement, "plan_verified_replacement", plan_repl_mock), \
             mock.patch.object(app_module.composite_workflows, "apply_track_replacement", apply_repl_mock), \
             mock.patch.object(app_module.composite_workflows, "plan_existing_album_reconcile", plan_rec_mock), \
             mock.patch.object(app_module.composite_workflows, "apply_existing_album_reconcile", apply_rec_mock):

            res = app_module._merge_imported_album_into_existing(
                2, 1, str(self.staging_root), [], mb_albumid=""
            )

        # A replacement is only ever planned (Preview) during an import;
        # applying it needs an operator's approval.
        apply_repl_mock.assert_not_called()
        plan_rec_mock.assert_called_once()
        apply_rec_mock.assert_called_once_with("op_rec_20")
        self.assertEqual(res, 1)

    def test_merge_job_reports_failure_truthfully(self):
        """SEC-002 Wave 20 final review, findings #45/#46: a failed engine
        Apply must not be reported as a successful merge upstream."""
        self._create_album(1, "Existing Album")
        self._create_album(2, "Imported Temp Album")
        self._create_item(10, 1, "Track 1 Old", 1, 1, "track1_existing.wav")
        self._create_item(20, 2, "Track 1 New", 1, 1, "track1_imported.wav", staging=True)

        plan_rec_mock = mock.MagicMock(return_value={"ok": True, "operation_id": "op_rec_20"})
        apply_rec_mock = mock.MagicMock(return_value={"ok": False, "error": "simulated engine failure"})

        with mock.patch.object(app_module.composite_workflows, "plan_existing_album_reconcile", plan_rec_mock), \
             mock.patch.object(app_module.composite_workflows, "apply_existing_album_reconcile", apply_rec_mock):
            res = app_module._merge_imported_album_into_existing(
                2, 1, str(self.staging_root), [], mb_albumid=""
            )

        # Apply failed -- the function must NOT claim the merge succeeded.
        self.assertEqual(res, 2)




class WebManagerMutationProhibitionTests(unittest.TestCase):
    def test_merge_imported_album_into_existing_contains_no_direct_mutations(self):
        source = app_family_source()  # ARCH-001: app.py module family
        tree = ast.parse(source)

        merge_fn_def = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_merge_imported_album_into_existing":
                merge_fn_def = node
                break

        self.assertIsNotNone(merge_fn_def, "_merge_imported_album_into_existing not found in app.py")
        fn_source = ast.get_source_segment(source, merge_fn_def)

        prohibited_strings = [
            "Path.unlink", "os.unlink", "os.remove", "os.rename", "os.replace",
            "shutil.move", "shutil.rmtree", "DELETE FROM items", "UPDATE items SET album_id",
            "DELETE FROM albums", "_beet_run",
        ]
        for p in prohibited_strings:
            self.assertNotIn(p, fn_source, f"Prohibited call '{p}' found in _merge_imported_album_into_existing")


if __name__ == "__main__":
    unittest.main()
