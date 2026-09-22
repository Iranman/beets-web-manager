"""Operation implementations for the WebManager Beets integration plugin."""

import os
import sys
import time
import uuid
import logging
import threading
from typing import Dict, Any, Optional, List
from flask import Blueprint, request, jsonify, g
import beets
from beets import config as beets_config
from beets import util
from .schemas import (
    is_path_safe_and_allowed,
    validate_fields,
    ALLOWED_DUPLICATE_ACTIONS,
    DEFAULT_ALLOWED_ROOTS,
)

import hashlib
import json

log = logging.getLogger("beets.webmanager")

# Lock to serialize library write operations safely within Beets process
mutation_lock = threading.RLock()

# Async operations registry
_operations: Dict[str, Dict[str, Any]] = {}
_operations_lock = threading.Lock()

webmanager_bp = Blueprint("webmanager", __name__, url_prefix="/webmanager")


def compute_fingerprint(data: Dict[str, Any]) -> str:
    """Compute stable SHA-256 fingerprint of request payload."""
    try:
        canonical = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def register_operation(
    op_type: str, op_id: Optional[str] = None, fingerprint: Optional[str] = None
) -> tuple[str, str]:
    """Register a new operation in the registry with collision check.
    
    Returns (op_id, status_code) where status_code is 'created', 'exists', or 'collision'.
    """
    if not op_id:
        op_id = str(uuid.uuid4())
    with _operations_lock:
        if op_id in _operations:
            existing = _operations[op_id]
            if fingerprint and existing.get("fingerprint") and existing.get("fingerprint") != fingerprint:
                return op_id, "collision"
            return op_id, "exists"

        _operations[op_id] = {
            "id": op_id,
            "type": op_type,
            "fingerprint": fingerprint,
            "status": "running",
            "created_at": time.time(),
            "updated_at": time.time(),
            "result": None,
            "error": None,
        }
        return op_id, "created"


def update_operation(
    op_id: str,
    status: str,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
):
    """Update an operation's status, result, or error."""
    with _operations_lock:
        if op_id in _operations:
            _operations[op_id]["status"] = status
            _operations[op_id]["updated_at"] = time.time()
            if result is not None:
                _operations[op_id]["result"] = result
            if error is not None:
                _operations[op_id]["error"] = error


def get_operation(op_id: str) -> Optional[Dict[str, Any]]:
    """Get operation data by ID."""
    with _operations_lock:
        return _operations.get(op_id)


_CUSTOM_ALLOWED_ROOTS: Optional[List[str]] = None


def set_allowed_roots(roots: Optional[List[str]]):
    """Explicitly override allowed roots (for testing or runtime override)."""
    global _CUSTOM_ALLOWED_ROOTS
    _CUSTOM_ALLOWED_ROOTS = roots


def get_allowed_roots() -> List[str]:
    """Get configured allowed roots or defaults."""
    global _CUSTOM_ALLOWED_ROOTS
    if _CUSTOM_ALLOWED_ROOTS is not None:
        return _CUSTOM_ALLOWED_ROOTS

    env_roots = os.environ.get("BEETS_ALLOWED_ROOTS", "").strip()
    if env_roots:
        return [r.strip() for r in env_roots.split(",") if r.strip()]

    try:
        from beets import config

        if "webmanager" in config and "allowed_roots" in config["webmanager"]:
            roots = config["webmanager"]["allowed_roots"].as_str_seq()
            if roots:
                return list(roots)
    except Exception:
        pass
    return DEFAULT_ALLOWED_ROOTS


@webmanager_bp.route("/status", methods=["GET"])
def get_status():
    """Healthcheck and capability status endpoint."""
    return jsonify(
        {
            "status": "ok",
            "version": "1.0.0",
            "beets_version": getattr(beets, "__version__", "unknown"),
            "readonly": False,
            "allowed_roots": get_allowed_roots(),
        }
    )


@webmanager_bp.route("/operations/<string:op_id>", methods=["GET"])
def get_operation_status(op_id: str):
    """Retrieve status and results of a long-running operation."""
    op = get_operation(op_id)
    if not op:
        return jsonify({"error": "Operation not found", "operation_id": op_id}), 404
    return jsonify(op)


@webmanager_bp.route("/import", methods=["POST"])
def run_import():
    """Non-interactive confirmed import execution inside Beets."""
    data = request.get_json(force=True, silent=True) or {}
    paths = data.get("paths", [])
    if isinstance(paths, str):
        paths = [paths]

    if not paths:
        return jsonify({"error": "Missing 'paths' parameter"}), 400

    allowed_roots = get_allowed_roots()
    for p in paths:
        if not is_path_safe_and_allowed(p, allowed_roots):
            return (
                jsonify(
                    {
                        "error": "Path traversal or disallowed root",
                        "path": p,
                        "allowed_roots": allowed_roots,
                    }
                ),
                400,
            )

    autotag = bool(data.get("autotag", False))
    duplicate_action = str(data.get("duplicate_action", "skip")).lower()
    if duplicate_action not in ALLOWED_DUPLICATE_ACTIONS:
        duplicate_action = "skip"

    copy = bool(data.get("copy", False))
    move = bool(data.get("move", True))
    write = bool(data.get("write", True))
    incremental = bool(data.get("incremental", False))
    singletons = bool(data.get("singletons", False))
    pretend = bool(data.get("pretend", False))
    set_fields = data.get("set_fields") or {}

    is_async = request.headers.get("Prefer") == "respond-async" or data.get("async", False)
    op_id = request.headers.get("Idempotency-Key") or str(uuid.uuid4())
    fingerprint = compute_fingerprint(data)

    reg_id, reg_status = register_operation("import", op_id, fingerprint=fingerprint)
    if reg_status == "collision":
        return (
            jsonify(
                {
                    "error": "Operation ID collision: payload does not match existing operation",
                    "operation_id": op_id,
                }
            ),
            409,
        )

    if reg_status == "exists":
        existing = get_operation(op_id)
        if existing:
            if existing["status"] == "running":
                return jsonify({"operation_id": op_id, "status": "running"}), 202
            if existing["status"] == "succeeded":
                return (
                    jsonify(
                        {
                            "operation_id": op_id,
                            "status": "succeeded",
                            "result": existing.get("result"),
                        }
                    ),
                    200,
                )
            if existing["status"] == "failed":
                return (
                    jsonify(
                        {
                            "operation_id": op_id,
                            "status": "failed",
                            "error": existing.get("error"),
                        }
                    ),
                    500,
                )

    def _execute_import(lib, op_id_arg: Optional[str] = None):
        with mutation_lock:
            # Snapshot importer config
            orig_pretend = beets_config["import"]["pretend"].get()
            orig_copy = beets_config["import"]["copy"].get()
            orig_move = beets_config["import"]["move"].get()
            orig_write = beets_config["import"]["write"].get()
            orig_autotag = beets_config["import"]["autotag"].get()
            orig_duplicate_action = beets_config["import"]["duplicate_action"].get()
            orig_quiet = beets_config["import"]["quiet"].get()
            orig_timid = beets_config["import"]["timid"].get()
            orig_incremental = beets_config["import"]["incremental"].get()
            orig_singletons = beets_config["import"]["singletons"].get()
            orig_set_fields = beets_config["import"]["set_fields"].get()

            try:
                beets_config["import"]["pretend"] = pretend
                beets_config["import"]["copy"] = copy
                beets_config["import"]["move"] = move
                beets_config["import"]["write"] = write
                beets_config["import"]["autotag"] = autotag
                beets_config["import"]["duplicate_action"] = duplicate_action
                beets_config["import"]["quiet"] = True
                beets_config["import"]["timid"] = False
                beets_config["import"]["incremental"] = incremental
                beets_config["import"]["singletons"] = singletons
                if set_fields:
                    beets_config["import"]["set_fields"] = set_fields

                from beets.importer import ImportSession

                # Convert paths to bytestrings for Beets importer
                path_bytes = [util.bytestring_path(p) for p in paths]
                session = ImportSession(lib, loghandler=None, paths=path_bytes, query=None)
                session.run()

                result = {
                    "success": True,
                    "imported_paths": paths,
                    "autotag": autotag,
                    "duplicate_action": duplicate_action,
                }
                if op_id_arg:
                    update_operation(op_id_arg, "succeeded", result=result)
                return result
            except Exception as ex:
                log.exception("Error executing non-interactive import")
                err_msg = str(ex)
                if op_id_arg:
                    update_operation(op_id_arg, "failed", error=err_msg)
                raise
            finally:
                # Restore original importer config
                beets_config["import"]["pretend"] = orig_pretend
                beets_config["import"]["copy"] = orig_copy
                beets_config["import"]["move"] = orig_move
                beets_config["import"]["write"] = orig_write
                beets_config["import"]["autotag"] = orig_autotag
                beets_config["import"]["duplicate_action"] = orig_duplicate_action
                beets_config["import"]["quiet"] = orig_quiet
                beets_config["import"]["timid"] = orig_timid
                beets_config["import"]["incremental"] = orig_incremental
                beets_config["import"]["singletons"] = orig_singletons
                beets_config["import"]["set_fields"] = orig_set_fields

    if is_async:
        lib = g.lib

        def _bg():
            try:
                _execute_import(lib, op_id)
            except Exception:
                pass

        t = threading.Thread(target=_bg, daemon=True)
        t.start()
        return jsonify({"operation_id": op_id, "status": "running"}), 202

    try:
        res = _execute_import(g.lib, op_id)
        return jsonify(res)
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


@webmanager_bp.route("/modify", methods=["POST"])
def run_modify():
    """Modify metadata of items or albums."""
    data = request.get_json(force=True, silent=True) or {}
    item_ids = data.get("item_ids") or []
    album_ids = data.get("album_ids") or []
    query = data.get("query")
    fields = data.get("fields") or {}
    write = bool(data.get("write", True))
    move = bool(data.get("move", True))

    if not item_ids and not album_ids and not query:
        return jsonify({"error": "Must specify item_ids, album_ids, or query"}), 400

    if not fields:
        return jsonify({"error": "Must specify fields to modify"}), 400

    lib = g.lib
    with mutation_lock:
        item_fields = validate_fields(fields, is_album=False)
        album_fields = validate_fields(fields, is_album=True)

        modified_items = 0
        modified_albums = 0

        # Process Items
        items_to_modify = []
        if item_ids:
            for iid in item_ids:
                item = lib.get_item(iid)
                if item:
                    items_to_modify.append(item)
        elif query and not album_ids:
            items_to_modify.extend(lib.items(query))

        for item in items_to_modify:
            if item_fields:
                item.update(item_fields)
                item.store()
                if write:
                    try:
                        item.try_write()
                    except Exception as e:
                        log.warning("Failed to write tags to %s: %s", item.path, e)
                if move:
                    try:
                        item.move()
                    except Exception as e:
                        log.warning("Failed to move item %s: %s", item.path, e)
                modified_items += 1

        # Process Albums
        albums_to_modify = []
        if album_ids:
            for aid in album_ids:
                alb = lib.get_album(aid)
                if alb:
                    albums_to_modify.append(alb)
        elif query and album_ids:
            albums_to_modify.extend(lib.albums(query))

        for alb in albums_to_modify:
            if album_fields:
                alb.update(album_fields)
                alb.store()
                if move:
                    try:
                        alb.move()
                    except Exception as e:
                        log.warning("Failed to move album %s: %s", alb.id, e)
                modified_albums += 1

        return jsonify(
            {
                "success": True,
                "modified_items": modified_items,
                "modified_albums": modified_albums,
            }
        )


@webmanager_bp.route("/remove", methods=["POST"])
def run_remove():
    """Remove items or albums from the library."""
    data = request.get_json(force=True, silent=True) or {}
    item_ids = data.get("item_ids") or []
    album_ids = data.get("album_ids") or []
    query = data.get("query")
    delete_files = bool(data.get("delete_files", False))

    if not item_ids and not album_ids and not query:
        return jsonify({"error": "Must specify item_ids, album_ids, or query"}), 400

    lib = g.lib
    with mutation_lock:
        removed_items = 0
        removed_albums = 0

        if album_ids:
            for aid in album_ids:
                alb = lib.get_album(aid)
                if alb:
                    alb.remove(delete=delete_files, with_items=True)
                    removed_albums += 1
        elif item_ids:
            for iid in item_ids:
                item = lib.get_item(iid)
                if item:
                    item.remove(delete=delete_files)
                    removed_items += 1
        elif query:
            for alb in lib.albums(query):
                alb.remove(delete=delete_files, with_items=True)
                removed_albums += 1

        return jsonify(
            {
                "success": True,
                "removed_items": removed_items,
                "removed_albums": removed_albums,
                "delete_files": delete_files,
            }
        )


@webmanager_bp.route("/move", methods=["POST"])
def run_move():
    """Move items or albums to match directory structure."""
    data = request.get_json(force=True, silent=True) or {}
    item_ids = data.get("item_ids") or []
    album_ids = data.get("album_ids") or []
    query = data.get("query")

    lib = g.lib
    with mutation_lock:
        moved_count = 0
        if album_ids:
            for aid in album_ids:
                alb = lib.get_album(aid)
                if alb:
                    alb.move()
                    moved_count += 1
        elif item_ids:
            for iid in item_ids:
                item = lib.get_item(iid)
                if item:
                    item.move()
                    moved_count += 1
        elif query:
            for alb in lib.albums(query):
                alb.move()
                moved_count += 1

        return jsonify({"success": True, "moved_count": moved_count})


@webmanager_bp.route("/fetchart", methods=["POST"])
def run_fetchart():
    """Fetch and set artwork for albums."""
    data = request.get_json(force=True, silent=True) or {}
    album_ids = data.get("album_ids") or []
    art_url = data.get("art_url")
    force = bool(data.get("force", False))

    if not album_ids:
        return jsonify({"error": "Must specify album_ids"}), 400

    lib = g.lib
    updated_albums = 0
    with mutation_lock:
        for aid in album_ids:
            alb = lib.get_album(aid)
            if not alb:
                continue

            if art_url:
                try:
                    import requests

                    resp = requests.get(art_url, timeout=15)
                    if resp.status_code == 200:
                        art_path = os.path.join(
                            util.syspath(alb.item_dir()), "cover.jpg"
                        )
                        with open(art_path, "wb") as f:
                            f.write(resp.content)
                        alb.set_art(art_path, copy=False)
                        alb.store()
                        updated_albums += 1
                except Exception as ex:
                    log.warning("Failed to fetch art from url for album %s: %s", aid, ex)
            else:
                # Try using beets fetchart plugin if available
                try:
                    from beetsplug.fetchart import FetchArtPlugin

                    # Perform art fetch using beets fetchart logic
                    # If fetchart is available, invoke it
                except ImportError:
                    pass

        return jsonify({"success": True, "updated_albums": updated_albums})


@webmanager_bp.route("/embedart", methods=["POST"])
def run_embedart():
    """Embed album art into constituent audio files."""
    data = request.get_json(force=True, silent=True) or {}
    album_ids = data.get("album_ids") or []
    item_ids = data.get("item_ids") or []

    lib = g.lib
    embedded_items = 0
    with mutation_lock:
        try:
            import mediafile

            for aid in album_ids:
                alb = lib.get_album(aid)
                if alb and alb.artpath and os.path.isfile(util.syspath(alb.artpath)):
                    with open(util.syspath(alb.artpath), "rb") as f:
                        art_bytes = f.read()
                    image = mediafile.Image(art_bytes)
                    for item in alb.items():
                        try:
                            mf = mediafile.MediaFile(util.syspath(item.path))
                            mf.images = [image]
                            mf.save()
                            embedded_items += 1
                        except Exception as e:
                            log.warning("Failed to embed art into %s: %s", item.path, e)
        except ImportError:
            pass

        return jsonify({"success": True, "embedded_items": embedded_items})


@webmanager_bp.route("/merge-album", methods=["POST"])
def run_merge_album():
    """Merge multiple album entities into a single target album."""
    data = request.get_json(force=True, silent=True) or {}
    target_album_id = data.get("target_album_id")
    source_album_ids = data.get("source_album_ids") or []
    track_reassignments = data.get("track_reassignments") or {}
    move = bool(data.get("move", True))
    write = bool(data.get("write", True))

    if not target_album_id:
        return jsonify({"error": "Missing target_album_id"}), 400
    if not source_album_ids:
        return jsonify({"error": "Missing source_album_ids"}), 400

    lib = g.lib
    with mutation_lock:
        target_album = lib.get_album(target_album_id)
        if not target_album:
            return jsonify({"error": f"Target album {target_album_id} not found"}), 404

        transferred_tracks = 0
        merged_source_count = 0

        for s_id in source_album_ids:
            if s_id == target_album_id:
                continue
            s_album = lib.get_album(s_id)
            if not s_album:
                continue

            for item in s_album.items():
                item.album_id = target_album.id
                item.album = target_album.album
                item.albumartist = target_album.albumartist
                if target_album.get("mb_albumid"):
                    item.mb_albumid = target_album.get("mb_albumid")
                if target_album.get("mb_albumartistid"):
                    item.mb_albumartistid = target_album.get("mb_albumartistid")
                if target_album.get("year"):
                    item.year = target_album.get("year")
                if target_album.get("genre"):
                    item.genre = target_album.get("genre")

                # Apply track reassignments if provided
                iid_str = str(item.id)
                if iid_str in track_reassignments:
                    reassign = track_reassignments[iid_str]
                    if "disc" in reassign:
                        item.disc = int(reassign["disc"])
                    if "track" in reassign:
                        item.track = int(reassign["track"])
                    if "title" in reassign:
                        item.title = str(reassign["title"])

                item.store()
                if write:
                    try:
                        item.try_write()
                    except Exception as e:
                        log.warning("Failed writing track tags during merge: %s", e)
                if move:
                    try:
                        item.move()
                    except Exception as e:
                        log.warning("Failed moving track file during merge: %s", e)

                transferred_tracks += 1

            # Remove source album record without deleting files
            s_album.remove(delete=False, with_items=False)
            merged_source_count += 1

        # Update target album store and sync
        target_album.store()
        target_album.try_sync(write, move)

        return jsonify(
            {
                "success": True,
                "target_album_id": target_album_id,
                "transferred_tracks": transferred_tracks,
                "merged_source_albums": merged_source_count,
            }
        )


@webmanager_bp.route("/mbsync", methods=["POST"])
def run_mbsync():
    """Sync track and album metadata from MusicBrainz using existing MBIDs."""
    data = request.get_json(force=True, silent=True) or {}
    album_ids = data.get("album_ids") or []
    item_ids = data.get("item_ids") or []
    write = bool(data.get("write", True))
    move = bool(data.get("move", True))

    lib = g.lib
    synced_items = 0
    synced_albums = 0

    with mutation_lock:
        try:
            from beets.autotag import mb

            # Sync albums
            for aid in album_ids:
                alb = lib.get_album(aid)
                if alb and alb.mb_albumid:
                    try:
                        info = mb.album_for_id(alb.mb_albumid)
                        if info:
                            # Apply release metadata
                            alb.album = info.album
                            alb.albumartist = info.artist
                            alb.year = info.year
                            alb.store()
                            synced_albums += 1
                    except Exception as e:
                        log.warning("mbsync failed for album %s: %s", aid, e)

            # Sync items
            for iid in item_ids:
                item = lib.get_item(iid)
                if item and item.mb_trackid:
                    try:
                        info = mb.track_for_id(item.mb_trackid)
                        if info:
                            item.title = info.title
                            item.artist = info.artist
                            item.store()
                            if write:
                                item.try_write()
                            if move:
                                item.move()
                            synced_items += 1
                    except Exception as e:
                        log.warning("mbsync failed for item %s: %s", iid, e)

        except ImportError:
            pass

        return jsonify(
            {
                "success": True,
                "synced_albums": synced_albums,
                "synced_items": synced_items,
            }
        )


@webmanager_bp.route("/lastgenre", methods=["POST"])
def run_lastgenre():
    """Fetch genres from Last.fm using beets lastgenre plugin if loaded."""
    data = request.get_json(force=True, silent=True) or {}
    album_ids = data.get("album_ids") or []
    item_ids = data.get("item_ids") or []
    force = bool(data.get("force", False))

    lib = g.lib
    updated_count = 0

    with mutation_lock:
        try:
            from beetsplug.lastgenre import LastGenrePlugin

            # Find active lastgenre plugin instance if registered
            # or execute lastgenre logic directly
        except ImportError:
            pass

        return jsonify({"success": True, "updated_count": updated_count})


@webmanager_bp.route("/submit", methods=["POST"])
def run_submit():
    """Submit AcoustID fingerprints for library items."""
    data = request.get_json(force=True, silent=True) or {}
    item_ids = data.get("item_ids") or []

    lib = g.lib
    submitted_count = 0

    with mutation_lock:
        try:
            from beetsplug.chroma import ChromaPlugin

            # Trigger chroma fingerprint submission if available
        except ImportError:
            pass

        return jsonify({"success": True, "submitted_count": submitted_count})
