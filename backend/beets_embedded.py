"""The Beets engine: Beets runs inside Web Manager (docs/adr/0006-embedded-beets.md).

The only module that loads Beets' configuration and plugins and constructs
``beets.library.Library``. ``BeetsAdapter`` dispatches to the
``beetsplug/webmanager`` handlers (and stock ``beetsplug.web`` reads) through an
in-process WSGI call: no socket, one request/response contract, one set of
refusal codes. There is no other engine.

Beets is imported lazily, inside ``EmbeddedBeets``, so modules (and tests) that
never touch the library never load it.

One writer per library: the engine holds an exclusive lock next to the
database for its lifetime, and before every write it checks that no separate
Beets server (the old two-container ``beets`` service) and no ``beet`` command
in this container is running. Any of those refuses writes; reads continue.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import socket
import stat
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional

log = logging.getLogger("beets.embedded")

PINNED_BEETS_VERSION = "2.14.1"  # keep equal to requirements.txt
_REQUIRED_PLUGINS = ("web", "webmanager")
# Settings of the removed two-container setup. Ignored; a warning names them.
LEGACY_SETTINGS = ("BEETS_ENGINE", "BEETS_WEB_URL", "BEETS_WEBMANAGER_API_KEY",
                   "BEETS_WEBMANAGER_API_KEY_FILE")
# Where the old `beets` Compose service answered (BEETS_WEB_URL wins if set).
OLD_BEETS_SERVICE_URL = "http://beets:8337"
LOCK_SUFFIX = ".webmanager.lock"
_PROBE_TTL = 30.0  # seconds a "separate Beets server" probe result is reused
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
_lock_fd: Optional[int] = None
_probe_cache: Dict[str, Any] = {"at": None, "url": ""}


class EmbeddedBeetsUnavailable(Exception):
    """The engine could not start, or writes are refused; ``status`` says why."""

    def __init__(self, status: Dict[str, Any]):
        failed = [c for c in status.get("checks", []) if c["severity"] == "error" and not c["ok"]]
        super().__init__(failed[0]["message"] if failed else "Beets is unavailable")
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
        # Before the library is opened: another Beets version could migrate its schema.
        if not _check(checks, "beets_version_mismatch", version == PINNED_BEETS_VERSION,
                      f"Beets {version} is installed but Web Manager is built and tested against "
                      f"{PINNED_BEETS_VERSION}, so the library was not opened. Use the published "
                      f"image, or install beets=={PINNED_BEETS_VERSION}."):
            self.status = _summary(checks, beets_version=version)
            raise EmbeddedBeetsUnavailable(self.status)
        legacy = [k for k in LEGACY_SETTINGS if (os.environ.get(k) or "").strip()]
        _check(checks, "legacy_settings_ignored", not legacy,
               f"{', '.join(legacy)} {'is' if len(legacy) == 1 else 'are'} left over from the "
               "two-container setup and ignored: Beets runs inside Web Manager. Remove "
               f"{'it' if len(legacy) == 1 else 'them'} from your Compose file, and remove the old "
               "`beets` service (docs/INSTALLATION.md).", severity="warning")

        config_path = ""
        try:
            config_path = os.fsdecode(config.user_config_path())
            config.read(user=True, defaults=True)
            if not os.path.isfile(config_path):
                # Fresh install: the layout the stock Beets container created
                # (library in /config, music in MUSIC_ROOT), in memory only.
                from backend.config_layers import music_root
                config["library"].set(os.path.join(os.fsdecode(config.config_dir()), "musiclibrary.blb"))
                config["directory"].set(music_root())
            dbpath = os.fsdecode(config["library"].as_filename())
            directory = os.fsdecode(config["directory"].as_filename())
            configured = list(config["plugins"].as_str_seq())
        except Exception as ex:  # confuse.ConfigError and YAML errors
            _check(checks, "config_invalid", False,
                   f"Beets config {config_path or '(BEETSDIR/config.yaml)'} could not be read: "
                   f"{type(ex).__name__}: {ex}. Fix the file and restart Web Manager.")
            self.status = _summary(checks, beets_version=version)
            raise EmbeddedBeetsUnavailable(self.status)
        _check(checks, "config_invalid", True, "")
        _check(checks, "config_missing", os.path.isfile(config_path),
               f"No Beets config at {config_path}; Beets defaults are in use, with the library "
               "at musiclibrary.blb beside it and MUSIC_ROOT as the music folder. If you have a "
               "Beets setup, mount its /config folder (with config.yaml) there.", severity="warning")

        # In-memory overlays only; the user's config.yaml is never rewritten.
        # web and webmanager are required, so `disabled_plugins` cannot remove them.
        config["plugins"].set(configured + [p for p in _REQUIRED_PLUGINS if p not in configured])
        # Older configs have no `disabled_plugins` default at all.
        disabled = config["disabled_plugins"].as_str_seq() if config["disabled_plugins"].exists() else []
        config["disabled_plugins"].set([p for p in disabled if p not in _REQUIRED_PLUGINS])
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
        failed = sorted((set(config["plugins"].as_str_seq()) - disabled - set(loaded))
                        | (set(_REQUIRED_PLUGINS) - set(loaded)))
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
               "there (normally /music).")
        _check(checks, "library_dir_not_writable", os.access(directory, os.W_OK),
               f"Beets library directory {directory} is not writable by this process. Beets "
               "runs inside Web Manager: mount the music folder read-write (not :ro).")
        _check(checks, "library_db_not_writable",
               os.access(os.path.dirname(os.path.abspath(dbpath)), os.W_OK)
               and (not os.path.exists(dbpath) or os.access(dbpath, os.W_OK)),
               f"Beets library {dbpath} (or its folder, needed for SQLite's journal) is not "
               "writable by this process. Fix the ownership for PUID/PGID.")
        from backend.config_layers import music_root
        music = music_root()
        _check(checks, "library_dir_mismatch",
               os.path.realpath(music) == os.path.realpath(directory),
               f"Beets' `directory:` is {directory} but Web Manager's MUSIC_ROOT is {music}. "
               "They must name the same folder, and Web Manager changes neither: fix "
               "`directory:` in config.yaml or MUSIC_ROOT (and its mount), then restart.")
        holder = _acquire_library_lock(dbpath)
        _check(checks, "library_locked", holder is None,
               f"Another Web Manager ({holder}) has the library {dbpath} open. Only one may "
               "run against a library: stop the other one, then restart this one.")

        chroma = "chroma" in loaded
        _check(checks, "fpcalc_missing", bool(shutil.which("fpcalc")),
               "fpcalc (Chromaprint) is not on PATH; AcoustID fingerprinting is off. "
               "The published image includes it.", severity="error" if chroma else "warning")
        _check(checks, "ffmpeg_missing", bool(shutil.which("ffmpeg")),
               "ffmpeg is not on PATH; some Beets plugins and audio checks need it.",
               severity="warning")

        from beetsplug.web import app as web_app
        self._key = secrets.token_hex(32)  # never leaves this process
        # beetsplug.web.app is a module-global Flask app whose config (and the
        # webmanager auth key) are process-wide: anything else in this process
        # that imports it can overwrite config["lib"] with its own Library.
        # The engine owns these, so it re-asserts them before every dispatch
        # (see _bind / call); otherwise a stale/closed Library left in the
        # global config would be used, and a transaction on a closed
        # connection strands Beets' per-database lock forever (a hang, not an
        # error). In production nothing else touches the global, so this is a
        # no-op; it matters when other code (e.g. the test suite) shares it.
        self._web_config = {
            "lib": lib,
            "INCLUDE_PATHS": True,
            "READONLY": True,  # stock web's write routes are never used
            "JSONIFY_PRETTYPRINT_REGULAR": False,
        }

        self.lib = lib
        self.app = web_app
        self._bind()
        plugins.send("library_opened", lib=lib)
        self.status = _summary(checks, beets_version=version, loaded_plugins=loaded,
                               library_path=dbpath, library_directory=directory)
        for c in checks + second_writers():
            if not c["ok"]:
                (log.error if c["severity"] == "error" else log.warning)("Beets self-test %s: %s", c["code"], c["message"])

    def _bind(self) -> None:
        """Re-assert this engine's Library, web config and auth key onto the
        process-global beets web app, so a dispatch always runs against this
        engine's own (open) Library even if other code overwrote the global."""
        from beetsplug.webmanager import auth as ops_auth
        self.app.config.update(self._web_config)
        ops_auth.set_api_key(self._key)

    def current_status(self) -> Dict[str, Any]:
        """The startup self-test plus the live second-writer checks."""
        live = second_writers()
        if not live:
            return self.status
        return {**self.status, **_summary(self.status["checks"] + live)}

    def call(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
             json_data: Any = None, headers: Optional[Dict[str, str]] = None, stream: bool = False):
        """One in-process request; returns the Werkzeug response. Writes are
        refused while any error-level check fails or another writer runs;
        reads continue."""
        if method.upper() not in _READ_METHODS:
            st = self.current_status()
            if not st["ok"]:
                raise EmbeddedBeetsUnavailable(st)
        self._bind()  # the global app's lib/key may have been overwritten since the last call
        hdrs = dict(headers or {})
        if path.startswith("/webmanager"):
            hdrs.setdefault("Authorization", f"Bearer {self._key}")
        query = {k: v for k, v in (params or {}).items() if v is not None}
        client = self.app.test_client()
        return client.open(path, method=method, query_string=query, json=json_data,
                           headers=hdrs, buffered=not stream)


def _acquire_library_lock(dbpath: str) -> Optional[str]:
    """Hold an exclusive lock next to the library for this process' lifetime.
    Returns None when this process holds it (or locking is unavailable on this
    platform), else a short description of who/what holds it. Fail safe: a lock
    that cannot be proven free refuses writes, it is never read as "free".
    ponytail: flock is advisory and only as good as the filesystem's locking
    (NFS/SMB may not honour it); the live checks below cover the old service."""
    global _lock_fd
    if _lock_fd is not None:
        return None
    try:
        import fcntl
    except ImportError:  # Windows development checkouts: no flock available
        return None
    lockpath = dbpath + LOCK_SUFFIX
    # O_NOFOLLOW: the lock path is predictable and sits in the mounted /config
    # dir, so a planted symlink must never be followed and then truncated
    # (#183 S-4 applies here too). fstat confirms a regular file before the
    # ftruncate/write below.
    try:
        fd = os.open(lockpath, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        # A symlink (ELOOP), a special file, or an unopenable lock file (e.g.
        # EACCES on one owned by another user): refuse, never assume it is free.
        return f"an unusable lock file next to {os.path.basename(dbpath)}"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return f"an unusable lock file next to {os.path.basename(dbpath)}"
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            holder = os.read(fd, 200).decode("utf-8", "replace").strip() or "unknown"
        finally:
            os.close(fd)
        return holder
    os.ftruncate(fd, 0)
    os.write(fd, f"host {socket.gethostname()}, pid {os.getpid()}".encode())
    _lock_fd = fd
    return None


def _own_addresses() -> set:
    addrs = {"127.0.0.1", "::1"}
    try:
        addrs |= {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None)}
    except OSError:
        pass
    return addrs


def _separate_beets_address() -> str:
    """``host:port`` of a separate Beets server that is listening, or "".

    Where the old two-container setup reached Beets (BEETS_WEB_URL, default
    ``http://beets:8337``). Only a TCP connect, no request: Docker's DNS
    resolves a service name only while that container runs, so a listener
    there is the old ``beets`` service. This container's own addresses are
    skipped (a Web Manager service that is itself named ``beets``).
    """
    raw = (os.environ.get("BEETS_WEB_URL") or "").strip() or OLD_BEETS_SERVICE_URL
    try:
        u = urllib.parse.urlsplit(raw)
        host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
        if not host:
            return ""
        own = _own_addresses()
        for family, kind, proto, _, addr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            if addr[0] in own:
                continue
            with socket.socket(family, kind, proto) as sock:
                sock.settimeout(2)
                if sock.connect_ex(addr) == 0:
                    return f"{host}:{port}"
    except (OSError, ValueError):  # no such host, or an unparseable URL
        pass
    return ""


# `beet` subcommands that only read the library; anything else may write it.
_READ_ONLY_BEET_COMMANDS = frozenset({"ls", "list", "stats", "version", "info", "fields", "config", "help"})
_BEET_VALUE_OPTIONS = frozenset({"-c", "--config", "-l", "--library", "-d", "--directory", "-p", "--plugins"})


def _beet_subcommand(argv: List[str]) -> Optional[str]:
    """The subcommand of a `beet`/`python -m beets` command line, "" for none,
    or None when ``argv`` is not a Beets command."""
    if argv[:1] and os.path.basename(argv[0]) == "beet":
        rest = argv[1:]
    elif argv[1:3] == ["-m", "beets"]:
        rest = argv[3:]
    elif argv[1:2] and os.path.basename(argv[1]) == "beet":  # `python /usr/bin/beet ...`
        rest = argv[2:]
    else:
        return None
    skip = False
    for arg in rest:
        if skip:
            skip = False
        elif arg in _BEET_VALUE_OPTIONS:
            skip = True
        elif not arg.startswith("-"):
            return arg
    return ""


def _beet_commands(proc: str = "/proc") -> List[int]:
    """PIDs of `beet` commands in this container that may write the library
    (Linux /proc; a read-only subcommand such as `beet ls` is not counted)."""
    pids: List[int] = []
    try:
        entries = os.listdir(proc)
    except OSError:
        return pids
    for pid in entries:
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        sub = _beet_subcommand(argv)
        if sub is not None and sub not in _READ_ONLY_BEET_COMMANDS:
            pids.append(int(pid))
    return sorted(pids)


def second_writers() -> List[Dict[str, Any]]:
    """Failing checks for other writers running now (empty when none).
    The network probe result is reused for ``_PROBE_TTL`` seconds."""
    now = time.monotonic()
    if _probe_cache["at"] is None or now - _probe_cache["at"] > _PROBE_TTL:
        _probe_cache.update(at=now, url=_separate_beets_address())
    checks: List[Dict[str, Any]] = []
    if _probe_cache["url"]:
        _check(checks, "separate_beets_running", False,
               f"A separate Beets server is listening at {_probe_cache['url']} (the old `beets` "
               "container?). Beets runs inside Web Manager and two programs must never write "
               "one library, so library changes are refused. Stop and remove the `beets` "
               "service from your Compose file (docs/INSTALLATION.md); changes resume within a "
               "minute after it stops.")
    pids = _beet_commands()
    if pids:
        _check(checks, "beet_command_running", False,
               f"A `beet` command that can change the library (pid {', '.join(map(str, pids))}) is "
               "running in this container, so Web Manager's library changes are refused until it "
               "finishes. While Web Manager runs, use only read-only `beet` commands (ls, stats, "
               "info, version).")
    return checks


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
                log.exception("Beets failed to start")
                checks: List[Dict[str, Any]] = []
                _check(checks, "engine_start_failed", False,
                       f"Beets could not start inside Web Manager: {type(ex).__name__}. See the log.")
                _failed_status = _summary(checks)
                raise EmbeddedBeetsUnavailable(_failed_status) from ex
    return _engine


def status() -> Dict[str, Any]:
    """The self-test with the live second-writer checks (starts the engine
    if needed); never raises."""
    try:
        return engine().current_status()
    except EmbeddedBeetsUnavailable as ex:
        return ex.status


def public_status() -> Dict[str, Any]:
    """For the unauthenticated /api/health: codes only, no paths or messages."""
    st = status()
    return {"engine": st["engine"], "ok": st["ok"],
            "codes": [c["code"] for c in st.get("checks", []) if not c["ok"]]}


def reset() -> None:
    """Forget the engine (tests; a restart does the same in production)."""
    global _engine, _failed_status, _lock_fd
    with _init_lock:
        _engine = None
        _failed_status = None
        _probe_cache.update(at=None, url="")
        if _lock_fd is not None:
            os.close(_lock_fd)  # closing the descriptor releases the lock
            _lock_fd = None
