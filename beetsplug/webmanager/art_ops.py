"""Set an album's cover from an image the Web Manager supplies (upload or URL).

POST /webmanager/album-art
    {"album_id": N, "image_b64": "<base64>", "image_sha256": "<64 hex>",
     "expected_mb_releasegroupid": "<optional>"}
    Beets does the work, the same way fetchart does once it has an image:
    ``Album.set_art()`` copies the image to ``art_destination()`` (the
    configured ``art_filename`` in the album folder), sets ``artpath`` and
    sends ``art_set``; embedart, when it is loaded, embeds it from that event
    (its ``auto``, ``maxwidth``, ``ifempty`` and ``remove_art_file`` options
    apply as they do after fetchart). Before anything changes, the engine
    copies the previous cover file and each item's embedded images into its
    own quarantine folder and writes a manifest there; it returns the
    folder's id (``art_id``).

POST /webmanager/album-art/rollback
    {"art_id": "<32 hex>"}
    Reads the engine's own manifest -- never paths supplied by the caller --
    moves the new cover into the quarantine folder (never deleted), copies
    the previous cover back, restores ``artpath`` and puts each item's
    embedded images back. Refused (409) when the album's art changed since.

The only code here that Beets has no API for is the snapshot/restore of the
previous cover and embedded images. The Web Manager decides *whether* to
replace (the operator's upload); this module only performs it inside Beets.
"""

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import uuid
from typing import Any, Dict, List, Optional

from flask import g, jsonify, request

from . import operations as ops
from .engine_common import (
    MANIFEST_NAME,
    _error,
    _fspath,
    _inside_allowed,
    _manifest_dir,
    _quarantine_root,
)

MAX_IMAGE_BYTES = 15 * 1024 * 1024
# base64 grows 4/3, plus the small JSON envelope.
MAX_REQUEST_BYTES = MAX_IMAGE_BYTES * 4 // 3 + 64 * 1024
# Same pixel limits as Web Manager's own gate (artwork_service), so a
# direct plugin caller cannot hand embedart/ArtResizer a decompression bomb.
MAX_IMAGE_SIDE = 12_000
MAX_IMAGE_PIXELS = 50_000_000
MANIFEST_KIND = "album_art"
_SHA256 = re.compile(r"[0-9a-f]{64}")


def image_extension(data: bytes) -> str:
    """Extension for the image types Web Manager accepts (from the bytes,
    never from a caller-supplied name), or "" for anything else."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""


def image_size_ok(data: bytes) -> bool:
    """Header-only dimension check (Pillow reads the size without decoding)."""
    try:
        from PIL import Image as PILImage
    except ImportError:  # ponytail: no Pillow -> magic/size checks only; Web Manager's gate still applies
        return True
    import io
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            with PILImage.open(io.BytesIO(data), formats=("JPEG", "PNG", "WEBP")) as img:
                width, height = img.size
    except Exception:
        return False
    return 0 < width <= MAX_IMAGE_SIDE and 0 < height <= MAX_IMAGE_SIDE and width * height <= MAX_IMAGE_PIXELS


def _manifest_ok(manifest: Dict[str, Any]) -> bool:
    """Every field rollback will use, checked before anything is written."""
    if not isinstance(manifest.get("album_id"), int):
        return False
    for key in ("old_artpath", "previous_cover", "new_artpath"):
        if not isinstance(manifest.get(key) or "", str):
            return False
    displaced = manifest.get("displaced") or {}
    if not isinstance(displaced, dict) or (displaced and not (
            isinstance(displaced.get("path"), str) and isinstance(displaced.get("copy"), str))):
        return False
    embedded = manifest.get("embedded") or {}
    if not isinstance(embedded, dict):
        return False
    for entries in embedded.values():
        if not isinstance(entries, list):
            return False
        for e in entries:
            if not (isinstance(e, dict) and _SHA256.fullmatch(str(e.get("sha256")))
                    and (e.get("type") is None or isinstance(e.get("type"), int))
                    and isinstance(e.get("desc") or "", str)):
                return False
    return True


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: str) -> str:
    with open(path, "rb") as fh:
        return _sha(fh.read())


def _embedded_images(item) -> List[Any]:
    from mediafile import MediaFile
    return list(MediaFile(_fspath(item.path)).images or [])


def _snapshot_embedded(items, folder: str) -> Dict[str, List[Dict[str, Any]]]:
    """Each item's embedded images, the bytes stored once per hash."""
    os.makedirs(os.path.join(folder, "embedded"), exist_ok=True)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        entries = []
        for img in _embedded_images(item):
            digest = _sha(img.data)
            blob = os.path.join(folder, "embedded", digest)
            if not os.path.exists(blob):
                with open(blob, "wb") as fh:
                    fh.write(img.data)
            kind = getattr(img, "type", None)
            entries.append({"sha256": digest, "desc": img.desc or "",
                            "type": None if kind is None else int(getattr(kind, "value", kind))})
        out[str(item.id)] = entries
    return out


def _restore_embedded(lib, album_id: int, saved: Dict[str, List[Dict[str, Any]]], folder: str) -> int:
    """Write back each item's snapshot images where they differ now, through
    Beets' own Item.write (the call embedart itself uses)."""
    from mediafile import Image
    restored = 0
    for item in lib.items(f"album_id:{int(album_id)}"):
        entries = saved.get(str(item.id))
        if entries is None:
            continue
        if [_sha(i.data) for i in _embedded_images(item)] == [e["sha256"] for e in entries]:
            continue
        images = []
        for e in entries:
            if not _SHA256.fullmatch(str(e.get("sha256"))):
                raise ValueError("bad manifest entry")
            with open(os.path.join(folder, "embedded", e["sha256"]), "rb") as fh:
                images.append(Image(data=fh.read(), desc=e.get("desc") or "", type=e.get("type")))
        item.write(tags={"images": images})
        restored += 1
    return restored


def _write_manifest(folder: str, manifest: Dict[str, Any]) -> None:
    tmp = os.path.join(folder, MANIFEST_NAME + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    os.replace(tmp, os.path.join(folder, MANIFEST_NAME))


def _restore(lib, album, manifest: Dict[str, Any], folder: str) -> Dict[str, Any]:
    """Put the previous cover file, artpath and embedded images back."""
    old_artpath = _fspath(manifest.get("old_artpath"))
    previous = _fspath(manifest.get("previous_cover"))
    current = _fspath(album.artpath)
    if current and os.path.isfile(current):
        replaced_dir = os.path.join(folder, "replaced")
        os.makedirs(replaced_dir, exist_ok=True)
        shutil.move(current, os.path.join(replaced_dir, os.path.basename(current)))
    if old_artpath and previous and not os.path.exists(old_artpath):
        os.makedirs(os.path.dirname(old_artpath), exist_ok=True)
        shutil.copy2(previous, old_artpath)
    displaced = manifest.get("displaced") or {}
    if displaced.get("path") and not os.path.exists(displaced["path"]):
        shutil.copy2(displaced["copy"], displaced["path"])
    album.artpath = os.fsencode(old_artpath) if old_artpath else None
    album.store()
    embedded = _restore_embedded(lib, album.id, manifest.get("embedded") or {}, folder)
    return {"restored_artpath": old_artpath, "restored_embedded_items": embedded}


def _album_check(lib, data: Dict[str, Any]):
    """(album, None) or (None, (message, code, status))."""
    try:
        album_id = int(data.get("album_id"))
    except (TypeError, ValueError):
        return None, ("album_id must be an integer", "INVALID_ALBUM_ID", 400)
    album = lib.get_album(album_id)
    if album is None:
        return None, ("album not found", "ALBUM_NOT_FOUND", 404)
    expected = str(data.get("expected_mb_releasegroupid") or "").strip().lower()
    if expected and str(album.get("mb_releasegroupid") or "").strip().lower() != expected:
        return None, ("the album's release group changed since the request was made",
                      "IDENTITY_CHANGED", 409)
    return album, None


@ops.webmanager_bp.route("/album-art", methods=["POST"])
def run_set_album_art():
    # Checked before the body is read: a chunked request has no length.
    if request.content_length is None:
        return _error("Content-Length is required", "LENGTH_REQUIRED", 411)
    if request.content_length > MAX_REQUEST_BYTES:
        return _error("image is larger than 15 MB", "IMAGE_TOO_LARGE", 413)
    # One lock over check-then-write, so nothing changes the album between
    # the path checks and set_art (RLock: the inner holds nest).
    with ops.mutation_lock:
        return _set_album_art()


def _set_album_art():
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    raw = data.get("image_b64")
    digest = str(data.get("image_sha256") or "")
    if not isinstance(raw, str) or not _SHA256.fullmatch(digest):
        return _error("image_b64 and image_sha256 are required", "INVALID_REQUEST")
    try:
        image = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return _error("image_b64 is not valid base64", "INVALID_IMAGE")
    if len(image) > MAX_IMAGE_BYTES:
        return _error("image is larger than 15 MB", "IMAGE_TOO_LARGE", 413)
    ext = image_extension(image)
    if not ext or not image_size_ok(image):
        return _error("image must be JPEG, PNG or WebP, at most 12000 pixels a side", "INVALID_IMAGE")
    if _sha(image) != digest:
        return _error("image_sha256 does not match the image", "IMAGE_HASH_MISMATCH")

    op_id, _fingerprint, early = ops._idempotency_precheck("album_art_set", data)
    if early is not None:
        return early

    def fail(message: str, code: str, status: int = 400):
        ops.update_operation(op_id, "failed", error=message, error_code=code)
        return _error(message, code, status)

    album, err = _album_check(lib, data)
    if err is not None:
        return fail(*err)
    old_artpath = _fspath(album.artpath)
    items = list(album.items())
    if not items:
        return fail("the album has no items, so Beets has no folder for its art", "ALBUM_EMPTY")
    try:
        destination = _fspath(album.art_destination(os.fsencode("cover" + ext)))
    except Exception:
        return fail("Beets could not work out the cover path", "ART_DESTINATION_FAILED")
    if not _inside_allowed(destination):
        return fail("the cover path is outside the allowed roots", "DESTINATION_PATH_INVALID")
    if old_artpath and not _inside_allowed(old_artpath):
        return fail("the current cover is outside the allowed roots", "OLD_ART_PATH_INVALID")

    art_id = uuid.uuid4().hex
    folder = os.path.join(_quarantine_root(), art_id)
    manifest: Dict[str, Any] = {"kind": MANIFEST_KIND, "album_id": album.id, "status": "applying",
                                "old_artpath": old_artpath, "previous_cover": "", "image_sha256": digest}
    try:
        with ops.mutation_lock:
            os.makedirs(folder)
            new_image = os.path.join(folder, "new" + ext)
            with open(new_image, "wb") as fh:
                fh.write(image)
            if old_artpath and os.path.isfile(old_artpath):
                manifest["previous_cover"] = os.path.join(
                    folder, "previous" + os.path.splitext(old_artpath)[1])
                shutil.copy2(old_artpath, manifest["previous_cover"])
            # set_art also removes whatever file holds the destination name.
            if os.path.isfile(destination) and os.path.normpath(destination) != os.path.normpath(old_artpath or "."):
                manifest["displaced"] = {"path": destination, "copy": os.path.join(folder, "displaced" + ext)}
                shutil.copy2(destination, manifest["displaced"]["copy"])
            manifest["embedded"] = _snapshot_embedded(items, folder)
            _write_manifest(folder, manifest)

            # Beets' own art path: copy into place, set artpath, send
            # art_set (embedart embeds from it when it is loaded).
            album.set_art(os.fsencode(new_image), copy=True)
            album.store()
            album.load()
            new_artpath = _fspath(album.artpath)
            changed = sum(
                1 for item in lib.items(f"album_id:{album.id}")
                if [_sha(i.data) for i in _embedded_images(item)]
                != [e["sha256"] for e in manifest["embedded"].get(str(item.id), [])])
            manifest.update(status="applied", new_artpath=new_artpath)
            _write_manifest(folder, manifest)
        result = {"success": True, "album_id": album.id, "art_id": art_id, "artpath": new_artpath,
                  "old_artpath": old_artpath, "embedded_items": changed, "item_count": len(items)}
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("album-art failed; restoring")
        try:
            with ops.mutation_lock:
                album.load()
                if os.path.exists(os.path.join(folder, MANIFEST_NAME)):
                    # A copy set_art made before failing, not yet in artpath.
                    if (os.path.normpath(destination) != os.path.normpath(_fspath(album.artpath) or ".")
                            and os.path.isfile(destination) and _sha_file(destination) == digest):
                        os.makedirs(os.path.join(folder, "replaced"), exist_ok=True)
                        shutil.move(destination, os.path.join(folder, "replaced", os.path.basename(destination)))
                    _restore(lib, album, manifest, folder)
                    manifest["status"] = "compensated"
                    _write_manifest(folder, manifest)
        except Exception:
            ops.log.exception("album-art restore failed")
        return fail("Setting album art failed", "ALBUM_ART_FAILED", 500)


@ops.webmanager_bp.route("/album-art/rollback", methods=["POST"])
def run_album_art_rollback():
    with ops.mutation_lock:
        return _album_art_rollback()


def _album_art_rollback():
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    folder = _manifest_dir(data.get("art_id"))
    if folder is None:
        return _error("art_id is not a valid engine id", "INVALID_ART_ID")
    # Replay check first, without registering: after a rollback the manifest
    # is no longer "applied", so a retry must replay the stored result. A
    # precondition refusal below is NOT recorded under the key: Web Manager
    # retries with the same fixed key ("<txn>:rollback") once the cause is
    # gone (e.g. ART_CHANGED after a later change was rolled back), and a
    # stored refusal would be replayed for the registry's lifetime.
    # The key is registered only right before the restore (mutation_lock is
    # held by the caller, so nothing can register it in between).
    _op_id, _fingerprint, early = ops._idempotency_precheck("album_art_rollback", data, register=False)
    if early is not None:
        return early
    fail = _error

    try:
        with open(os.path.join(folder, MANIFEST_NAME), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return fail("no engine album-art record for this id", "ART_RECORD_NOT_FOUND", 404)
    if not isinstance(manifest, dict) or manifest.get("kind") != MANIFEST_KIND \
            or manifest.get("status") != "applied":
        return fail("no applied album-art change for this id", "ART_RECORD_NOT_FOUND", 404)
    if not _manifest_ok(manifest):
        return fail("the engine album-art record is malformed", "SNAPSHOT_PATH_INVALID")
    album = lib.get_album(manifest.get("album_id"))
    if album is None:
        return fail("album not found", "ALBUM_NOT_FOUND", 404)
    old_artpath = _fspath(manifest.get("old_artpath"))
    previous = _fspath(manifest.get("previous_cover"))
    if old_artpath and not _inside_allowed(old_artpath):
        return fail("recorded path is outside allowed roots", "SNAPSHOT_PATH_INVALID")
    displaced = manifest.get("displaced") or {}
    copies = [previous] if previous else []
    if displaced:
        copies.append(_fspath(displaced.get("copy")))
        if not _inside_allowed(_fspath(displaced.get("path"))):
            return fail("recorded path is outside allowed roots", "SNAPSHOT_PATH_INVALID")
    real_folder = os.path.realpath(folder)
    if any(not os.path.realpath(c).startswith(real_folder + os.sep) for c in copies):
        return fail("recorded path is outside the engine folder", "SNAPSHOT_PATH_INVALID")
    current = _fspath(album.artpath)
    if os.path.normpath(current or ".") != os.path.normpath(_fspath(manifest.get("new_artpath")) or "."):
        return fail("the album's art changed since; not rolling back", "ART_CHANGED", 409)
    if current and not _inside_allowed(current):
        return fail("current cover is outside allowed roots", "SNAPSHOT_PATH_INVALID")
    if old_artpath and os.path.exists(old_artpath) and os.path.normpath(old_artpath) != os.path.normpath(current or "."):
        return fail("the previous cover's path is occupied", "OLD_ART_PATH_OCCUPIED", 409)
    op_id, _fingerprint, early = ops._idempotency_precheck("album_art_rollback", data)
    if early is not None:
        return early
    try:
        with ops.mutation_lock:
            restored = _restore(lib, album, manifest, folder)
            manifest["status"] = "rolled_back"
            _write_manifest(folder, manifest)
        result = {"success": True, "album_id": album.id, **restored}
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("album-art rollback failed")
        ops.update_operation(op_id, "failed", error="Rollback failed", error_code="ROLLBACK_FAILED")
        return _error("Rollback failed", "ROLLBACK_FAILED", 500)
