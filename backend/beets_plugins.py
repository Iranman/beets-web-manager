"""Authoritative Beets Plugin Manifest & Management Engine for Beets Web Manager.

Stock lscr.io/linuxserver/beets:latest is the sole, authoritative Beets
runtime -- Web Manager never runs Beets itself, never loads Beets plugins
locally, and never owns plugin-loading decisions. This module's job is
narrowly: maintain the plugin manifest, provision bundled plugin files into
the shared /config/beetsplug mount, safely update config.yaml, and inspect
stock Beets' actual loaded-plugin state over BeetsAdapter (never a local
`import beets` runtime check).

Provides:
1. One authoritative manifest (`BEETS_PLUGIN_MANIFEST`) classifying every plugin
   used by Beets Web Manager as REQUIRED, OPTIONAL, or INTEGRATION, and as
   builtin, bundled, or third-party.
2. Plugin discovery and verification against the real, running stock Beets
   process, via BeetsAdapter.get_plugin_status() -- falling back to
   in-process `beets.plugins.find_plugins()` only for local/unit-test
   environments where no stock Beets process is reachable at all.
3. Bundled plugin provisioning to the shared `/config/beetsplug` mount.
4. Safe, atomic YAML configuration updates with timestamped backups and
   preservation of existing user settings and custom plugins.
5. Strict security controls: curated allowlists only, no arbitrary package/plugin
   execution, path traversal containment.
"""

from __future__ import annotations

import datetime
import difflib
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

log = logging.getLogger("beets.plugins.manifest")

ROOT = Path(__file__).resolve().parents[1]

# Shared /config integration paths
DEFAULT_CONFIG_DIR = Path("/config")
DEFAULT_BEETSPLUG_DIR = Path("/config/beetsplug")
DEFAULT_PLUGIN_PACKAGES_DIR = Path("/config/plugin-packages")
SOURCE_BEETSPLUG_DIR = ROOT / "beetsplug"


class PluginCategory:
    # REQUIRED: the integration transport itself (`web` + `webmanager`).
    # Web Manager cannot operate without these, so they are the only
    # plugins it ever adds to an existing config.yaml automatically.
    REQUIRED = "REQUIRED"
    # RECOMMENDED: plugins that power specific Web Manager features
    # (artwork, fingerprints, ReplayGain, ...). They are shipped enabled
    # in config.yaml.example for fresh installs, but on an existing
    # library they are only *reported*; enabling them is an explicit
    # opt-in via preview_recommended_plugins()/apply_recommended_plugins().
    RECOMMENDED = "RECOMMENDED"
    OPTIONAL = "OPTIONAL"
    INTEGRATION = "INTEGRATION"


class PluginType:
    BUILTIN = "builtin"
    BUNDLED = "bundled"
    THIRD_PARTY = "third_party"


@dataclass(frozen=True)
class PluginDefinition:
    name: str
    display_name: str
    category: str  # REQUIRED, RECOMMENDED, OPTIONAL, INTEGRATION
    plugin_type: str  # builtin, bundled, third_party
    description: str
    python_packages: List[str] = field(default_factory=list)
    binary_dependencies: List[str] = field(default_factory=list)
    commands: List[str] = field(default_factory=list)
    template_fields: List[str] = field(default_factory=list)
    bundled_file: Optional[str] = None
    config_defaults: Optional[Dict[str, Any]] = None
    integration_env_var: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Authoritative Beets Plugin Manifest
# ─────────────────────────────────────────────────────────────────────────────

BEETS_PLUGIN_MANIFEST: Dict[str, PluginDefinition] = {
    # ── Recommended Plugins (feature plugins; opt-in on existing libraries) ──
    "fetchart": PluginDefinition(
        name="fetchart",
        display_name="Fetch Artwork",
        category=PluginCategory.RECOMMENDED,
        plugin_type=PluginType.BUILTIN,
        description="High-resolution cover art discovery, retrieval, and caching.",
        commands=["fetchart"],
    ),
    "embedart": PluginDefinition(
        name="embedart",
        display_name="Embed Artwork",
        category=PluginCategory.RECOMMENDED,
        plugin_type=PluginType.BUILTIN,
        description="Embeds cover images directly into media file tags across formats.",
        commands=["embedart"],
    ),
    "scrub": PluginDefinition(
        name="scrub",
        display_name="Tag Scrubber",
        category=PluginCategory.RECOMMENDED,
        plugin_type=PluginType.BUILTIN,
        description="Cleans extraneous and corrupt metadata tags from audio files.",
        commands=["scrub"],
    ),
    "zero": PluginDefinition(
        name="zero",
        display_name="Field Zeroing",
        category=PluginCategory.RECOMMENDED,
        plugin_type=PluginType.BUILTIN,
        description="Nulls out unwanted metadata fields on import according to rules.",
    ),
    "ftintitle": PluginDefinition(
        name="ftintitle",
        display_name="Featured Artist Formatter",
        category=PluginCategory.RECOMMENDED,
        plugin_type=PluginType.BUILTIN,
        description="Standard featuring artist formatting (moves feat. from artist to title).",
    ),
    "fromfilename": PluginDefinition(
        name="fromfilename",
        display_name="From Filename Guesser",
        category=PluginCategory.RECOMMENDED,
        plugin_type=PluginType.BUILTIN,
        description="Infers artist/title metadata from file paths for un-tagged audio.",
    ),
    "mbsync": PluginDefinition(
        name="mbsync",
        display_name="MusicBrainz Resync",
        category=PluginCategory.RECOMMENDED,
        plugin_type=PluginType.BUILTIN,
        description="Synchronizes existing library metadata with updated MusicBrainz database.",
        commands=["mbsync"],
    ),
    "mbsubmit": PluginDefinition(
        name="mbsubmit",
        display_name="MusicBrainz Submit",
        category=PluginCategory.RECOMMENDED,
        plugin_type=PluginType.BUILTIN,
        description="Generates submission URLs and tracklists for unmatched releases.",
        commands=["mbsubmit"],
    ),
    "chroma": PluginDefinition(
        name="chroma",
        display_name="Chroma / AcoustID",
        category=PluginCategory.RECOMMENDED,
        plugin_type=PluginType.BUILTIN,
        description="AcoustID audio fingerprinting, automated matching, and deduplication.",
        # pyacoustid/fpcalc run inside the stock Beets container, never
        # inside Web Manager -- this plugin's health comes from stock
        # Beets' own live loaded_plugins signal (see `loaded` below), not
        # a local Python-package/binary check against the wrong process.
        binary_dependencies=["fpcalc"],
        commands=["submit"],
    ),
    "replaygain": PluginDefinition(
        name="replaygain",
        display_name="ReplayGain Normalization",
        category=PluginCategory.RECOMMENDED,
        plugin_type=PluginType.BUILTIN,
        description="Calculates volume normalization peak and gain tags using ffmpeg.",
        binary_dependencies=["ffmpeg"],
        commands=["replaygain"],
    ),
    "lastgenre": PluginDefinition(
        name="lastgenre",
        display_name="Canonical Genre Tagging",
        category=PluginCategory.RECOMMENDED,
        plugin_type=PluginType.BUILTIN,
        description="Canonical genre resolution, normalization, and repair.",
        commands=["lastgenre"],
    ),
    "discpath": PluginDefinition(
        name="discpath",
        display_name="Multi-Disc Subfolders (discpath)",
        category=PluginCategory.RECOMMENDED,
        plugin_type=PluginType.BUNDLED,
        description="Web Manager multi-disc album directory formatting (disc_subfolder).",
        bundled_file="discpath.py",
        template_fields=["disc_subfolder"],
    ),
    "musicbrainz": PluginDefinition(
        name="musicbrainz",
        display_name="MusicBrainz Autotagger (Core)",
        category=PluginCategory.RECOMMENDED,
        plugin_type=PluginType.BUILTIN,
        description="Core MusicBrainz album matching, release queries, and track identification.",
        commands=[],
    ),

    # ── Integration-Specific Plugins ─────────────────────────────────────────
    "discogs": PluginDefinition(
        name="discogs",
        display_name="Discogs Database Matching",
        category=PluginCategory.INTEGRATION,
        plugin_type=PluginType.BUILTIN,
        description="Discogs database candidate matching, extra tags, and art.",
        integration_env_var="DISCOGS_TOKEN",
    ),
    "listenbrainz": PluginDefinition(
        name="listenbrainz",
        display_name="ListenBrainz Integration",
        category=PluginCategory.INTEGRATION,
        plugin_type=PluginType.BUILTIN,
        description="ListenBrainz scrobbling, listen history, and user feedback.",
        python_packages=["pylistenbrainz==0.5.1"],
        integration_env_var="LISTENBRAINZ_TOKEN",
    ),
    "deezer": PluginDefinition(
        name="deezer",
        display_name="Deezer Metadata & Art",
        category=PluginCategory.INTEGRATION,
        plugin_type=PluginType.BUILTIN,
        description="Deezer cover art and metadata search provider.",
        python_packages=["deezer-python==2.1.0"],
    ),
    "spotify": PluginDefinition(
        name="spotify",
        display_name="Spotify Ingestion",
        category=PluginCategory.INTEGRATION,
        plugin_type=PluginType.BUILTIN,
        description="Spotify playlist metadata ingestion.",
        integration_env_var="SPOTIFY_CLIENT_ID",
    ),
    "plexsync": PluginDefinition(
        name="plexsync",
        display_name="Plex Sync",
        category=PluginCategory.INTEGRATION,
        plugin_type=PluginType.BUILTIN,
        description="Plex library synchronization.",
        integration_env_var="PLEX_TOKEN",
    ),
    "bpsync": PluginDefinition(
        name="bpsync",
        display_name="Beatport Sync",
        category=PluginCategory.INTEGRATION,
        plugin_type=PluginType.BUILTIN,
        description="Beatport metadata synchronization.",
    ),

    # ── Optional Capability Plugins ──────────────────────────────────────────
    "convert": PluginDefinition(
        name="convert",
        display_name="Audio Converter",
        category=PluginCategory.OPTIONAL,
        plugin_type=PluginType.BUILTIN,
        description="Audio transcoding, format conversion, and waveform generation.",
        binary_dependencies=["ffmpeg"],
        commands=["convert"],
    ),
    "duplicates": PluginDefinition(
        name="duplicates",
        display_name="Duplicate Finder",
        category=PluginCategory.OPTIONAL,
        plugin_type=PluginType.BUILTIN,
        description="Identifies duplicate tracks and releases in the music library.",
        commands=["duplicates"],
    ),
    "missing": PluginDefinition(
        name="missing",
        display_name="Missing Tracks Detector",
        category=PluginCategory.OPTIONAL,
        plugin_type=PluginType.BUILTIN,
        description="Detects missing tracks from incomplete albums.",
        commands=["missing"],
    ),
    "smartplaylist": PluginDefinition(
        name="smartplaylist",
        display_name="Smart Playlists",
        category=PluginCategory.OPTIONAL,
        plugin_type=PluginType.BUILTIN,
        description="Generates dynamic .m3u playlists from library query definitions.",
        commands=["splupdate"],
    ),
    "unimported": PluginDefinition(
        name="unimported",
        display_name="Unimported Files Finder",
        category=PluginCategory.OPTIONAL,
        plugin_type=PluginType.BUILTIN,
        description="Locates media files in music directories not registered in Beets library.",
        commands=["unimported"],
    ),
    "lyrics": PluginDefinition(
        name="lyrics",
        display_name="Lyrics Downloader",
        category=PluginCategory.OPTIONAL,
        plugin_type=PluginType.BUILTIN,
        description="Downloads song lyrics from Genius, Musixmatch, and web sources.",
        python_packages=["beautifulsoup4==4.12.3"],
        commands=["lyrics"],
    ),
    "parentwork": PluginDefinition(
        name="parentwork",
        display_name="Parent Work Fetcher",
        category=PluginCategory.OPTIONAL,
        plugin_type=PluginType.BUILTIN,
        description="Fetches parent work metadata for classical compositions.",
    ),
    "edit": PluginDefinition(
        name="edit",
        display_name="CLI Text Tag Editor",
        category=PluginCategory.OPTIONAL,
        plugin_type=PluginType.BUILTIN,
        description="Edit metadata in a text editor via CLI.",
        commands=["edit"],
    ),
    "web": PluginDefinition(
        name="web",
        display_name="Beets Built-in Web Server",
        # REQUIRED, not optional: this is the read transport BeetsAdapter
        # depends on for every non-mutation read, and stock Beets' own
        # default supervised service is `beet web` -- without this
        # plugin enabled, the container crash-loops with "unknown
        # command 'web'".
        category=PluginCategory.REQUIRED,
        plugin_type=PluginType.BUILTIN,
        description="Beets simple built-in web server -- the read transport BeetsAdapter depends on.",
        commands=["web"],
    ),
    "webmanager": PluginDefinition(
        name="webmanager",
        display_name="Web Manager Integration Plugin",
        # REQUIRED: the sole authenticated mutation transport BeetsAdapter
        # depends on. Provisioned as a bundled directory (not a single
        # bundled_file) by provision_bundled_plugins(); its health/loaded
        # state must come from stock Beets' own live handshake
        # (`loaded_plugins`), never a local check, since this plugin
        # only ever runs inside the stock Beets container.
        category=PluginCategory.REQUIRED,
        plugin_type=PluginType.BUNDLED,
        description="Beets Web Manager's own integration plugin -- the sole authenticated mutation transport.",
        commands=[],
    ),
    "hook": PluginDefinition(
        name="hook",
        display_name="Event Hook Runner",
        category=PluginCategory.OPTIONAL,
        plugin_type=PluginType.BUILTIN,
        description="Runs custom shell commands on Beets events.",
    ),
}

# Ordered list of plugins required for full Web Manager functionality
REQUIRED_PLUGIN_NAMES: List[str] = [
    name for name, p in BEETS_PLUGIN_MANIFEST.items() if p.category == PluginCategory.REQUIRED
]

REQUIRED_CONFIG_PLUGINS: List[str] = [
    name for name, p in BEETS_PLUGIN_MANIFEST.items()
    if p.category == PluginCategory.REQUIRED and name != "musicbrainz"
]

# Feature plugins Web Manager recommends but never adds to an existing
# config.yaml on its own (BI-5). Fresh installs still get them from
# config.yaml.example.
RECOMMENDED_PLUGIN_NAMES: List[str] = [
    name for name, p in BEETS_PLUGIN_MANIFEST.items() if p.category == PluginCategory.RECOMMENDED
]

RECOMMENDED_CONFIG_PLUGINS: List[str] = [
    name for name in RECOMMENDED_PLUGIN_NAMES if name != "musicbrainz"
]

OPTIONAL_PLUGIN_NAMES: List[str] = [
    name for name, p in BEETS_PLUGIN_MANIFEST.items() if p.category == PluginCategory.OPTIONAL
]

INTEGRATION_PLUGIN_NAMES: List[str] = [
    name for name, p in BEETS_PLUGIN_MANIFEST.items() if p.category == PluginCategory.INTEGRATION
]


@dataclass
class PluginHealthStatus:
    name: str
    display_name: str
    category: str
    plugin_type: str
    description: str
    installed: bool
    enabled: bool
    loaded: bool
    healthy: bool
    binaries: Dict[str, bool] = field(default_factory=dict)
    python_packages: Dict[str, bool] = field(default_factory=dict)
    commands_available: Dict[str, bool] = field(default_factory=dict)
    template_fields_available: Dict[str, bool] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Python Path & Environment Setup
# ─────────────────────────────────────────────────────────────────────────────

def ensure_plugin_sys_path(config_dir: Optional[Path | str] = None) -> None:
    """Ensure `/config/beetsplug` and `/config/plugin-packages` are in sys.path."""
    cfg_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    beetsplug_dir = cfg_dir / "beetsplug"
    packages_dir = cfg_dir / "plugin-packages"

    for d in (str(beetsplug_dir), str(packages_dir)):
        if d not in sys.path and Path(d).exists():
            sys.path.insert(0, d)


# ─────────────────────────────────────────────────────────────────────────────
# Bundled Plugin Provisioning
# ─────────────────────────────────────────────────────────────────────────────

def provision_bundled_plugins(config_dir: Optional[Path | str] = None) -> List[str]:
    """Copy all Web Manager bundled plugins from `beetsplug/` into `/config/beetsplug`.

    Preserves existing user plugins in `/config/beetsplug`. Uses atomic copy.
    Returns list of provisioned plugin file names.
    """
    cfg_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    target_beetsplug_dir = cfg_dir / "beetsplug"
    target_beetsplug_dir.mkdir(parents=True, exist_ok=True)

    # Ensure .webmanager_api_key file is always provisioned in config directory
    provision_api_key_file(cfg_dir)

    # Locate source beetsplug directory
    source_dir = SOURCE_BEETSPLUG_DIR
    if not source_dir.exists():
        fallback = Path("/app/beetsplug")
        if fallback.exists():
            source_dir = fallback

    provisioned: List[str] = []
    if not source_dir.exists():
        return provisioned

    import shutil
    import secrets

    for entry in source_dir.iterdir():
        if entry.is_file() and entry.suffix == ".py" and entry.name != "__init__.py":
            target_file = target_beetsplug_dir / entry.name
            content = entry.read_bytes()

            # Atomic write to target file if not identical
            if not target_file.exists() or target_file.read_bytes() != content:
                tmp_file = target_file.with_suffix(".tmp." + entry.suffix)
                try:
                    tmp_file.write_bytes(content)
                    try:
                        os.chmod(tmp_file, 0o644)
                    except Exception:
                        pass
                    tmp_file.replace(target_file)
                    provisioned.append(entry.name)
                except Exception as exc:
                    try:
                        tmp_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise RuntimeError(f"Failed to copy bundled plugin {entry.name}: {exc}") from exc
            else:
                provisioned.append(entry.name)

        elif entry.is_dir() and not entry.name.startswith((".", "_", "__pycache__")):
            target_sub = target_beetsplug_dir / entry.name
            target_sub.mkdir(parents=True, exist_ok=True)
            for sub_entry in entry.rglob("*"):
                if sub_entry.is_file() and not sub_entry.name.endswith(".pyc") and "__pycache__" not in sub_entry.parts:
                    rel_p = sub_entry.relative_to(entry)
                    dest_file = target_sub / rel_p
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    if not dest_file.exists() or dest_file.read_bytes() != sub_entry.read_bytes():
                        shutil.copy2(sub_entry, dest_file)
            provisioned.append(entry.name)

    # Ensure .webmanager_api_key file is provisioned in config directory
    provision_api_key_file(cfg_dir)

    return provisioned


def provision_api_key_file(config_dir: Path) -> bool:
    """Securely provision /config/.webmanager_api_key at mode 0600 with 256 bits entropy.

    Invariants:
    - Exactly 64 hex characters (256 bits entropy via secrets.token_hex(32))
    - Created with 0o600 permissions from the start (no world/group readable window)
    - Rejects existing symlinks (does not follow or overwrite symlinks)
    - Does not overwrite an existing valid 64-hex key
    - Atomic file creation via O_CREAT | O_EXCL + fsync + atomic rename
    - Sets PUID/PGID ownership when configured on POSIX
    - Never logs token
    """
    import secrets
    import os

    api_key_file = config_dir / ".webmanager_api_key"
    key_str_path = str(api_key_file)

    # 1. Reject symlinks immediately
    if os.path.islink(key_str_path):
        log.error("Rejecting symlinked API key file at %s", key_str_path)
        return False

    hex_pattern = re.compile(r"^[0-9a-fA-F]{64}$")

    # 2. Check if valid key already exists
    if api_key_file.is_file():
        try:
            existing = api_key_file.read_text(encoding="utf-8").strip()
            if hex_pattern.match(existing):
                return True
        except Exception:
            pass

    # 3. Generate 256-bit token (64 hex characters)
    token = secrets.token_hex(32)
    payload = (token + "\n").encode("utf-8")

    # 4. Low-level file creation with mode 0o600 from the start
    tmp_path = config_dir / f".webmanager_api_key.tmp.{secrets.token_hex(8)}"
    tmp_str_path = str(tmp_path)

    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        fd = os.open(tmp_str_path, flags, 0o600)
        try:
            os.write(fd, payload)
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)

        # Chown to configured PUID/PGID if running as root on POSIX
        if hasattr(os, "chown") and os.name == "posix":
            puid_str = os.environ.get("PUID")
            pgid_str = os.environ.get("PGID")
            if puid_str and pgid_str:
                try:
                    puid = int(puid_str)
                    pgid = int(pgid_str)
                    os.chown(tmp_str_path, puid, pgid)
                except Exception:
                    pass

        # Atomic replace
        os.replace(tmp_str_path, key_str_path)
        return True
    except Exception as ex:
        log.warning("Could not provision .webmanager_api_key in %s: %s", config_dir, type(ex).__name__)
        try:
            if os.path.exists(tmp_str_path):
                os.unlink(tmp_str_path)
        except Exception:
            pass
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Safe YAML Configuration Management
# ─────────────────────────────────────────────────────────────────────────────

_PLUGIN_MIGRATION_BACKUP_PREFIX = "config.yaml.bak-plugins-"


def parse_configured_plugins(config_text: str) -> List[str]:
    """Parse configured plugins from YAML text without losing order."""
    plugins: List[str] = []
    match = re.search(r"(?m)^plugins:[ \t]*(.*)$((?:\n[ \t]+-[ \t]*\S.*$)*)", config_text)
    if not match:
        return plugins

    inline_val = match.group(1).strip()
    if inline_val:
        plugins.extend(inline_val.split())

    list_block = match.group(2) or ""
    for line in list_block.splitlines():
        stripped = line.strip()
        if stripped.startswith("-"):
            token = stripped.lstrip("- \t").strip()
            if token and token not in plugins:
                plugins.append(token)

    return plugins


def parse_configured_pluginpath(config_text: str) -> List[str]:
    """Parse configured pluginpath entries from YAML text."""
    paths: List[str] = []
    match = re.search(r"(?m)^pluginpath:[ \t]*(.*)$((?:\n[ \t]+-[ \t]*\S.*$)*)", config_text)
    if not match:
        return paths

    inline_val = match.group(1).strip()
    if inline_val:
        paths.append(inline_val)

    list_block = match.group(2) or ""
    for line in list_block.splitlines():
        stripped = line.strip()
        if stripped.startswith("-"):
            token = stripped.lstrip("- \t").strip()
            if token and token not in paths:
                paths.append(token)

    return paths


_TRUTHY_YAML = {"yes", "true", "on", "y", "1"}
_FALSY_YAML = {"no", "false", "off", "n", "0"}

# Settings blocks added together with a plugin name when that plugin is newly
# added and its block is completely absent. Adding just the plugin NAME
# without these is worse than not adding it at all: stock Beets' own `beet
# web` default service crash-loops ("unknown command 'web'") without a
# loadable `web` plugin, and `replaygain` raises a hard, plugin-load-aborting
# FatalReplayGainError without an explicit `backend:`. `replaygain` is only
# ever added through the explicit recommended-plugin opt-in (BI-5); it is
# never added, and an existing replaygain block is never rewritten, on
# startup.
_PLUGIN_SETTINGS_BLOCKS: Tuple[Tuple[str, List[str]], ...] = (
    ("web", [
        "web:",
        "    host: 0.0.0.0",
        "    port: 8337",
        "    readonly: yes",
        "    include_paths: yes",
    ]),
    ("webmanager", [
        "webmanager:",
        "    api_key_file: /config/.webmanager_api_key",
    ]),
    ("replaygain", [
        "replaygain:",
        "    auto: no",
        "    backend: ffmpeg",
    ]),
)


class BeetsConfigEditError(RuntimeError):
    """config.yaml cannot be edited safely (unsupported layout, I/O error)."""


def _find_top_level_block(text: str, key: str) -> Optional["re.Match[str]"]:
    """Locate a block-style top-level mapping `key:` and its indented body.

    Group 1 is the header line, group 2 the (possibly empty) indented body,
    including blank and comment lines inside it.
    """
    return re.search(
        rf"(?m)^({re.escape(key)}:[ \t]*(?:#.*)?)$((?:\n(?:[ \t]+.*|[ \t]*))*?)(?=\n\S|\Z)",
        text,
    )


def read_web_include_paths(config_text: str) -> Optional[bool]:
    """Return the configured `web.include_paths` (None when not set)."""
    block = _find_top_level_block(config_text, "web")
    if not block:
        return None
    m = re.search(r"(?m)^[ \t]+include_paths:[ \t]*([^#\s]*)", block.group(2) or "")
    if not m:
        return None
    raw = m.group(1).strip().strip("'\"").lower()
    if raw in _TRUTHY_YAML:
        return True
    if raw in _FALSY_YAML:
        return False
    return None


def _set_web_include_paths(text: str, *, overwrite_false: bool) -> Tuple[str, bool]:
    """Return (new_text, changed) with `web.include_paths: yes` ensured.

    Text-level edit so comments and formatting elsewhere are preserved. An
    explicit `include_paths: no` is only rewritten when ``overwrite_false``
    (an explicit, user-initiated fix action); startup provisioning never
    flips a value the user set.
    """
    if re.search(r"(?m)^web:[ \t]*[^\s#]", text):
        raise BeetsConfigEditError(
            "config.yaml uses an inline (flow-style) `web:` mapping; add "
            "`include_paths: yes` under `web:` manually"
        )
    block = _find_top_level_block(text, "web")
    if not block:
        return text.rstrip("\n") + "\n\nweb:\n    include_paths: yes\n", True
    body = block.group(2) or ""
    existing = re.search(r"(?m)^([ \t]+)include_paths:[ \t]*([^#\n]*?)[ \t]*(#.*)?$", body)
    if existing:
        current = existing.group(2).strip().strip("'\"").lower()
        if current in _TRUTHY_YAML:
            return text, False
        if not overwrite_false:
            return text, False
        comment = f"  {existing.group(3)}" if existing.group(3) else ""
        new_body = (
            body[:existing.start()]
            + f"{existing.group(1)}include_paths: yes{comment}"
            + body[existing.end():]
        )
    else:
        indent_m = re.search(r"(?m)^([ \t]+)\S", body)
        indent = indent_m.group(1) if indent_m else "    "
        # Insert right after the last non-blank line of the block body so a
        # trailing blank line / comment separator stays where it was.
        stripped_body = body.rstrip()
        new_body = stripped_body + f"\n{indent}include_paths: yes" + body[len(stripped_body):]
    start, end = block.span(2)
    return text[:start] + new_body + text[end:], True


def _plan_config_yaml_plugins(
    text: str,
    plugins_to_ensure: List[str],
    pluginpath_to_ensure: List[str],
) -> Tuple[str, List[str], bool]:
    """Pure planner shared by provisioning, preview and opt-in apply.

    Returns (new_text, missing_plugins_added, changed).
    """
    changed = False
    current_plugins = parse_configured_plugins(text)
    current_pluginpath = parse_configured_pluginpath(text)

    # 1. Update plugins line / block
    missing_plugins = [p for p in plugins_to_ensure if p not in current_plugins]
    if missing_plugins:
        changed = True
        new_plugins = list(current_plugins) + missing_plugins
        plugins_match = re.search(r"(?m)^plugins:[ \t]*(.*)$((?:\n[ \t]+-[ \t]*\S.*$)*)", text)
        if plugins_match:
            new_line = "plugins: " + " ".join(new_plugins)
            text = text[:plugins_match.start()] + new_line + text[plugins_match.end():]
        else:
            text = f"plugins: {' '.join(new_plugins)}\n" + text

    # 2. Update pluginpath line / block
    obsolete_paths = {"/opt/beets-web-manager-agent/beetsplug"}
    filtered_pluginpath = [p for p in current_pluginpath if p not in obsolete_paths]
    missing_paths = [p for p in pluginpath_to_ensure if p not in filtered_pluginpath]

    if missing_paths or len(filtered_pluginpath) != len(current_pluginpath):
        changed = True
        final_pluginpath = filtered_pluginpath + missing_paths
        if not final_pluginpath:
            final_pluginpath = ["/config/beetsplug"]

        pluginpath_match = re.search(r"(?m)^pluginpath:[ \t]*(.*)$((?:\n[ \t]+-[ \t]*\S.*$)*)", text)
        pluginpath_block = "pluginpath:\n" + "".join(f"  - {p}\n" for p in final_pluginpath)

        if pluginpath_match:
            text = text[:pluginpath_match.start()] + pluginpath_block.rstrip("\n") + text[pluginpath_match.end():]
        else:
            # Place after plugins: line if possible
            if re.search(r"(?m)^plugins:.*$", text):
                text = re.sub(r"(?m)^plugins:.*$\n?", lambda m: m.group(0) + pluginpath_block, text, count=1)
            else:
                text = pluginpath_block + text

    # 3. Settings blocks for newly added plugins (never touches an EXISTING
    # block -- only adds one when it is completely absent).
    for block_name, block_lines in _PLUGIN_SETTINGS_BLOCKS:
        if block_name not in missing_plugins:
            continue
        if re.search(rf"(?m)^{block_name}:", text):
            continue  # user already has this block -- never overwrite it
        changed = True
        text = text.rstrip("\n") + "\n\n" + "\n".join(block_lines) + "\n"

    # 4. `web.include_paths` is part of the required transport contract
    # (BI-6): file-path reads return nothing without it. Add the key when it
    # is ABSENT from an existing block-style `web:` mapping; an explicit
    # `no` is left alone and surfaced as a setup warning with a fix action.
    if "web" in plugins_to_ensure and re.search(r"(?m)^web:[ \t]*(?:#.*)?$", text):
        if read_web_include_paths(text) is None:
            text, added = _set_web_include_paths(text, overwrite_false=False)
            changed = changed or added

    return text, missing_plugins, changed


def _write_config_text(
    path: Path,
    text: str,
    *,
    backup_name: Optional[str],
    require_backup: bool,
) -> Optional[str]:
    """Back up ``path`` (when ``backup_name``) and atomically replace it."""
    backup_path: Optional[Path] = None
    if backup_name:
        backup_path = path.parent / backup_name
        try:
            shutil.copy2(str(path), str(backup_path))
            try:
                os.chmod(backup_path, 0o600)
            except Exception:
                pass
        except Exception as exc:
            if require_backup:
                raise BeetsConfigEditError(f"Could not back up {path.name}: {type(exc).__name__}") from exc
            backup_path = None

    tmp_path = path.with_suffix(".tmp.yaml")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        try:
            os.chmod(tmp_path, 0o644)
        except Exception:
            pass
        tmp_path.replace(path)
    except Exception as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise BeetsConfigEditError(f"Failed to write updated config.yaml: {type(exc).__name__}") from exc
    return str(backup_path) if backup_path else None


def update_config_yaml_plugins(
    config_path: Path | str,
    ensure_plugins: Optional[List[str]] = None,
    ensure_pluginpath: Optional[List[str]] = None,
    backup: bool = True,
) -> Tuple[bool, str]:
    """Safely update config.yaml with the integration transport plugins.

    - By default ensures only ``web`` + ``webmanager`` (REQUIRED); feature
      plugins (RECOMMENDED) are never added to an existing config here --
      see preview_recommended_plugins()/apply_recommended_plugins() (BI-5).
    - Preserves all existing plugins and their custom configuration.
    - Ensures `/config/beetsplug` is in `pluginpath:`.
    - Removes obsolete `/opt/beets-web-manager-agent/beetsplug` path.
    - Adds `include_paths: yes` to an existing `web:` block that lacks it.
    - Creates a timestamped backup before modification.
    - Writes atomically via temporary file and replace.
    - A missing config.yaml (fresh install) is created from
      config.yaml.example, which still enables the recommended set.

    Returns (changed: bool, message: str).
    """
    path = Path(config_path)
    plugins_to_ensure = ensure_plugins if ensure_plugins is not None else list(REQUIRED_CONFIG_PLUGINS)
    pluginpath_to_ensure = ensure_pluginpath if ensure_pluginpath is not None else ["/config/beetsplug"]

    if not path.exists():
        # Create default config.yaml with canonical settings
        example_path = ROOT / "config.yaml.example"
        if example_path.exists():
            content = example_path.read_text(encoding="utf-8")
        else:
            plugin_str = " ".join(plugins_to_ensure)
            content = (
                f"plugins: {plugin_str}\n"
                "pluginpath:\n"
                "  - /config/beetsplug\n"
                "directory: /music\n"
                "library: /config/musiclibrary.blb\n"
                "import:\n"
                "    write: yes\n"
                "    copy: yes\n"
                "    move: no\n"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp." + path.suffix)
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        return True, "Created default config.yaml with required plugins"

    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc

    text, missing_plugins, changed = _plan_config_yaml_plugins(text, plugins_to_ensure, pluginpath_to_ensure)

    if not changed:
        return False, "All required plugins and pluginpath already configured"

    backup_name = None
    if backup:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_name = f"{_PLUGIN_MIGRATION_BACKUP_PREFIX}{ts}"
    try:
        _write_config_text(path, text, backup_name=backup_name, require_backup=False)
    except BeetsConfigEditError as exc:
        raise RuntimeError(str(exc)) from exc

    if missing_plugins:
        return True, f"Configured {len(missing_plugins)} missing plugins ({', '.join(missing_plugins)}) in config.yaml"
    return True, "Updated pluginpath / web settings in config.yaml"


def _read_config_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BeetsConfigEditError(f"{path.name} does not exist") from exc
    except Exception as exc:
        raise BeetsConfigEditError(f"Could not read {path.name}: {type(exc).__name__}") from exc


def _timestamped_backup_name(path: Path) -> str:
    return f"{path.name}.bak-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"


def preview_recommended_plugins(config_path: Path | str) -> Dict[str, Any]:
    """Read-only preview of enabling the missing RECOMMENDED plugins (BI-5)."""
    path = Path(config_path)
    text = _read_config_text(path)
    configured = parse_configured_plugins(text)
    missing = [p for p in RECOMMENDED_CONFIG_PLUGINS if p not in configured]
    new_text, _added, _changed = _plan_config_yaml_plugins(
        text, missing, parse_configured_pluginpath(text) or ["/config/beetsplug"]
    )
    diff = "".join(
        difflib.unified_diff(
            text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile="config.yaml",
            tofile="config.yaml (proposed)",
        )
    )
    return {
        "recommended": list(RECOMMENDED_CONFIG_PLUGINS),
        "configured": [p for p in RECOMMENDED_CONFIG_PLUGINS if p in configured],
        "missing": missing,
        "would_change": new_text != text,
        "diff": diff,
    }


def apply_recommended_plugins(config_path: Path | str, plugins: Iterable[str]) -> Dict[str, Any]:
    """Explicit opt-in: add the selected RECOMMENDED plugins to config.yaml.

    Only names from RECOMMENDED_CONFIG_PLUGINS are accepted (ValueError
    otherwise). Existing plugin settings blocks are never rewritten. Takes a
    timestamped backup (a failed backup aborts the write) and writes
    atomically. Beets must be restarted to load newly enabled plugins.
    """
    requested = [str(p).strip() for p in plugins]
    unknown = sorted({p for p in requested if p not in RECOMMENDED_CONFIG_PLUGINS})
    if unknown or not requested:
        raise ValueError("unsupported plugin selection")
    ordered = [p for p in RECOMMENDED_CONFIG_PLUGINS if p in requested]

    path = Path(config_path)
    text = _read_config_text(path)
    new_text, added, changed = _plan_config_yaml_plugins(
        text, ordered, parse_configured_pluginpath(text) or ["/config/beetsplug"]
    )
    if not changed:
        return {"ok": True, "changed": False, "added": [], "backup": None, "restart_required": False}
    backup = _write_config_text(path, new_text, backup_name=_timestamped_backup_name(path), require_backup=True)
    return {"ok": True, "changed": True, "added": added, "backup": backup, "restart_required": True}


def ensure_web_include_paths(config_path: Path | str) -> Dict[str, Any]:
    """Explicit fix action: set `web.include_paths: yes` in config.yaml (BI-6).

    Additive text edit that preserves comments; flips an explicit `no` (the
    user asked for the fix). Idempotent. Timestamped backup (a failed backup
    aborts the write) and atomic replace. Beets must be restarted.
    """
    path = Path(config_path)
    text = _read_config_text(path)
    new_text, changed = _set_web_include_paths(text, overwrite_false=True)
    if not changed:
        return {"ok": True, "changed": False, "backup": None, "restart_required": False}
    backup = _write_config_text(path, new_text, backup_name=_timestamped_backup_name(path), require_backup=True)
    return {"ok": True, "changed": True, "backup": backup, "restart_required": True}


# ─────────────────────────────────────────────────────────────────────────────
# Verification Engine
# ─────────────────────────────────────────────────────────────────────────────

def _check_binary(name: str, available_binaries: Optional[Dict[str, bool]] = None) -> Tuple[bool, Optional[str]]:
    """Check if a required executable binary is available on PATH or in remote diagnostics."""
    if available_binaries is not None and name in available_binaries:
        avail = bool(available_binaries[name])
        return (avail, name if avail else None)
    try:
        loc = shutil.which(name)
        return (loc is not None, loc)
    except Exception:
        return (False, None)


def _check_python_package(pkg_spec: str) -> Tuple[bool, str]:
    """Check if a required Python module is importable and read its version."""
    pkg_name = pkg_spec.split("==")[0].split(">=")[0].strip()
    import_map = {
        "pyacoustid": "acoustid",
        "beautifulsoup4": "bs4",
        "deezer-python": "deezer",
        "pylistenbrainz": "pylistenbrainz",
        "pylast": "pylast",
        "pillow": "PIL",
    }
    mod_name = import_map.get(pkg_name.lower(), pkg_name)
    try:
        mod = importlib.import_module(mod_name)
        ver = getattr(mod, "__version__", "installed")
        return True, str(ver)
    except Exception as exc:
        return False, f"Not importable: {exc}"


def verify_plugin(
    plugin_def: PluginDefinition,
    configured_plugins: Set[str],
    loaded_plugins: Set[str],
    config_dir: Optional[Path | str] = None,
    available_binaries: Optional[Dict[str, bool]] = None,
) -> PluginHealthStatus:
    """Perform comprehensive health verification for a single plugin."""
    name = plugin_def.name
    cfg_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    beetsplug_dir = cfg_dir / "beetsplug"

    errors: List[str] = []
    binaries_status: Dict[str, bool] = {}
    python_status: Dict[str, bool] = {}
    commands_status: Dict[str, bool] = {}
    template_fields_status: Dict[str, bool] = {}

    enabled = name in configured_plugins or name == "musicbrainz"  # MusicBrainz is core built-in
    loaded = name in loaded_plugins or name == "musicbrainz"

    # MusicBrainz is special: built into Beets core
    if name == "musicbrainz":
        enabled = True
        loaded = True

    # 1. Binary Dependencies
    for b in plugin_def.binary_dependencies:
        found, loc = _check_binary(b, available_binaries)
        binaries_status[b] = found
        if not found and (plugin_def.category == PluginCategory.REQUIRED or enabled):
            errors.append(f"Required binary '{b}' is not installed on PATH.")

    # 2. Python Packages
    for p in plugin_def.python_packages:
        found, ver_msg = _check_python_package(p)
        python_status[p] = found
        if not found and (plugin_def.category == PluginCategory.REQUIRED or enabled):
            errors.append(f"Required Python package '{p}' is not available ({ver_msg}).")

    # 3. Bundled File Verification
    installed = True
    if plugin_def.plugin_type == PluginType.BUNDLED and plugin_def.bundled_file:
        bundled_target = beetsplug_dir / plugin_def.bundled_file
        source_target = SOURCE_BEETSPLUG_DIR / plugin_def.bundled_file
        if not bundled_target.exists() and not source_target.exists() and not Path(f"/app/beetsplug/{plugin_def.bundled_file}").exists():
            installed = False
            if plugin_def.category == PluginCategory.REQUIRED or enabled:
                errors.append(f"Bundled plugin file '{plugin_def.bundled_file}' is missing from {beetsplug_dir}.")

    # 4. Configuration and Loaded Status
    if plugin_def.category == PluginCategory.REQUIRED:
        if not enabled:
            errors.append(f"Plugin '{name}' is required by Web Manager but not enabled in config.yaml.")
        elif not loaded and not errors:
            errors.append(f"Plugin '{name}' is enabled in config.yaml but failed to load in the Beets runtime.")

    # 5. Commands
    for cmd in plugin_def.commands:
        commands_status[cmd] = loaded

    # 6. Template Fields
    for tf in plugin_def.template_fields:
        template_fields_status[tf] = loaded

    # Determine health
    if plugin_def.category == PluginCategory.REQUIRED:
        healthy = (len(errors) == 0) and loaded
    elif plugin_def.category == PluginCategory.INTEGRATION:
        if enabled:
            healthy = (len(errors) == 0) and loaded
        else:
            healthy = True  # Not enabled, optional integration
    else:  # RECOMMENDED / OPTIONAL
        if enabled:
            healthy = (len(errors) == 0) and loaded
        else:
            healthy = True  # Not required; disabled is a valid choice

    # Note generation
    if not enabled:
        if plugin_def.category == PluginCategory.INTEGRATION:
            note = "Optional integration (disabled / not configured)"
        elif plugin_def.category == PluginCategory.RECOMMENDED:
            note = "Recommended (not enabled in config.yaml; opt in via preview)"
        elif plugin_def.category == PluginCategory.OPTIONAL:
            note = "Optional (disabled in config.yaml)"
        else:
            note = "; ".join(errors) if errors else "Disabled"
    elif not loaded:
        note = "; ".join(errors) if errors else "Enabled but not loaded by Beets engine"
    elif healthy:
        note = "Ready"
    else:
        note = "; ".join(errors)

    return PluginHealthStatus(
        name=name,
        display_name=plugin_def.display_name,
        category=plugin_def.category,
        plugin_type=plugin_def.plugin_type,
        description=plugin_def.description,
        installed=installed,
        enabled=enabled,
        loaded=loaded,
        healthy=healthy,
        binaries=binaries_status,
        python_packages=python_status,
        commands_available=commands_status,
        template_fields_available=template_fields_status,
        errors=errors,
        note=note,
    )


def verify_all_plugins(
    config_dir: Optional[Path | str] = None,
    *,
    remote_status: Optional[Dict[str, Any]] = None,
    loaded_plugins: Optional[Iterable[str]] = None,
    available_binaries: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """Inspect and verify all plugins across categories."""
    cfg_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    ensure_plugin_sys_path(cfg_dir)

    config_path = cfg_dir / "config.yaml"
    config_text = ""
    if config_path.exists():
        try:
            config_text = config_path.read_text(encoding="utf-8")
        except Exception:
            pass

    configured = set(parse_configured_plugins(config_text))

    # Query stock Beets plugin integration status via BeetsAdapter
    loaded: Set[str] = set(loaded_plugins) if loaded_plugins is not None else set()
    if not loaded and remote_status is not None:
        if isinstance(remote_status, dict):
            raw_loaded = remote_status.get("loaded_plugins") or remote_status.get("plugins") or []
            loaded = set(raw_loaded)

    if not loaded and remote_status is None and loaded_plugins is None:
        try:
            from backend.beets_adapter import beets_adapter
            plugin_res = beets_adapter.get_plugin_status()
            if isinstance(plugin_res, dict) and plugin_res.get("protocol_version"):
                raw_loaded = plugin_res.get("loaded_plugins") or []
                loaded = set(raw_loaded) | {"web", "webmanager"}
        except Exception:
            pass

    # If BeetsAdapter didn't return plugins (e.g. running in test or local), check in-process Beets
    if not loaded:
        try:
            import beets.plugins as bp
            loaded = {p.name for p in bp.find_plugins()}
        except Exception:
            # Fall back to configured list for non-failing builtins
            loaded = set(configured)

    # Derive binary availability from remote_status if not explicitly given
    binaries = dict(available_binaries) if available_binaries is not None else {}
    if "fpcalc" not in binaries and isinstance(remote_status, dict) and "fpcalc_available" in remote_status:
        binaries["fpcalc"] = bool(remote_status.get("fpcalc_available"))
    if "ffmpeg" not in binaries and isinstance(remote_status, dict) and "ffmpeg_available" in remote_status:
        binaries["ffmpeg"] = bool(remote_status.get("ffmpeg_available"))

    results: List[PluginHealthStatus] = []
    for name, pdef in BEETS_PLUGIN_MANIFEST.items():
        st = verify_plugin(pdef, configured, loaded, cfg_dir, available_binaries=binaries)
        results.append(st)

    required_statuses = [r for r in results if r.category == PluginCategory.REQUIRED]
    recommended_statuses = [r for r in results if r.category == PluginCategory.RECOMMENDED]
    optional_statuses = [r for r in results if r.category == PluginCategory.OPTIONAL]
    integration_statuses = [r for r in results if r.category == PluginCategory.INTEGRATION]

    all_required_healthy = all(r.healthy for r in required_statuses)
    required_healthy_count = sum(1 for r in required_statuses if r.healthy)

    return {
        "ok": all_required_healthy,
        "all_required_healthy": all_required_healthy,
        "required_count": len(required_statuses),
        "required_healthy_count": required_healthy_count,
        "plugins": [r.to_dict() for r in results],
        "recommended_missing": [r.name for r in recommended_statuses if not r.enabled],
        "categories": {
            "required": [r.to_dict() for r in required_statuses],
            "recommended": [r.to_dict() for r in recommended_statuses],
            # Back-compat: UIs predating the RECOMMENDED tier (BI-5) render
            # only required/optional/integration; recommended plugins were
            # formerly "required" and are still listed here so they stay
            # visible there.
            "optional": [r.to_dict() for r in recommended_statuses + optional_statuses],
            "integration": [r.to_dict() for r in integration_statuses],
        },
        "summary": {
            "total": len(results),
            "healthy": sum(1 for r in results if r.healthy),
            "errors": [f"{r.name}: {r.note}" for r in results if not r.healthy and r.category == PluginCategory.REQUIRED],
        },
    }


def provision_and_verify(config_dir: Optional[Path | str] = None) -> Dict[str, Any]:
    """Execute complete plugin provisioning workflow:

    1. Copy bundled plugins to `/config/beetsplug`.
    2. Safely update `config.yaml` with missing REQUIRED transport plugins
       (web, webmanager) only; recommended plugins are opt-in.
    3. Re-run verification against stock Beets (BeetsAdapter.get_plugin_status()
       is a live HTTP call, not cached, so no forced-refresh step is needed
       after writing config -- unlike the deleted embedded control agent's
       own 30s status cache).
    4. Return full diagnostic response.
    """
    cfg_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    cfg_dir.mkdir(parents=True, exist_ok=True)

    # 1. Provision bundled plugins
    provisioned_files = provision_bundled_plugins(cfg_dir)

    # 2. Update config.yaml
    config_path = cfg_dir / "config.yaml"
    changed, msg = update_config_yaml_plugins(config_path)

    # 3. Verify all plugins against the live stock Beets process.
    verification = verify_all_plugins(cfg_dir)
    verification["provisioned_files"] = provisioned_files
    verification["config_updated"] = changed
    verification["message"] = msg

    return verification
