"""Operation implementations for the WebManager Beets integration plugin."""

import os
import re
import shutil
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
from . import plugin_ops

log = logging.getLogger("beets.webmanager")

# Lock to serialize library write operations safely within Beets process
mutation_lock = threading.RLock()

# Async operations registry
_operations: Dict[str, Dict[str, Any]] = {}
_operations_lock = threading.Lock()

DEFAULT_RETENTION_SECONDS = 3600
MAX_COMPLETED_OPERATIONS = 1000

# #300 R3: folder-op outcomes are kept on disk next to the library DB (the
# Beets config dir in the shipped Compose files), so a replayed idempotency
# key still reads its result after a Beets restart. Only final outcomes are
# saved: a step that was running when Beets stopped is not running any more,
# so replaying its key runs it (or refuses it) against the current files.
_DURABLE_TYPES = frozenset({"folder_op", "album_art_set", "album_art_rollback"})
DURABLE_RETENTION_SECONDS = 7 * 86400
REGISTRY_FILENAME = "webmanager_operations.json"
_FINAL = ("succeeded", "failed")
_durable_file: Optional[str] = None

webmanager_bp = Blueprint("webmanager", __name__, url_prefix="/webmanager")


@webmanager_bp.before_request
def _bind_library_music_dir():
    """Beets stores item paths relative to the library directory and
    expands them through a ContextVar that Library() sets only in the
    thread that created it. Werkzeug serves each request on another thread,
    where the var is empty and every item.path loads relative -- so file
    checks, moves and tag writes would act on the wrong path. Bind the
    library's directory for this request's thread."""
    lib = getattr(g, "lib", None)
    directory = getattr(lib, "directory", None)
    if not directory:
        return None
    try:
        from beets import context as beets_context
    except ImportError:  # Beets before relative path storage: nothing to bind
        return None
    beets_context.set_music_dir(directory)
    return None


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
        if op.get("status") in _FINAL:
            keep = DURABLE_RETENTION_SECONDS if op.get("type") in _DURABLE_TYPES else DEFAULT_RETENTION_SECONDS
            if now - op.get("updated_at", now) > keep:
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


def _save_durable_locked() -> None:
    """Write the final durable outcomes atomically (must hold _operations_lock)."""
    if not _durable_file:
        return
    entries = [op for op in _operations.values() if op.get("type") in _DURABLE_TYPES and op.get("status") in _FINAL]
    tmp = _durable_file + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entries, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _durable_file)
    except OSError as exc:
        log.warning("webmanager: could not save the operation registry: %s", exc)


def bind_durable_registry(lib) -> None:
    """Load this library's saved folder-op outcomes, once (#300 R3)."""
    global _durable_file
    db = os.fsdecode(getattr(lib, "path", None) or "")
    path = os.path.join(os.path.dirname(os.path.abspath(db)), REGISTRY_FILENAME) if db and db != ":memory:" else None
    with _operations_lock:
        if path == _durable_file:
            return
        _durable_file = path
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("webmanager: ignoring an unreadable operation registry: %s", exc)
            return
        for op in saved if isinstance(saved, list) else []:
            if not (isinstance(op, dict) and isinstance(op.get("operation_id"), str)
                    and op.get("type") in _DURABLE_TYPES and op.get("status") in _FINAL):
                continue
            try:
                created, updated = float(op.get("created_at") or 0), float(op.get("updated_at") or 0)
            except (TypeError, ValueError):
                continue
            _operations.setdefault(op["operation_id"], {
                "operation_id": op["operation_id"], "type": op["type"], "_fingerprint": op.get("_fingerprint"),
                "status": op["status"], "created_at": created, "updated_at": updated,
                "result": op.get("result"), "error": op.get("error"), "error_code": op.get("error_code"),
            })
        _prune_operations_locked()


def register_operation(
    op_type: str, op_id: Optional[str] = None, fingerprint: Optional[str] = None, create: bool = True
) -> Tuple[str, str]:
    """Register a new operation in the registry with collision check.

    Returns (op_id, status_code) where status_code is 'created', 'exists',
    'collision', or 'absent' (an unknown key when ``create`` is False).
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
        if not create:
            return op_id, "absent"

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
            if status in _FINAL and _operations[op_id].get("type") in _DURABLE_TYPES:
                _save_durable_locked()


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
    """Effective allowed roots for path containment checks.

    Precedence: runtime override > BEETS_ALLOWED_ROOTS env >
    explicit ``webmanager.allowed_roots`` in config.yaml > derived default.
    The derived default is Beets' own library ``directory`` plus the
    configured import roots, so Beets stays the authority over where its
    library lives. DEFAULT_ALLOWED_ROOTS is used only if Beets' directory
    cannot be read at all.
    """
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
    return _derived_allowed_roots()


def get_library_directory() -> Optional[str]:
    """Beets' configured library directory, or None when unreadable."""
    try:
        value = beets_config["directory"].as_filename()
    except Exception:
        return None
    if not value or not str(value).strip():
        return None
    return str(value)


def get_library_db_path() -> Optional[str]:
    """Beets' configured library database path, or None when unreadable."""
    try:
        value = beets_config["library"].as_filename()
    except Exception:
        return None
    if not value or not str(value).strip():
        return None
    return str(value)


def _covers_config_dir(directory: str) -> bool:
    """True when ``directory`` is ``/``, the Beets config dir, one of its
    ancestors or one of its descendants (F3i: ``/config/<sub>``). Such a
    directory must not become a mutation root: it would put config.yaml, the
    API key file, the library DB or the provisioned plugin code in scope."""
    config_dirs = ["/config"]
    try:
        config_dirs.append(str(beets_config.config_dir()))
    except Exception:
        pass
    directory = os.path.realpath(str(directory))
    for config_dir in config_dirs:
        real_config_dir = os.path.realpath(config_dir)
        try:
            if os.path.commonpath([directory, real_config_dir]) in (directory, real_config_dir):
                return True
        except ValueError:  # different drives (Windows)
            continue
    return False


_WARNED_DROPPED_ROOTS: set = set()


def _warn_dropped_root(root: str, source: str) -> None:
    # get_allowed_roots() runs per request: warn once per root, not every call.
    if (source, root) in _WARNED_DROPPED_ROOTS:
        return
    _WARNED_DROPPED_ROOTS.add((source, root))
    log.warning(
        "webmanager: not using %s %r as an allowed mutation root (it is /, or contains or is "
        "inside the Beets config directory); set webmanager.allowed_roots explicitly if you "
        "need something else",
        source, root,
    )


def _derived_allowed_roots() -> List[str]:
    directory = get_library_directory()
    if directory and _covers_config_dir(directory):
        _warn_dropped_root(directory, "directory")
        return list(DEFAULT_ALLOWED_ROOTS)
    if not directory:
        return list(DEFAULT_ALLOWED_ROOTS)
    roots: List[str] = []
    for root in [directory] + list(get_import_roots()):
        normalized = os.path.normpath(str(root))
        # An import root of `/` or `/config` would put config.yaml and the
        # API key file in mutation scope too: skip it (S-5).
        if not normalized or normalized in roots:
            continue
        if _covers_config_dir(normalized):
            _warn_dropped_root(normalized, "webmanager.import_roots")
            continue
        roots.append(normalized)
    return roots


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


_CORE_CAPABILITIES = ["import", "modify", "remove", "move", "replace_item_file", "quarantine_remove_items", "album_row_merge", "untracked_quarantine", "untracked_attach", "untracked_attach_album", "folder_op", "album_art", "operations", "status"]
_PLUGIN_GATED_CAPABILITIES = ["mbsync", "fetchart", "embedart", "lastgenre", "mbsubmit"]


def get_capabilities() -> List[str]:
    """Capabilities this process can actually execute right now.

    Core capabilities (import/modify/remove/move/operations/status) are
    always listed -- they use only Beets' own library API, never a
    third-party plugin. Plugin-gated capabilities (mbsync/fetchart/
    embedart/lastgenre) are listed only when the corresponding Beets
    plugin is genuinely loaded/configured in this process, so Web Manager
    can disable/report those actions cleanly rather than discovering the
    failure only when a user tries to use them.
    """
    caps = list(_CORE_CAPABILITIES)
    for name in _PLUGIN_GATED_CAPABILITIES:
        try:
            if plugin_ops.is_capability_available(name):
                caps.append(name)
        except Exception:
            pass
    return caps


def get_loaded_plugin_names() -> List[str]:
    """Every Beets plugin genuinely loaded in this process right now.

    Distinct from get_capabilities(): capabilities is a curated subset (the
    handful of plugins the webmanager endpoints can drive), this is the
    full list -- used by Web Manager's plugin manifest/health reporting so
    it never has to guess or fall back to a local `import beets` runtime
    check of its own.
    """
    try:
        from beets.plugins import find_plugins
        return sorted({p.name for p in find_plugins() if getattr(p, "name", "")})
    except Exception:
        return []


@webmanager_bp.route("/status", methods=["GET"])
def get_status():
    """Healthcheck and capability status handshake endpoint."""
    lib_ready = hasattr(g, "lib") and g.lib is not None and hasattr(g.lib, "items")
    return jsonify(
        {
            "protocol_version": PROTOCOL_VERSION,
            "plugin_version": PLUGIN_VERSION,
            "beets_version": getattr(beets, "__version__", "unknown"),
            "capabilities": get_capabilities(),
            "loaded_plugins": get_loaded_plugin_names(),
            "library_ready": lib_ready,
            "upstream_web_readonly": get_upstream_web_readonly(),
            "plugin_mutations_enabled": True,
            # Added in plugin 1.6.0 (protocol 1.0, additive). Older plugins
            # omit these; Web Manager reports them as "unknown".
            "library_directory": get_library_directory(),
            "library_path": get_library_db_path(),
            "allowed_roots": list(get_allowed_roots()),
            "import_roots": list(get_import_roots()),
            "web_include_paths": get_web_include_paths(),
            "fpcalc_available": _binary_available("fpcalc"),
            "ffmpeg_available": _binary_available("ffmpeg"),
        }
    )


def get_web_include_paths() -> Optional[bool]:
    """Beets' own web.include_paths, or None when unreadable."""
    try:
        return bool(beets_config["web"]["include_paths"].get(bool))
    except Exception:
        return None


def _binary_available(name: str) -> bool:
    """Whether an executable is on PATH inside the Beets container."""
    try:
        return shutil.which(name) is not None
    except Exception:
        return False


@webmanager_bp.route("/operations/<string:op_id>", methods=["GET"])
def get_operation_status(op_id: str):
    """Retrieve status and results of an operation."""
    op = get_operation(op_id)
    if not op:
        return jsonify({"error": "Operation not found", "error_code": "NOT_FOUND"}), 404
    return jsonify(op)


_QUIET_FALLBACKS = ("skip", "asis")
_MB_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _import_session_class(autotag: bool, tasks: List[Any]):
    """Beets' own import session: the quiet terminal session (what
    ``beet import -q`` runs) when autotagging, else the base session.

    The returned subclass only records each task Beets decides on, so the
    caller can report the ones Beets skipped (no strong match under
    quiet_fallback=skip, or a duplicate with duplicate_action=skip)."""
    from beets.importer import ImportSession

    base = ImportSession
    if autotag:
        try:  # beets >= 2.4
            from beets.ui.commands.import_.session import TerminalImportSession as base
        except ImportError:  # older beets
            from beets.ui.commands import TerminalImportSession as base

    class _RecordingSession(base):
        def choose_match(self, task):
            tasks.append(task)
            return super().choose_match(task)

        def choose_item(self, task):
            tasks.append(task)
            return super().choose_item(task)

        def get_duplicate_action(self, task, found_duplicates):
            task.webmanager_duplicate = True
            if task not in tasks:
                tasks.append(task)
            return super().get_duplicate_action(task, found_duplicates)

    return _RecordingSession


_LINK_MODES = ("link", "hardlink", "reflink")


@webmanager_bp.route("/import", methods=["POST"])
def run_import():
    """Confirmed non-interactive import execution inside Beets."""
    data = request.get_json(force=True, silent=True) or {}
    paths = data.get("paths", [])
    if isinstance(paths, str):
        paths = [paths]

    if not paths or not isinstance(paths, list):
        return jsonify({"error": "Missing or invalid 'paths' parameter", "error_code": "INVALID_PATHS"}), 400

    # Policy 1: Beets' own quiet importer. With autotag, Beets matches the
    # files itself (beet import -q [--search-id]); quiet_fallback decides what
    # happens to an album without a strong match (skip by default).
    autotag = bool(data.get("autotag", False))
    quiet_fallback = str(data.get("quiet_fallback") or "skip").lower()
    if quiet_fallback not in _QUIET_FALLBACKS:
        return jsonify({
            "error": "quiet_fallback must be 'skip' or 'asis'",
            "error_code": "INVALID_QUIET_FALLBACK",
        }), 400
    search_ids = data.get("search_ids") or []
    if (
        not isinstance(search_ids, list)
        or not all(isinstance(x, str) and _MB_UUID.match(x) for x in search_ids)
        or (search_ids and not autotag)
    ):
        return jsonify({
            "error": "search_ids must be lowercase MusicBrainz Release IDs and need autotag",
            "error_code": "INVALID_SEARCH_IDS",
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
    copy = bool(data.get("copy", False))
    move = bool(data.get("move", True))
    # In-place import (copy=no, move=no) may also target a folder strictly
    # inside the Beets library directory: the files stay where they are.
    library_dir = get_library_directory() if not copy and not move else None
    if library_dir and _covers_config_dir(library_dir):
        # Same guard as _derived_allowed_roots(): a library directory of `/`
        # or one containing /config must not open the config dir to imports.
        return jsonify({
            "error": "In-place import is not allowed: the Beets library directory contains the config directory",
            "error_code": "PATH_NOT_ALLOWED",
        }), 400
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
        if safe_p is None and library_dir:
            safe_p = resolve_safe_descendant(p, [library_dir])
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
            orig_search_ids = beets_config["import"]["search_ids"].get()
            orig_quiet_fallback = beets_config["import"]["quiet_fallback"].get()
            # ImportSession forces these off when move is on, and the change
            # outlives the request; an in-place import must never link.
            orig_links = {k: beets_config["import"][k].get() for k in _LINK_MODES}

            try:
                beets_config["import"]["pretend"] = pretend
                beets_config["import"]["copy"] = copy
                beets_config["import"]["move"] = move
                beets_config["import"]["write"] = write
                beets_config["import"]["autotag"] = autotag
                beets_config["import"]["search_ids"] = list(search_ids)
                beets_config["import"]["quiet_fallback"] = quiet_fallback
                beets_config["import"]["duplicate_action"] = duplicate_action
                beets_config["import"]["quiet"] = True
                beets_config["import"]["timid"] = False
                beets_config["import"]["resume"] = False
                beets_config["import"]["incremental"] = incremental
                beets_config["import"]["singletons"] = singletons
                for k in _LINK_MODES:
                    beets_config["import"][k] = False
                if set_fields:
                    beets_config["import"]["set_fields"] = set_fields

                # Convert paths to bytestrings for Beets importer
                path_bytes = [util.bytestring_path(p) for p in safe_paths]
                tasks: List[Any] = []
                session = _import_session_class(autotag, tasks)(
                    lib, loghandler=None, paths=path_bytes, query=None
                )
                session.run()

                skipped: List[str] = []
                skipped_reasons: List[Dict[str, str]] = []
                for task in tasks:
                    if getattr(task, "skip", False):
                        if getattr(task, "webmanager_duplicate", False):
                            reason = "duplicate"
                        elif not getattr(task, "candidates", None):
                            reason = "no_candidates"  # nothing found, or the source was unreachable
                        else:
                            reason = "no_strong_match"
                        for tp in getattr(task, "paths", None) or []:
                            shown = util.displayable_path(tp)
                            if shown not in skipped:
                                skipped.append(shown)
                                skipped_reasons.append({"path": shown, "reason": reason})
                result = {
                    "success": True,
                    "imported_paths": safe_paths,
                    "autotag": autotag,
                    "quiet_fallback": quiet_fallback,
                    "search_ids": list(search_ids),
                    "duplicate_action": duplicate_action,
                    "skipped_paths": skipped,
                    "skipped": skipped_reasons,
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
                beets_config["import"]["search_ids"] = orig_search_ids
                beets_config["import"]["quiet_fallback"] = orig_quiet_fallback
                beets_config["import"]["incremental"] = orig_incremental
                beets_config["import"]["singletons"] = orig_singletons
                beets_config["import"]["set_fields"] = orig_set_fields
                for k, v in orig_links.items():
                    beets_config["import"][k] = v

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


def _idempotency_precheck(op_type: str, data: Dict[str, Any], register: bool = True):
    """Shared idempotency/collision handling for destructive/long operations.

    Returns (op_id, fingerprint, early_response) -- early_response is a
    Flask response tuple to return immediately (collision, or a replay of
    an existing running/succeeded/failed operation), or None if this is a
    genuinely new operation the caller should now execute. With
    ``register=False`` an unknown key is left unregistered.
    """
    op_id = request.headers.get("Idempotency-Key") or str(uuid.uuid4())
    fingerprint = compute_fingerprint(data)
    reg_id, reg_status = register_operation(op_type, op_id, fingerprint=fingerprint, create=register)

    if reg_status == "collision":
        return op_id, fingerprint, (
            jsonify({
                "error": "Operation ID collision: payload does not match existing operation",
                "error_code": "OPERATION_COLLISION",
            }),
            409,
        )

    if reg_status == "exists":
        existing = get_operation(op_id)
        if existing:
            if existing["status"] == "running":
                return op_id, fingerprint, (jsonify({"operation_id": op_id, "status": "running"}), 202)
            if existing["status"] == "succeeded":
                return op_id, fingerprint, (
                    jsonify({"operation_id": op_id, "status": "succeeded", "result": existing.get("result")}),
                    200,
                )
            if existing["status"] == "failed":
                return op_id, fingerprint, (
                    jsonify({
                        "operation_id": op_id,
                        "status": "failed",
                        "error": existing.get("error"),
                        "error_code": existing.get("error_code", "OPERATION_FAILED"),
                    }),
                    500,
                )

    return op_id, fingerprint, None


def _parse_id_list(data: Dict[str, Any], key: str) -> List[int]:
    raw = data.get(key) or []
    if not isinstance(raw, list):
        raise ValueError(f"{key} must be a list")
    out = []
    for v in raw:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            raise ValueError(f"{key} contains a non-integer value")
    return out


@webmanager_bp.route("/remove", methods=["POST"])
def run_remove():
    """Remove explicit items/albums from the library using Beets' own
    Item.remove()/Album.remove(). Never deletes physical files unless
    delete_files is explicitly true (default false) -- the caller (Web
    Manager) is responsible for having already obtained user/workflow
    confirmation before setting it."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        item_ids = _parse_id_list(data, "item_ids")
        album_ids = _parse_id_list(data, "album_ids")
    except ValueError as ex:
        return jsonify({"error": str(ex), "error_code": "INVALID_IDS"}), 400

    if not item_ids and not album_ids:
        return jsonify({"error": "Must specify item_ids or album_ids", "error_code": "MISSING_TARGET"}), 400

    delete_files = bool(data.get("delete_files", False))

    op_id, fingerprint, early = _idempotency_precheck("remove", data)
    if early is not None:
        return early

    lib = g.lib
    removed_items = 0
    removed_albums = 0
    missing_item_ids: List[int] = []
    missing_album_ids: List[int] = []
    try:
        with mutation_lock:
            for iid in item_ids:
                item = lib.get_item(iid)
                if not item:
                    missing_item_ids.append(iid)
                    continue
                item.remove(delete=delete_files, with_album=True)
                removed_items += 1

            for aid in album_ids:
                album = lib.get_album(aid)
                if not album:
                    missing_album_ids.append(aid)
                    continue
                album.remove(delete=delete_files, with_items=True)
                removed_albums += 1

        result = {
            "success": True,
            "removed_items": removed_items,
            "removed_albums": removed_albums,
            "delete_files": delete_files,
            "missing_item_ids": missing_item_ids,
            "missing_album_ids": missing_album_ids,
        }
        update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        log.exception("Error during remove operation")
        update_operation(op_id, "failed", error="Remove failed", error_code="REMOVE_FAILED")
        return jsonify({"error": "Remove failed", "error_code": "REMOVE_FAILED"}), 500


@webmanager_bp.route("/move", methods=["POST"])
def run_move():
    """Move explicit items/albums using Beets' own Item.move()/Album.move()
    and configured path templates. Never accepts an arbitrary destination
    path -- Beets' own config remains the sole authority for where files
    land."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        item_ids = _parse_id_list(data, "item_ids")
        album_ids = _parse_id_list(data, "album_ids")
    except ValueError as ex:
        return jsonify({"error": str(ex), "error_code": "INVALID_IDS"}), 400

    if not item_ids and not album_ids:
        return jsonify({"error": "Must specify item_ids or album_ids", "error_code": "MISSING_TARGET"}), 400

    op_id, fingerprint, early = _idempotency_precheck("move", data)
    if early is not None:
        return early

    lib = g.lib
    moved_items = 0
    moved_albums = 0
    missing_item_ids: List[int] = []
    missing_album_ids: List[int] = []
    try:
        with mutation_lock:
            for iid in item_ids:
                item = lib.get_item(iid)
                if not item:
                    missing_item_ids.append(iid)
                    continue
                item.move()
                moved_items += 1

            for aid in album_ids:
                album = lib.get_album(aid)
                if not album:
                    missing_album_ids.append(aid)
                    continue
                album.move()
                moved_albums += 1

        result = {
            "success": True,
            "moved_items": moved_items,
            "moved_albums": moved_albums,
            "missing_item_ids": missing_item_ids,
            "missing_album_ids": missing_album_ids,
        }
        update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, **result})
    except Exception:
        log.exception("Error during move operation")
        update_operation(op_id, "failed", error="Move failed", error_code="MOVE_FAILED")
        return jsonify({"error": "Move failed", "error_code": "MOVE_FAILED"}), 500


def _run_plugin_gated_operation(op_type: str, capability_name: str, validate_and_build_fn):
    """Shared async-capable transport for plugin-gated operations (mbsync,
    fetchart, embedart, lastgenre): request validation happens up front
    (before any operation is registered) so a malformed request gets a
    clean 400, not a registered-then-failed operation; idempotency/
    collision handling; a capability pre-check so an unavailable plugin
    fails with a stable error rather than a generic 500; and the Phase 1
    async/operation-poll pattern for calls that could outlive a normal
    request. `validate_and_build_fn(data)` must return a zero-argument
    callable to execute, or raise ValueError for a bad request.
    """
    data = request.get_json(force=True, silent=True) or {}

    try:
        fn = validate_and_build_fn(data)
    except ValueError as ex:
        return jsonify({"error": str(ex), "error_code": "INVALID_REQUEST"}), 400

    if not plugin_ops.is_capability_available(capability_name):
        return jsonify({
            "error": f"{capability_name} plugin is not loaded or configured on this Beets server",
            "error_code": "CAPABILITY_UNAVAILABLE",
        }), 409

    op_id, fingerprint, early = _idempotency_precheck(op_type, data)
    if early is not None:
        return early

    is_async = request.headers.get("Prefer") == "respond-async" or bool(data.get("async", False))

    def _execute():
        with mutation_lock:
            return fn()

    if is_async:
        def _bg():
            try:
                result = _execute()
                update_operation(op_id, "succeeded", result=result)
            except (plugin_ops.PluginCapabilityError, plugin_ops.PluginIncompatibleError) as ex:
                update_operation(op_id, "failed", error=str(ex), error_code="CAPABILITY_UNAVAILABLE")
            except Exception:
                log.exception("Error executing async %s operation", op_type)
                update_operation(op_id, "failed", error=f"{op_type} failed", error_code=f"{op_type.upper()}_FAILED")

        t = threading.Thread(target=_bg, daemon=True)
        t.start()
        return jsonify({"operation_id": op_id, "status": "running"}), 202

    try:
        result = _execute()
        update_operation(op_id, "succeeded", result=result)
        return jsonify({"operation_id": op_id, "status": "succeeded", "result": result})
    except (plugin_ops.PluginCapabilityError, plugin_ops.PluginIncompatibleError) as ex:
        update_operation(op_id, "failed", error=str(ex), error_code="CAPABILITY_UNAVAILABLE")
        return jsonify({"error": str(ex), "error_code": "CAPABILITY_UNAVAILABLE"}), 409
    except Exception:
        log.exception("Error executing %s operation", op_type)
        update_operation(op_id, "failed", error=f"{op_type} failed", error_code=f"{op_type.upper()}_FAILED")
        return jsonify({"error": f"{op_type} failed", "error_code": f"{op_type.upper()}_FAILED"}), 500


@webmanager_bp.route("/mbsync", methods=["POST"])
def run_mbsync_route():
    """Sync metadata from MusicBrainz for explicit items/albums using the
    real Beets mbsync plugin (never a hand-rolled reimplementation)."""
    lib = g.lib

    def _build(data):
        item_ids = _parse_id_list(data, "item_ids")
        album_ids = _parse_id_list(data, "album_ids")
        if not item_ids and not album_ids:
            raise ValueError("Must specify item_ids or album_ids")
        move = bool(data.get("move", False))
        pretend = bool(data.get("pretend", False))
        write = bool(data.get("write", True))
        return lambda: plugin_ops.run_mbsync(
            lib, item_ids=item_ids, album_ids=album_ids, move=move, pretend=pretend, write=write
        )

    return _run_plugin_gated_operation("mbsync", "mbsync", _build)


@webmanager_bp.route("/fetchart", methods=["POST"])
def run_fetchart_route():
    """Fetch cover art for explicit albums using the real Beets fetchart
    plugin and its own configured sources (never an arbitrary user URL)."""
    lib = g.lib

    def _build(data):
        album_ids = _parse_id_list(data, "album_ids")
        if not album_ids:
            raise ValueError("Must specify album_ids")
        force = bool(data.get("force", False))
        return lambda: plugin_ops.run_fetchart(lib, album_ids, force=force)

    return _run_plugin_gated_operation("fetchart", "fetchart", _build)


@webmanager_bp.route("/embedart", methods=["POST"])
def run_embedart_route():
    """Embed each album's existing artwork into its items' tags using the
    real Beets embedart plugin."""
    lib = g.lib

    def _build(data):
        album_ids = _parse_id_list(data, "album_ids")
        if not album_ids:
            raise ValueError("Must specify album_ids")
        return lambda: plugin_ops.run_embedart(lib, album_ids)

    return _run_plugin_gated_operation("embedart", "embedart", _build)


@webmanager_bp.route("/lastgenre", methods=["POST"])
def run_lastgenre_route():
    """Repair genre tags for explicit albums using the real Beets lastgenre
    plugin."""
    lib = g.lib

    def _build(data):
        album_ids = _parse_id_list(data, "album_ids")
        if not album_ids:
            raise ValueError("Must specify album_ids")
        force = bool(data.get("force", False))
        return lambda: plugin_ops.run_lastgenre(lib, album_ids, force=force)

    return _run_plugin_gated_operation("lastgenre", "lastgenre", _build)


@webmanager_bp.route("/mbsubmit", methods=["POST"])
def run_mbsubmit_route():
    """Submit AcoustID fingerprints for explicit items using the real Beets
    chroma plugin's submit_items()."""
    lib = g.lib

    def _build(data):
        item_ids = _parse_id_list(data, "item_ids")
        if not item_ids:
            raise ValueError("Must specify item_ids")
        api_key = data.get("api_key") or None
        return lambda: plugin_ops.run_mbsubmit(lib, item_ids, api_key=api_key)

    return _run_plugin_gated_operation("mbsubmit", "mbsubmit", _build)


# Routes defined in sibling modules register on webmanager_bp at import time.
from . import replace_ops  # noqa: E402,F401
from . import remove_ops  # noqa: E402,F401
from . import merge_ops  # noqa: E402,F401
from . import untracked_ops  # noqa: E402,F401
from . import folder_ops  # noqa: E402,F401
from . import art_ops  # noqa: E402,F401
