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
from . import plugin_ops
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
#: The manifest kind of a fetched (not supplied) cover: same shape, so the
#: same rollback restores it.
MANIFEST_KIND_FETCH = "album_art_fetch"
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


def _displaced_entries(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The files the apply displaced, in either manifest shape (a single
    dict from older records, a list from current ones)."""
    displaced = manifest.get("displaced") or []
    if isinstance(displaced, dict):
        return [displaced] if displaced else []
    if isinstance(displaced, list):
        return [d for d in displaced if isinstance(d, dict)]
    return []


def _manifest_ok(manifest: Dict[str, Any]) -> bool:
    """Every field rollback will use, checked before anything is written."""
    if not isinstance(manifest.get("album_id"), int):
        return False
    for key in ("old_artpath", "previous_cover", "new_artpath"):
        if not isinstance(manifest.get(key) or "", str):
            return False
    displaced = manifest.get("displaced") or []
    if isinstance(displaced, dict):
        displaced = [displaced] if displaced else []
    if not isinstance(displaced, list):
        return False
    for entry in displaced:
        if not (isinstance(entry, dict) and isinstance(entry.get("path"), str)
                and isinstance(entry.get("copy"), str)):
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
    # H7: the post-apply hashes are optional (older manifests have none),
    # but when present they must be usable.
    cover_sha = manifest.get("cover_sha256")
    if cover_sha not in (None, "") and not _SHA256.fullmatch(str(cover_sha)):
        return False
    embedded_after = manifest.get("embedded_after")
    if embedded_after is not None and not isinstance(embedded_after, dict):
        return False
    for hashes in (embedded_after or {}).values():
        if not isinstance(hashes, list) or any(not _SHA256.fullmatch(str(h)) for h in hashes):
            return False
    return True


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: str) -> str:
    with open(path, "rb") as fh:
        return _sha(fh.read())


def _inside_folder(path: str, folder: str) -> bool:
    """Resolved containment (a ``..`` or symlink cannot escape the folder)."""
    try:
        root = os.path.realpath(folder)
        return os.path.commonpath([os.path.realpath(path), root]) == root
    except (OSError, ValueError):
        return False


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


def _embedded_hashes(lib, album_id: int) -> Dict[str, List[str]]:
    """Every item's embedded-image hashes right after the apply (H7)."""
    return {str(item.id): [_sha(img.data) for img in _embedded_images(item)]
            for item in lib.items(f"album_id:{int(album_id)}")}


def _art_changed(manifest: Dict[str, Any], album, lib) -> Optional[str]:
    """Why the art changed after the apply, or None.

    New manifests record the cover's bytes and every item's embedded-image
    hashes after the apply, so replacing a file with different bytes under
    the same name is caught (H7). Older manifests only have paths; that
    weaker check is kept for them."""
    current = _fspath(album.artpath)
    expected = _fspath(manifest.get("new_artpath"))
    if expected and not _inside_allowed(expected):
        return "the cover path in the engine record is outside the allowed roots"
    if os.path.normpath(current or ".") != os.path.normpath(expected or "."):
        return "the album's artpath changed since the apply"
    cover_sha = str(manifest.get("cover_sha256") or "")
    if cover_sha:
        if not expected:
            return "the record claims a cover hash but no cover path"
        if not os.path.isfile(expected):
            return "the cover file the apply wrote is gone"
        if _sha_file(expected) != cover_sha:
            return "the cover file's content changed since the apply"
    after = manifest.get("embedded_after")
    if isinstance(after, dict):
        for iid, hashes in after.items():
            if not str(iid).isdigit():
                return "the record's item id is malformed"
            item = lib.get_item(int(iid))
            if item is None:
                return f"item {iid} is gone since the apply"
            live = [_sha(img.data) for img in _embedded_images(item)]
            if live != [str(h) for h in hashes]:
                return f"item {iid}'s embedded art changed since the apply"
    return None


def _art_destination_candidates(album) -> List[str]:
    """Every path fetchart may overwrite: the configured art filename with
    each extension fetchart can choose (it is not always ``cover.jpg``)."""
    out: List[str] = []
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        try:
            path = _fspath(album.art_destination(os.fsencode("image" + ext)))
        except Exception:
            continue
        if path and path not in out:
            out.append(path)
    return out


def _restore(lib, album, manifest: Dict[str, Any], folder: str) -> Dict[str, Any]:
    """Put the previous cover file, artpath and embedded images back, then
    prove the live library and the files agree with the manifest (H7/H10)."""
    old_artpath = _fspath(manifest.get("old_artpath"))
    previous = _fspath(manifest.get("previous_cover"))
    current = _fspath(album.artpath)
    same_as_old = bool(
        current and old_artpath
        and os.path.normcase(os.path.abspath(current))
        == os.path.normcase(os.path.abspath(old_artpath)))
    if current and os.path.isfile(current) and (not same_as_old or previous):
        replaced_dir = os.path.join(folder, "replaced")
        os.makedirs(replaced_dir, exist_ok=True)
        shutil.move(current, os.path.join(replaced_dir, os.path.basename(current)))
    if old_artpath and previous and not os.path.exists(old_artpath):
        os.makedirs(os.path.dirname(old_artpath), exist_ok=True)
        shutil.copy2(previous, old_artpath)
    for displaced in _displaced_entries(manifest):
        path = _fspath(displaced.get("path"))
        copy = _fspath(displaced.get("copy"))
        if path and copy and not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copy2(copy, path)
    album.artpath = os.fsencode(old_artpath) if old_artpath else None
    album.store()
    album.load()
    if _fspath(album.artpath) != old_artpath:
        raise OSError("the album's artpath did not return to its old value")
    if old_artpath:
        if not os.path.isfile(old_artpath):
            raise OSError("the restored cover file is missing")
        if previous and _sha_file(previous) != _sha_file(old_artpath):
            raise OSError("the restored cover file differs from its quarantined copy")
    embedded = _restore_embedded(lib, album.id, manifest.get("embedded") or {}, folder)
    for iid, entries in (manifest.get("embedded") or {}).items():
        item = lib.get_item(int(iid)) if str(iid).isdigit() else None
        if item is None:
            raise OSError(f"item {iid} is gone; its embedded art cannot be restored")
        if [_sha(img.data) for img in _embedded_images(item)] != [e["sha256"] for e in entries]:
            raise OSError(f"item {iid}'s embedded art did not return to its old value")
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
            # What the apply actually wrote, by content: rollback refuses a
            # same-named change instead of trusting the pathname (H7).
            if new_artpath and os.path.isfile(new_artpath):
                manifest["cover_sha256"] = _sha_file(new_artpath)
            manifest["embedded_after"] = _embedded_hashes(lib, album.id)
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


@ops.webmanager_bp.route("/album-art/fetch", methods=["POST"])
def run_fetch_album_art():
    """Fetch cover art for one album and embed it into its tracks, with a
    manifest that puts the previous cover and embedded images back.

    Beets' own fetchart plugin fetches and Beets' own embedart plugin
    embeds; this module only snapshots what they are about to replace, so
    the change can be undone. The Web Manager decides *whether* to fetch
    (the operator's request); this module only performs it inside Beets.
    """
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    album, err = _album_check(lib, data)
    if err is not None:
        return _error(*err)
    items = list(album.items())
    if not items:
        return _error("the album has no items, so Beets has no folder for its art", "ALBUM_EMPTY")
    if not plugin_ops.is_capability_available("fetchart"):
        return _error("fetchart plugin is not loaded or configured on this Beets server",
                      "CAPABILITY_UNAVAILABLE", 409)
    # H8: this op always embeds after fetching. Refuse up front rather than
    # letting fetchart write a cover and then discovering embedart is absent.
    if not plugin_ops.is_capability_available("embedart"):
        return _error("embedart plugin is not loaded on this Beets server; fetch-and-embed "
                      "cannot run", "CAPABILITY_UNAVAILABLE", 409)
    op_id, _fingerprint, early = ops._idempotency_precheck("album_art_fetch", data)
    if early is not None:
        return early

    def fail(message: str, code: str, status: int = 400):
        ops.update_operation(op_id, "failed", error=message, error_code=code)
        return _error(message, code, status)

    old_artpath = _fspath(album.artpath)
    if old_artpath and not _inside_allowed(old_artpath):
        return fail("the current cover is outside the allowed roots", "OLD_ART_PATH_INVALID")
    # fetchart may choose any of these extensions, not only cover.jpg. Every
    # one that exists is displaced by the fetch, so every one is snapshotted.
    destinations = _art_destination_candidates(album)
    if any(not _inside_allowed(path) for path in destinations):
        return fail("the cover path is outside the allowed roots", "DESTINATION_PATH_INVALID")

    art_id = uuid.uuid4().hex
    folder = os.path.join(_quarantine_root(), art_id)
    manifest: Dict[str, Any] = {"kind": MANIFEST_KIND_FETCH, "album_id": album.id, "status": "applying",
                                "old_artpath": old_artpath, "previous_cover": "", "new_artpath": "",
                                "embedded": {}}

    def compensate() -> None:
        """Undo whatever fetchart/embedart already wrote (H8)."""
        try:
            with ops.mutation_lock:
                album.load()
                if os.path.exists(os.path.join(folder, MANIFEST_NAME)):
                    _restore(lib, album, manifest, folder)
                    manifest["status"] = "compensated"
                    _write_manifest(folder, manifest)
        except Exception:
            ops.log.exception("album-art fetch restore failed")

    try:
        with ops.mutation_lock:
            os.makedirs(folder)
            if old_artpath and os.path.isfile(old_artpath):
                manifest["previous_cover"] = os.path.join(
                    folder, "previous" + os.path.splitext(old_artpath)[1])
                shutil.copy2(old_artpath, manifest["previous_cover"])
            # The cover files fetchart is about to write over, if they are
            # not the current cover: kept so the rollback can put them back.
            displaced = []
            for candidate in destinations:
                if candidate and os.path.isfile(candidate) \
                        and os.path.normpath(candidate) != os.path.normpath(old_artpath or "."):
                    entry = {"path": candidate,
                             "copy": os.path.join(
                                 folder, f"displaced{len(displaced)}" + os.path.splitext(candidate)[1])}
                    shutil.copy2(candidate, entry["copy"])
                    displaced.append(entry)
            if displaced:
                manifest["displaced"] = displaced
            manifest["embedded"] = _snapshot_embedded(items, folder)
            _write_manifest(folder, manifest)

            # Force: the operator explicitly asked to fetch and embed. Without
            # it fetchart skips an album that already has a cover file, so the
            # snapshot/rollback below could never replace one -- and the
            # request would report success while doing nothing.
            plugin_ops.run_fetchart(lib, [album.id], True)
            plugin_ops.run_embedart(lib, [album.id])
            album.load()
            new_artpath = _fspath(album.artpath)
            changed = sum(
                1 for item in lib.items(f"album_id:{album.id}")
                if [_sha(i.data) for i in _embedded_images(item)]
                != [e["sha256"] for e in manifest["embedded"].get(str(item.id), [])])
            manifest.update(status="applied", new_artpath=new_artpath)
            if new_artpath and os.path.isfile(new_artpath):
                manifest["cover_sha256"] = _sha_file(new_artpath)
            manifest["embedded_after"] = _embedded_hashes(lib, album.id)
            _write_manifest(folder, manifest)
        result = {"success": True, "album_id": album.id, "art_id": art_id, "artpath": new_artpath,
                  "old_artpath": old_artpath, "embedded_items": changed, "item_count": len(items)}
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except plugin_ops.PluginCapabilityError as ex:
        ops.log.warning("album-art fetch hit a missing plugin: %s", ex)
        compensate()
        return fail("fetchart or embedart plugin is not loaded or configured on this Beets server",
                    "CAPABILITY_UNAVAILABLE", 409)
    except plugin_ops.PluginIncompatibleError as ex:
        ops.log.warning("album-art fetch failed: %s", ex)
        compensate()
        return fail("fetchart or embedart plugin is not usable on this Beets version",
                    "CAPABILITY_UNAVAILABLE", 409)
    except Exception:
        ops.log.exception("album-art fetch failed; restoring")
        compensate()
        return fail("Fetching album art failed", "ALBUM_ART_FAILED", 500)


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
    if not isinstance(manifest, dict) or manifest.get("kind") not in (MANIFEST_KIND, MANIFEST_KIND_FETCH) \
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
    displaced = _displaced_entries(manifest)
    copies = [previous] if previous else []
    for entry in displaced:
        if not _inside_allowed(_fspath(entry.get("path"))):
            return fail("recorded path is outside allowed roots", "SNAPSHOT_PATH_INVALID")
        copies.append(_fspath(entry.get("copy")))
    if any(copy and not _inside_folder(copy, folder) for copy in copies):
        return fail("recorded path is outside the engine folder", "SNAPSHOT_PATH_INVALID")
    # Prove the saved copies exist before _restore moves the current cover.
    # A missing snapshot must be a clean refusal, not a half-done rollback.
    if previous and not os.path.isfile(previous):
        return fail("the saved previous cover is missing", "SNAPSHOT_PATH_INVALID", 409)
    for entry in displaced:
        copy = _fspath(entry.get("copy"))
        if copy and not os.path.isfile(copy):
            return fail("a saved displaced cover is missing", "SNAPSHOT_PATH_INVALID", 409)
    current = _fspath(album.artpath)
    if old_artpath and not previous and not os.path.isfile(old_artpath):
        return fail("the record has no saved copy of the previous cover", "SNAPSHOT_PATH_INVALID", 409)
    changed = _art_changed(manifest, album, lib)
    if changed:
        return fail(f"the album's art changed since; not rolling back ({changed})",
                    "ART_CHANGED", 409)
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


@ops.webmanager_bp.route("/album-art/remove", methods=["POST"])
def run_remove_album_art():
    """Remove the album's cover file (D10): the file is kept in the engine's
    quarantine folder, artpath is cleared, and /album-art/rollback puts both
    back. Embedded art is not touched."""
    with ops.mutation_lock:
        return _remove_album_art()


def _remove_album_art():
    data = request.get_json(force=True, silent=True) or {}
    lib = g.lib
    op_id, _fingerprint, early = ops._idempotency_precheck("album_art_remove", data)
    if early is not None:
        return early

    def fail(message: str, code: str, status: int = 400):
        ops.update_operation(op_id, "failed", error=message, error_code=code)
        return _error(message, code, status)

    album, err = _album_check(lib, data)
    if err is not None:
        return fail(*err)
    old_artpath = _fspath(album.artpath)
    if not old_artpath:
        return fail("the album has no cover file", "NO_ART", 409)
    if not _inside_allowed(old_artpath) or os.path.islink(old_artpath):
        return fail("the current cover is outside the allowed roots", "OLD_ART_PATH_INVALID")
    art_id = uuid.uuid4().hex
    folder = os.path.join(_quarantine_root(), art_id)
    manifest: Dict[str, Any] = {"kind": MANIFEST_KIND, "album_id": album.id, "status": "applying", "remove": True,
                                "old_artpath": old_artpath, "previous_cover": "", "new_artpath": "", "embedded": {}}
    try:
        os.makedirs(folder)
        on_disk = os.path.isfile(old_artpath)
        if on_disk:
            # Kept until the copy is proven identical (never delete the only copy).
            manifest["previous_cover"] = os.path.join(folder, "previous" + os.path.splitext(old_artpath)[1])
            shutil.copy2(old_artpath, manifest["previous_cover"])
            if _sha_file(manifest["previous_cover"]) != _sha_file(old_artpath):
                raise OSError("quarantine copy differs from the cover")
        _write_manifest(folder, manifest)
        album.artpath = None
        album.store()
        if on_disk:
            os.remove(old_artpath)
        manifest["status"] = "applied"
        _write_manifest(folder, manifest)
        removed = [old_artpath] if on_disk else []
        result = {"success": True, "album_id": album.id, "art_id": art_id, "artpath": "",
                  "old_artpath": old_artpath, "removed": removed, "removed_count": len(removed),
                  "embedded_items": 0, "item_count": len(list(album.items()))}
        ops.update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        ops.log.exception("album-art remove failed; restoring")
        try:
            if os.path.exists(os.path.join(folder, MANIFEST_NAME)):
                album.load()
                _restore(lib, album, manifest, folder)
                manifest["status"] = "compensated"
                _write_manifest(folder, manifest)
        except Exception:
            ops.log.exception("album-art remove restore failed")
        return fail("Removing album art failed", "ALBUM_ART_FAILED", 500)
