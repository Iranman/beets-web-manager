"""ARCH-004/ARCH-023: restore proof against real Beets libraries and files.

Every test in this module runs against a disposable Beets library and real
audio files. Mocked adapters are used only where the point is the transaction
state machine (route reachability, recovery), never for a restore claim.

Coverage (issue #486, C5 a-j):
  a. album-row and import rollback/undo, moved and in-place
  b. artwork fetch/rollback, bytes and embedded images
  c. restore truth for item/album metadata and genre repair
  d. route reachability for the item/genre rollback executors
  f. crash/restart truth (Recovery Required, manifest before mutation)
  g. concurrency: apply and rollback use the same locks
  h. idempotent retries, occupied paths/ids, missing quarantine files,
     membership changes
  i. artist-folder rollback restores exact paths, bytes and tags
  j. the frontend jobStart multipart/5xx contract lives in frontend tests
"""

import hashlib
import io
import os
import shutil
import struct
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

import mutagen.id3
import mutagen.wave
from beets import config as beets_config
from beets.library import Album, Item, Library
from beets.plugins import BeetsPlugin
from beetsplug.web import app as beets_web_app
from beetsplug.webmanager import WebManagerPlugin
from beetsplug.webmanager.auth import set_api_key
from beetsplug.webmanager.engine_common import (
    _restore_album_row,
    set_quarantine_root,
)
from mediafile import Image as MFImage
from mediafile import MediaFile
from PIL import Image

import beets.plugins as beets_plugins
import beetsplug.webmanager.art_ops as art_ops
import beetsplug.webmanager.operations as ops_mod
import beetsplug.webmanager.restore_ops as restore_ops
import backend.composite_workflows as cw
import backend.transaction_recovery as recovery
from backend import resource_locks
from backend.beets_adapter import BeetsAdapter, BeetsAdapterError
from backend.transaction_engine import TransactionStore
from tests.test_staging_mutation_hardening import _RouteEnv
from tests.test_wave0_s1_containment import FakeAdapter

TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wav(path: str, title: str, *, artist: str = "Artist", albumartist: str = "Artist",
         album: str = "Album", track: int = 1, genre: str = "Rock") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(struct.pack("<h", 0) * 800)
    audio = mutagen.wave.WAVE(path)
    audio.add_tags()
    for frame in (
        mutagen.id3.TIT2(encoding=3, text=[title]),
        mutagen.id3.TPE1(encoding=3, text=[artist]),
        mutagen.id3.TPE2(encoding=3, text=[albumartist]),
        mutagen.id3.TALB(encoding=3, text=[album]),
        mutagen.id3.TRCK(encoding=3, text=[str(track)]),
        mutagen.id3.TCON(encoding=3, text=[genre]),
    ):
        audio.tags.add(frame)
    audio.save()


def _flac(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    packed = (44100 << 44) | (1 << 41) | (15 << 36) | 44100
    streaminfo = struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + packed.to_bytes(8, "big") + b"\x00" * 16
    with open(path, "wb") as fh:
        fh.write(b"fLaC" + bytes([0x80]) + len(streaminfo).to_bytes(3, "big") + streaminfo)


def _image(fmt: str = "JPEG", color=(200, 10, 10)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(buf, format=fmt)
    return buf.getvalue()


def _wav_frames(path: str) -> bytes:
    """The audio payload, independent of tag rewrites (a WAV tag write
    changes the file bytes, but not the sound)."""
    with wave.open(path, "rb") as handle:
        return handle.readframes(handle.getnframes())


def _embedded(path: str):
    return [hashlib.sha256(image.data).hexdigest() for image in (MediaFile(path).images or [])]


class _Bridge(BeetsAdapter):
    """The real BeetsAdapter wire path, with Flask's test client as transport."""

    def __init__(self, client, token: str = TOKEN):
        super().__init__()
        self._client = client
        self._token = token

    def _request(self, method, path, params=None, json_data=None, headers=None, timeout=None):
        response = self._client.open(
            path, method=method, json=json_data, query_string=params,
            headers={"Authorization": f"Bearer {self._token}", **(headers or {})},
        )
        body = response.get_json(silent=True)
        if response.status_code >= 400:
            raise BeetsAdapterError(
                f"{response.status_code} {body}",
                error_code=(body or {}).get("error_code"),
            )
        return body


class _GenreBridge(_Bridge):
    """Real DB/file writes; the network lastgenre source is replaced by a
    deterministic genre, so apply and rollback exercise the real plugin."""

    def __init__(self, *args, genre: str = "Electronic", **kwargs):
        super().__init__(*args, **kwargs)
        self.genre = genre
        self.lastgenre_calls = 0

    def lastgenre(self, album_ids, force=False, is_async=False, idempotency_key=None):
        self.lastgenre_calls += 1
        self.modify({"genre": self.genre}, album_ids=list(album_ids), write=True, move=False)
        return {"success": True, "output": self.genre, "skipped": 0}


class _ModifyFake(FakeAdapter):
    """FakeAdapter plus the modify call the rollback executors make."""

    def __init__(self, items=None, albums=None):
        super().__init__(items=items, albums=albums)
        self.modified = []

    def modify(self, fields, item_ids=None, album_ids=None, write=True, move=False, **kwargs):
        self.modified.append({"fields": dict(fields), "item_ids": list(item_ids or []),
                              "album_ids": list(album_ids or []), "write": write, "move": move})
        for iid in item_ids or []:
            if int(iid) in self.items:
                self.items[int(iid)].update(fields)
        for aid in album_ids or []:
            if int(aid) in self.albums:
                self.albums[int(aid)].update(fields)
        return {"success": True, "modified_items": len(item_ids or []),
                "modified_albums": len(album_ids or [])}


class RealRestoreBase(unittest.TestCase):
    """Disposable Beets library, real files, the real webmanager plugin."""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="arch004_023_")
        self.music = os.path.join(self.td, "music")
        self.quarantine = os.path.join(self.td, "quarantine")
        os.makedirs(self.music)
        self._old_dir = beets_config["directory"].get()
        self._old_paths = beets_web_app.config.get("INCLUDE_PATHS", False)
        self._old_testing = beets_web_app.config.get("TESTING", False)
        beets_config["directory"] = self.music
        self.lib = Library(os.path.join(self.td, "library.blb"), directory=self.music)
        self.plugin = WebManagerPlugin()
        set_api_key(TOKEN)
        ops_mod.set_allowed_roots([self.music])
        ops_mod.set_import_roots(None)
        set_quarantine_root(self.quarantine)
        beets_web_app.config["lib"] = self.lib
        beets_web_app.config["INCLUDE_PATHS"] = True
        beets_web_app.config["TESTING"] = True
        self.client = beets_web_app.test_client()
        self.auth = {"Authorization": f"Bearer {TOKEN}"}
        self.store = TransactionStore(os.path.join(self.td, "transactions"))
        resource_locks.set_locks(resource_locks.ResourceLocks(Path(self.td) / "locks"))
        self.adapter = _Bridge(self.client)
        self._listeners_before = list(BeetsPlugin.listeners["art_set"])
        self._instances_before = list(beets_plugins._instances)

    def tearDown(self):
        BeetsPlugin.listeners["art_set"][:] = self._listeners_before
        beets_plugins._instances[:] = self._instances_before
        beets_config["directory"] = self._old_dir
        beets_web_app.config["INCLUDE_PATHS"] = self._old_paths
        beets_web_app.config["TESTING"] = self._old_testing
        ops_mod.set_allowed_roots(None)
        ops_mod.set_import_roots(None)
        set_quarantine_root(None)
        set_api_key(None)
        resource_locks.set_locks(None)
        try:
            self.lib._connection().close()
        except Exception:
            pass
        shutil.rmtree(self.td, ignore_errors=True)

    def _add_wav_album(self, count=2, folder="Artist/Album", *, title_prefix="Track",
                       artist="Artist", albumartist="Artist", album="Album", genre="Rock"):
        paths = [os.path.join(self.music, folder, f"0{n} {title_prefix}.wav") for n in range(1, count + 1)]
        items = []
        for n, path in enumerate(paths, 1):
            _wav(path, f"{title_prefix} {n}", artist=artist, albumartist=albumartist,
                 album=album, track=n, genre=genre)
            item = Item.from_path(path)
            item.update({"title": f"{title_prefix} {n}", "artist": artist,
                         "albumartist": albumartist, "album": album, "track": n})
            items.append(item)
        album_row = self.lib.add_album(items)
        self.lib._connection().commit()
        return album_row, paths

    def _post(self, path, payload, key=None):
        headers = dict(self.auth)
        if key:
            headers["Idempotency-Key"] = key
        return self.client.post(path, headers=headers, json=payload)

    def _manifest(self, folder_id):
        return Path(self.quarantine) / folder_id / "manifest.json"

    def _read_manifest(self, folder_id):
        import json
        return json.loads(self._manifest(folder_id).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# (a) album-row and import rollback, moved and in-place
# ---------------------------------------------------------------------------

class AlbumRowRestoreTests(RealRestoreBase):
    def _empty_album(self):
        album = Album(album="Empty Album", albumartist="Empty Artist", year=1999,
                      mb_albumid="11111111-1111-1111-1111-111111111111")
        self.lib.add(album)
        self.lib._connection().commit()
        return album

    def test_remove_and_rollback_restores_original_row_and_id(self):
        album = self._empty_album()
        original = {"album": album.album, "albumartist": album.albumartist,
                    "year": album.year, "mb_albumid": album.mb_albumid}
        response = self._post("/webmanager/album-row/remove", {"album_id": album.id}, key="row-remove-1")
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        row_id = body["album_row_id"]
        self.assertIsNone(self.lib.get_album(album.id))
        self.assertEqual(self._read_manifest(row_id)["status"], "applied")

        response = self._post("/webmanager/album-row/rollback", {"album_row_id": row_id}, key="row-undo-1")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["success"])
        live = self.lib.get_album(album.id)
        self.assertIsNotNone(live)
        self.assertEqual(live.id, album.id)
        self.assertEqual({key: live.get(key) for key in original}, original)
        self.assertEqual(self._read_manifest(row_id)["status"], "restored")

        # A retry with the same key replays the recorded outcome; it does not
        # create a second row.
        replay = self._post("/webmanager/album-row/rollback", {"album_row_id": row_id}, key="row-undo-1")
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(replay.get_json()["status"], "succeeded")
        self.assertEqual(len(self.lib.albums()), 1)

    def test_rollback_resumes_an_applying_album_row_removal(self):
        import json
        album = self._empty_album()
        original_id = album.id
        response = self._post("/webmanager/album-row/remove", {"album_id": original_id})
        self.assertEqual(response.status_code, 200, response.get_json())
        row_id = response.get_json()["album_row_id"]
        # A hard crash after album.remove() but before the "applied" write.
        manifest = self._read_manifest(row_id)
        manifest["status"] = "applying"
        self._manifest(row_id).write_text(json.dumps(manifest), encoding="utf-8")
        self.assertIsNone(self.lib.get_album(original_id))

        response = self._post("/webmanager/album-row/rollback", {"album_row_id": row_id})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["success"])
        self.assertIsNotNone(self.lib.get_album(original_id))
        self.assertEqual(self._read_manifest(row_id)["status"], "restored")

    def test_manifest_failure_after_removal_compensates_the_original_row(self):
        album = self._empty_album()
        original = restore_ops._album_snapshot(album)
        real_write = restore_ops._write_manifest
        calls = 0

        def write(folder, manifest):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated final-manifest failure")
            return real_write(folder, manifest)

        with mock.patch.object(restore_ops, "_write_manifest", side_effect=write):
            response = self._post("/webmanager/album-row/remove", {"album_id": album.id})
        self.assertEqual(response.status_code, 500, response.get_json())
        live = self.lib.get_album(album.id)
        self.assertIsNotNone(live)
        self.assertTrue(restore_ops._row_matches(live, original))
        import json
        manifest = next(Path(self.quarantine).glob("*/manifest.json"))
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["status"], "compensated")

    def test_applying_manifest_with_intact_row_is_verified_without_duplicate(self):
        album = self._empty_album()
        row_id = "c" * 32
        folder = Path(self.quarantine) / row_id
        folder.mkdir(parents=True)
        restore_ops._write_manifest(str(folder), {
            "kind": restore_ops.ALBUM_ROW_KIND, "album_id": album.id,
            "snapshot": restore_ops._album_snapshot(album), "status": "applying"})
        response = self._post("/webmanager/album-row/rollback", {"album_row_id": row_id})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["already_present"])
        self.assertEqual(len(self.lib.albums()), 1)
        self.assertEqual(self._read_manifest(row_id)["status"], "restored")

    def test_rollback_refuses_an_occupied_album_id_without_overwriting(self):
        album = self._empty_album()
        original_id = album.id
        response = self._post("/webmanager/album-row/remove", {"album_id": original_id})
        row_id = response.get_json()["album_row_id"]
        intruder = Album(album="Different Album", albumartist="Different Artist")
        self.lib.add(intruder)
        self.lib._connection().commit()
        # Put the intruder on the original id, the only way Beets lets this
        # situation happen (another process restored/reused the id).
        with self.lib.transaction():
            self.lib._connection().execute("UPDATE albums SET id=? WHERE id=?", (original_id, intruder.id))
        self.lib._connection().commit()
        self.lib._connection().execute("PRAGMA foreign_keys=ON")

        response = self._post("/webmanager/album-row/rollback", {"album_row_id": row_id})
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()["error_code"], "ALBUM_ID_OCCUPIED")
        live = self.lib.get_album(original_id)
        self.assertIsNotNone(live)
        self.assertEqual(live.album, "Different Album")


class ImportRollbackRestoreTests(RealRestoreBase):
    def test_moved_rollback_and_undo_restore_rows_bytes_and_membership(self):
        album, paths = self._add_wav_album()
        original_hashes = [_sha(path) for path in paths]
        item_ids = sorted(item.id for item in album.items())
        original_album = {"album": album.album, "albumartist": album.albumartist,
                          "mb_albumid": album.mb_albumid}

        response = self._post("/webmanager/import/rollback",
                              {"album_id": album.id, "item_ids": item_ids, "move_files": True},
                              key="import-rollback-1")
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        quarantine_id = body["quarantine_id"]
        self.assertIsNone(self.lib.get_album(album.id))
        for iid in item_ids:
            self.assertIsNone(self.lib.get_item(iid))
        for path, original_hash in zip(paths, original_hashes):
            self.assertFalse(os.path.exists(path))
        by_path = {os.path.normcase(os.path.normpath(path)): digest
                   for path, digest in zip(paths, original_hashes)}
        for removed in body["removed"]:
            qpath = removed["quarantine_path"]
            self.assertTrue(os.path.isfile(qpath))
            original = os.path.normcase(os.path.normpath(removed["original_path"]))
            self.assertEqual(_sha(qpath), by_path[original])
        self.assertEqual(self._read_manifest(quarantine_id)["status"], "applied")

        response = self._post("/webmanager/import/rollback/undo", {"quarantine_id": quarantine_id},
                              key="import-undo-1")
        self.assertEqual(response.status_code, 200, response.get_json())
        restored_album = self.lib.get_album(album.id)
        self.assertIsNotNone(restored_album)
        self.assertEqual({key: restored_album.get(key) for key in original_album}, original_album)
        restored = sorted(restored_album.items(), key=lambda item: item.id)
        self.assertEqual([item.id for item in restored], item_ids)
        for item, path, original_hash in zip(restored, paths, original_hashes):
            self.assertEqual(os.path.normcase(os.fsdecode(item.path)), os.path.normcase(path))
            self.assertEqual(item.album_id, album.id)
            self.assertEqual(_sha(path), original_hash)
        self.assertEqual(self._read_manifest(quarantine_id)["status"], "undone")

        replay = self._post("/webmanager/import/rollback/undo", {"quarantine_id": quarantine_id},
                            key="import-undo-1")
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(replay.get_json()["status"], "succeeded")
        self.assertEqual(len(self.lib.get_album(album.id).items()), 2)

    def test_undo_resumes_an_applying_manifest_after_a_partial_crash(self):
        import json
        album, paths = self._add_wav_album(count=2, folder="Artist/Applying")
        item_ids = sorted(item.id for item in album.items())
        response = self._post("/webmanager/import/rollback",
                              {"album_id": album.id, "item_ids": item_ids, "move_files": True})
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        quarantine_id = body["quarantine_id"]
        # Simulate a crash after one quarantine move and before the final
        # status write: one original is back, both rows are still absent.
        first = body["removed"][0]
        os.replace(first["quarantine_path"], first["original_path"])
        manifest = self._read_manifest(quarantine_id)
        manifest["status"] = "applying"
        self._manifest(quarantine_id).write_text(json.dumps(manifest), encoding="utf-8")

        response = self._post("/webmanager/import/rollback/undo", {"quarantine_id": quarantine_id})
        self.assertEqual(response.status_code, 200, response.get_json())
        restored = self.lib.get_album(album.id)
        self.assertIsNotNone(restored)
        self.assertEqual(sorted(item.id for item in restored.items()), item_ids)
        for path in paths:
            self.assertTrue(os.path.isfile(path))
        self.assertEqual(self._read_manifest(quarantine_id)["status"], "undone")

    def test_in_place_rollback_keeps_files_and_undo_restores_rows(self):
        album, paths = self._add_wav_album(count=1, folder="Artist/In Place")
        item_ids = [item.id for item in album.items()]
        original_hash = _sha(paths[0])
        response = self._post("/webmanager/import/rollback",
                              {"album_id": album.id, "item_ids": item_ids, "move_files": False})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(os.path.isfile(paths[0]))
        self.assertEqual(_sha(paths[0]), original_hash)
        self.assertIsNone(self.lib.get_album(album.id))
        response = self._post("/webmanager/import/rollback/undo",
                              {"quarantine_id": response.get_json()["quarantine_id"]})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(sorted(item.id for item in self.lib.get_album(album.id).items()), item_ids)
        self.assertTrue(os.path.isfile(paths[0]))
        self.assertEqual(_sha(paths[0]), original_hash)

    def test_undo_refuses_an_occupied_original_path_without_overwriting(self):
        album, paths = self._add_wav_album(count=1, folder="Artist/Occupied")
        item_ids = [item.id for item in album.items()]
        response = self._post("/webmanager/import/rollback",
                              {"album_id": album.id, "item_ids": item_ids, "move_files": True})
        quarantine_id = response.get_json()["quarantine_id"]
        with open(paths[0], "wb") as fh:
            fh.write(b"an operator put a different file here")
        response = self._post("/webmanager/import/rollback/undo", {"quarantine_id": quarantine_id})
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()["error_code"], "ORIGINAL_PATH_OCCUPIED")
        self.assertEqual(Path(paths[0]).read_bytes(), b"an operator put a different file here")
        self.assertIsNone(self.lib.get_album(album.id))

    def test_undo_refuses_a_missing_quarantine_file_before_restoring_anything(self):
        album, paths = self._add_wav_album(count=2, folder="Artist/Missing")
        item_ids = [item.id for item in album.items()]
        response = self._post("/webmanager/import/rollback",
                              {"album_id": album.id, "item_ids": item_ids, "move_files": True})
        body = response.get_json()
        quarantine_id = body["quarantine_id"]
        missing = body["removed"][0]["quarantine_path"]
        other = body["removed"][1]["quarantine_path"]
        os.remove(missing)
        response = self._post("/webmanager/import/rollback/undo", {"quarantine_id": quarantine_id})
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()["error_code"], "QUARANTINE_FILE_MISSING")
        self.assertFalse(os.path.exists(body["removed"][0]["original_path"]))
        self.assertFalse(os.path.exists(body["removed"][1]["original_path"]))
        self.assertTrue(os.path.isfile(other))
        self.assertIsNone(self.lib.get_album(album.id))

    def test_undo_refuses_an_occupied_album_id(self):
        album, paths = self._add_wav_album(count=1, folder="Artist/AlbumId")
        original_id = album.id
        item_ids = [item.id for item in album.items()]
        response = self._post("/webmanager/import/rollback",
                              {"album_id": original_id, "item_ids": item_ids, "move_files": True})
        body = response.get_json()
        quarantine_id = body["quarantine_id"]
        intruder = Album(album="Intruder", albumartist="Intruder")
        self.lib.add(intruder)
        with self.lib.transaction():
            self.lib._connection().execute("UPDATE albums SET id=? WHERE id=?", (original_id, intruder.id))
        self.lib._connection().commit()
        response = self._post("/webmanager/import/rollback/undo", {"quarantine_id": quarantine_id})
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()["error_code"], "ALBUM_ID_OCCUPIED")
        self.assertEqual(self.lib.get_album(original_id).album, "Intruder")
        self.assertFalse(os.path.exists(paths[0]))
        self.assertTrue(os.path.isfile(body["removed"][0]["quarantine_path"]))

    def test_undo_refuses_a_directory_in_place_of_the_quarantined_file(self):
        album, paths = self._add_wav_album(count=1, folder="Artist/QuarantineDirectory")
        item_ids = [item.id for item in album.items()]
        response = self._post("/webmanager/import/rollback",
                              {"album_id": album.id, "item_ids": item_ids, "move_files": True})
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        qpath = body["removed"][0]["quarantine_path"]
        os.remove(qpath)
        os.mkdir(qpath)
        response = self._post("/webmanager/import/rollback/undo", {"quarantine_id": body["quarantine_id"]})
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()["error_code"], "QUARANTINE_FILE_MISSING")
        self.assertIsNone(self.lib.get_album(album.id))
        self.assertFalse(os.path.exists(paths[0]))
        self.assertFalse(restore_ops._contained(str(Path(qpath).parents[1]), str(Path(qpath).parents[1])))

    def test_membership_change_is_refused_before_the_manifest_or_a_move(self):
        album, paths = self._add_wav_album(count=2, folder="Artist/Membership")
        item_ids = sorted(item.id for item in album.items())
        response = self._post("/webmanager/import/rollback",
                              {"album_id": album.id, "item_ids": item_ids[:1], "move_files": True})
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()["error_code"], "ALBUM_MEMBERSHIP_CHANGED")
        self.assertEqual(len(self.lib.get_album(album.id).items()), 2)
        for path in paths:
            self.assertTrue(os.path.isfile(path))
        self.assertEqual(list(Path(self.quarantine).glob("*/manifest.json")), [])

    def test_crash_after_the_first_move_is_compensated(self):
        album, paths = self._add_wav_album(count=2, folder="Artist/Crash")
        original_hashes = [_sha(path) for path in paths]
        item_ids = sorted(item.id for item in album.items())
        real_move = restore_ops._move_no_clobber
        calls = {"n": 0}

        def move_then_crash(src, dst, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated crash after the first file move")
            return real_move(src, dst, *args, **kwargs)

        with mock.patch.object(restore_ops, "_move_no_clobber", side_effect=move_then_crash):
            response = self._post("/webmanager/import/rollback",
                                  {"album_id": album.id, "item_ids": item_ids, "move_files": True})
        self.assertEqual(response.status_code, 500, response.get_json())
        self.assertEqual(response.get_json()["error_code"], "ROLLBACK_FAILED")
        # The compensation put the moved file and every row back.
        live_album = self.lib.get_album(album.id)
        self.assertIsNotNone(live_album)
        self.assertEqual(sorted(item.id for item in live_album.items()), item_ids)
        for path, original_hash in zip(paths, original_hashes):
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(_sha(path), original_hash)
        manifest = next(Path(self.quarantine).glob("*/manifest.json"))
        import json
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["status"], "compensated")

    def test_manifest_is_written_before_the_first_file_move(self):
        album, paths = self._add_wav_album(count=1, folder="Artist/BeforeMove")
        item_ids = [item.id for item in album.items()]
        real_move = restore_ops._move_no_clobber
        observed = {}

        def inspect_then_move(src, dst, *args, **kwargs):
            manifests = list(Path(self.quarantine).glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            import json
            observed["manifest"] = json.loads(manifests[0].read_text(encoding="utf-8"))
            return real_move(src, dst, *args, **kwargs)

        with mock.patch.object(restore_ops, "_move_no_clobber", side_effect=inspect_then_move):
            response = self._post("/webmanager/import/rollback",
                                  {"album_id": album.id, "item_ids": item_ids, "move_files": True})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(observed["manifest"]["status"], "applying")
        self.assertEqual(observed["manifest"]["items"][0]["file_moved"], False)

    def test_hard_crash_after_move_before_progress_write_can_be_undone(self):
        class ProcessStopped(BaseException):
            pass

        album, paths = self._add_wav_album(count=2, folder="Artist/HardCrash")
        hashes = [_sha(path) for path in paths]
        item_ids = sorted(item.id for item in album.items())
        real_move = restore_ops._move_no_clobber

        def move_then_stop(src, dst):
            real_move(src, dst)
            raise ProcessStopped()

        with mock.patch.object(restore_ops, "_move_no_clobber", side_effect=move_then_stop):
            with self.assertRaises(ProcessStopped):
                self._post("/webmanager/import/rollback",
                           {"album_id": album.id, "item_ids": item_ids, "move_files": True})
        folder = next(Path(self.quarantine).glob("*/manifest.json")).parent
        manifest = self._read_manifest(folder.name)
        self.assertEqual(manifest["status"], "applying")
        self.assertFalse(manifest["items"][0]["file_moved"])
        self.assertTrue(os.path.isfile(manifest["items"][0]["quarantine_path"]))

        response = self._post("/webmanager/import/rollback/undo", {"quarantine_id": folder.name})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(sorted(item.id for item in self.lib.get_album(album.id).items()), item_ids)
        self.assertEqual([_sha(path) for path in paths], hashes)
        self.assertEqual(self._read_manifest(folder.name)["status"], "undone")


# ---------------------------------------------------------------------------
# (b) artwork fetch/rollback against real FLACs and embedded images
# ---------------------------------------------------------------------------

class ArtworkFetchRestoreTests(RealRestoreBase):
    def setUp(self):
        super().setUp()
        from beetsplug.embedart import EmbedCoverArtPlugin
        from beetsplug.fetchart import FetchArtPlugin
        self.fetch_plugin = FetchArtPlugin()
        self.embed_plugin = EmbedCoverArtPlugin()
        beets_plugins._instances.extend([self.fetch_plugin, self.embed_plugin])
        self._old_sources = beets_config["fetchart"]["sources"].get()
        beets_config["fetchart"]["sources"] = ["filesystem"]

    def tearDown(self):
        beets_config["fetchart"]["sources"] = self._old_sources
        super().tearDown()

    def _art_album(self):
        folder = os.path.join(self.music, "Artist", "Album")
        paths = []
        for n in (1, 2):
            path = os.path.join(folder, f"0{n}.flac")
            _flac(path)
            paths.append(path)
        old_image = _image("PNG", (1, 2, 3))
        old_cover = os.path.join(self.music, "oldcover.png")
        with open(old_cover, "wb") as fh:
            fh.write(old_image)
        items = []
        for n, path in enumerate(paths, 1):
            item = Item.from_path(path)
            item.update({"title": f"Track {n}", "artist": "Artist", "albumartist": "Artist",
                         "album": "Album", "track": n})
            items.append(item)
        album = self.lib.add_album(items)
        album.artpath = os.fsencode(old_cover)
        album.store()
        for item in items:
            item.write(tags={"images": [MFImage(data=old_image, desc="orig", type=3)]})
        self.lib._connection().commit()
        # The album already owns a cover.jpg. fetchart will displace it; a
        # rollback must put those exact bytes back, not delete the file.
        cover = os.path.join(folder, "cover.jpg")
        cover_bytes = _image("JPEG", (250, 250, 0))
        with open(cover, "wb") as fh:
            fh.write(cover_bytes)
        source = os.path.join(self.music, "newcover.jpg")
        with open(source, "wb") as fh:
            fh.write(_image("JPEG", (10, 10, 240)))
        return {"album": album, "old_cover": old_cover, "old_image": old_image,
                "paths": paths, "cover": cover, "cover_bytes": cover_bytes,
                "source": source}

    def _local_fetch(self, lib, album_ids, force=False):
        """A fetchart source that returns a deterministic local image.

        The real fetchart plugin still owns the write (album.set_art); only
        source discovery is replaced so the test does not depend on the
        network. Snapshot, displace, embed and rollback are all real."""
        album = lib.get_album(int(album_ids[0]))
        album.set_art(os.fsencode(self._fetch_source), copy=True)
        album.store()
        album.load()
        return {"processed_albums": 1, "albums_with_art": 1}

    def test_fetch_and_rollback_restore_cover_bytes_and_embedded_images(self):
        data = self._art_album()
        album, old_cover, old_image, paths = data["album"], data["old_cover"], data["old_image"], data["paths"]
        old_hash = _sha(old_cover)
        old_embedded = [_embedded(path) for path in paths]
        self._fetch_source = data["source"]
        with mock.patch.object(art_ops.plugin_ops, "run_fetchart", side_effect=self._local_fetch):
            response = self._post("/webmanager/album-art/fetch", {"album_id": album.id}, key="art-fetch-1")
        self.assertEqual(response.status_code, 200, response.get_json())
        art_id = response.get_json()["art_id"]
        fetched = self.lib.get_album(album.id)
        self.assertNotEqual(os.fsdecode(fetched.artpath), old_cover)
        self.assertEqual(os.path.normcase(os.fsdecode(fetched.artpath)),
                         os.path.normcase(data["cover"]))
        self.assertEqual(Path(data["cover"]).read_bytes(), Path(data["source"]).read_bytes())
        self.assertNotEqual([_embedded(path) for path in paths], old_embedded)
        self.assertEqual(self._read_manifest(art_id)["status"], "applied")

        response = self._post("/webmanager/album-art/rollback", {"art_id": art_id}, key="art-undo-1")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["success"])
        restored = self.lib.get_album(album.id)
        self.assertEqual(os.path.normcase(os.fsdecode(restored.artpath)), os.path.normcase(old_cover))
        self.assertEqual(_sha(old_cover), old_hash)
        self.assertEqual(Path(old_cover).read_bytes(), old_image)
        # The pre-existing cover.jpg is restored byte-for-byte; the fetched
        # file is quarantined under the manifest, never deleted.
        self.assertEqual(Path(data["cover"]).read_bytes(), data["cover_bytes"])
        self.assertTrue(list(Path(self.quarantine, art_id, "replaced").iterdir()))
        self.assertEqual([_embedded(path) for path in paths], old_embedded)
        self.assertEqual(self._read_manifest(art_id)["status"], "rolled_back")

        replay = self._post("/webmanager/album-art/rollback", {"art_id": art_id}, key="art-undo-1")
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(replay.get_json()["status"], "succeeded")

    def test_rollback_refuses_when_embedded_art_changed_after_the_apply(self):
        data = self._art_album()
        album, old_cover, old_image, paths = data["album"], data["old_cover"], data["old_image"], data["paths"]
        self._fetch_source = data["source"]
        with mock.patch.object(art_ops.plugin_ops, "run_fetchart", side_effect=self._local_fetch):
            response = self._post("/webmanager/album-art/fetch", {"album_id": album.id})
        art_id = response.get_json()["art_id"]
        changed = _image("PNG", (9, 9, 9))
        item = self.lib.get_item(self.lib.get_album(album.id).items().__iter__().__next__().id)
        item.write(tags={"images": [MFImage(data=changed, desc="operator", type=3)]})
        before = _embedded(os.fsdecode(item.path))
        response = self._post("/webmanager/album-art/rollback", {"art_id": art_id})
        self.assertIn(response.status_code, (409, 500), response.get_json())
        self.assertNotEqual(response.get_json().get("status"), "Rolled Back")
        self.assertEqual(_embedded(os.fsdecode(item.path)), before)
        self.assertNotEqual(_embedded(os.fsdecode(item.path)), [hashlib.sha256(old_image).hexdigest()])

    def test_transaction_without_an_art_snapshot_is_rollback_unavailable(self):
        tx = self.store.create(
            operation_type="Artwork Update", status="Completed",
            summary="Artwork fetch", rollback_available=True,
            metadata={"mutation_family": cw.ALBUM_ARTWORK_FETCH_FAMILY, "album_id": 1,
                      "engine_result": {}},
        )
        result = cw.rollback_album_artwork_fetch(tx["id"], adapter=self.adapter, store=self.store)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "rollback_unavailable")
        self.assertNotEqual(self.store.get(tx["id"])["status"], "Rolled Back")


# ---------------------------------------------------------------------------
# (c) restore truth: item/album metadata, genre repair, empty diff
# ---------------------------------------------------------------------------

class RestoreTruthTests(RealRestoreBase):
    def test_item_metadata_apply_and_rollback_restore_db_and_file_tag(self):
        album, paths = self._add_wav_album(count=1, folder="Artist/Metadata", title_prefix="Old")
        item = album.items().__iter__().__next__()
        self.assertEqual(MediaFile(paths[0]).title, "Old 1")
        plan = cw.plan_item_metadata({"item_id": item.id, "updates": {"title": "New Title"},
                                      "write_tags": True}, store=self.store)
        self.assertTrue(plan["ok"], plan)
        applied = cw.apply_item_metadata(plan["operation_id"], adapter=self.adapter, store=self.store)
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(self.lib.get_item(item.id).title, "New Title")
        self.assertEqual(MediaFile(paths[0]).title, "New Title")

        rolled = cw.rollback_item_metadata(plan["operation_id"], adapter=self.adapter, store=self.store)
        self.assertTrue(rolled["ok"], rolled)
        self.assertEqual(rolled["status"], "Rolled Back")
        self.assertEqual(self.lib.get_item(item.id).title, "Old 1")
        self.assertEqual(MediaFile(paths[0]).title, "Old 1")

    def test_album_metadata_apply_and_rollback_restore_album_and_track_tags(self):
        album, paths = self._add_wav_album(count=1, folder="Artist/AlbumMeta", album="Old Album")
        plan = cw.plan_album_metadata({"album_id": album.id,
                                       "updates": {"album": "New Album"},
                                       "write_tags": True},
                                      adapter=self.adapter, store=self.store)
        self.assertTrue(plan["ok"], plan)
        applied = cw.apply_album_metadata(plan["operation_id"], adapter=self.adapter, store=self.store)
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(self.lib.get_album(album.id).album, "New Album")
        self.assertEqual(MediaFile(paths[0]).album, "New Album")

        rolled = cw.rollback_album_metadata(plan["operation_id"], adapter=self.adapter, store=self.store)
        self.assertTrue(rolled["ok"], rolled)
        self.assertEqual(rolled["status"], "Rolled Back")
        self.assertEqual(self.lib.get_album(album.id).album, "Old Album")
        self.assertEqual(MediaFile(paths[0]).album, "Old Album")

    def test_genre_repair_rollback_restores_db_and_file_genres(self):
        album, paths = self._add_wav_album(count=1, folder="Artist/Genre", genre="Old Genre")
        # The file tags loaded by Item.from_path carry the genre; the DB uses
        # Beets' multi-value field, so assert through the file for both sides.
        self.assertEqual(MediaFile(paths[0]).genre, "Old Genre")
        adapter = _GenreBridge(self.client)
        plan = cw.plan_album_genre_repair({"album_id": album.id}, store=self.store)
        self.assertTrue(plan["ok"], plan)
        applied = cw.apply_album_genre_repair(plan["operation_id"], adapter=adapter, store=self.store)
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(adapter.lastgenre_calls, 1)
        self.assertEqual(MediaFile(paths[0]).genre, "Electronic")

        rolled = cw.rollback_album_genre_repair(plan["operation_id"], adapter=adapter, store=self.store)
        self.assertTrue(rolled["ok"], rolled)
        self.assertEqual(rolled["status"], "Rolled Back")
        self.assertEqual(MediaFile(paths[0]).genre, "Old Genre")
        live = self.lib.get_item(self.lib.get_album(album.id).items().__iter__().__next__().id)
        self.assertIn("Old Genre", list(live.genres))

    def test_empty_db_diff_is_not_reported_as_rolled_back_when_tags_differ(self):
        album, paths = self._add_wav_album(count=1, folder="Artist/EmptyDiff", title_prefix="Stable")
        item = album.items().__iter__().__next__()
        # The DB row already says "Stable"; the file tag was changed behind its
        # back. An empty diff must not be accepted as proof of restoration.
        audio = mutagen.wave.WAVE(paths[0])
        audio.tags.setall("TIT2", [mutagen.id3.TIT2(encoding=3, text=["Different"])])
        audio.save()
        tx = self.store.create(
            operation_type="Metadata Update", status="Completed",
            summary="Legacy item metadata", rollback_available=True,
            metadata={"mutation_family": cw.ITEM_METADATA_FAMILY, "item_id": item.id,
                      "engine_result": {"before_state": {"items": [
                          {"id": item.id, "fields": {"title": item.title}, "path": paths[0]}
                      ]}}},
        )
        result = cw.rollback_item_metadata(tx["id"], adapter=self.adapter, store=self.store)
        self.assertFalse(result["ok"])
        self.assertNotEqual(result["status"], "Rolled Back")
        self.assertEqual(result["rollback_ok"], 0)
        self.assertEqual(MediaFile(paths[0]).title, "Different")

    def test_legacy_item_metadata_without_before_state_is_unavailable(self):
        tx = self.store.create(
            operation_type="Metadata Update", status="Completed",
            summary="Legacy item metadata", rollback_available=True,
            metadata={"mutation_family": cw.ITEM_METADATA_FAMILY, "item_id": 1,
                      "engine_result": {}},
        )
        result = cw.rollback_item_metadata(tx["id"], adapter=self.adapter, store=self.store)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "rollback_unavailable")
        self.assertEqual(self.store.get(tx["id"])["status"], "Completed")


# ---------------------------------------------------------------------------
# (d) route reachability for the item/genre rollback executors
# ---------------------------------------------------------------------------

class DiscographyJobFollowTests(_RouteEnv):
    def test_duplicate_discography_starts_follow_the_existing_job(self):
        import routes_library
        from job_engine import DuplicateJobError

        job = mock.Mock(job_id="existing-discography", label="Lookup", status="running")
        for suffix, kind in (("", "artist-discography"), ("/discogs", "artist-discography-discogs")):
            with self.subTest(source=kind):
                with mock.patch.object(routes_library.jobs, "start_python", side_effect=DuplicateJobError(job)) as start:
                    response = self.client.get(f"/api/artist-discography{suffix}?artist=ARCH004+Retry+Artist")
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(response.get_json()["job_id"], job.job_id)
                self.assertEqual(response.get_json()["status"], "running")
                self.assertEqual(start.call_args.kwargs["metadata"]["dedupe_key"],
                                 f"{kind}:arch004 retry artist")


class RollbackRouteReachabilityTests(_RouteEnv):
    def _item_tx(self, title="New"):
        item_id = 41
        self.store.create(
            operation_type="Metadata Update", status="Preview",
            summary="item", metadata={"mutation_family": cw.ITEM_METADATA_FAMILY,
                                      "item_id": item_id, "updates": {"title": title}},
        )
        # Create a fresh, already-applied transaction with the same shape.
        tx = self.store.create(
            operation_type="Metadata Update", status="Completed",
            summary="item", rollback_available=True,
            metadata={"mutation_family": cw.ITEM_METADATA_FAMILY, "item_id": item_id,
                      "engine_result": {"before_state": {"items": [
                          {"id": item_id, "fields": {"title": "Old"}}
                      ]}}},
        )
        return tx

    def test_item_metadata_rollback_route_reaches_the_executor(self):
        tx = self._item_tx()
        fake = _ModifyFake(items={41: {"id": 41, "title": "New", "path": ""}})
        with mock.patch.object(cw, "beets_adapter", fake), \
                mock.patch.object(cw, "_default_store", self.store):
            response = self.client.post(f"/api/transactions/{tx['id']}/rollback")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["status"], "Rolled Back")
        self.assertEqual(fake.items[41]["title"], "Old")
        self.assertEqual(fake.modified[0]["fields"], {"title": "Old"})

    def test_genre_repair_rollback_route_reaches_the_executor(self):
        tx = self.store.create(
            operation_type="Repair", status="Completed",
            summary="genre", rollback_available=True,
            metadata={"mutation_family": cw.GENRE_REPAIR_FAMILY, "album_id": 7,
                      "engine_result": {"before_state": {
                          "album": {"id": 7, "fields": {"genre": "Old Genre"}},
                          "items": [{"id": 8, "fields": {"genre": "Old Genre"}}],
                      }}},
        )
        fake = _ModifyFake(items={8: {"id": 8, "album_id": 7, "genre": "New Genre", "path": ""}},
                           albums={7: {"id": 7, "genre": "New Genre"}})
        with mock.patch.object(cw, "beets_adapter", fake), \
                mock.patch.object(cw, "_default_store", self.store):
            response = self.client.post(f"/api/transactions/{tx['id']}/rollback")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["status"], "Rolled Back")
        self.assertEqual(fake.albums[7]["genre"], "Old Genre")
        self.assertEqual(fake.items[8]["genre"], "Old Genre")


# ---------------------------------------------------------------------------
# (f) crash/restart truth
# ---------------------------------------------------------------------------

class RecoveryTruthTests(_RouteEnv):
    def test_interrupted_rollback_becomes_recovery_required_not_completed(self):
        tx = self.store.create(
            operation_type="Repair", status="Running",
            summary="interrupted rollback",
            metadata={"mutation_family": cw.ALBUM_ART_REPLACE_FAMILY,
                      "engine_request": {"idempotency_key": "old-apply"},
                      "engine_result": {"before_state": {"items": [
                          {"id": 1, "fields": {"title": "Old"}}
                      ]}},
                      "rollback_request": {"requested_at": time.time(), "owner": "test"}},
        )
        outcome = recovery.resolve_transaction(self.store.get(tx["id"]), store=self.store)
        self.assertEqual(outcome.get("action"), "recovery_required_interrupted_rollback", outcome)
        self.assertEqual(self.store.get(tx["id"])["status"], "Recovery Required")
        self.assertFalse(self.store.get(tx["id"])["metadata"]["rollback_request"].get("outcome"))

    def test_rollback_claim_contains_recovery_marker_in_the_same_durable_write(self):
        class ProcessStopped(BaseException):
            pass

        tx = self.store.create(operation_type="Artwork Update", status="Completed",
                               metadata={"mutation_family": cw.ALBUM_ART_REPLACE_FAMILY})
        real_transition = self.store.transition

        def stop_after_claim(operation_id, expected, status, **updates):
            result = real_transition(operation_id, expected, status, **updates)
            if status == "Running":
                raise ProcessStopped()
            return result

        with mock.patch.object(self.store, "transition", side_effect=stop_after_claim):
            with self.assertRaises(ProcessStopped):
                with cw.rollback_guard(self.store, tx["id"], [], family=cw.ALBUM_ART_REPLACE_FAMILY):
                    self.fail("the process stopped at the claim")
        claimed = self.store.get(tx["id"])
        self.assertEqual(claimed["status"], "Running")
        self.assertEqual(claimed["metadata"]["rollback_request"]["claimed_from"], "Completed")
        recovery.resolve_transaction(claimed, store=self.store)
        self.assertEqual(self.store.get(tx["id"])["status"], "Recovery Required")

    def test_a_live_rollback_started_after_the_process_is_not_recovered(self):
        started = time.time()
        tx = self.store.create(
            operation_type="Repair", status="Running",
            summary="live rollback",
            metadata={"mutation_family": cw.ITEM_METADATA_FAMILY,
                      "engine_result": {"before_state": {"items": []}},
                      "rollback_request": {"started_at": started, "owner": "test"}},
        )
        outcome = recovery.resolve_transaction(self.store.get(tx["id"]), store=self.store,
                                               before=started - 1)
        self.assertEqual(outcome.get("action"), "still_running", outcome)
        self.assertEqual(self.store.get(tx["id"])["status"], "Running")

    def test_finished_rollback_is_not_reopened_by_recovery(self):
        tx = self.store.create(
            operation_type="Repair", status="Running",
            summary="finished rollback",
            metadata={"mutation_family": cw.ITEM_METADATA_FAMILY,
                      "engine_result": {"before_state": {"items": []}},
                      "rollback_request": {"requested_at": time.time(), "outcome": {"status": "Rolled Back"}}},
        )
        recovery.resolve_transaction(self.store.get(tx["id"]), store=self.store)
        self.assertNotEqual(self.store.get(tx["id"])["status"], "Recovery Required")


# ---------------------------------------------------------------------------
# (g) concurrency: apply and rollback use the same resource keys
# ---------------------------------------------------------------------------

class ConcurrencyLockTests(RealRestoreBase):
    def test_item_apply_and_rollback_use_the_same_lock_key(self):
        meta = {"item_id": 11}
        self.assertEqual(cw._family_lock_keys(cw.ITEM_METADATA_FAMILY, meta), ["item:11"])
        with cw._apply_locks(cw.ITEM_METADATA_FAMILY, meta, "op-test"):
            with self.assertRaises(resource_locks.ResourceLockConflictError):
                with resource_locks.locks().hold(["item:11"], "other-owner", timeout=0):
                    pass

    def test_real_apply_holds_the_item_lock_before_snapshot_and_blocks_rollback(self):
        album, paths = self._add_wav_album(count=1, folder="Artist/ApplyRace", title_prefix="Old")
        item = next(iter(album.items()))
        first = cw.plan_item_metadata({"item_id": item.id, "updates": {"title": "First"},
                                       "write_tags": True}, store=self.store)["operation_id"]
        self.assertTrue(cw.apply_item_metadata(first, adapter=self.adapter, store=self.store)["ok"])
        second = cw.plan_item_metadata({"item_id": item.id, "updates": {"title": "Second"},
                                        "write_tags": True}, store=self.store)["operation_id"]
        entered, release = threading.Event(), threading.Event()
        results = []
        real_capture, real_guard = cw._capture_item_before, cw.rollback_guard

        def capture(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test did not release the apply")
            return real_capture(*args, **kwargs)

        def short_guard(*args, **kwargs):
            return real_guard(*args, timeout=0.05, **kwargs)

        def apply():
            results.append(cw.apply_item_metadata(second, adapter=self.adapter, store=self.store))

        with mock.patch.object(cw, "_capture_item_before", side_effect=capture), \
                mock.patch.object(cw, "rollback_guard", side_effect=short_guard):
            thread = threading.Thread(target=apply)
            thread.start()
            try:
                self.assertTrue(entered.wait(10), "apply did not reach its snapshot")
                refused = cw.rollback_item_metadata(first, adapter=self.adapter, store=self.store)
                self.assertEqual(refused.get("code"), "rollback_busy", refused)
                self.assertEqual(self.store.get(first)["status"], "Completed")
                self.assertEqual(MediaFile(paths[0]).title, "First")
            finally:
                release.set()
                thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1, results)
        self.assertTrue(results[0]["ok"], results)
        self.assertEqual(MediaFile(paths[0]).title, "Second")
        self.assertTrue(cw.rollback_item_metadata(second, adapter=self.adapter, store=self.store)["ok"])
        self.assertEqual(MediaFile(paths[0]).title, "First")
        self.assertTrue(cw.rollback_item_metadata(first, adapter=self.adapter, store=self.store)["ok"])
        self.assertEqual(self.lib.get_item(item.id).title, "Old 1")
        self.assertEqual(MediaFile(paths[0]).title, "Old 1")

    def test_concurrent_item_metadata_rollbacks_restore_once(self):
        album, paths = self._add_wav_album(count=1, folder="Artist/Concurrent", title_prefix="Old")
        item = album.items().__iter__().__next__()
        plan = cw.plan_item_metadata({"item_id": item.id, "updates": {"title": "New"},
                                      "write_tags": True}, store=self.store)
        cw.apply_item_metadata(plan["operation_id"], adapter=self.adapter, store=self.store)
        results = []
        barrier = threading.Barrier(2)

        def rollback():
            barrier.wait()
            results.append(cw.rollback_item_metadata(plan["operation_id"], adapter=self.adapter,
                                                     store=self.store))

        threads = [threading.Thread(target=rollback) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        successes = [result for result in results if result.get("ok")]
        conflicts = [result for result in results if not result.get("ok")]
        self.assertEqual(len(successes), 1, results)
        self.assertEqual(len(conflicts), 1, results)
        self.assertEqual(self.lib.get_item(item.id).title, "Old 1")
        self.assertEqual(MediaFile(paths[0]).title, "Old 1")


# ---------------------------------------------------------------------------
# (h) exact-path restore op, security and resume
# ---------------------------------------------------------------------------

class ExactPathRestoreTests(RealRestoreBase):
    def test_restores_an_exact_path_and_row_without_clobber(self):
        album, paths = self._add_wav_album(count=1, folder="Old Artist/Album")
        item = album.items().__iter__().__next__()
        original = paths[0]
        moved = os.path.join(self.music, "Canon", "Album", "01 Track 1.wav")
        os.makedirs(os.path.dirname(moved), exist_ok=True)
        shutil.move(original, moved)
        item.path = os.fsencode(moved)
        item.store()
        self.lib._connection().commit()
        digest = _sha(moved)

        response = self._post("/webmanager/item-path/restore",
                              {"items": [{"id": item.id, "path": original, "sha256": digest}]})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["success"])
        self.assertFalse(os.path.exists(moved))
        self.assertEqual(_sha(original), digest)
        self.assertEqual(os.path.normcase(os.fsdecode(self.lib.get_item(item.id).path)),
                         os.path.normcase(original))

        # Resume: the file is already at the target but the row still points at
        # the (now missing) moved path. The digest makes this safe.
        item = self.lib.get_item(item.id)
        item.path = os.fsencode(moved)
        item.store()
        self.lib._connection().commit()
        response = self._post("/webmanager/item-path/restore",
                              {"items": [{"id": item.id, "path": original, "sha256": digest}]})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["restored"][0]["resumed"])

    def test_refuses_an_occupied_target_and_a_path_outside_allowed_roots(self):
        album, paths = self._add_wav_album(count=1, folder="Old Artist/Occupied")
        item = album.items().__iter__().__next__()
        target = paths[0]
        moved = os.path.join(self.music, "Canon", "Album", "01 Track 1.wav")
        os.makedirs(os.path.dirname(moved), exist_ok=True)
        shutil.move(target, moved)
        item.path = os.fsencode(moved)
        item.store()
        self.lib._connection().commit()
        with open(target, "wb") as fh:
            fh.write(b"operator file")
        response = self._post("/webmanager/item-path/restore",
                              {"items": [{"id": item.id, "path": target, "sha256": _sha(moved)}]})
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()["error_code"], "DESTINATION_OCCUPIED")
        self.assertEqual(Path(target).read_bytes(), b"operator file")
        self.assertTrue(os.path.isfile(moved))

        outside = os.path.join(self.td, "outside.wav")
        response = self._post("/webmanager/item-path/restore",
                              {"items": [{"id": item.id, "path": outside, "sha256": _sha(moved)}]})
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(response.get_json()["error_code"], "PATH_NOT_ALLOWED")

        # A stale/tampered row must not make the op move a file from outside
        # the allowed roots into the library.
        outside_source = os.path.join(self.td, "outside-source.wav")
        shutil.move(moved, outside_source)
        item = self.lib.get_item(item.id)
        item.path = os.fsencode(outside_source)
        item.store()
        self.lib._connection().commit()
        response = self._post("/webmanager/item-path/restore",
                              {"items": [{"id": item.id, "path": target,
                                          "sha256": _sha(outside_source)}]})
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(response.get_json()["error_code"], "PATH_NOT_ALLOWED")
        self.assertTrue(os.path.isfile(outside_source))


# ---------------------------------------------------------------------------
# (i) artist-folder rollback with real files
# ---------------------------------------------------------------------------

class ArtistFolderRestoreTests(RealRestoreBase):
    def test_real_artist_restore_cannot_be_proven_without_original_paths(self):
        album, _paths = self._add_wav_album(count=1, folder="Artist/LegacyPaths")
        self.assertFalse(cw._artist_paths_restored(self.adapter, {"album_id": album.id,
                                                                "albumartist": "Artist"}))

    def test_apply_and_rollback_restore_exact_paths_audio_and_tags(self):
        album, paths = self._add_wav_album(count=2, folder="Old Artist/Album",
                                           artist="Old Artist", albumartist="Old Artist")
        original_frames = [_wav_frames(path) for path in paths]
        plan = cw.plan_artist_folder_reconcile(
            {"album_ids": [album.id], "canonical_name": "Canon"},
            adapter=self.adapter, store=self.store,
        )
        self.assertTrue(plan["ok"], plan)
        tx = self.store.get(plan["operation_id"])
        self.assertEqual(os.path.normcase(os.path.normpath(tx["metadata"]["before_state"][0]["items"][0]["path"])),
                         os.path.normcase(os.path.normpath(paths[0])))

        applied = cw.apply_artist_folder_reconcile(plan["operation_id"], adapter=self.adapter,
                                                   store=self.store)
        self.assertTrue(applied["ok"], applied)
        self.assertEqual(self.lib.get_album(album.id).albumartist, "Canon")
        applied_items = sorted(self.lib.get_album(album.id).items(), key=lambda item: item.track)
        applied_paths = [os.fsdecode(item.path) for item in applied_items]
        self.assertTrue(all("Canon" in path for path in applied_paths), applied_paths)
        self.assertEqual([_wav_frames(path) for path in applied_paths], original_frames)

        rolled = cw.rollback_artist_folder_reconcile(plan["operation_id"], adapter=self.adapter,
                                                     store=self.store)
        self.assertTrue(rolled["ok"], rolled)
        self.assertEqual(rolled["status"], "Rolled Back")
        live = self.lib.get_album(album.id)
        self.assertEqual(live.albumartist, "Old Artist")
        live_items = sorted(live.items(), key=lambda item: item.track)
        live_paths = [os.fsdecode(item.path) for item in live_items]
        self.assertEqual([os.path.normcase(os.path.normpath(path)) for path in live_paths],
                         [os.path.normcase(os.path.normpath(path)) for path in paths])
        self.assertEqual([_wav_frames(path) for path in live_paths], original_frames)
        for path in live_paths:
            self.assertEqual(MediaFile(path).albumartist, "Old Artist")
        self.assertFalse(os.path.exists(os.path.join(self.music, "Canon", "Album", "01 Track 1.wav")))

    def test_rollback_refuses_a_legacy_plan_without_before_state(self):
        tx = self.store.create(
            operation_type="Artist Folder Reconcile", status="Completed",
            summary="legacy", rollback_available=False,
            metadata={"mutation_family": "artist_folder_reconcile_v1"},
        )
        result = cw.rollback_artist_folder_reconcile(tx["id"], adapter=self.adapter, store=self.store)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "rollback_unavailable")
        self.assertEqual(self.store.get(tx["id"])["status"], "Completed")
