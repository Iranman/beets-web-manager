"""Fields-limited bulk library reads (plugin 1.18.0, capability ``library_fields``).

Stock web's ``GET /item/`` and ``GET /album/`` serialise every field of every
row, and ``/item/`` also stats every item's file for its size. Web Manager's
library builder needs about a dozen fields, so this route returns only those,
read through Beets' own Library API. Field names come from a fixed allowlist;
request input never reaches a query or an attribute lookup outside it.
"""

from flask import current_app, g, jsonify, request
from beets import util

from . import operations as ops

READ_FIELDS = {
    "item": frozenset({
        "id", "path", "album_id", "title", "track", "disc", "tracktotal", "mb_trackid",
        "albumartist", "artist", "album", "year",
    }),
    "album": frozenset({
        "id", "album", "albumartist", "albumartist_credit", "albumartists", "albumartists_credit",
        "mb_albumartistid", "mb_albumartistids", "mb_albumid", "mb_releasegroupid",
        "albumtype", "albumtypes", "artpath",
    }),
}
_PATH_FIELD = {"item": "path", "album": "artpath"}
_ROOT = {"item": "items", "album": "albums"}

# ponytail: operations.py is held by another branch; move this into
# _CORE_CAPABILITIES (and import read_ops there) once it is free.
if "library_fields" not in ops._CORE_CAPABILITIES:
    ops._CORE_CAPABILITIES.append("library_fields")


def _bad(message: str, code: str):
    return jsonify({"error": message, "error_code": code}), 400


@ops.webmanager_bp.route("/fields/<string:entity>", methods=["GET"])
def read_fields(entity: str):
    """``GET /webmanager/fields/<item|album>?fields=a,b`` -> ``{"items"|"albums": [...]}``.

    ``id`` is always returned. Paths follow stock web's ``include_paths``:
    absolute text when it is on, left out when it is off."""
    allowed = READ_FIELDS.get(entity)
    if allowed is None:
        return _bad("entity must be item or album", "INVALID_ENTITY")
    fields = {f for f in request.args.get("fields", "").split(",") if f}
    if not fields:
        return _bad("fields is required", "INVALID_FIELD")
    unknown = fields - allowed
    if unknown:
        # Echo nothing from the request: the names are user input.
        return _bad(f"{len(unknown)} field(s) are not readable here", "INVALID_FIELD")
    fields.add("id")
    path_field = _PATH_FIELD[entity]
    if not current_app.config.get("INCLUDE_PATHS", False):
        fields.discard(path_field)
    names = sorted(fields)

    lib = g.lib
    rows = []
    for obj in (lib.items() if entity == "item" else lib.albums()):
        row = {name: obj.get(name) for name in names}
        if path_field in row:
            value = row[path_field]
            row[path_field] = util.displayable_path(value) if value else ""
        rows.append(row)
    return jsonify({_ROOT[entity]: rows})
