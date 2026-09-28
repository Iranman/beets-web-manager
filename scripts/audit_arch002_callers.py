#!/usr/bin/env python3
"""ARCH-002 caller audit: map every final-decision pattern hit in production
code to its enclosing function and check it against the reviewed
classification in docs/arch002_caller_audit.json.

Fails (exit 1) when a hit-bearing unit is unclassified, so a new independent
fuzzy/confidence decision cannot land unreviewed. Prints exact per-class unit
and hit counts. Units classified but no longer hit-bearing are reported as
informational (a migration can legitimately remove every pattern hit).

    python scripts/audit_arch002_callers.py            # check + summary
    python scripts/audit_arch002_callers.py --list     # every unit
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLASSIFICATION = ROOT / "docs" / "arch002_caller_audit.json"
PATTERN = re.compile(
    r"SequenceMatcher|rapidfuzz|fuzz\.|similarity\(|_similarity\(|confidence|score\s*>=|threshold|"
    r"action_allowed|attach_eligible|safety_key|best_match|can_auto_accept"
)
TOP_LEVEL = ["app.py", "helpers_mb.py", "routes_submissions.py", "routes_jobs.py", "routes_lidarr.py",
             "routes_setup.py", "job_engine.py"]
CLASSES = [
    "CANONICAL_FINAL_DECISION", "CANDIDATE_GENERATION_ONLY", "DISPLAY_ONLY", "COMPATIBILITY_WRAPPER",
    "NEEDS_MIGRATION", "SAFE_SPECIALIZED_EVIDENCE", "TEST_ONLY",
]


def production_files():
    tracked = subprocess.check_output(
        ["git", "ls-files", "backend/*.py", "beetsplug/*.py"], cwd=ROOT, text=True
    ).split()
    # ARCH-001: every top-level route module is production code too.
    routes = sorted(p.name for p in ROOT.glob("routes_*.py"))
    files = list(dict.fromkeys(TOP_LEVEL + routes + tracked))
    return [f for f in files if (ROOT / f).exists() and not f.startswith("tests/")]


def hit_map():
    units = Counter()
    for rel in production_files():
        src = (ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(src)
        spans = [
            (n.lineno, n.end_lineno, n.name)
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        # An import line naming a threshold/score helper decides nothing.
        import_lines = {
            ln for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
            for ln in range(n.lineno, n.end_lineno + 1)
        }
        for lineno, line in enumerate(src.splitlines(), 1):
            if lineno in import_lines or not PATTERN.search(line):
                continue
            containing = [s for s in spans if s[0] <= lineno <= s[1]]
            name = min(containing, key=lambda s: s[0])[2] if containing else "<module>"
            units[f"{rel}::{name}"] += 1
    return units


def app_family_modules():
    """Modules extracted from app.py by ARCH-001 (docs/arch001_app_ownership.json)."""
    try:
        data = json.loads((ROOT / "docs" / "arch001_app_ownership.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set(data.get("extracted_modules") or [])


def load_classification(hits):
    """Classifications, with ARCH-001 moves inheriting their app.py entry.

    A function moved verbatim from app.py into an owned module keeps the
    classification it had as `app.py::<name>`; module-level units and any
    function without an app.py entry must be classified explicitly.
    """
    classified = dict(json.loads(CLASSIFICATION.read_text(encoding="utf-8"))["units"])
    family = app_family_modules()
    for unit in hits:
        rel, _, name = unit.partition("::")
        if unit not in classified and rel in family and name != "<module>" and f"app.py::{name}" in classified:
            classified[unit] = classified[f"app.py::{name}"]
    return classified


def main(argv):
    hits = hit_map()
    classified = load_classification(hits)
    unclassified = sorted(u for u in hits if u not in classified)
    bad_class = sorted(u for u, (cls, _) in classified.items() if cls not in CLASSES)
    unit_counts = Counter()
    hit_counts = Counter()
    for unit, count in hits.items():
        cls = classified.get(unit, ["UNCLASSIFIED"])[0]
        unit_counts[cls] += 1
        hit_counts[cls] += count
    if "--list" in argv:
        for unit in sorted(hits):
            print(f"{hits[unit]:4d}  {classified.get(unit, ['UNCLASSIFIED'])[0]:26s} {unit}")
    print(f"hit-bearing units: {len(hits)}   pattern hits: {sum(hits.values())}")
    for cls in CLASSES + (["UNCLASSIFIED"] if unclassified else []):
        print(f"  {cls:26s} units={unit_counts[cls]:3d}  hits={hit_counts[cls]:4d}")
    stale = sorted(u for u in classified if u not in hits)
    if stale:
        print("classified units with no remaining pattern hits (informational):")
        for unit in stale:
            print(f"  {unit}  [{classified[unit][0]}]")
    needs = sorted(u for u in hits if classified.get(u, [""])[0] == "NEEDS_MIGRATION")
    print(f"NEEDS_MIGRATION remaining: {len(needs)}")
    for unit in needs:
        print(f"  {unit}")
    if unclassified or bad_class:
        for unit in unclassified:
            print(f"UNCLASSIFIED: {unit}", file=sys.stderr)
        for unit in bad_class:
            print(f"INVALID CLASS: {unit}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
