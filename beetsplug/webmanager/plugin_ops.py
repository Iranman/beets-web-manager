"""Isolated adapter for real Beets plugin operations (mbsync, fetchart,
embedart, lastgenre).

Beets Web Manager does not reimplement MusicBrainz metadata sync, album art
acquisition, artwork embedding, or Last.fm genre lookup logic -- these
operations call into the real, already-loaded Beets plugin instances
running inside this same process (the plugin objects Beets itself
constructed from the user's `plugins:` config), the same way the `beet
mbsync`/`fetchart`/`embedart`/`lastgenre` CLI commands do internally.

Coupling to internal plugin APIs is isolated here on purpose, in one
module, so it is easy to re-verify against a new Beets version and fails
cleanly (PluginCapabilityError / PluginIncompatibleError) rather than being
scattered across route handlers or silently returning a fake success. See
tests/test_stock_beets_acceptance.py for the acceptance proof against the
real, unmodified lscr.io/linuxserver/beets:latest image.
"""

from typing import Any, Dict, List, Optional


class PluginCapabilityError(Exception):
    """Raised when a required Beets plugin is not loaded/configured."""


class PluginIncompatibleError(Exception):
    """Raised when the loaded Beets version's plugin internals don't match
    what this module expects (e.g. an internal helper moved/renamed)."""


_CAPABILITY_CLASS_NAMES = {
    "mbsync": "MBSyncPlugin",
    "fetchart": "FetchArtPlugin",
    "embedart": "EmbedCoverArtPlugin",
    "lastgenre": "LastGenrePlugin",
    "mbsubmit": "AcoustidPlugin",
}


def _find_plugin_instance(plugin_class_name: str) -> Optional[Any]:
    from beets.plugins import find_plugins

    for p in find_plugins():
        if type(p).__name__ == plugin_class_name:
            return p
    return None


def is_capability_available(name: str) -> bool:
    """Report whether the named plugin is genuinely loaded right now.

    Used by the /webmanager/status capability handshake -- capabilities
    must reflect what can actually execute, not just what code exists.
    """
    cls_name = _CAPABILITY_CLASS_NAMES.get(name)
    if not cls_name:
        return False
    try:
        return _find_plugin_instance(cls_name) is not None
    except Exception:
        return False


def _require_plugin(name: str):
    cls_name = _CAPABILITY_CLASS_NAMES[name]
    plugin = _find_plugin_instance(cls_name)
    if plugin is None:
        raise PluginCapabilityError(f"{name} plugin is not loaded/configured")
    return plugin


# =============================================================================
# Upstream Symbol Reference & Version Coupling Documentation
# =============================================================================
#
# Tested against: Upstream Beets 2.14.x / 2.13.x / LinuxServer Beets :latest
#
# 1. mbsync:
#    - Upstream Class: `beetsplug.mbsync.MBSyncPlugin`
#    - Symbols Used:
#      * `MBSyncPlugin.singletons(lib, query, move, pretend, write)`
#      * `MBSyncPlugin.albums(lib, query, move, pretend, write)`
#    - Query Semantics:
#      * singletons: `id:<item id>` (MBSyncPlugin internally adds `singleton:true`)
#      * albums: `id:<album id>` (Album query uses `id`, NOT `album_id`)
#    - Coupling Type: Direct method invocation on loaded Beets plugin instance.
#
# 2. fetchart:
#    - Upstream Class: `beetsplug.fetchart.FetchArtPlugin`
#    - Symbols Used:
#      * `FetchArtPlugin.batch_fetch_art(lib, albums, force, quiet)`
#    - Coupling Type: Direct method invocation on loaded Beets plugin instance.
#
# 3. embedart:
#    - Upstream Class: `beetsplug.embedart.EmbedCoverArtPlugin`
#    - Symbols Used:
#      * `beetsplug._utils.art.embed_album(log, album, maxwidth, quiet, compare_threshold, ifempty, quality)`
#    - Coupling Type: Direct invocation of Beets internal art embedding helper.
#
# 4. lastgenre:
#    - Upstream Class: `beetsplug.lastgenre.LastGenrePlugin`
#    - Symbols Used:
#      * `LastGenrePlugin._get_genre(album)`
#    - Coupling Type: Internal helper invocation on loaded Beets plugin instance.
# =============================================================================


def run_mbsync(
    lib,
    item_ids: Optional[List[int]] = None,
    album_ids: Optional[List[int]] = None,
    move: bool = False,
    pretend: bool = False,
    write: bool = True,
) -> Dict[str, Any]:
    """Sync metadata from MusicBrainz using the real mbsync plugin.

    Targets explicit item_ids (singleton tracks) and/or album_ids.
    Neither reimplements nor bypasses the plugin's own singletons()/albums()
    logic -- this only selects which items/albums it runs against via exact-ID
    queries (never a caller-supplied free-form query).

    Album queries target `id:<album id>` (Beets Album primary key is `id`).
    Item queries target `id:<item id>`.

    Results report truthful counters distinguishing requested, processed,
    changed, and skipped targets.
    """
    plugin = _require_plugin("mbsync")
    if not hasattr(plugin, "singletons") or not hasattr(plugin, "albums"):
        raise PluginIncompatibleError(
            "mbsync plugin does not expose the expected singletons()/albums() methods"
        )

    requested_items = len(item_ids or [])
    requested_albums = len(album_ids or [])
    processed_items = 0
    processed_albums = 0
    changed_items = 0
    changed_albums = 0
    skipped_items = 0
    skipped_albums = 0

    try:
        for iid in item_ids or []:
            item = lib.get_item(int(iid))
            if not item:
                skipped_items += 1
                continue
            if not getattr(item, "mb_trackid", ""):
                # Upstream mbsync skips singletons without mb_trackid
                skipped_items += 1
                continue

            # Capture baseline values to accurately detect changes
            before_vals = {k: item[k] for k in ("title", "artist", "album", "year", "track", "mb_trackid") if k in item}
            plugin.singletons(lib, [f"id:{int(iid)}"], move, pretend, write)
            processed_items += 1

            refreshed = lib.get_item(int(iid))
            if refreshed:
                after_vals = {k: refreshed[k] for k in before_vals}
                if after_vals != before_vals:
                    changed_items += 1

        for aid in album_ids or []:
            album = lib.get_album(int(aid))
            if not album:
                skipped_albums += 1
                continue
            if not getattr(album, "mb_albumid", ""):
                # Upstream mbsync skips albums without mb_albumid
                skipped_albums += 1
                continue

            # Capture baseline album values
            before_vals = {k: album[k] for k in ("album", "albumartist", "year", "mb_albumid") if k in album}
            plugin.albums(lib, [f"id:{int(aid)}"], move, pretend, write)
            processed_albums += 1

            refreshed_album = lib.get_album(int(aid))
            if refreshed_album:
                after_vals = {k: refreshed_album[k] for k in before_vals}
                if after_vals != before_vals:
                    changed_albums += 1

    except Exception as ex:
        raise PluginIncompatibleError(f"mbsync plugin call failed: {type(ex).__name__}") from ex

    return {
        "requested_items": requested_items,
        "requested_albums": requested_albums,
        "processed_items": processed_items,
        "processed_albums": processed_albums,
        "changed_items": changed_items,
        "changed_albums": changed_albums,
        "skipped_items": skipped_items,
        "skipped_albums": skipped_albums,
        "synced_items": changed_items,
        "synced_albums": changed_albums,
    }


def run_fetchart(lib, album_ids: List[int], force: bool = False) -> Dict[str, Any]:
    """Fetch cover art for explicit albums using the real fetchart plugin.

    Uses the plugin's own configured sources (which default to including
    the local `filesystem` source -- no arbitrary user-supplied URL is ever
    fetched here).
    """
    plugin = _require_plugin("fetchart")
    if not hasattr(plugin, "batch_fetch_art"):
        raise PluginIncompatibleError(
            "fetchart plugin does not expose the expected batch_fetch_art() method"
        )

    albums = [a for a in (lib.get_album(int(aid)) for aid in album_ids) if a is not None]
    if not albums:
        return {"processed_albums": 0}

    try:
        plugin.batch_fetch_art(lib, albums, force, True)
    except Exception as ex:
        raise PluginIncompatibleError(f"fetchart plugin call failed: {type(ex).__name__}") from ex

    fetched = sum(1 for a in albums if getattr(a, "artpath", None))
    return {"processed_albums": len(albums), "albums_with_art": fetched}


def run_embedart(lib, album_ids: List[int]) -> Dict[str, Any]:
    """Embed each album's existing artpath into its items' tags using the
    real embedart plugin. Does not accept an arbitrary file path or URL --
    only embeds art Beets itself already associated with the album (e.g.
    via fetchart)."""
    plugin = _require_plugin("embedart")

    art_module = None
    import_errors = []
    for module_path in ("beets.art", "beetsplug._utils.art"):
        try:
            import importlib

            art_module = importlib.import_module(module_path)
            break
        except ImportError as ex:
            import_errors.append(f"{module_path}: {ex}")
    if art_module is None or not hasattr(art_module, "embed_album"):
        raise PluginIncompatibleError(
            "Could not locate a compatible embed_album() helper for this Beets version "
            f"(tried: {', '.join(import_errors)})"
        )

    maxwidth = plugin.config["maxwidth"].get(int) if "maxwidth" in plugin.config else 0
    quality = plugin.config["quality"].get(int) if "quality" in plugin.config else 0
    compare_threshold = (
        plugin.config["compare_threshold"].get(int) if "compare_threshold" in plugin.config else 0
    )
    ifempty = plugin.config["ifempty"].get(bool) if "ifempty" in plugin.config else False

    albums = [a for a in (lib.get_album(int(aid)) for aid in album_ids) if a is not None]
    embedded = 0
    for album in albums:
        if not getattr(album, "artpath", None):
            continue
        try:
            art_module.embed_album(
                plugin._log, album, maxwidth, False, compare_threshold, ifempty, quality=quality
            )
            embedded += 1
        except Exception as ex:
            raise PluginIncompatibleError(f"embedart plugin call failed: {type(ex).__name__}") from ex

    return {"processed_albums": len(albums), "embedded_albums": embedded}


def run_lastgenre(lib, album_ids: List[int], force: bool = False) -> Dict[str, Any]:
    """Repair genre tags for explicit albums using the real lastgenre
    plugin (Last.fm-derived genre matching against configured whitelist)."""
    plugin = _require_plugin("lastgenre")
    if not hasattr(plugin, "_get_genre"):
        raise PluginIncompatibleError(
            "lastgenre plugin does not expose the expected _get_genre() method"
        )

    albums = [a for a in (lib.get_album(int(aid)) for aid in album_ids) if a is not None]
    updated = 0
    for album in albums:
        if not force and getattr(album, "genre", ""):
            continue
        try:
            genres, source = plugin._get_genre(album)
        except Exception as ex:
            raise PluginIncompatibleError(f"lastgenre plugin call failed: {type(ex).__name__}") from ex
        if genres:
            album.genre = ", ".join(genres) if isinstance(genres, (list, tuple)) else str(genres)
            album.store()
            updated += 1

    return {"processed_albums": len(albums), "updated_albums": updated}


def run_mbsubmit(lib, item_ids: List[int], api_key: Optional[str] = None) -> Dict[str, Any]:
    """Submit AcoustID fingerprints for explicit items using the real
    chroma plugin's submit_items() (the same function `beet submit` uses).
    Never reimplements fingerprinting or the AcoustID submission format."""
    plugin = _require_plugin("mbsubmit")

    try:
        from beetsplug.chroma import submit_items
    except ImportError as ex:
        raise PluginIncompatibleError(
            f"Could not locate chroma's submit_items() helper for this Beets version: {ex}"
        ) from ex

    userkey = api_key
    if not userkey:
        try:
            from beets import config as beets_config
            userkey = beets_config["acoustid"]["apikey"].as_str()
        except Exception:
            userkey = None
    if not userkey:
        raise PluginCapabilityError("No AcoustID user API key configured")

    items = [it for it in (lib.get_item(int(iid)) for iid in item_ids) if it is not None]
    try:
        submit_items(plugin._log, userkey, items)
    except Exception as ex:
        raise PluginIncompatibleError(f"mbsubmit plugin call failed: {type(ex).__name__}") from ex

    return {"submitted_items": len(items)}
