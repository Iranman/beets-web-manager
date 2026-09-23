"""Operation implementations for the WebManager Beets integration plugin."""

import os
import time
import uuid
import logging
import threading
import hashlib
import json
from typing import Dict, Any, Optional, List, Tuple
from flask import Blueprint, request, jsonify, g
import beets
from beets import config as beets_config
from beets import util
from .schemas import (
    resolve_safe_descendant,
    is_strict_descendant,
    validate_fields,
    ALLOWED_DUPLICATE_ACTIONS,
    DEFAULT_ALLOWED_ROOTS,
    DEFAULT_IMPORT_ROOTS,
)
from .version import PLUGIN_VERSION, PROTOCOL_VERSION

log = logging.getLogger("beets.webmanager")

# Lock to serialize library write operations safely within Beets process
mutation_lock = threading.RLock()

# Async operations registry
_operations: Dict[str, Dict[str, Any]] = {}
_operations_lock = threading.Lock()

DEFAULT_RETENTION_SECONDS = 3600
MAX_COMPLETED_OPERATIONS = 1000

webmanager_bp = Blueprint("webmanager", __name__, url_prefix="/webmanager")


def compute_fingerprint(data: Dict[str, Any]) -> str:
    """Compute stable SHA-256 fingerprint of request payload."""
    try:
        canonical = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _prune_operations_locked():
    """Prune expired completed/failed operations from registry (must hold _operations_lock)."""
    now = time.time()
    to_delete = []
    completed = []

    for op_id, op in _operations.items():
        if op.get("status") in ("succeeded", "failed"):
            if now - op.get("updated_at", now) > DEFAULT_RETENTION_SECONDS:
                to_delete.append(op_id)
            else:
                completed.append((op.get("updated_at", 0), op_id))

    for op_id in to_delete:
        del _operations[op_id]

    # Enforce hard upper bound on completed operations
    if len(completed) > MAX_COMPLETED_OPERATIONS:
        completed.sort(key=lambda x: x[0])  # oldest first
        overflow = len(completed) - MAX_COMPLETED_OPERATIONS
        for _, op_id in completed[:overflow]:
            if op_id in _operations and _operations[op_id].get("status") != "running":
                del _operations[op_id]


def register_operation(
    op_type: str, op_id: Optional[str] = None, fingerprint: Optional[str] = None
) -> Tuple[str, str]:
    """Register a new operation in the registry with collision check.

    Returns (op_id, status_code) where status_code is 'created', 'exists', or 'collision'.
    """
    if not op_id:
        op_id = str(uuid.uuid4())
    with _operations_lock:
        _prune_operations_locked()
        if op_id in _operations:
            existing = _operations[op_id]
            if fingerprint and existing.get("_fingerprint") and existing.get("_fingerprint") != fingerprint:
                return op_id, "collision"
            return op_id, "exists"

        _operations[op_id] = {
            "operation_id": op_id,
            "type": op_type,
            "_fingerprint": fingerprint,
            "status": "running",
            "created_at": time.time(),
            "updated_at": time.time(),
            "result": None,
            "error": None,
            "error_code": None,
        }
        return op_id, "created"


def update_operation(
    op_id: str,
    status: str,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    error_code: Optional[str] = None,
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
            if error_code is not None:
                _operations[op_id]["error_code"] = error_code


def get_operation(op_id: str) -> Optional[Dict[str, Any]]:
    """Get public operation data by ID, hiding internal fingerprints."""
    with _operations_lock:
        _prune_operations_locked()
        op = _operations.get(op_id)
        if not op:
            return None
        # Return sanitized public copy without _fingerprint
        return {
            "operation_id": op["operation_id"],
            "type": op["type"],
            "status": op["status"],
            "created_at": op["created_at"],
            "updated_at": op["updated_at"],
            "result": op["result"],
            "error": op["error"],
            "error_code": op["error_code"],
        }


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


_CUSTOM_IMPORT_ROOTS: Optional[List[str]] = None


def set_import_roots(roots: Optional[List[str]]):
    """Explicitly override import roots (for testing or runtime override)."""
    global _CUSTOM_IMPORT_ROOTS
    _CUSTOM_IMPORT_ROOTS = roots


def get_import_roots() -> List[str]:
    """Get configured import roots or defaults.

    Distinct from get_allowed_roots(): import_roots is the narrower set of
    directories a POST /webmanager/import source path may live under. It
    must never fall back to allowed_roots, since allowed_roots also
    includes /music (a Beets-managed destination) and /web-manager-data
    (unrelated app state) -- neither is a valid import intake point.
    """
    global _CUSTOM_IMPORT_ROOTS
    if _CUSTOM_IMPORT_ROOTS is not None:
        return _CUSTOM_IMPORT_ROOTS

    env_roots = os.environ.get("BEETS_WEBMANAGER_IMPORT_ROOTS", "").strip()
    if env_roots:
        return [r.strip() for r in env_roots.split(",") if r.strip()]

    try:
        from beets import config

        if "webmanager" in config and "import_roots" in config["webmanager"]:
            roots = config["webmanager"]["import_roots"].as_str_seq()
            if roots:
                return list(roots)
    except Exception:
        pass
    return DEFAULT_IMPORT_ROOTS


def get_upstream_web_readonly() -> bool:
    """Read Beets' own web.readonly setting, normalized to a safe bool.

    Fails closed (reports readonly=True) on any missing/malformed config
    rather than silently reporting a permissive default -- do not hide an
    unsafe or unexpected upstream configuration state.
    """
    try:
        return bool(beets_config["web"]["readonly"].get(bool))
    except Exception:
        return True


@webmanager_bp.route("/status", methods=["GET"])
def get_status():
    """Healthcheck and capability status handshake endpoint."""
    lib_ready = hasattr(g, "lib") and g.lib is not None and hasattr(g.lib, "items")
    return jsonify(
        {
            "protocol_version": PROTOCOL_VERSION,
            "plugin_version": PLUGIN_VERSION,
            "beets_version": getattr(beets, "__version__", "unknown"),
            "capabilities": ["import", "modify", "operations", "status"],
            "library_ready": lib_ready,
            "upstream_web_readonly": get_upstream_web_readonly(),
            "plugin_mutations_enabled": True,
        }
    )


@webmanager_bp.route("/operations/<string:op_id>", methods=["GET"])
def get_operation_status(op_id: str):
    """Retrieve status and results of an operation."""
    op = get_operation(op_id)
    if not op:
        return jsonify({"error": "Operation not found", "error_code": "NOT_FOUND"}), 404
    return jsonify(op)


@webmanager_bp.route("/import", methods=["POST"])
def run_import():
    """Confirmed non-interactive import execution inside Beets."""
    data = request.get_json(force=True, silent=True) or {}
    paths = data.get("paths", [])
    if isinstance(paths, str):
        paths = [paths]

    if not paths or not isinstance(paths, list):
        return jsonify({"error": "Missing or invalid 'paths' parameter", "error_code": "INVALID_PATHS"}), 400

    # Policy 1: Autotag must be disabled for confirmed non-interactive imports
    if bool(data.get("autotag", False)):
        return jsonify({
            "error": "autotag must be disabled for confirmed import",
            "error_code": "AUTOTAG_NOT_ALLOWED",
        }), 400

    # Policy 2: Validate duplicate_action strictly
    raw_dup = data.get("duplicate_action", "skip")
    if raw_dup is not None:
        duplicate_action = str(raw_dup).lower()
        if duplicate_action not in ALLOWED_DUPLICATE_ACTIONS:
            return jsonify({
                "error": f"Invalid duplicate_action '{duplicate_action}'",
                "error_code": "INVALID_DUPLICATE_ACTION",
            }), 400
    else:
        duplicate_action = "skip"

    # Policy 3: Path containment — import source paths must be strict
    # descendants of import_roots (e.g. /downloads), never allowed_roots.
    # import_roots is deliberately narrower: /music (a Beets-managed
    # destination) and /web-manager-data must never be valid import
    # sources just because they are allowed roots for another operation.
    import_roots = get_import_roots()
    safe_paths: List[str] = []
    for p in paths:
        if not p or not isinstance(p, str) or "\x00" in p:
            return (
                jsonify(
                    {
                        "error": "Source path must be a strict child of an import root",
                        "error_code": "PATH_NOT_ALLOWED",
                    }
                ),
                400,
            )
        safe_p = resolve_safe_descendant(p, import_roots)
        if safe_p is None:
            return (
                jsonify(
                    {
                        "error": "Source path must be a strict child of an import root",
                        "error_code": "PATH_NOT_ALLOWED",
                    }
                ),
                400,
            )
        safe_paths.append(safe_p)

    copy = bool(data.get("copy", False))
    move = bool(data.get("move", True))
    write = bool(data.get("write", True))
    incremental = bool(data.get("incremental", False))
    singletons = bool(data.get("singletons", False))
    pretend = bool(data.get("pretend", False))
    raw_set_fields = data.get("set_fields") or {}
    set_fields = validate_fields(raw_set_fields, is_album=False) if isinstance(raw_set_fields, dict) else {}

    is_async = request.headers.get("Prefer") == "respond-async" or data.get("async", False)
    op_id = request.headers.get("Idempotency-Key") or str(uuid.uuid4())
    fingerprint = compute_fingerprint(data)

    reg_id, reg_status = register_operation("import", op_id, fingerprint=fingerprint)
    if reg_status == "collision":
        return (
            jsonify(
                {
                    "error": "Operation ID collision: payload does not match existing operation",
                    "error_code": "OPERATION_COLLISION",
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
                            "error_code": existing.get("error_code", "IMPORT_FAILED"),
                        }
                    ),
                    500,
                )

    # For newly created operations, verify source path existence
    for safe_p in safe_paths:
        if not os.path.exists(safe_p):
            update_operation(op_id, "failed", error="Source path does not exist", error_code="SOURCE_NOT_FOUND")
            return (
                jsonify(
                    {
                        "error": "Source path does not exist",
                        "error_code": "SOURCE_NOT_FOUND",
                    }
                ),
                400,
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
            orig_resume = beets_config["import"]["resume"].get()

            try:
                beets_config["import"]["pretend"] = pretend
                beets_config["import"]["copy"] = copy
                beets_config["import"]["move"] = move
                beets_config["import"]["write"] = write
                beets_config["import"]["autotag"] = False
                beets_config["import"]["duplicate_action"] = duplicate_action
                beets_config["import"]["quiet"] = True
                beets_config["import"]["timid"] = False
                beets_config["import"]["resume"] = False
                beets_config["import"]["incremental"] = incremental
                beets_config["import"]["singletons"] = singletons
                if set_fields:
                    beets_config["import"]["set_fields"] = set_fields

                from beets.importer import ImportSession

                # Convert paths to bytestrings for Beets importer
                path_bytes = [util.bytestring_path(p) for p in safe_paths]
                session = ImportSession(lib, loghandler=None, paths=path_bytes, query=None)
                session.run()

                result = {
                    "success": True,
                    "imported_paths": safe_paths,
                    "autotag": False,
                    "duplicate_action": duplicate_action,
                }
                if op_id_arg:
                    update_operation(op_id_arg, "succeeded", result=result)
                return result
            except Exception:
                log.exception("Error executing non-interactive import")
                sanitized_err = "Import failed"
                sanitized_code = "IMPORT_FAILED"
                if op_id_arg:
                    update_operation(op_id_arg, "failed", error=sanitized_err, error_code=sanitized_code)
                raise
            finally:
                # Restore original importer config unconditionally
                beets_config["import"]["pretend"] = orig_pretend
                beets_config["import"]["copy"] = orig_copy
                beets_config["import"]["move"] = orig_move
                beets_config["import"]["write"] = orig_write
                beets_config["import"]["autotag"] = orig_autotag
                beets_config["import"]["duplicate_action"] = orig_duplicate_action
                beets_config["import"]["quiet"] = orig_quiet
                beets_config["import"]["timid"] = orig_timid
                beets_config["import"]["resume"] = orig_resume
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
    except Exception:
        return jsonify({"error": "Import failed", "error_code": "IMPORT_FAILED"}), 500


@webmanager_bp.route("/modify", methods=["POST"])
def run_modify():
    """Modify metadata of items or albums."""
    data = request.get_json(force=True, silent=True) or {}
    item_ids = data.get("item_ids") or []
    album_ids = data.get("album_ids") or []
    query = data.get("query")
    raw_fields = data.get("fields") or {}
    write = bool(data.get("write", True))
    move = bool(data.get("move", True))

    if not item_ids and not album_ids and not query:
        return jsonify({
            "error": "Must specify item_ids, album_ids, or query",
            "error_code": "MISSING_TARGET",
        }), 400

    if not raw_fields or not isinstance(raw_fields, dict):
        return jsonify({
            "error": "Must specify fields to modify",
            "error_code": "MISSING_FIELDS",
        }), 400

    lib = g.lib
    try:
        with mutation_lock:
            item_fields = validate_fields(raw_fields, is_album=False)
            album_fields = validate_fields(raw_fields, is_album=True)

            if not item_fields and not album_fields:
                return jsonify({
                    "error": "No valid fields provided for modification",
                    "error_code": "INVALID_FIELDS",
                }), 400

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
    except Exception:
        log.exception("Error during modify operation")
        return jsonify({"error": "Modify operation failed", "error_code": "MODIFY_FAILED"}), 500
