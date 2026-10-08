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
from .schemas import beets_native_fields


class PluginCapabilityError(Exception):
    """Raised when a required Beets plugin is not loaded/configured."""


class PluginIncompatibleError(Exception):
    """Raised when the loaded Beets version's plugin internals don't match
    what this module expects (e.g. an internal helper moved/renamed)."""


_CAPABILITY_CLASS_NAMES = {
    "mbsync": "MBSyncPlugin",
    "mbsync_library": "MBSyncPlugin",
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


#: Most changed albums/singletons one library sync lists in its result; the
#: counters always cover everything.
MAX_LIBRARY_CHANGES = 200
#: Most failures listed in a library sync result.
MAX_LIBRARY_FAILURES = 100
#: Stop a library sync after this many albums in a row raised (MusicBrainz
#: down, for example) instead of failing every remaining album.
MAX_CONSECUTIVE_FAILURES = 10
_VALUE_CHARS = 120
_UNTRACKED_FIELDS = frozenset({"mtime"})  # changes on every tag write


def _field_values(model) -> Dict[str, Any]:
    from beets import util

    out: Dict[str, Any] = {}
    for key in model.keys():
        if key in _UNTRACKED_FIELDS:
            continue
        value = model.get(key)
        if isinstance(value, bytes):
            value = util.displayable_path(value)
        elif not (value is None or isinstance(value, (bool, int, float))):
            value = str(value)
        out[key] = value
    return out


def _short(value: Any) -> Any:
    return value[:_VALUE_CHARS] if isinstance(value, str) else value


def _diff(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, List[Any]]:
    return {k: [_short(before.get(k)), _short(after.get(k))]
            for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)}


def _album_state(lib, album_id: int):
    album = lib.get_album(album_id)
    if album is None:
        return None
    return _field_values(album), {it.id: _field_values(it) for it in album.items()}


class _FoundProbe:
    """The Library as MBSyncPlugin.albums()/singletons() see it, counting
    their own `lib.transaction()` calls. mbsync opens that transaction only
    once the Release/Recording was found at the metadata source; when it is
    not found it only logs and moves on. Our own reads go through the real
    Library (bound methods), so they are not counted."""

    def __init__(self, lib):
        self._lib = lib
        self.applied = 0

    def __getattr__(self, name):
        return getattr(self._lib, name)

    def transaction(self):
        self.applied += 1
        return self._lib.transaction()


def run_mbsync_library(lib, write: bool, cancel, lock, progress=None) -> Dict[str, Any]:
    """Run Beets' own mbsync over the whole library, one album or singleton
    at a time: exactly the targets `beet mbsync` with no query visits
    (albums with an mb_albumid, singletons with an mb_trackid), through
    MBSyncPlugin.albums()/singletons() with an exact `id:` query each.

    Files are never moved (move=False). Tags are written when ``write`` is
    true, but not by mbsync itself: its apply_item_changes() ignores
    Item.try_write()'s result, so a failed tag write would pass silently
    while the database changed. mbsync therefore runs with write=False and
    this function calls Item.try_write() on each item mbsync changed,
    stores the new mtime when it worked, and lists the item under
    ``write_failed`` when it did not (its database fields did change).

    Going one target at a time is what lets the caller cancel between
    targets (``cancel`` is a threading.Event), hold ``lock`` (the plugin's
    mutation lock) only for one album, and record what Beets changed: each
    target's stored fields are read before and after the call. Beets keeps
    no undo, so this change log is the only record; it lists at most
    MAX_LIBRARY_CHANGES targets, with values cut to 120 characters. Targets
    finished before a cancel or a restart stay changed (mbsync commits one
    transaction per album).

    A target whose Release/Recording the source no longer has is counted as
    ``not_found``. A target that raises is recorded as failed and the sync
    goes on, unless MAX_CONSECUTIVE_FAILURES targets in a row raise
    (``aborted``)."""
    plugin = _require_plugin("mbsync")
    if not hasattr(plugin, "singletons") or not hasattr(plugin, "albums"):
        raise PluginIncompatibleError(
            "mbsync plugin does not expose the expected singletons()/albums() methods"
        )
    from beets import util

    albums = list(lib.albums())
    singles = list(lib.items("singleton:true"))
    targets = [("album", a.id) for a in albums if a.mb_albumid]
    targets += [("singleton", i.id) for i in singles if i.mb_trackid]
    res: Dict[str, Any] = {
        "write": bool(write), "move": False,
        "albums_total": len(albums), "singletons_total": len(singles),
        "targets": len(targets), "processed": 0, "changed_albums": 0, "changed_singletons": 0,
        "changed_items": 0, "unchanged": 0, "not_found": 0,
        "skipped_no_id": len(albums) + len(singles) - len(targets),
        "skipped_empty": 0, "skipped_missing": 0, "failed_count": 0, "failed": [],
        "write_failed_count": 0, "write_failed": [],
        "cancelled": False, "aborted": False, "changes": [], "changes_truncated": False,
    }
    consecutive = 0
    for kind, tid in targets:
        if progress:  # counters after each finished, skipped or failed target
            progress({k: v for k, v in res.items() if k not in ("changes", "failed", "write_failed")})
        if cancel.is_set():
            res["cancelled"] = True
            break
        probe = _FoundProbe(lib)
        with lock:
            try:
                if kind == "album":
                    before = _album_state(lib, tid)
                    if before is None:
                        res["skipped_missing"] += 1
                        continue
                    if not before[1]:  # mbsync reads items()[0]: an empty album raises
                        res["skipped_empty"] += 1
                        continue
                    plugin.albums(probe, [f"id:{tid}"], False, False, False)
                    after = _album_state(lib, tid) or ({}, {})
                else:
                    item = lib.get_item(tid)
                    if item is None:
                        res["skipped_missing"] += 1
                        continue
                    before = ({}, {tid: _field_values(item)})
                    plugin.singletons(probe, [f"id:{tid}"], False, False, False)
                    refreshed = lib.get_item(tid)
                    after = ({}, {tid: _field_values(refreshed)} if refreshed is not None else {})
            except Exception as ex:
                res["failed_count"] += 1
                if len(res["failed"]) < MAX_LIBRARY_FAILURES:
                    res["failed"].append({"kind": kind, "id": tid, "error": f"{type(ex).__name__}: {str(ex)[:200]}"})
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    res["aborted"] = True
                    break
                continue
            consecutive = 0
            res["processed"] += 1
            if not probe.applied:
                res["not_found"] += 1
            album_fields = _diff(before[0], after[0])
            items = []
            for iid, old in before[1].items():
                fields = _diff(old, after[1].get(iid, {}))
                if not fields:
                    continue
                entry = {"item_id": iid, "title": _short(after[1].get(iid, {}).get("title") or old.get("title")),
                         "fields": fields}
                if write:
                    changed_item = lib.get_item(iid)
                    if changed_item is not None and changed_item.try_write():
                        changed_item.store()  # the new mtime, as apply_item_changes would
                    else:
                        entry["write_failed"] = True
                        res["write_failed_count"] += 1
                        if len(res["write_failed"]) < MAX_LIBRARY_FAILURES:
                            path = changed_item.path if changed_item is not None else b""
                            res["write_failed"].append({"item_id": iid, "title": entry["title"],
                                                        "path": _short(util.displayable_path(path))})
                items.append(entry)
        if not (album_fields or items):
            res["unchanged"] += 1
        else:
            res["changed_albums" if kind == "album" else "changed_singletons"] += 1
            res["changed_items"] += len(items)
            if len(res["changes"]) < MAX_LIBRARY_CHANGES:
                state = after[0] or before[0] or (after[1].get(tid) or {})
                res["changes"].append({
                    "kind": kind, "id": tid,
                    "artist": _short(state.get("albumartist") or state.get("artist")),
                    "album": _short(state.get("album")),
                    "album_fields": album_fields, "items": items,
                })
            else:
                res["changes_truncated"] = True
    return res


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
        # Beets 2.13+ keeps genres in the multi-valued ``genres`` field.
        current = album.get("genres") if "genres" in type(album)._fields else album.get("genre")
        if not force and current:
            continue
        try:
            genres, source = plugin._get_genre(album)
        except Exception as ex:
            raise PluginIncompatibleError(f"lastgenre plugin call failed: {type(ex).__name__}") from ex
        if genres:
            value = ", ".join(genres) if isinstance(genres, (list, tuple)) else str(genres)
            album.update(beets_native_fields(type(album), {"genre": value}))
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
