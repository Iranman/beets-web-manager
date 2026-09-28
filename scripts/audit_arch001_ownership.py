#!/usr/bin/env python3
"""ARCH-001 ownership inventory for app.py.

Classifies every top-level function in app.py into an owning domain and
records its size, route exposure, mutation capability, callers and
dependencies. The committed inventory is docs/arch001_app_ownership.json.

    python scripts/audit_arch001_ownership.py            # check (CI)
    python scripts/audit_arch001_ownership.py --write    # regenerate
    python scripts/audit_arch001_ownership.py --summary  # per-domain totals

Classification is rule-based (route path prefix, then function-name tokens)
with explicit per-function overrides in the inventory's "overrides" map.
The check fails when a *substantial* function (>= SUBSTANTIAL_LINES lines,
not counting its docstring) has no domain -- formatting-only edits never
change a function's classification, and small glue helpers are exempt.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
INVENTORY = ROOT / "docs" / "arch001_app_ownership.json"
SUBSTANTIAL_LINES = 25

# Domain -> desired owner module. Existing modules are preferred owners.
OWNERS = {
    "ROUTE_ONLY": "app.py (routes)",
    "IMPORT": "backend/import_service.py",
    "IMPORT_REVIEW": "backend/import_review_service.py",
    "IMPORT_RECONCILIATION": "backend/import_reconciliation_service.py",
    "LIBRARY": "backend/library_service.py",
    "MATCHING": "backend/matching_service.py",
    "CLEANUP": "backend/cleanup_service.py",
    "DEDUP": "backend/dedup_service.py",
    "MAINTENANCE": "backend/maintenance_service.py",
    "PLAYLIST": "backend/playlist_service.py",
    "REPLACEMENT": "backend/replacement_service.py",
    "MUSICBRAINZ": "backend/musicbrainz_service.py",
    "ACOUSTID": "backend/acoustid_service.py",
    "AI": "backend/ai_service.py",
    "PLEX": "backend/plex_service.py",
    "LIDARR": "routes_lidarr.py",
    "SLSKD": "backend/slskd_service.py",
    "YTDLP": "backend/ytdlp_service.py",
    "ACQUISITION": "backend/acquisition_service.py",
    "ARTWORK": "backend/artwork_service.py",
    "CONFIG": "backend/config_service.py",
    "SETUP": "backend/setup_service.py",
    "AUTH": "backend/auth_service.py",
    "TRANSACTIONS": "backend/transaction_service.py",
    "JOBS": "backend/job_service.py",
    "DISPLAY/SERIALIZATION": "backend/serializers.py",
    "COMPATIBILITY": "app.py (compatibility glue)",
    "BOOTSTRAP": "app.py (application creation)",
    "CORE": "backend/app_runtime.py",
}

# A domain may own several modules (a service split for layering, plus its
# HTTP route module). Code living in any of them is EXTRACTED; code living in a
# different domain's module (a helper shared with a lower layer) is
# EXTRACTED_SHARED.
EXTRA_OWNERS = {
    "AI": ["backend/ai_batch_state_service.py", "backend/ai_evidence_service.py", "routes_import.py"],
    "IMPORT_REVIEW": ["backend/pending_review_store.py", "routes_import.py"],
    "IMPORT": ["routes_import.py"],
    "IMPORT_RECONCILIATION": ["routes_import.py"],
    "LIBRARY": ["routes_library.py"],
    "ARTWORK": ["routes_library.py"],
    "CLEANUP": ["routes_cleanup.py"],
    "DEDUP": ["routes_cleanup.py"],
    "PLAYLIST": ["routes_playlist.py"],
    "PLEX": ["routes_playlist.py"],
    "MAINTENANCE": ["routes_maintenance.py"],
    "TRANSACTIONS": ["routes_maintenance.py"],
    "ACQUISITION": ["routes_acquisition.py"],
    "YTDLP": ["routes_acquisition.py"],
    "REPLACEMENT": ["routes_acquisition.py"],
    "CONFIG": ["routes_system.py", "backend/serializers.py"],
    "AUTH": ["routes_system.py", "app.py"],
    "ROUTE_ONLY": ["routes_system.py", "app.py"],
    "BOOTSTRAP": ["app.py"],
    "COMPATIBILITY": ["app.py"],
}

ROUTE_RULES = [
    ("/api/import-reconciliation", "IMPORT_RECONCILIATION"),
    ("/api/import-review", "IMPORT_REVIEW"),
    ("/api/ai-pending-review", "IMPORT_REVIEW"),
    ("/api/ai-review", "IMPORT_REVIEW"),
    ("/api/unmatched-tracks", "IMPORT_REVIEW"),
    ("/api/import", "IMPORT"),
    ("/api/folders", "IMPORT"),
    ("/api/recent-imports", "IMPORT"),
    ("/api/ai-batch", "AI"),
    ("/api/ai-match", "AI"),
    ("/api/dedup", "DEDUP"),
    ("/api/clean", "CLEANUP"),
    ("/api/jobs/maintenance", "MAINTENANCE"),
    ("/api/jobs", "JOBS"),
    ("/api/playlist", "PLAYLIST"),
    ("/api/download", "ACQUISITION"),
    ("/api/acquisition", "ACQUISITION"),
    ("/api/qbittorrent", "ACQUISITION"),
    ("/api/ytdlp", "YTDLP"),
    ("/api/transactions", "TRANSACTIONS"),
    ("/api/config", "CONFIG"),
    ("/api/settings", "CONFIG"),
    ("/api/plugins", "CONFIG"),
    ("/api/music-format", "REPLACEMENT"),
    ("/api/library/music-format", "REPLACEMENT"),
    ("/api/plex", "PLEX"),
    ("/api/auth", "AUTH"),
    ("/login", "AUTH"),
    ("/logout", "AUTH"),
    ("/api/rebuild-album-art", "ARTWORK"),
    ("/api/fetch-missing-art", "ARTWORK"),
    ("/api/save-album-art", "ARTWORK"),
    ("/api/disk-art", "ARTWORK"),
    ("/api/album-art", "ARTWORK"),
    ("/api/release-art", "ARTWORK"),
    ("/api/artist-image", "ARTWORK"),
    ("/api/albums", "LIBRARY"),
    ("/api/library", "LIBRARY"),
    ("/api/items", "LIBRARY"),
    ("/api/item", "LIBRARY"),
    ("/api/artist", "LIBRARY"),
    ("/api/artists", "LIBRARY"),
    ("/api/search", "LIBRARY"),
    ("/api/stats", "LIBRARY"),
    ("/api/recent", "LIBRARY"),
    ("/api/browse", "LIBRARY"),
    ("/api/candidates", "IMPORT_REVIEW"),
    ("/api/restart", "MAINTENANCE"),
    ("/api/health", "BOOTSTRAP"),
    ("/", "BOOTSTRAP"),
]

# Ordered name-token rules (first match wins).
NAME_RULES = [
    (r"reconcil", "IMPORT_RECONCILIATION"),
    (r"(import_review|pending_review|manual_review|_pending|add_to_pending|queue_folder_for_manual_review|candidate_track|unmatched|review_queue|review_item)", "IMPORT_REVIEW"),
    (r"(dedup|duplicate|resolver)", "DEDUP"),
    (r"(maintenance|clean_all|library_health|auto_scan|scan_job|scan_loop)", "MAINTENANCE"),
    (r"playlist|spotify|m3u", "PLAYLIST"),
    (r"(music_format|replacement|format_pref)", "REPLACEMENT"),
    (r"plex", "PLEX"),
    (r"(^_?ai_|openai|track_ai|_ai_|score_mb_release|llm)", "AI"),
    (r"(slskd|soulseek)", "SLSKD"),
    (r"(ytdlp|yt_dlp|spotiflac|youtube)", "YTDLP"),
    (r"(acq_|acquisition|qbit|torrent|download|lidarr|wanted)", "ACQUISITION"),
    (r"(acoustid|audio_identity|fingerprint|fpcalc)", "ACOUSTID"),
    (r"(^_?mb_|musicbrainz|fetch_mb|discogs|resolve_release|prefer_album_mb|release_group)", "MUSICBRAINZ"),
    (r"(clean|cleanup|prune|empty_folder|no_audio|junk|template_token|filename_cleanup|sidecar)", "CLEANUP"),
    (r"(artwork|_art_|_art$|cover|image|embed_art|art_url)", "ARTWORK"),
    (r"(album_track|best_album_track|album_title_match|match_tracks|album_mb|preflight|track_align|tracklist)", "MATCHING"),
    (r"(import|reimport|stage_|staging|confirmed_import)", "IMPORT"),
    (r"transaction", "TRANSACTIONS"),
    (r"(^_?jobs?_|start_python|wait_for_child_job|running_job|job_)", "JOBS"),
    (r"(config|settings|beet_plugins|plugin|^_?env|load_env|beets_config|pluginpath)", "CONFIG"),
    (r"(setup|first_run|bootstrap|install)", "SETUP"),
    (r"(auth|login|logout|csrf|session|token|security|redact|password|rate_limit)", "AUTH"),
    (r"(album|artist|library|item|folder|root|lib_|stamp|relocate|move|merge|repair|genre|track|scan|path_template|year|label)", "LIBRARY"),
    (r"(serialize|compact|to_dict|format_|json_from|summary|_dict$|payload)", "DISPLAY/SERIALIZATION"),
    (r"^_?(s|safe_|path|norm|normalize|normalise|is_|env_int|now|utc|sanitize|clip|coerce|int_|float_|bool_|as_|to_|parse|extract|strip)", "CORE"),]


def _decorator_route(node):
    for d in node.decorator_list:
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in {
            "get", "post", "put", "patch", "delete", "route"
        } and d.args and isinstance(d.args[0], ast.Constant):
            method = d.func.attr.upper() if d.func.attr != "route" else "ROUTE"
            return method, d.args[0].value
    return None


def _body_lines(node) -> int:
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    if not body:
        return 0
    return body[-1].end_lineno - body[0].lineno + 1


MUTATION_CALL_RE = re.compile(
    r"^(apply_|plan_|update_album_metadata|relocate_album|merge_|write_tags|delete_|remove_|modify|mbsubmit|"
    r"run_command|start_python|quarantine|unlink|rmtree|rename|replace)"
)


def classify(name, route, overrides):
    if name in overrides:
        return overrides[name]
    if route:
        path = route[1]
        for prefix, domain in ROUTE_RULES:
            if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "-") or \
                    (prefix != "/" and path.startswith(prefix)):
                return domain
        return "ROUTE_ONLY"
    for pattern, domain in NAME_RULES:
        if re.search(pattern, name):
            return domain
    return "UNCLASSIFIED"


def family_paths():
    """app.py plus the modules ARCH-001 extracted from it (inventory order)."""
    inv = load_inventory()
    mods = [ROOT / m for m in (inv.get("extracted_modules") or [])]
    return [APP] + [m for m in mods if m.exists()]


def analyze(overrides=None):
    overrides = overrides or {}
    parsed = []
    for path in family_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(ROOT).as_posix()
        parsed += [(rel, n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    top_names = {n.name for _, n in parsed}
    refs = {}
    mutation = {}
    for _, fn in parsed:
        used, mut = set(), False
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Name) and sub.id in top_names and sub.id != fn.name:
                used.add(sub.id)
            if isinstance(sub, ast.Call):
                callee = sub.func.attr if isinstance(sub.func, ast.Attribute) else getattr(sub.func, "id", "")
                if callee and MUTATION_CALL_RE.match(callee):
                    mut = True
        refs[fn.name] = used
        mutation[fn.name] = mut
    callers = defaultdict(set)
    for name, used in refs.items():
        for u in used:
            callers[u].add(name)
    rows = []
    for module, fn in parsed:
        route = _decorator_route(fn)
        domain = classify(fn.name, route, overrides)
        owner = OWNERS.get(domain, "UNCLASSIFIED")
        owners = [owner] + EXTRA_OWNERS.get(domain, [])
        if module == "app.py":
            # app.py keeps only application creation, request hooks, static /
            # SPA serving and compatibility glue (BOOTSTRAP/AUTH hook/COMPAT).
            status = "APP_GLUE" if domain in ("BOOTSTRAP", "COMPATIBILITY", "AUTH", "ROUTE_ONLY") else "IN_APP"
        elif module in owners:
            status = "EXTRACTED"
        else:
            # Layered extraction: a helper shared across domains lives in the
            # lowest-layer module that uses it (see docs/arch001_service_decomposition.md).
            status = "EXTRACTED_SHARED"
        rows.append({
            "name": fn.name,
            "module": module,
            "lines": [fn.lineno, fn.end_lineno],
            "size": fn.end_lineno - fn.lineno + 1,
            "body_lines": _body_lines(fn),
            "route": {"method": route[0], "path": route[1]} if route else None,
            "mutation_capable": mutation[fn.name],
            "domain": domain,
            "desired_owner": owner,
            "migration_status": status,
            "callers": sorted(callers[fn.name])[:25],
            "caller_count": len(callers[fn.name]),
            "dependencies": sorted(refs[fn.name])[:40],
        })
    return rows


def load_inventory():
    if INVENTORY.exists():
        return json.loads(INVENTORY.read_text(encoding="utf-8"))
    return {"overrides": {}, "extracted": {}, "functions": []}


def summarize(rows):
    by = defaultdict(lambda: [0, 0])
    for r in rows:
        if r.get("module", "app.py") != "app.py":
            continue
        by[r["domain"]][0] += 1
        by[r["domain"]][1] += r["size"]
    return {k: {"functions": v[0], "lines": v[1]} for k, v in sorted(by.items(), key=lambda kv: -kv[1][1])}


def main(argv):
    inv = load_inventory()
    rows = analyze(inv.get("overrides") or {})
    if "--write" in argv:
        inv["functions"] = rows
        inv["summary"] = summarize(rows)
        inv["app_py_lines"] = len(APP.read_text(encoding="utf-8").splitlines())
        INVENTORY.write_text(json.dumps(inv, indent=1, sort_keys=False) + "\n", encoding="utf-8")
        print(f"wrote {INVENTORY.relative_to(ROOT)}: {len(rows)} functions")
    if "--summary" in argv:
        for domain, stats in summarize(rows).items():
            print(f"{domain:24s} functions={stats['functions']:4d} lines={stats['lines']:6d}")
    missing = [r for r in rows if r["domain"] == "UNCLASSIFIED" and r["body_lines"] >= SUBSTANTIAL_LINES]
    # ARCH-001 closure: app.py holds application glue only; domain code in
    # app.py fails the check (move it to its owning service or route module).
    in_app = [r for r in rows if r["migration_status"] == "IN_APP"]
    for r in in_app:
        print(f"domain code left in app.py: {r['name']} ({r['domain']})", file=sys.stderr)
    if in_app:
        return 1
    if "--write" in argv:
        by_status = defaultdict(int)
        for r in rows:
            by_status[r["migration_status"]] += 1
        print("migration status:", dict(by_status))
    if missing:
        for r in missing:
            print(f"UNCLASSIFIED substantial app.py function: {r['name']} ({r['body_lines']} lines)", file=sys.stderr)
        return 1
    if "--write" not in argv and "--summary" not in argv:
        in_app = [r for r in rows if r["module"] == "app.py"]
        print(f"ARCH-001 ownership check passed: {len(rows)} functions ({len(in_app)} still in app.py, "
              f"{sum(r['size'] for r in in_app)} lines), 0 unclassified substantial functions")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
