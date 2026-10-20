"""In-process Beets engine (docs/adr/0006-embedded-beets.md).

The only module that loads Beets' configuration and plugins and constructs
``beets.library.Library``. ``BeetsAdapter`` dispatches to the existing
``beetsplug/webmanager`` handlers (and stock ``beetsplug.web`` reads) through an
in-process WSGI call: no socket, same request/response contract, same refusal
codes. Selected with ``BEETS_ENGINE=embedded`` during the migration (Phase 1
default: ``http``).

Beets is imported lazily, inside ``EmbeddedBeets``, so the HTTP transport and
the test suite never load it unless the embedded engine is used.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import threading
from typing import Any, Dict, List, Optional

log = logging.getLogger("beets.embedded")

PINNED_BEETS_VERSION = "2.14.1"  # keep equal to requirements.txt
ENGINE_ENV = "BEETS_ENGINE"
_REQUIRED_PLUGINS = ("web", "webmanager")
# Plugins the image ships (beetsplug/webmanager, beetsplug/discpath.py). They
# are imported from here before Beets adds `pluginpath`, so a stale copy once
# provisioned into /config/beetsplug can never shadow them.
_BUNDLED_PLUGIN_DIR = os.path.normcase(os.path.realpath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "beetsplug")))
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _bundled_plugins() -> List[str]:
    return sorted(
        n[:-3] if n.endswith(".py") else n
        for n in os.listdir(_BUNDLED_PLUGIN_DIR)
        if (n.endswith(".py") and n != "__init__.py")
        or os.path.isfile(os.path.join(_BUNDLED_PLUGIN_DIR, n, "__init__.py"))
    )

_init_lock = threading.Lock()
_engine: Optional["EmbeddedBeets"] = None
_failed_status: Optional[Dict[str, Any]] = None


def enabled() -> bool:
    return os.environ.get(ENGINE_ENV, "http").strip().lower() == "embedded"


class EmbeddedBeetsUnavailable(Exception):
    """The engine could not start; ``status`` holds the self-test."""

    def __init__(self, status: Dict[str, Any]):
        failed = [c for c in status.get("checks", []) if c["severity"] == "error" and not c["ok"]]
        super().__init__(failed[0]["message"] if failed else "Embedded Beets engine is unavailable")
        self.status = status


def _check(checks: List[Dict[str, Any]], code: str, ok: bool, message: str, severity: str = "error") -> bool:
    checks.append({"code": code, "ok": bool(ok), "severity": severity, "message": "" if ok else message})
    return bool(ok)


def _summary(checks: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    ok = all(c["ok"] for c in checks if c["severity"] == "error")
    return {"engine": "embedded", "ok": ok, "checks": checks, **extra}


class EmbeddedBeets:
    """One Beets runtime per process: config, plugins, Library, WSGI app."""

    def __init__(self) -> None:
        checks: List[Dict[str, Any]] = []
        self.status: Dict[str, Any] = {}
        import beets
        from beets import config, plugins
        from beets.library import Library

        version = getattr(beets, "__version__", "unknown")
        _check(checks, "beets_version_mismatch", version == PINNED_BEETS_VERSION,
               f"Beets {version} is running but Web Manager is built and tested against "
               f"{PINNED_BEETS_VERSION}. Use the published image, or install beets=={PINNED_BEETS_VERSION}.")

        config_path = ""
        try:
            config_path = os.fsdecode(config.user_config_path())
            config.read(user=True, defaults=True)
            dbpath = os.fsdecode(config["library"].as_filename())
            directory = os.fsdecode(config["directory"].as_filename())
            configured = list(config["plugins"].as_str_seq())
        except Exception as ex:  # confuse.ConfigError and YAML errors
            _check(checks, "config_invalid", False,
                   f"Beets config {config_path or '(BEETSDIR/config.yaml)'} could not be read: "
                   f"{type(ex).__name__}: {ex}. Fix the file (it is the same config.yaml the Beets "
                   "container used) and restart Web Manager.")
            self.status = _summary(checks, beets_version=version)
            raise EmbeddedBeetsUnavailable(self.status)
        _check(checks, "config_invalid", True, "")
        _check(checks, "config_missing", os.path.isfile(config_path),
               f"No Beets config at {config_path}; Beets defaults are in use. Mount the Beets "
               "/config folder (with config.yaml) and set BEETSDIR to it.", severity="warning")

        # In-memory overlays only; the user's config.yaml is never rewritten.
        config["plugins"].set(configured + [p for p in _REQUIRED_PLUGINS if p not in configured])
        config["web"]["include_paths"].set(True)

        # Import the image's own plugins BEFORE Beets extends beetsplug.__path__
        # with `pluginpath`. Fail closed if one resolves anywhere else: running
        # foreign operations code in this process is never acceptable.
        import importlib
        for name in _bundled_plugins():
            mod = importlib.import_module(f"beetsplug.{name}")
            where = os.path.normcase(os.path.realpath(mod.__file__))
            if not _check(checks, "operations_shadowed", where.startswith(_BUNDLED_PLUGIN_DIR + os.sep),
                          f"Beets plugin {name!r} was loaded from {where}, not from the Web Manager "
                          "image. Remove that copy from Beets' pluginpath and restart."):
                self.status = _summary(checks, beets_version=version)
                raise EmbeddedBeetsUnavailable(self.status)

        plugins.load_plugins()
        loaded = sorted({p.name for p in plugins.find_plugins()})
        disabled = set(config["disabled_plugins"].as_str_seq())
        failed = sorted(set(config["plugins"].as_str_seq()) - disabled - set(loaded))
        _check(checks, "plugins_failed", not failed,
               f"Beets plugin(s) {', '.join(failed)} failed to load (see the log for the import "
               "error). Check the names in config.yaml `plugins:`; a plugin with extra Python "
               "dependencies needs them in the image.")

        existed = os.path.isfile(dbpath)
        try:
            lib = Library(dbpath, directory)
            lib.get_item(0)  # opens the connection, as `beet` does
        except Exception as ex:
            _check(checks, "library_open_failed", False,
                   f"Beets library {dbpath} could not be opened: {type(ex).__name__}: {ex}. "
                   "Check the /config mount and its permissions for PUID/PGID.")
            self.status = _summary(checks, beets_version=version, loaded_plugins=loaded)
            raise EmbeddedBeetsUnavailable(self.status)
        _check(checks, "library_open_failed", True, "")
        _check(checks, "library_created", existed,
               f"No library existed at {dbpath}, so an empty one was created. If you expected "
               "your existing library, mount the Beets /config folder that holds musiclibrary.blb.",
               severity="warning")
        _check(checks, "library_dir_missing", os.path.isdir(directory),
               f"Beets library directory {directory} does not exist. Mount your music folder "
               "there (the same path the Beets container used, normally /music).")
        _check(checks, "library_dir_not_writable", os.access(directory, os.W_OK),
               f"Beets library directory {directory} is not writable by this process. Beets now "
               "runs inside Web Manager: mount the music folder read-write (not :ro).")
        _check(checks, "library_db_not_writable",
               os.access(os.path.dirname(os.path.abspath(dbpath)), os.W_OK)
               and (not os.path.exists(dbpath) or os.access(dbpath, os.W_OK)),
               f"Beets library {dbpath} (or its folder, needed for SQLite's journal) is not "
               "writable by this process. Fix the ownership for PUID/PGID.")

        chroma = "chroma" in loaded
        _check(checks, "fpcalc_missing", bool(shutil.which("fpcalc")),
               "fpcalc (Chromaprint) is not on PATH; AcoustID fingerprinting is off. "
               "The published image includes it.", severity="error" if chroma else "warning")
        _check(checks, "ffmpeg_missing", bool(shutil.which("ffmpeg")),
               "ffmpeg is not on PATH; some Beets plugins and audio checks need it.",
               severity="warning")

        from beetsplug.web import app as web_app
        from beetsplug.webmanager import auth as ops_auth
        web_app.config["lib"] = lib
        web_app.config["INCLUDE_PATHS"] = True
        web_app.config["READONLY"] = True  # stock web's write routes are never used
        web_app.config["JSONIFY_PRETTYPRINT_REGULAR"] = False
        self._key = secrets.token_hex(32)  # never leaves this process
        ops_auth.set_api_key(self._key)
        plugins.send("library_opened", lib=lib)

        self.lib = lib
        self.app = web_app
        self.status = _summary(checks, beets_version=version, loaded_plugins=loaded,
                               library_path=dbpath, library_directory=directory)
        for c in checks:
            if not c["ok"]:
                (log.error if c["severity"] == "error" else log.warning)("Beets self-test %s: %s", c["code"], c["message"])

    def call(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
             json_data: Any = None, headers: Optional[Dict[str, str]] = None, stream: bool = False):
        """One in-process request; returns the Werkzeug response. Writes are
        refused while any error-level self-test check fails; reads continue."""
        if method.upper() not in _READ_METHODS and not self.status["ok"]:
            raise EmbeddedBeetsUnavailable(self.status)
        hdrs = dict(headers or {})
        if path.startswith("/webmanager"):
            hdrs.setdefault("Authorization", f"Bearer {self._key}")
        query = {k: v for k, v in (params or {}).items() if v is not None}
        client = self.app.test_client()
        return client.open(path, method=method, query_string=query, json=json_data,
                           headers=hdrs, buffered=not stream)


def engine() -> EmbeddedBeets:
    """The process' engine, started on first use. A failed start is
    remembered (the self-test says why) until ``reset()``."""
    global _engine, _failed_status
    if _engine is not None:
        return _engine
    with _init_lock:
        if _engine is None:
            if _failed_status is not None:
                raise EmbeddedBeetsUnavailable(_failed_status)
            try:
                _engine = EmbeddedBeets()
            except EmbeddedBeetsUnavailable as ex:
                _failed_status = ex.status
                raise
            except Exception as ex:
                log.exception("Embedded Beets engine failed to start")
                checks: List[Dict[str, Any]] = []
                _check(checks, "engine_start_failed", False,
                       f"Beets could not start inside Web Manager: {type(ex).__name__}. See the log.")
                _failed_status = _summary(checks)
                raise EmbeddedBeetsUnavailable(_failed_status) from ex
    return _engine


def status() -> Dict[str, Any]:
    """The startup self-test (starts the engine if needed); never raises."""
    if not enabled():
        return {"engine": "http", "ok": True, "checks": []}
    try:
        return engine().status
    except EmbeddedBeetsUnavailable as ex:
        return ex.status


def public_status() -> Dict[str, Any]:
    """For the unauthenticated /api/health: codes only, no paths or messages."""
    st = status()
    return {"engine": st["engine"], "ok": st["ok"],
            "codes": [c["code"] for c in st.get("checks", []) if not c["ok"]]}


def reset() -> None:
    """Forget the engine (tests; a restart does the same in production)."""
    global _engine, _failed_status
    with _init_lock:
        _engine = None
        _failed_status = None
