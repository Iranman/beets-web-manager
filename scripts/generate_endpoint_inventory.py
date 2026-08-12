#!/usr/bin/env python3
"""Regenerate security/endpoint_inventory.json from the actual route
decorators in app.py, routes_jobs.py, routes_lidarr.py, routes_setup.py,
and routes_submissions.py.

Why this exists: the hand-built inventory committed 2026-07-16 only ever
covered app.py + routes_jobs.py (187 routes) and was never updated when
routes_lidarr.py, routes_setup.py, and routes_submissions.py were added --
so the most security-sensitive new surface (routes_setup.py: credentials,
setup completion, env editing) was never in the audit trail at all, and the
original two files had also grown by dozens of routes since. A stale
inventory that still *looks* authoritative is worse than an honest, current
one, per "Review every endpoint, not just representative samples."

This script mechanically extracts every route (method, path, endpoint,
source file, line) and classifies `auth_required`/`changes_state` from
ground truth: the actual _AUTH_PUBLIC_ENDPOINTS/_FIRST_RUN_PUBLIC_ENDPOINTS
allowlists parsed out of app.py (not a second hand-maintained guess) and
HTTP method. Richer judgment fields that need a human/AI reader to actually
understand what a route does (purpose, destructive_or_high_impact,
rate_limit_required, existing_security_tests, ...) are carried forward
unchanged from the previous inventory entry for any route that already
existed there (matched by method+endpoint), and left as explicit
"NEEDS_REVIEW" placeholders for routes that are new since the last
inventory -- so nothing is silently guessed, and `grep NEEDS_REVIEW` finds
exactly what a follow-up review pass still owes.

Usage:
    python scripts/generate_endpoint_inventory.py [--check]

--check exits nonzero if security/endpoint_inventory.json is out of date
relative to the current source (for CI use), without writing anything.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = ROOT / "security" / "endpoint_inventory.json"
ROUTE_FILES = [
    "app.py",
    "routes_jobs.py",
    "routes_lidarr.py",
    "routes_setup.py",
    "routes_submissions.py",
]

_DECORATOR_METHOD_MAP = {
    "get": ["GET"], "post": ["POST"], "put": ["PUT"],
    "patch": ["PATCH"], "delete": ["DELETE"],
}


def _extract_public_endpoint_sets(app_source: str) -> Tuple[set, set]:
    """Parse _AUTH_PUBLIC_ENDPOINTS / _FIRST_RUN_PUBLIC_ENDPOINTS out of
    app.py's own source via the AST -- ground truth, not a re-guess."""
    tree = ast.parse(app_source, filename="app.py")
    found: Dict[str, set] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("_AUTH_PUBLIC_ENDPOINTS", "_FIRST_RUN_PUBLIC_ENDPOINTS") and isinstance(node.value, ast.Set):
                pairs = set()
                for elt in node.value.elts:
                    if isinstance(elt, ast.Tuple) and len(elt.elts) == 2:
                        try:
                            method = ast.literal_eval(elt.elts[0])
                            endpoint = ast.literal_eval(elt.elts[1])
                        except Exception:
                            continue
                        pairs.add((method, endpoint))
                found[name] = pairs
    return found.get("_AUTH_PUBLIC_ENDPOINTS", set()), found.get("_FIRST_RUN_PUBLIC_ENDPOINTS", set())


def _extract_routes(filename: str) -> List[Dict[str, Any]]:
    source = (ROOT / filename).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=filename)
    routes: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            if not (isinstance(dec.func.value, ast.Name) and dec.func.value.id == "app"):
                continue
            attr = dec.func.attr
            if attr not in ("get", "post", "put", "patch", "delete", "route"):
                continue
            if not dec.args or not isinstance(dec.args[0], ast.Constant):
                continue
            path = dec.args[0].value
            if attr == "route":
                methods = ["GET"]
                for kw in dec.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        methods = [ast.literal_eval(e) for e in kw.value.elts]
            else:
                methods = _DECORATOR_METHOD_MAP[attr]
            for method in methods:
                routes.append({
                    "method": method,
                    "route": path,
                    "endpoint": node.name,
                    "source_file": filename,
                    "line": dec.lineno,
                })
    return routes


def _load_previous_entries() -> Dict[Tuple[str, str], Dict[str, Any]]:
    if not INVENTORY_PATH.exists():
        return {}
    try:
        data = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for entry in data.get("routes", []):
        out[(entry.get("method"), entry.get("endpoint"))] = entry
    return out


_CARRY_FORWARD_FIELDS = [
    "purpose", "required_role_or_privilege", "handles_secrets", "accesses_files",
    "creates_background_job", "invokes_external_services_or_subprocesses",
    "destructive_or_high_impact", "rate_limit_required", "rate_limit_implemented",
    "existing_security_tests",
]
_NEEDS_REVIEW = "NEEDS_REVIEW"


def build_inventory() -> Dict[str, Any]:
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    public_endpoints, first_run_public = _extract_public_endpoint_sets(app_source)
    previous = _load_previous_entries()

    all_routes: List[Dict[str, Any]] = []
    for filename in ROUTE_FILES:
        all_routes.extend(_extract_routes(filename))
    all_routes.sort(key=lambda r: (r["source_file"], r["line"], r["method"]))

    entries = []
    for route in all_routes:
        key = (route["method"], route["endpoint"])
        is_public = key in public_endpoints
        changes_state = route["method"] not in ("GET", "HEAD", "OPTIONS")
        prev = previous.get(key)
        entry: Dict[str, Any] = {
            "method": route["method"],
            "route": route["route"],
            "endpoint": route["endpoint"],
            "source_file": route["source_file"],
            "line": route["line"],
            "auth_required": not is_public,
            "public_pre_setup_only": key in first_run_public and key not in public_endpoints,
            "changes_state": changes_state,
            "csrf_required": changes_state and not is_public,
        }
        for field in _CARRY_FORWARD_FIELDS:
            entry[field] = prev.get(field, _NEEDS_REVIEW) if prev else _NEEDS_REVIEW
        entries.append(entry)

    needs_review_routes = sum(
        1 for e in entries
        if any(e.get(f) == _NEEDS_REVIEW for f in _CARRY_FORWARD_FIELDS)
    )

    return {
        "generated_by": "scripts/generate_endpoint_inventory.py",
        "scope": "All @app.{get,post,put,patch,delete,route} decorators in "
                 + ", ".join(ROUTE_FILES),
        "auth_model": "Global Flask before_request guard (app.py _enforce_security_boundary); "
                      "exact endpoint+method public allowlist (_AUTH_PUBLIC_ENDPOINTS); "
                      "narrower pre-setup-only allowlist (_FIRST_RUN_PUBLIC_ENDPOINTS); "
                      "owner/admin bearer token, Basic auth, or session cookie.",
        "limitations": [
            "Mechanically derived from decorators via AST parsing, cross-referenced against the "
            "real allowlist constants in app.py -- auth_required/csrf_required/changes_state are "
            "ground truth, not estimates.",
            "purpose/destructive_or_high_impact/rate_limit_required/existing_security_tests are "
            "carried forward from the previous manual review for routes that already existed there; "
            "routes new since the last review are marked 'NEEDS_REVIEW' in those fields rather than "
            "guessed -- see needs_review_route_count below.",
            "Static inventory does not prove object-level authorization or every helper a route calls.",
        ],
        "route_count": len(entries),
        "needs_review_route_count": needs_review_routes,
        "public_endpoint_allowlist": [
            {"method": m, "endpoint": e} for (m, e) in sorted(public_endpoints)
        ],
        "first_run_public_endpoint_allowlist": [
            {"method": m, "endpoint": e} for (m, e) in sorted(first_run_public)
        ],
        "routes": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Exit nonzero if the file is stale; do not write.")
    args = parser.parse_args()

    fresh = build_inventory()
    fresh_for_compare = {k: v for k, v in fresh.items() if k != "generated_at"}

    if args.check:
        if not INVENTORY_PATH.exists():
            print("security/endpoint_inventory.json is missing.", file=sys.stderr)
            return 1
        current = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        current_for_compare = {k: v for k, v in current.items() if k != "generated_at"}
        if current_for_compare != fresh_for_compare:
            print(
                "security/endpoint_inventory.json is stale. Run "
                "`python scripts/generate_endpoint_inventory.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("security/endpoint_inventory.json is up to date.")
        return 0

    import datetime
    fresh["generated_at"] = datetime.date.today().isoformat()
    INVENTORY_PATH.write_text(json.dumps(fresh, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"Wrote {INVENTORY_PATH} with {fresh['route_count']} routes "
          f"({fresh['needs_review_route_count']} need manual review).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
